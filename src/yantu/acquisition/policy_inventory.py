"""逐文档候选字段原文清单；段落定位不等于条件结构化或适用性核验。

纯函数只处理当前来源的正文视图，不读取文件/网络，不合并文档、不发布机会。
标题/编号是受限发现策略，未识别部分显式保留缺损，不能推断官方没有要求。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .fetcher import Snapshot
from .parse_models import ParseResult, ParseStatus
from .source_evidence import SourceClaim, claim_record, source_quality

INVENTORY_VERSION = "policy-inventory-v2-contexts"

# 每项是实际输出字段；标题与补充提及分开，避免“材料审核”冒充申请材料清单。
_FIELDS = {
    "conditions": (("报名条件", "申请条件", "申报条件", "基本条件"), None),
    "materials": (("报名材料", "申请材料", "提交材料", "材料要求"), None),
    "assessment": (("综合考核", "复试考核", "考核安排", "考核方式", "复试安排", "复试", "考核内容"), r"考核|复试|材料审核"),
    "channels": (("报名", "报名流程", "报名方式", "时间及方式", "申请流程"), r"报名|填报志愿|确认待录取|推免服务系统|教育部推免系统"),
    "scope": (("招生学科专业", "招生专业", "招生计划", "专业目录", "招生范围"), r"学科专业|联培|联合培养|研究方向|直博|招生计划|\b\d{6}\b"),
    "other_policies": (("其他说明", "注意事项", "其他事项"), None),
}
FIELD_NAMES = tuple(_FIELDS)
_TITLES = sorted({title for titles, _ in _FIELDS.values() for title in titles} | {
    "报名截止时间", "材料审核", "资格审核", "考核结果、拟录取与教育部推免系统流程", "咨询方式", "咨询及申诉", "咨询", "申诉",
}, key=len, reverse=True)
_TITLE_PATTERNS = [(title, re.compile(r"\s*" + r"\s*".join(map(re.escape, title)) + r"(?=$|\s|[：:])")) for title in _TITLES]
_HEADING = re.compile(r"(?:^|(?<=[\s。；;]))(?:(?P<major>[一二三四五六七八九十]+、)|(?P<minor>[（(][一二三四五六七八九十]+[）)]))")
_ITEM = re.compile(r"(?:^|(?<=[\s。；;]))(?P<number>\d{1,3})[.．、](?!\d)\s*")


@dataclass(frozen=True)
class Section:
    """原文区间上的标题层级；未识别标题也形成边界，避免吞入后续章节。"""

    title: str
    level: int
    start: int
    body_start: int
    end: int
    own_end: int
    recognized: bool


def _sections(text: str) -> tuple[list[Section], list[str]]:
    headers = list(_HEADING.finditer(text))
    if len(headers) > 128:
        return [], ["SECTION_BUDGET_EXCEEDED"]
    sections = []
    for index, header in enumerate(headers):
        title, body_start, recognized = "", header.end(), False
        for candidate, pattern in _TITLE_PATTERNS:
            match = pattern.match(text, header.end())
            if match:
                if re.match(r"\s*(?:及|与|和|、|/|条\s*件\s*(?:及|与|和)|材\s*料\s*(?:及|与|和))", text[match.end():]):
                    break  # 混合标题不能因空白变成纯字段，也不能再退到更短标题。
                title, body_start, recognized = candidate, match.end(), True
                break
        if not title:
            token = re.match(r"\s*([^\s：:。；;，,]{1,40})", text[header.end():])
            title = token[1] if token else "unrecognized_heading"
        level = 1 if header["major"] else 2
        end = next((following.start() for following in headers[index + 1:] if level == 2 or following["major"]), len(text))
        own_end = min(end, headers[index + 1].start() if index + 1 < len(headers) else len(text))
        sections.append(Section(title, level, header.start(), body_start, end, own_end, recognized))
    return sections, []


def _claim(text: str, path: str, start: int, end: int, pages) -> dict:
    """沿用SourceClaim契约，引用不经过清洗/拼接，页码仅在同一文本视图解释。"""
    claim = SourceClaim(path, text[start:end], start, end)
    return claim_record(claim, text, pages)


def _numbered_parts(text: str, start: int, end: int) -> tuple[list[tuple[int | None, int, int]], list[str]]:
    markers = list(_ITEM.finditer(text, start, end))
    if len(markers) > 256:
        return [], ["ITEM_BUDGET_EXCEEDED"]
    if not markers:
        return [(None, start, end)] if text[start:end].strip() else [], ["NUMBERED_LIST_NOT_IDENTIFIED"]
    parts = []
    if text[start:markers[0].start()].strip():
        parts.append((None, start, markers[0].start()))
    numbers = [int(marker["number"]) for marker in markers]
    issues = [] if numbers == list(range(1, len(numbers) + 1)) else ["ITEM_SEQUENCE_UNCERTAIN"]
    for index, marker in enumerate(markers):
        part_end = markers[index + 1].start() if index + 1 < len(markers) else end
        parts.append((int(marker["number"]), marker.start(), part_end))
    return parts, issues


def _source_view(snapshot: Snapshot, parsed: ParseResult) -> tuple[str, str, tuple, list[str]]:
    """载体适配集中在此；字段发现仅消费一个明确的正文视图。"""
    if snapshot.carrier == "application/pdf":
        return parsed.text, "pdf_text", parsed.pages, []
    issues = ["TABLE_RELATIONSHIPS_NOT_INTERPRETED"] if re.search(rb"<table\b", snapshot.body, re.I) else []
    return parsed.content_text, "html_content_text", (), issues


def inventory_document(snapshot: Snapshot, parsed: ParseResult) -> dict:
    """输出固定字段分母和候选原文；未知范围、未解析页、缺标题均不会当作完整。"""
    text, view, pages, view_issues = _source_view(snapshot, parsed)
    fields = {name: {"status": "not_found", "sections": [], "relatedExcerpts": [], "issues": [],
                     "semanticVerification": "not_evaluated", "applicability": "not_evaluated"} for name in _FIELDS}
    result = {"url": snapshot.url, "snapshotSha256": snapshot.sha256, "fetchedAt": snapshot.fetched_at.isoformat(),
              "view": view, "textSha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "text": text,
              "sourceQuality": source_quality(parsed), "fields": fields, "issues": view_issues, "unclassifiedSections": [], "contextClaims": [],
              "publicationStatus": "not_published", "semanticVerification": "not_evaluated", "applicability": "not_evaluated",
              "coverage": "not_established", "extractorVersion": INVENTORY_VERSION}
    if parsed.status in (ParseStatus.FAILED, ParseStatus.UNSUPPORTED):
        for field in fields.values():
            field["status"] = "source_unreadable"
        return result
    if len(text) > 500_000:
        result["issues"].append("TEXT_BUDGET_EXCEEDED")
        for field in fields.values():
            field["status"] = "not_attempted"
        return result
    if not text.strip():
        result["issues"].append("BODY_TEXT_MISSING")
        return result
    if view == "html_content_text" and parsed.content_scope == "body_fallback":
        result["issues"].append("BODY_SCOPE_UNCERTAIN")
        for field in fields.values():
            field["status"] = "not_attempted"
        return result
    sections, issues = _sections(text)
    result["issues"].extend(issues)
    if issues:
        for field in fields.values():
            field["status"] = "not_attempted"
        return result
    if not sections:
        result["issues"].append("SUPPORTED_HEADINGS_NOT_FOUND")
        return result
    result["documentPreamble"] = _claim(text, "documentPreamble", 0, sections[0].start, pages) if text[:sections[0].start].strip() else None
    context_paths: dict[tuple[int, int], str] = {}

    def context_refs(section: Section, *, include_own: bool = False) -> list[str]:
        """字段带回文档前言、上位限定及本节前言；按区间去重，避免反复复制整章。"""
        refs = ["documentPreamble"] if result["documentPreamble"] else []
        parents = [candidate for candidate in sections if candidate.start < section.start and section.end <= candidate.end and candidate.level < section.level]
        if include_own:
            parents.append(section)
        for parent in parents:
            end = parent.own_end
            if parent is section:
                first_item = _ITEM.search(text, parent.body_start, parent.own_end)
                end = first_item.start() if first_item else parent.own_end
            span = (parent.start, end)
            if span not in context_paths:
                path = f"contextClaims[{len(context_paths)}]"
                context_paths[span] = path
                result["contextClaims"].append(_claim(text, path, *span, pages))
            refs.append(context_paths[span])
        return refs
    if re.search(r"(?m)^\s*-\s*\d+\s*-\s*$", text):
        result["issues"].append("PAGE_DECORATION_RETAINED_IN_RAW_QUOTES")
    # 每个节点自己的内容用于补充提及；父节点主摘录保留全部子标题和限定语。
    for field_name, (titles, keyword) in _FIELDS.items():
        field = fields[field_name]
        selected = [section for section in sections if section.recognized and section.title in titles]
        selected = [section for section in selected if not any(parent.start < section.start and section.end <= parent.end for parent in selected)]
        if len(selected) > 1:
            field["issues"].append("MULTIPLE_SECTIONS_SCOPE_UNRESOLVED")
        for index, section in enumerate(selected):
            row = {"heading": section.title, "claim": _claim(text, f"{field_name}.sections[{index}]", section.start, section.end, pages),
                   "items": [], "issues": [], "contextRefs": context_refs(section)}
            if not text[section.body_start:section.end].strip():
                row["issues"].append("EMPTY_SECTION")
            if field_name in ("conditions", "materials"):
                parts, item_issues = _numbered_parts(text, section.body_start, section.end)
                row["issues"].extend(item_issues)
                for item_index, (number, start, end) in enumerate(parts):
                    row["items"].append({"number": number, "claim": _claim(text, f"{field_name}.sections[{index}].items[{item_index}]", start, end, pages),
                                         "contextRefs": [row["claim"]["path"], *row["contextRefs"]]})
            field["sections"].append(row)
        if keyword:
            for section in sections:
                if any(parent.start <= section.start < parent.end for parent in selected):
                    continue
                if not section.recognized or section.title in ("咨询方式", "咨询及申诉", "咨询", "申诉"):
                    continue
                parts, part_issues = _numbered_parts(text, section.body_start, section.own_end)
                for issue in part_issues:
                    if issue != "NUMBERED_LIST_NOT_IDENTIFIED" and issue not in field["issues"]:
                        field["issues"].append(issue)
                for number, start, end in parts:
                    if re.search(keyword, re.sub(r"\s+", "", text[start:end])):
                        field["relatedExcerpts"].append({"heading": section.title, "number": number,
                            "claim": _claim(text, f"{field_name}.relatedExcerpts[{len(field['relatedExcerpts'])}]", start, end, pages),
                            "contextRefs": context_refs(section, include_own=True)})
        field["status"] = ("candidate_excerpt" if field["sections"] else "related_excerpt_only" if field["relatedExcerpts"]
                           else "not_attempted" if "ITEM_BUDGET_EXCEEDED" in field["issues"] else "not_found")
    for section in sections:
        if not section.recognized:
            index = len(result["unclassifiedSections"])
            result["unclassifiedSections"].append({"titleCandidate": section.title,
                "claim": _claim(text, f"unclassifiedSections[{index}]", section.start, section.end, pages)})
    if result["unclassifiedSections"]:
        result["issues"].append("UNCLASSIFIED_SECTIONS")
    if "TABLE_RELATIONSHIPS_NOT_INTERPRETED" in view_issues:
        fields["scope"]["issues"].append("TABLE_RELATIONSHIPS_NOT_INTERPRETED")
    return result
