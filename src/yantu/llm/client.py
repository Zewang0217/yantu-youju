"""OpenAI 兼容 chat 客户端(urllib 实现,无第三方依赖)。"""

from __future__ import annotations

import json
import pathlib
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

DEFAULT_CONFIG_PATH = pathlib.Path("config") / "llm.json"


class LLMError(Exception):
    """模型调用失败(网络/认证/响应不合法)。结构化错误,调用方降级而非重试轰炸。"""

    def __init__(self, reason: str, status: Optional[int] = None):
        super().__init__(f"LLM调用失败: {reason}")
        self.reason = reason
        self.status = status


@dataclass(frozen=True)
class ChatClient:
    """最小 chat 客户端:messages -> 文本。不在本层做重试(07 契约由调用方定)。"""

    base_url: str
    api_key: str
    model: str
    timeout: float = 60.0
    max_tokens: int = 4096

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key) and not self.api_key.startswith("在这里")

    def chat(self, messages: list[dict], temperature: float = 0.1) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": self.max_tokens,
        }
        req = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise LLMError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}", e.code) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise LLMError(type(e).__name__) from e
        except json.JSONDecodeError as e:
            raise LLMError("响应非JSON") from e
        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"响应结构异常: {e}") from e


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
