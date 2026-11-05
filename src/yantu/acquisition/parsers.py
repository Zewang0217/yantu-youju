"""载体解析器:HTML 通知页的确定性启发式解析。

定位(诚实边界):本解析器只做**标题/正文/日期候选**的确定性提取,用于
快照登记与人工结构化的辅助;**不声称**自动完成条件结构化——政策条款到
条件树(spec)的转换在 M1 由人工种子数据承担,自动结构化属后续采集
Agent 的 LLM 任务并按 ACQ-05/06 过复核门禁。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Protocol

from .fetcher import Snapshot

_DATE_PATTERNS = (
    # 2026-09-10 / 2026-9-1,可选 12:00 / 12:00:00
    (re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?"), False),
    # 2026年9月10日,可选 12时/12:00
    (re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日(?:\s*(\d{1,2})[时:点](\d{2})?)?"), True),
)


class ParseStatus(str, Enum):
    """SRS §4 解析状态:complete/partial/unsupported/failed。"""

    COMPLETE = "complete"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


@dataclass
class ParseResult:
    status: ParseStatus
    title: str = ""
    text: str = ""
    dates: list[datetime] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class Parser(Protocol):
    """F7 协议:按载体声明能力,解析返回带状态的 ParseResult。"""

    def supports(self, carrier: str) -> bool: ...

    def parse(self, snapshot: Snapshot) -> ParseResult: ...


def extract_dates(text: str) -> list[datetime]:
    """提取日期候选(启发式,不区分语义:报名/考核/落款日期混在一起)。"""
    found: list[datetime] = []
    for pattern, _cjk in _DATE_PATTERNS:
        for m in pattern.finditer(text):
            try:
                found.append(
                    datetime(
                        int(m.group(1)),
                        int(m.group(2)),
                        int(m.group(3)),
                        int(m.group(4) or 0),
                        int(m.group(5) or 0),
                    )
                )
            except ValueError:
                continue  # 如 2026-13-99 之类正文噪声
    return sorted(set(found))


class HtmlNoticeParser:
    """HTML 通知页解析:标题+去标签正文+日期候选。"""

    def supports(self, carrier: str) -> bool:
        return carrier in ("text/html", "application/xhtml+xml")

    def parse(self, snapshot: Snapshot) -> ParseResult:
        if not self.supports(snapshot.carrier):
            return ParseResult(
                ParseStatus.UNSUPPORTED,
                notes=[f"载体 {snapshot.carrier!r} 非 HTML,需对应解析器(ACQ-04)"],
            )
        try:
            html = self._decode(snapshot.body)
        except UnicodeDecodeError as e:
            return ParseResult(ParseStatus.FAILED, notes=[f"解码失败: {e}"])

        title = self._title(html)
        text = self._strip_tags(html)
        dates = extract_dates(text)

        result = ParseResult(
            ParseStatus.PARTIAL if title else ParseStatus.FAILED,
            title=title,
            text=text,
            dates=dates,
        )
        if title and dates:
            result.status = ParseStatus.COMPLETE
        if not title:
            result.notes.append("未找到 <title> 或 <h1>")
        if not dates:
            result.notes.append("正文无日期候选(可能正文在PDF附件,G8)")
        return result

    @staticmethod
    def _decode(body: bytes) -> str:
        head = body[:2048].decode("ascii", errors="ignore")
        m = re.search(r'charset=["\']?([\w-]+)', head, re.I)
        encodings = [m.group(1)] if m else []
        encodings += ["utf-8", "gb18030"]
        for enc in encodings:
            try:
                return body.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        raise UnicodeDecodeError("decode", body, 0, 1, "所有候选编码失败")

    @staticmethod
    def _title(html: str) -> str:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        if not m:
            m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S | re.I)
        if not m:
            return ""
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1))).strip()

    @staticmethod
    def _strip_tags(html: str) -> str:
        text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"&nbsp;?", " ", text)
        return re.sub(r"[ \t\r\f\v]+", " ", text).strip()
