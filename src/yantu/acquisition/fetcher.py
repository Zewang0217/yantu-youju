"""URL 抓取与内容快照。

快照是不可变证据基底:同 URL 内容可变(核验报告 G8"同URL字段级修订"),
判定与展示引用快照哈希而非"访问过网页"这一事实(INV-2:访问≠核实)。
"""

from __future__ import annotations

import hashlib
import math
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from .source_urls import validate_source_url

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) yantu-research-snapshot/0.1 "
    "(academic competition project; contact via repo)"
)
_TIMEOUT = 20.0


class FetchError(Exception):
    """访问失败(网络层)。与解析失败(ParseResult.failed)分开(SRS §4)。"""

    def __init__(self, url: str, reason: str):
        super().__init__(f"访问失败 {url}: {reason}")
        self.url = url
        self.reason = reason


@dataclass(frozen=True)
class Snapshot:
    """一次成功抓取的不可变快照。"""

    url: str
    fetched_at: datetime
    status_code: int
    content_type: str
    body: bytes
    final_url: str = ""  # 旧快照未记录时保持未知，不推测重定向终点

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    @property
    def carrier(self) -> str:
        """载体类型(SRS §4 解析状态按载体选择解析器)。"""
        return (self.content_type or "").split(";")[0].strip().lower()


class _ScopedRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: tuple[str, ...]):
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            validate_source_url(newurl, self.allowed_hosts)
        except ValueError as e:
            raise FetchError(req.full_url, "重定向超出来源白名单") from e
        finally:
            # 基类仅在redirect_request返回后关闭旧响应；拒绝分支须自行关闭。
            fp.close()
        # 不调用基类跟随逻辑，避免其无界读取3xx响应体；入口更正单独核验。
        raise FetchError(req.full_url, "来源发生重定向，需核验新入口后重试")


def fetch(url: str, timeout: float = _TIMEOUT, user_agent: str = _USER_AGENT,
          *, max_bytes: int = 10 * 1024 * 1024, allowed_hosts: tuple[str, ...] | None = None) -> Snapshot:
    """抓取 URL 并返回快照。任何网络层失败抛 FetchError(不重试——
    重试/退避策略属 07 契约的调用方职责)。"""
    if (isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0
            or isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("下载字节预算和超时必须为正")
    if allowed_hosts is not None:
        try:
            url = validate_source_url(url, allowed_hosts)
        except ValueError as e:
            raise FetchError("blocked-source", "来源URL不在白名单") from e
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    open_url = urllib.request.build_opener(_ScopedRedirect(allowed_hosts)).open if allowed_hosts is not None else urllib.request.urlopen
    try:
        with open_url(req, timeout=timeout) as resp:
            final_url = resp.geturl()
            if allowed_hosts is not None:
                try:
                    validate_source_url(final_url, allowed_hosts)
                except ValueError as e:
                    raise FetchError(url, "响应终点超出来源白名单") from e
            body = resp.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise FetchError(url, "响应超过字节预算")
            return Snapshot(
                url=url,
                fetched_at=datetime.now(),
                status_code=resp.status,
                content_type=resp.headers.get("Content-Type", ""),
                body=body,
                final_url=final_url,
            )
    except urllib.error.HTTPError as e:
        e.close()
        raise FetchError(url, f"HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FetchError(url, type(e).__name__) from e
