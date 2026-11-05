"""URL 抓取与内容快照。

快照是不可变证据基底:同 URL 内容可变(核验报告 G8"同URL字段级修订"),
判定与展示引用快照哈希而非"访问过网页"这一事实(INV-2:访问≠核实)。
"""

from __future__ import annotations

import hashlib
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime

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

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    @property
    def carrier(self) -> str:
        """载体类型(SRS §4 解析状态按载体选择解析器)。"""
        return (self.content_type or "").split(";")[0].strip().lower()


def fetch(url: str, timeout: float = _TIMEOUT, user_agent: str = _USER_AGENT) -> Snapshot:
    """抓取 URL 并返回快照。任何网络层失败抛 FetchError(不重试——
    重试/退避策略属 07 契约的调用方职责)。"""
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return Snapshot(
                url=url,
                fetched_at=datetime.now(),
                status_code=resp.status,
                content_type=resp.headers.get("Content-Type", ""),
                body=body,
            )
    except urllib.error.HTTPError as e:
        raise FetchError(url, f"HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FetchError(url, type(e).__name__) from e
