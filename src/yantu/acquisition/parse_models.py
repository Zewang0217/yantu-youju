"""载体解析的共享结果与策略接口；各格式Adapter仅依赖这份契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol

from .fetcher import Snapshot



@dataclass(frozen=True)
class DocumentLink:
    """从原始HTML定位的候选关系，不代表来源身份或资料适用性已核验。"""

    url: str
    kind: str  # embedded_document / attachment / linked_page / body_image
    label: str
    source_attribute: str
    line: int
    column: int
    locator_kind: str = "html_tag"
    text_start: int | None = None
    text_end: int | None = None
    raw_value: str = ""


class ParseStatus(str, Enum):
    """SRS §4 解析状态:complete/partial/unsupported/failed。"""

    COMPLETE = "complete"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


@dataclass
class PageText:
    """页级提取及其在ParseResult.text中的区间；无文本不冒充OCR成功。"""

    number: int
    text: str
    start_offset: int
    end_offset: int
    status: str  # text_extracted / no_text / unsupported / failed / truncated


@dataclass
class ParseResult:
    """一次载体提取的范围与缺损；complete不代表政策事实已经核验。"""

    status: ParseStatus
    title: str = ""
    text: str = ""
    dates: list[datetime] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    pages: tuple[PageText, ...] = ()
    links: tuple[DocumentLink, ...] = ()
    content_scope: str = ""
    declared_page_count: int | None = None
    content_text: str = ""
    issues: tuple[str, ...] = ()
    parser_info: dict = field(default_factory=dict)


class Parser(Protocol):
    """按载体声明提取能力；parse假定分发器已确认输入载体。"""

    def supports(self, carrier: str) -> bool: ...

    def parse(self, snapshot: Snapshot) -> ParseResult: ...
