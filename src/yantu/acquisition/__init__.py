"""资料采集线(M1续):抓取快照、载体解析、落盘存储与确定性结构化。

层次:fetcher(快照) → parsers(注册表分发) → store(落盘三件套);
harvest 为门面编排;structurer 为确定性窗口提取基线;
text_dates 为统一日期解析；trusted_pipeline 负责单页证据绑定、独立门禁及
publication 当前指针。多来源探索/交叉核验/咨询侧复核仍由后续工作流承担。
"""

from .fetcher import FetchError, Snapshot, fetch
from .harvest import HarvestOutcome, HarvestService
from .parsers import (
    DEFAULT_REGISTRY,
    HtmlNoticeParser,
    ParseResult,
    ParseStatus,
    Parser,
    ParserRegistry,
    PageText,
    PdfTextParser,
    extract_dates,
    parse_snapshot,
)
from .store import (
    EntryBook,
    EntryRecord,
    OpportunityRepository,
    SnapshotStore,
    opportunity_to_record,
    record_to_opportunity,
)
from .structurer import WindowCandidate, extract_window_candidates, merge_deadline
from .structuring_agent import StructuringAgent, StructuringResult
from .text_dates import DatetimeHit, find_datetimes

__all__ = [
    "fetch", "Snapshot", "FetchError",
    "HtmlNoticeParser", "Parser", "ParserRegistry", "DEFAULT_REGISTRY",
    "PageText", "PdfTextParser",
    "parse_snapshot", "extract_dates",
    "ParseResult", "ParseStatus",
    "SnapshotStore", "EntryBook", "EntryRecord", "OpportunityRepository",
    "opportunity_to_record", "record_to_opportunity",
    "HarvestService", "HarvestOutcome",
    "find_datetimes", "DatetimeHit",
    "WindowCandidate", "extract_window_candidates", "merge_deadline",
    "StructuringAgent", "StructuringResult",
]
