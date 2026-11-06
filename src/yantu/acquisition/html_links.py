"""发现通知正文中的嵌入文件、附件和关联页面；只返回候选，不执行访问。

优先使用明确正文容器，避免模板图片和导航污染资料范围。没有正文标记时
保留body内候选并报告降级；URL信任和下载预算由采集应用负责。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from .parse_models import DocumentLink

@dataclass(frozen=True)
class LinkDiscovery:
    """正文范围及候选清单；truncated时不能声称链接覆盖完整。"""

    links: tuple[DocumentLink, ...]
    content_scope: str
    truncated: bool = False
    content_text: str = ""
    issues: tuple[str, ...] = ()


_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "section", "article"}
_ATTACHMENTS = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".png", ".jpg", ".jpeg")
# 纯文本候选限ASCII/百分号编码URL；中文标点不能吞入网址。
_TEXT_URL = re.compile(r"https?://[A-Za-z0-9:/?#\[\]@!$&'()*+,;=._~%{}-]+")


class _NoticeLinks(HTMLParser):
    def __init__(self, source_url: str, max_links: int):
        super().__init__(convert_charrefs=True)
        self.source_url = source_url
        self.max_links = max_links
        self.stack: list[tuple[str, int, bool]] = []
        self.candidates: list[tuple[DocumentLink, int]] = []
        self.best_scope = 0
        self.truncated = dict.fromkeys(range(4), False)
        self.counts = dict.fromkeys(range(4), 0)
        self.texts: dict[int, list[str]] = {rank: [] for rank in range(4)}
        self.issues: dict[int, set[str]] = {rank: set() for rank in range(4)}
        self.anchor: tuple[int, list[str]] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        classes = attributes.get("class", "").split()
        own_scope = 3 if "wp_articlecontent" in classes else 2 if tag == "article" else 1 if tag == "main" else 0
        scope = max([own_scope] + [frame[1] for frame in self.stack])
        hidden = (any(frame[2] for frame in self.stack) or "hidden" in attributes
                  or bool(re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", attributes.get("style", ""), re.I)))
        if not hidden:
            self.best_scope = max(self.best_scope, scope)
        in_body = tag == "body" or any(frame[0] == "body" for frame in self.stack)
        if tag not in _VOID:
            self.stack.append((tag, scope, hidden))
        if hidden or not (scope or in_body) or any(frame[0] in ("script", "style", "noscript") for frame in self.stack):
            return
        if tag in _BLOCK:
            self.texts[scope].append(" ")
        selected: tuple[str, str] | None = None
        if attributes.get("pdfsrc"):
            selected = ("pdfsrc", "embedded_document")
        elif tag in ("iframe", "embed") and attributes.get("src"):
            selected = ("src", "embedded_document")
        elif tag == "object" and attributes.get("data"):
            selected = ("data", "embedded_document")
        elif tag == "img" and attributes.get("src") and "sudy-attachment-icon" not in classes:
            if attributes.get("width") != "0" and attributes.get("height") != "0":
                selected = ("src", "body_image")
        elif tag == "a" and attributes.get("href"):
            selected = ("href", "linked_page")
        if selected is None:
            return
        attribute, kind = selected
        raw = attributes[attribute].strip()
        if raw.startswith("#"):
            return
        try:
            parsed = urlsplit(urljoin(self.source_url, raw))
        except ValueError:
            self.issues[scope].add("invalid_document_reference")
            return
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            if kind in ("embedded_document", "body_image"):
                self.issues[scope].add("unsupported_embedded_reference")
            return
        if tag == "a" and parsed.path.lower().endswith(_ATTACHMENTS):
            kind = "attachment"
        if self.counts[scope] >= self.max_links:
            self.truncated[scope] = True
            return
        self.counts[scope] += 1
        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
        title = attributes.get("title", "") or attributes.get("alt", "")
        if not title:
            match = re.search(r"['\"]title['\"]\s*:\s*['\"]([^'\"]+)", attributes.get("sudyfile-attr", "")[:2048])
            title = match[1] if match else ""
        line, column = self.getpos()
        self.candidates.append((DocumentLink(url, kind, title, attribute, line, column, raw_value=raw), scope))
        if tag == "a":
            self.anchor = (len(self.candidates) - 1, [])

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self.anchor:
            self.anchor[1].append(data)
        if self.stack and not any(frame[2] or frame[0] in ("head", "script", "style", "noscript") for frame in self.stack):
            if any(frame[0] == "body" or frame[1] > 0 for frame in self.stack):
                self.texts[max(frame[1] for frame in self.stack)].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK and self.stack and not any(frame[2] for frame in self.stack):
            self.texts[max(frame[1] for frame in self.stack)].append(" ")
        if tag == "a" and self.anchor:
            index, parts = self.anchor
            link, scope = self.candidates[index]
            if not link.label:
                self.candidates[index] = (replace(link, label=" ".join("".join(parts).split())), scope)
            self.anchor = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


def discover_document_links(html: str, source_url: str, *, max_links: int = 200) -> LinkDiscovery:
    """解析正文候选并按URL/角色去重；拒绝非正预算，保留预算截断状态。"""
    if not isinstance(max_links, int) or isinstance(max_links, bool) or max_links <= 0:
        raise ValueError("max_links必须是正整数")
    parser = _NoticeLinks(source_url, max_links)
    parser.feed(html)
    parser.close()
    selected = [link for link, scope in parser.candidates if scope == parser.best_scope]
    unique: dict[tuple[str, str], DocumentLink] = {}
    for link in selected:
        if not link.label:
            link = replace(link, label=unquote(urlsplit(link.url).path.rsplit("/", 1)[-1]))
        unique.setdefault((link.url, link.kind), link)
    scope_name = ("body_fallback", "main", "article", "wp_articlecontent")[parser.best_scope]
    content_text = " ".join("".join(parser.texts[parser.best_scope]).split())
    truncated = parser.truncated[parser.best_scope]
    native_urls = {link.url for link in unique.values()}
    for match in _TEXT_URL.finditer(content_text):
        raw = match[0].rstrip(".,;:!?)]}'}")
        try:
            parsed = urlsplit(raw)
            if not parsed.hostname:
                raise ValueError("缺少主机")
        except ValueError:
            parser.issues[parser.best_scope].add("invalid_document_reference")
            continue
        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
        if url in native_urls:
            continue
        if len(unique) >= max_links:
            truncated = True
            break
        kind = "attachment" if parsed.path.lower().endswith(_ATTACHMENTS) else "linked_page"
        context = content_text[max(0, match.start() - 70):match.start()]
        label = re.split(r"[。；;！？!?]", context)[-1].strip()
        link = DocumentLink(url, kind, label, "text_url", 0, 0, "content_text", match.start(),
                            match.start() + len(raw), raw)
        unique.setdefault((url, kind), link)
    return LinkDiscovery(tuple(unique.values()), scope_name, truncated, content_text,
                         tuple(sorted(parser.issues[parser.best_scope])))
