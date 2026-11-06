"""OpenAI 兼容 chat 客户端(urllib 实现,无第三方依赖)。"""

from __future__ import annotations

import json
import math
import pathlib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Optional

DEFAULT_CONFIG_PATH = pathlib.Path("config") / "llm.json"


class LLMError(Exception):
    """模型调用失败(网络/认证/响应不合法)。结构化错误,调用方降级而非重试轰炸。"""

    def __init__(self, reason: str, status: Optional[int] = None, *, code: str = 'RESULT_INVALID',
                 retry_after: float | None = None, retry_after_present: bool = False,
                 sent: bool = True, usage: ProviderUsage | None = None):
        super().__init__(f"LLM调用失败: {reason}")
        self.reason = reason
        self.status = status
        self.code, self.retry_after, self.retry_after_present, self.sent, self.usage = code, retry_after, retry_after_present, sent, usage


@dataclass(frozen=True)
class ProviderUsage:
    """供应商响应中可核验的token字段；缺失或不一致时不推测成本或账单。"""

    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    status: str
    source: str = 'provider_response'
    total_origin: str = 'unknown'


@dataclass(frozen=True)
class ChatResult:
    """模型输出及其独立计量；token已知不代表费用已结算。"""

    content: str
    usage: ProviderUsage


def _provider_usage(value: object) -> ProviderUsage:
    if not isinstance(value, dict):
        return ProviderUsage(None, None, None, 'missing')
    names = ('prompt_tokens', 'completion_tokens', 'total_tokens')
    values = [value.get(name) for name in names]
    if any(item is not None and (type(item) is not int or not 0 <= item <= 10**12) for item in values):
        valid = [item if type(item) is int and 0 <= item <= 10**12 else None for item in values]
        return ProviderUsage(*valid, 'invalid', total_origin='provider' if valid[2] is not None else 'unknown')
    prompt, completion, total = values
    origin = 'provider' if total is not None else 'unknown'
    if total is not None and any(part is not None and part > total for part in (prompt, completion)):
        return ProviderUsage(prompt, completion, total, 'inconsistent', total_origin=origin)
    if prompt is not None and completion is not None:
        if prompt + completion > 10**12 or (total is not None and total != prompt + completion):
            return ProviderUsage(prompt, completion, total, 'inconsistent', total_origin=origin)
        if total is None:
            total, origin = prompt + completion, 'derived'
    return ProviderUsage(prompt, completion, total, 'complete' if total is not None else 'partial', total_origin=origin)


def _retry_after(headers, now: datetime) -> tuple[float | None, bool]:
    """解析delta-seconds或HTTP日期；无效header与不存在区分，避免提前重试。"""
    value = headers.get('Retry-After') if headers is not None else None
    if value is None:
        return None, False
    try:
        value = value.strip()
        if value.isascii() and value.isdigit():
            delay = float(int(value))
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                return None, True
            delay = max(0.0, (date - now).total_seconds())
        return (delay if math.isfinite(delay) else None), True
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None, True


@dataclass(frozen=True)
class ChatClient:
    """最小 chat 客户端:messages -> 文本。不在本层做重试(07 契约由调用方定)。"""

    base_url: str = field(repr=False)
    api_key: str = field(repr=False)
    model: str
    timeout: float = 60.0
    max_tokens: int = 4096
    transport: Callable | None = field(default=None, repr=False, compare=False)

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key) and not self.api_key.startswith("在这里")

    def chat(self, messages: list[dict], temperature: float = 0.1) -> str:
        """兼容原有文本接口；需要用量的调用方使用chat_with_usage。"""
        return self.chat_with_usage(messages, temperature).content

    def chat_with_usage(self, messages: list[dict], temperature: float = 0.1) -> ChatResult:
        """单次POST并返回文本与供应商token；不重试，不读取HTTP错误响应内容。"""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": self.max_tokens,
        }
        try:
            req = urllib.request.Request(
                f"{self.base_url.rstrip('/')}/chat/completions",
                data=json.dumps(payload, allow_nan=False).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
                method="POST",
            )
        except (TypeError, ValueError, UnicodeError):
            raise LLMError('请求配置无效', code='INPUT_INVALID', sent=False) from None
        try:
            with (self.transport or urllib.request.urlopen)(req, timeout=self.timeout) as resp:
                raw = resp.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise LLMError('响应超过大小预算', code='RESULT_INVALID')
                body = json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            status = e.code if type(e.code) is int and 100 <= e.code <= 599 else None
            delay, present = _retry_after(e.headers, datetime.now(timezone.utc)) if status == 429 else (None, False)
            code = ('RATE_LIMITED' if status == 429 else 'AUTH_REQUIRED' if status == 401 else 'FORBIDDEN' if status == 403
                    else 'NOT_FOUND' if status == 404 else 'TEMP_UNAVAILABLE' if status is not None and status >= 500 else 'INPUT_INVALID')
            e.close()
            raise LLMError(f'HTTP {status}' if status else 'HTTP错误', status, code=code, retry_after=delay, retry_after_present=present) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            timeout = isinstance(e, TimeoutError) or isinstance(getattr(e, 'reason', None), TimeoutError)
            raise LLMError('连接超时' if timeout else '连接失败', code='NETWORK_TIMEOUT' if timeout else 'TEMP_UNAVAILABLE') from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise LLMError('响应非有效JSON', code='RESULT_INVALID') from None
        usage = _provider_usage(body.get('usage') if isinstance(body, dict) else None)
        try:
            content = body["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError('content')
            return ChatResult(content, usage)
        except (KeyError, IndexError, TypeError):
            raise LLMError('响应结构异常', code='RESULT_INVALID', usage=usage) from None


def load_client(config_path: pathlib.Path = DEFAULT_CONFIG_PATH) -> Optional[ChatClient]:
    """读配置;文件不存在/缺 key 返回 None(上层降级,不报错)。"""
    path = pathlib.Path(config_path)
    if not path.exists():
        return None
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    client = ChatClient(
        base_url=cfg.get("base_url", ""),
        api_key=cfg.get("api_key", ""),
        model=cfg.get("model", ""),
        timeout=float(cfg.get("timeout_seconds", 60)),
        max_tokens=int(cfg.get("max_tokens", 4096)),
    )
    return client if client.is_configured else None
