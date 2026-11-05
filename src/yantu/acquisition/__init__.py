"""资料采集线(M1续):抓取快照与载体解析。

按 HLD 决策 F7 协议实现:Parser 按载体选择(Registry),解析状态取 SRS §4
(complete/partial/unsupported/failed);抓取与解析是两类失败,分开建模
(访问失败≠解析失败)。本模块只做确定性处理,不含 LLM/爬虫策略(后续按
06 设计稿的采集Agent角色接入)。
"""

from .fetcher import FetchError, Snapshot, fetch
from .parsers import HtmlNoticeParser, ParseResult, ParseStatus

__all__ = ["fetch", "Snapshot", "FetchError", "HtmlNoticeParser", "ParseResult", "ParseStatus"]
