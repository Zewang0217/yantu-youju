"""PDF文字层解析Adapter；可选依赖延迟加载，页面缺损显式保留。"""

from __future__ import annotations

import io
from importlib.metadata import PackageNotFoundError, version

from .fetcher import Snapshot
from .parse_models import PageText, ParseResult, ParseStatus
from .text_dates import extract_dates


class PdfTextParser:
    """只读PDF文字层，逐页保留缺损；pypdf为可选依赖，不自动安装或OCR。

    页数/字符上限控制返回规模；这不替代不可信PDF执行环境的进程级资源隔离。
    """

    def __init__(self, *, max_bytes: int = 10 * 1024 * 1024, max_pages: int = 50,
                 max_page_chars: int = 100_000, max_total_chars: int = 500_000):
        limits = (max_bytes, max_pages, max_page_chars, max_total_chars)
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in limits):
            raise ValueError("PDF预算必须为正整数")
        self.max_bytes = max_bytes
        self.max_pages = max_pages
        self.max_page_chars = max_page_chars
        self.max_total_chars = max_total_chars

    def supports(self, carrier: str) -> bool:
        return carrier == "application/pdf"

    def manifest(self) -> dict:
        """同时记录应用解析策略和当前引擎版本，不把配置不同的重解析当同一次。"""
        try:
            engine_version = version("pypdf")
        except PackageNotFoundError:
            engine_version = None
        return {"identity": "pdf-text-layer", "version": "1", "engine": "pypdf", "engineVersion": engine_version,
                "configurationStatus": "reported", "configuration": {"maxBytes": self.max_bytes, "maxPages": self.max_pages,
                    "maxPageChars": self.max_page_chars, "maxTotalChars": self.max_total_chars}}

    def parse(self, snapshot: Snapshot) -> ParseResult:
        """不执行PDF内脚本/外链；损坏失败、加密/无文字层显式不支持。"""
        if len(snapshot.body) > self.max_bytes:
            return ParseResult(ParseStatus.FAILED, notes=["PDF超过字节预算"])
        try:
            from pypdf import PdfReader
            from pypdf.errors import PdfReadError
        except ModuleNotFoundError as e:
            if e.name != "pypdf":
                raise
            return ParseResult(ParseStatus.UNSUPPORTED, notes=["缺少可选依赖pypdf；未尝试安装"])
        try:
            reader = PdfReader(io.BytesIO(snapshot.body), strict=True)
            if reader.is_encrypted:
                return ParseResult(ParseStatus.UNSUPPORTED, notes=["PDF已加密，当前解析器不解密"])
            count = len(reader.pages)
            title = (reader.metadata.title or "") if reader.metadata else ""
        except NotImplementedError:
            return ParseResult(ParseStatus.UNSUPPORTED, notes=["PDF使用当前解析器不支持的特性"])
        except (PdfReadError, ValueError, TypeError, OSError) as e:
            return ParseResult(ParseStatus.FAILED, notes=[f"PDF不可读取:{type(e).__name__}"])
        if not count:
            return ParseResult(ParseStatus.FAILED, declared_page_count=0, notes=["PDF没有页面"])
        pages: list[PageText] = []
        notes: list[str] = []
        texts: list[str] = []
        offset = 0
        for index in range(min(count, self.max_pages)):
            try:
                text = reader.pages[index].extract_text() or ""
                status = "text_extracted" if text.strip() else "no_text"
            except NotImplementedError:
                text, status = "", "unsupported"
                notes.append(f"第{index + 1}页含当前解析器不支持的内容滤镜/特性")
            except (PdfReadError, ValueError, TypeError, OSError) as e:
                text, status = "", "failed"
                notes.append(f"第{index + 1}页文字层提取失败:{type(e).__name__}")
            if index and self.max_total_chars - offset >= 2:
                texts.append("\n\n")
                offset += 2
            available = max(0, min(self.max_page_chars, self.max_total_chars - offset))
            if len(text) > available:
                text, status = text[:available], "truncated"
            if status != "text_extracted":
                notes.append(f"第{index + 1}页状态={status}，不代表扫描文字已识别或页面完整")
            start = offset
            texts.append(text)
            offset += len(text)
            pages.append(PageText(index + 1, text, start, offset, status))
        if count > self.max_pages:
            notes.append(f"PDF共{count}页，页数预算仅允许提取{self.max_pages}页")
        text = "".join(texts)
        if all(page.status == "text_extracted" for page in pages) and len(pages) == count:
            status = ParseStatus.COMPLETE
        elif any(page.text.strip() for page in pages):
            status = ParseStatus.PARTIAL
        elif any(page.status == "failed" for page in pages):
            status = ParseStatus.FAILED
        else:
            status = ParseStatus.UNSUPPORTED
        return ParseResult(status, title=str(title), text=text, dates=extract_dates(text), notes=notes,
                           pages=tuple(pages), content_scope="pdf_text_layer", declared_page_count=count, content_text=text)
