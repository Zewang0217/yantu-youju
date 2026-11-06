"""有界获取关联政策资料，保存每份来源和正文载体的缺损；不合并招生规则。

资料包表示配置种子及其HTML正文关系，不代表全校发现完成或推荐事实已发布。
网页内容不能修改白名单、预算、种子角色或授予额外工具权限。
"""

from __future__ import annotations

import json
import hashlib
import logging
import math
import re
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Protocol

from .fetcher import FetchError, Snapshot, fetch
from .artifact_files import atomic_write, canonical_json
from .parse_models import ParseResult, ParseStatus
from .parsers import DEFAULT_REGISTRY, ParserRegistry, parse_snapshot
from .source_urls import validate_source_url
from .store import SnapshotStorageError, SnapshotStore

LOGGER = logging.getLogger(__name__)


class BoundedSourceFetcher(Protocol):
    """访问受控来源；失败可抛FetchError，其他编程异常不被当作资料未知。"""

    def __call__(self, url: str, *, timeout: float, max_bytes: int, allowed_hosts: tuple[str, ...]) -> Snapshot: ...


@dataclass(frozen=True)
class DocumentBudget:
    """顺序采集预算；失败请求的字节额度保守保留，不用0掩盖未知传输。"""

    max_documents: int = 8
    max_depth: int = 2
    max_file_bytes: int = 2 * 1024 * 1024
    max_total_bytes: int = 8 * 1024 * 1024
    deadline_seconds: float = 90.0
    fetch_timeout: float = 20.0

    def __post_init__(self) -> None:
        limits = (self.max_documents, self.max_file_bytes, self.max_total_bytes)
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in limits):
            raise ValueError("资料数量/字节预算必须为正整数")
        if isinstance(self.max_depth, bool) or not isinstance(self.max_depth, int) or self.max_depth < 0:
            raise ValueError("max_depth必须为非负整数")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0
               for v in (self.deadline_seconds, self.fetch_timeout)):
            raise ValueError("时间预算必须为有限正数")


@dataclass(frozen=True)
class DocumentSeed:
    """操作者配置的来源和角色；角色只作上下文，不裁定其招生阶段适用性。"""

    url: str
    role: str


@dataclass
class BundleDocument:
    """一项计划来源的实际终态；parsed不等于政策规则已核验或全部字段可判定。"""

    url: str
    role: str
    depth: int
    required: bool
    status: str = "queued"
    reason: str = ""
    snapshot_sha256: str = ""
    fetched_at: str = ""
    final_url: str = ""
    content_type: str = ""
    http_status: int | None = None
    parse_result: ParseResult | None = None


@dataclass(frozen=True)
class DocumentEdge:
    """从原HTML定位的候选关系与处理决定，排除项也保留。"""

    parent_url: str
    target_url: str
    kind: str
    label: str
    source_attribute: str
    line: int
    column: int
    selected: bool
    reason: str = ""
    locator_kind: str = "html_tag"
    text_start: int | None = None
    text_end: int | None = None
    raw_value: str = ""


@dataclass
class DocumentBundle:
    """可审计的单次资料包；覆盖仅限声明种子与支持的HTML关系。"""

    request_id: str
    documents: list[BundleDocument]
    edges: list[DocumentEdge]
    gaps: list[dict]
    status: str
    usage: dict
    configured_seeds: tuple[DocumentSeed, ...] = ()
    scope: str = "configured-policy-seeds-and-html-relations"
    recommendation_readiness: str = "not_evaluated"

    def to_record(self) -> dict:
        """转成JSON契约；保留页面/引用/原文，不将datetime交给隐式字符串转换。"""
        documents = []
        for item in self.documents:
            row = {key: value for key, value in asdict(item).items() if key != "parse_result"}
            if item.parse_result is not None:
                parsed = asdict(item.parse_result)
                parsed["status"] = item.parse_result.status.value
                parsed["dates"] = [date.isoformat() for date in item.parse_result.dates]
                row["parse_result"] = parsed
            documents.append(row)
        return {"schemaVersion": "document-bundle-v1", "requestId": self.request_id, "scope": self.scope,
                "status": self.status, "recommendationReadiness": self.recommendation_readiness,
                "documents": documents, "edges": [asdict(edge) for edge in self.edges],
                "configuredSeeds": [asdict(seed) for seed in self.configured_seeds],
                "gaps": self.gaps, "usage": self.usage}

    def save(self, path: Path) -> None:
        """保存运行资料包；这不是更新机会事实库或推荐结果的发布操作。"""
        record = self.to_record()
        record["recordDigest"] = hashlib.sha256(canonical_json(record)).hexdigest()
        atomic_write(path, canonical_json(record))


def load_bundle_record(path: Path, snapshots: SnapshotStore) -> dict:
    """复用前核对资料包摘要和源字节；摘要用于发现损坏，不是来源身份签名。"""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.get("schemaVersion") != "document-bundle-v1":
            raise ValueError("不支持的资料包结构")
        actual = hashlib.sha256(canonical_json({k: v for k, v in record.items() if k != "recordDigest"})).hexdigest()
        if actual != record.get("recordDigest"):
            raise ValueError("资料包摘要不匹配")
        for document in record["documents"]:
            if document["status"] == "parsed":
                snapshots.read(document["snapshot_sha256"])
    except (OSError, ValueError, TypeError, KeyError, SnapshotStorageError) as e:
        raise SnapshotStorageError("资料包或其来源快照不可验证，禁止复用") from e
    return record


class DocumentBundleCollector:
    """显式预算/范围的顺序编排；仅调用只读fetch/parse/store，无模型或报名操作。"""

    def __init__(self, snapshots: SnapshotStore, *, allowed_hosts: tuple[str, ...],
                 fetcher: BoundedSourceFetcher = fetch, registry: ParserRegistry = DEFAULT_REGISTRY,
                 budget: DocumentBudget = DocumentBudget(), clock: Callable[[], float] = time.monotonic):
        if not allowed_hosts:
            raise ValueError("必须配置官方来源主机白名单")
        self.snapshots = snapshots
        self.allowed_hosts = allowed_hosts
        self.fetcher = fetcher
        self.registry = registry
        self.budget = budget
        self.clock = clock

    def collect(self, seeds: tuple[DocumentSeed, ...], *, request_id: str) -> DocumentBundle:
        """获取支持关系中的资料；可预期获取失败形成显式缺损，存储/编程错误上抛。"""
        if not request_id or not seeds:
            raise ValueError("request_id和种子来源不能为空")
        documents: dict[str, BundleDocument] = {}
        edges: list[DocumentEdge] = []
        gaps: list[dict] = []
        queue: deque[str] = deque()
        started = self.clock()
        attempts, known_bytes, reserved_unknown = 0, 0, 0

        def plan(url: str, role: str, depth: int, required: bool) -> None:
            canonical = validate_source_url(url, self.allowed_hosts)
            if canonical in documents:
                documents[canonical].required |= required
                return
            item = BundleDocument(canonical, role, depth, required)
            documents[canonical] = item
            queue.append(canonical)

        for seed in seeds:
            plan(seed.url, seed.role, 0, True)
        LOGGER.info(json.dumps({"requestId": request_id, "event": "bundle_started", "seedCount": len(seeds)}))
        while queue:
            item = documents[queue.popleft()]
            remaining_time = self.budget.deadline_seconds - (self.clock() - started)
            remaining_bytes = self.budget.max_total_bytes - known_bytes - reserved_unknown
            if item.depth > self.budget.max_depth:
                item.status, item.reason = "not_run", "DEPTH_BUDGET"
            elif attempts >= self.budget.max_documents or remaining_bytes <= 0 or remaining_time <= 0:
                item.status, item.reason = "not_run", "TASK_BUDGET"
            else:
                attempts += 1
                allowance = min(self.budget.max_file_bytes, remaining_bytes)
                try:
                    source = self.fetcher(item.url, timeout=min(self.budget.fetch_timeout, remaining_time),
                                          max_bytes=allowance, allowed_hosts=self.allowed_hosts)
                except FetchError:
                    reserved_unknown += allowance
                    item.status, item.reason = "access_failed", "FETCH_FAILED_BYTES_UNKNOWN"
                    LOGGER.warning(json.dumps({"requestId": request_id, "event": "bundle_source_failed", "code": item.reason}))
                    continue
                known_bytes += len(source.body)
                if len(source.body) > allowance:
                    item.status, item.reason = "rejected", "FETCH_ADAPTER_BYTE_LIMIT_VIOLATION"
                    continue
                try:
                    if source.status_code != 200 or validate_source_url(source.url, self.allowed_hosts) != item.url:
                        raise ValueError("响应状态或来源不对应请求")
                    if source.final_url and validate_source_url(source.final_url, self.allowed_hosts) != item.url:
                        raise ValueError("资料包不接受未重新核验的重定向入口")
                except ValueError:
                    item.status, item.reason = "rejected", "RESPONSE_SCOPE_MISMATCH"
                    continue
                if self.clock() - started > self.budget.deadline_seconds:
                    item.status, item.reason = "not_run", "DEADLINE_AFTER_FETCH"
                    continue
                try:
                    self.snapshots.save(source)
                    parsed = parse_snapshot(source, self.registry)
                    self.snapshots.read(source.sha256)
                except (SnapshotStorageError, OSError):
                    LOGGER.exception(json.dumps({"requestId": request_id, "event": "bundle_storage_failed"}))
                    raise
                item.snapshot_sha256, item.fetched_at, item.final_url = source.sha256, source.fetched_at.isoformat(), source.final_url
                item.content_type, item.http_status = source.content_type, source.status_code
                item.parse_result = parsed
                item.status = "parsed"
                if parsed.status in (ParseStatus.FAILED, ParseStatus.UNSUPPORTED):
                    item.reason = "CARRIER_NOT_READABLE"
                if self.clock() - started > self.budget.deadline_seconds:
                    item.reason = "DEADLINE_AFTER_PARSE"
                for link in parsed.links:
                    selected, reason = self._select_link(link.kind, link.label, link.url)
                    target = validate_source_url(link.url, self.allowed_hosts) if selected else link.url
                    edges.append(DocumentEdge(item.url, target, link.kind, link.label, link.source_attribute,
                                              link.line, link.column, selected, reason, link.locator_kind,
                                              link.text_start, link.text_end, link.raw_value))
                    if selected:
                        plan(link.url, link.kind, item.depth + 1, True)

        def body_covered(url: str, visiting: set[str]) -> bool:
            item = documents.get(url)
            if url in visiting or item is None or item.parse_result is None:
                return False
            if item.parse_result.content_text.strip():
                return True
            children = [edge for edge in edges if edge.parent_url == url and edge.kind in ("embedded_document", "body_image")]
            return bool(children) and all(edge.selected and body_covered(edge.target_url, visiting | {url}) for edge in children)

        for item in documents.values():
            parsed = item.parse_result
            if item.status != "parsed":
                gaps.append({"url": item.url, "code": item.reason, "required": item.required})
                continue
            if parsed.status in (ParseStatus.UNSUPPORTED, ParseStatus.FAILED):
                gaps.append({"url": item.url, "code": "CARRIER_NOT_READABLE", "required": item.required})
            elif parsed.pages and parsed.status != ParseStatus.COMPLETE:
                gaps.append({"url": item.url, "code": "PDF_PAGE_GAP_OR_BUDGET", "required": item.required})
            if item.reason == "DEADLINE_AFTER_PARSE":
                gaps.append({"url": item.url, "code": item.reason, "required": item.required})
            if parsed.issues:
                gaps.append({"url": item.url, "code": "REFERENCE_COVERAGE_INCOMPLETE", "issues": parsed.issues, "required": item.required})
            if parsed.content_scope == "body_fallback":
                gaps.append({"url": item.url, "code": "BODY_SCOPE_UNCERTAIN", "required": item.required})
            for edge in edges:
                if edge.parent_url == item.url and edge.reason == "AMBIGUOUS_POLICY_RELATION":
                    gaps.append({"url": item.url, "target": edge.target_url, "code": edge.reason, "required": True})
            required_edges = [edge for edge in edges if edge.parent_url == item.url and
                              (edge.kind in ("embedded_document", "body_image") or
                               (edge.reason == "OUTSIDE_SOURCE_ALLOWLIST" and self._relation_scope(edge.kind, edge.label) == "required"))]
            if not body_covered(item.url, set()):
                gaps.append({"url": item.url, "code": "BODY_NOT_LOCATED_OR_UNRESOLVED", "required": item.required})
            if not item.final_url:
                gaps.append({"url": item.url, "code": "LEGACY_FINAL_URL_NOT_RECORDED", "required": False})
            for edge in required_edges:
                if not edge.selected:
                    gaps.append({"url": item.url, "target": edge.target_url, "code": "POLICY_REFERENCE_BLOCKED", "required": True})
        status = "partial" if any(gap["required"] for gap in gaps) else "completed"
        usage = {"attempts": attempts, "knownBodyBytes": known_bytes, "reservedUnknownBytes": reserved_unknown,
                 "elapsedSeconds": self.clock() - started, "modelCalls": 0,
                 "limit": asdict(self.budget), "redirectPolicy": "reject-and-revalidate-entry",
                 "limitation": "时间预算为节点间检查及网络socket超时，尚非解析进程硬隔离；只统计响应正文，不含HTTP头"}
        result = DocumentBundle(request_id, list(documents.values()), edges, gaps, status, usage, configured_seeds=seeds)
        LOGGER.info(json.dumps({"requestId": request_id, "event": "bundle_completed", "status": status,
                                "sources": len(documents), "gaps": len(gaps), "attempts": attempts}))
        return result

    def _select_link(self, kind: str, label: str, url: str) -> tuple[bool, str]:
        scope = self._relation_scope(kind, label)
        if scope == "ambiguous":
            return False, "AMBIGUOUS_POLICY_RELATION"
        if scope == "excluded":
            return False, "OUTSIDE_DECLARED_POLICY_SCOPE"
        try:
            validate_source_url(url, self.allowed_hosts)
        except ValueError:
            return False, "OUTSIDE_SOURCE_ALLOWLIST"
        return True, ""

    @staticmethod
    def _relation_scope(kind: str, label: str) -> str:
        """当前政策资料包的显式范围；嵌入正文不因标题启发式被排除。"""
        if kind in ("embedded_document", "body_image"):
            return "required"
        if re.search(r"名单|公示", label):
            # “推免考核名单”可排除；“名单及夏令营通知”等混合内容不能只凭名单二字排除。
            return "ambiguous" if re.search(r"政策|细则|通知", label) else "excluded"
        if kind == "attachment" or (kind == "linked_page" and re.search(r"预报名|推免|夏令营|通知|细则|政策", label)):
            return "required"
        return "excluded"
