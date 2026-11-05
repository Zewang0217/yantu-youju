"""载体解析器:注册表分发 + HTML 通知页解析(R1/R2/R4)。

职责分离(R1):**分发器**(parse_snapshot)按载体选解析器并对"无解析器"
产出 UNSUPPORTED;解析器自身只假设载体正确,不再自检。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Protocol

from .fetcher import Snapshot
from .text_dates import find_datetimes


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
    """F7 协议:按载体声明能力,parse 假定载体已由分发器确认。"""

    def supports(self, carrier: str) -> bool: ...

    def parse(self, snapshot: Snapshot) -> ParseResult: ...


class ParserRegistry:
    """解析器注册表(R1):按注册顺序取第一个 supports 的解析器。"""

    def __init__(self) -> None:
        self._parsers: list[Parser] = []

    def register(self, parser: Parser) -> None:
        self._parsers.append(parser)

    def select(self, carrier: str) -> Optional[Parser]:
        for p in self._parsers:
            if p.supports(carrier):
                return p
        return None


def extract_dates(text: str) -> list[datetime]:
    """日期候选(R2:委托 text_dates,保留旧签名)。"""
    return sorted({h.dt for h in find_datetimes(text)})


class HtmlNoticeParser:
    """HTML 通知页解析:标题+去标签正文(导航净化 R4)+日期候选。

    定位:确定性启发式,不做条件结构化(见模块 docstring 顶部说明)。
    """

    def supports(self, carrier: str) -> bool:
        return carrier in ("text/html", "application/xhtml+xml")

    def parse(self, snapshot: Snapshot) -> ParseResult:
        try:
            html = self._decode(snapshot.body)
        except UnicodeDecodeError as e:
            return ParseResult(ParseStatus.FAILED, notes=[f"解码失败: {e}"])

        title = self._title(html)
        text = self._strip_tags(html)
        text = self._cut_navigation(text, title)
        dates = extract_dates(text)

        # 标题与日期齐→complete;只有其一→partial;两者皆无→failed
        if title and dates:
            status = ParseStatus.COMPLETE
        elif title or dates:
            status = ParseStatus.PARTIAL
        else:
            status = ParseStatus.FAILED
        result = ParseResult(status, title=title, text=text, dates=dates)
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
        # head(含title/meta)是元数据不是正文;script/style同理剔除
        text = re.sub(r"(?is)<(head|script|style)[^>]*>.*?</\1>", " ", html)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"&nbsp;?", " ", text)
        return re.sub(r"[ \t\r\f\v]+", " ", text).strip()

    @staticmethod
    def _cut_navigation(text: str, title: str) -> str:
        """R4 正文净化:标题文本若出现在正文中,正文从该处开始(削掉导航菜单)。

        保守降级:找不到(标题被换行拆散/图片标题)则返回原文,宁多勿错。
        在"去空白视图"中定位,再精确映射回原文索引。
        """
        if not title:
            return text
        mapping: list[int] = []
        chars: list[str] = []
        for i, ch in enumerate(text):
            if not ch.isspace():
                chars.append(ch)
                mapping.append(i)
        compact = "".join(chars)
        # 优先全文标题命中;抗换行差异再退到16字前缀(截断策略记录于决策文档R4)
        core = re.sub(r"\s+", "", title)
        idx = compact.find(core)
        if idx < 0:
            idx = compact.find(core[:16])
        if idx <= 0:  # 0=正文本就始于标题; -1=未找到
            return text
        return text[mapping[idx]:]


DEFAULT_REGISTRY = ParserRegistry()
DEFAULT_REGISTRY.register(HtmlNoticeParser())


def parse_snapshot(snapshot: Snapshot, registry: ParserRegistry = DEFAULT_REGISTRY) -> ParseResult:
    """分发入口(R1):无解析器可处理该载体 → UNSUPPORTED(ACQ-04)。"""
    parser = registry.select(snapshot.carrier)
    if parser is None:
        return ParseResult(
            ParseStatus.UNSUPPORTED,
            notes=[f"载体 {snapshot.carrier!r} 无已注册解析器(ACQ-04)"],
        )
    return parser.parse(snapshot)
