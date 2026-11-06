"""将单份官方快照的申请条件草案核验、绑定证据并发布为可重载事实。

当前范围是配置好的单院系/专业/预报名通知。发现新入口、多份通知合并、
在线候选复核和全局更新不由此单文档切片承担。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass
from typing import Protocol

from ..domain import condition_codec as codec
from ..domain.models import AndGroup, Evidence, Opportunity
from .fetcher import Snapshot
from .parsers import ParseStatus, parse_snapshot
from .policy_verification import (
    PolicyValidationError, SourceClaim, application_section, bind_claims,
    verify_application_tree, validate_tree_budget,
)
from .publications import PublicationError
from .store import SnapshotStore, SnapshotStorageError, opportunity_to_record
from .structuring_agent import StructuringAgent, StructuringResult
from .registration_window import extract_registration_window
from .source_urls import validate_source_url

LOGGER = logging.getLogger(__name__)
VERIFIER_VERSION = "policy-subset-v2-window-precision"


class PolicyExtractor(Protocol):
    """消费正文返回草案；不具有事实库写权限。"""

    def structure(self, text: str) -> StructuringResult: ...


class PublicationSink(Protocol):
    """核验完成后可写入记录的端口；实现方负责自己的提交和并发边界。"""

    snapshots: SnapshotStore

    def write(self, record: dict, *, expected_version: str | None) -> str: ...


class ConservativeExtractor:
    """无模型基线：整条保留编号申请条件为未知，不推断定性门槛。"""

    def structure(self, text: str) -> StructuringResult:
        """生成完整原文占位；章节不受支持时显式失败。"""
        section = application_section(text)
        tree = {"type": "and", "conditions": [
            {"type": "unstructured", "label": clause, "quote": clause,
             "reason": "定性条款/未获独立可计算语义，保留原文待确认"}
            for clause in section.clauses
        ]}
        result = StructuringAgent.validate_output(json.dumps({"conditions": tree}, ensure_ascii=False), text)
        result.prompt_version = "conservative-extractor-v1-no-llm"
        return result


@dataclass(frozen=True)
class OpportunityScope:
    """配置的单一机会边界；模型不能改变这些字段，发布前需与来源逐项核对。"""

    oid: str
    school: str
    department: str
    program: str
    batch: str
    entry_year: int
    degree_type: str
    source_url: str
    allowed_host: str


@dataclass(frozen=True)
class PublicationOutcome:
    """任务终态与事实版本分离；隔离不替换之前已发布的事实。"""

    status: str
    version: str
    reason: str = ""


def _scope_evidence(scope: OpportunityScope, snapshot: Snapshot, text: str, title: str) -> dict:
    try:
        source_url = validate_source_url(snapshot.url, (scope.allowed_host,))
        if snapshot.final_url:
            validate_source_url(snapshot.final_url, (scope.allowed_host,))
    except ValueError as e:
        raise PolicyValidationError("来源不在该机会预配置的官方URL范围") from e
    if source_url != scope.source_url or snapshot.status_code != 200:
        raise PolicyValidationError("来源不在该机会预配置的官方URL范围")
    if isinstance(scope.entry_year, bool) or not isinstance(scope.entry_year, int):
        raise PolicyValidationError("入学年份必须是整数")
    # 当前仅支持单院系年度标题；多院系合并页必须另建范围核验器。
    title_compact = re.sub(r"\s+", "", title)
    for value in (scope.school, scope.department, f"{scope.entry_year}年"):
        if not value or value not in title_compact:
            raise PolicyValidationError("标题不支持配置的学校、院系或入学年份")
    scope_claims = {}
    for name in ("program", "batch", "degree_type"):
        value = getattr(scope, name)
        compact = re.sub(r"\s+", "", value)
        if not compact or compact not in re.sub(r"\s+", "", text):
            raise PolicyValidationError(f"来源不支持配置的{name}")
        scope_claims[name] = value
    row = re.sub(r"\s+", "", scope.program + scope.degree_type)
    if row not in re.sub(r"\s+", "", text):
        raise PolicyValidationError("未在同一原文片段证明专业与学位类型的对应关系")
    return {"title": title, "configuredScope": asdict(scope), "supportedTextValues": scope_claims,
            "limitation": "配置范围与单页标题/正文一致；不证明其他专业或关联通知覆盖完整"}


def _bind_evidence(tree: dict, claims: list[SourceClaim], snapshot: Snapshot, text: str) -> tuple[dict, tuple[Evidence, ...]]:
    by_path = {}
    evidence = []
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    for claim in claims:
        eid = hashlib.sha256(f"{snapshot.sha256}:{claim.path}:{claim.start}:{claim.end}".encode()).hexdigest()
        by_path[claim.path] = eid
        evidence.append(Evidence(eid=eid, source_name="来源快照原文", url=snapshot.url,
                                 quote=claim.quote, located_at=f"text[{claim.start}:{claim.end}]/{claim.path}",
                                 snapshot_sha256=snapshot.sha256, text_sha256=text_hash,
                                 start_offset=claim.start, end_offset=claim.end))

    def bind(node: dict, path: str) -> dict:
        node = dict(node)
        if path in by_path:
            node["evidence_refs"] = (by_path[path],)
            node["label"] = next(claim.quote for claim in claims if claim.path == path)
        if node["type"] in ("and", "or"):
            node["label"] = "必要条件(全部满足)" if node["type"] == "and" else "替代条件(满足其一)"
            node["conditions"] = [bind(child, f"{path}.conditions[{i}]") for i, child in enumerate(node["conditions"])]
        if node["type"] == "unstructured":
            node["label"] = node["quote"]
            node["reason"] = "尚未转换为可自动判定条件，请依据原文确认"
            claim = next(claim for claim in claims if claim.path == path)
            node["located_at"] = f"text[{claim.start}:{claim.end}]"
        return node

    return bind(tree, "conditions"), tuple(evidence)


class TrustedPolicyPipeline:
    """单份快照的有界提取/独立门禁/发布编排；不隐式调用网络或模型。"""

    def __init__(self, snapshots: SnapshotStore, publications: PublicationSink, extractor: PolicyExtractor):
        self.snapshots = snapshots
        self.publications = publications
        self.extractor = extractor

    def run(self, snapshot: Snapshot, scope: OpportunityScope, *, request_id: str,
            expected_version: str | None = None) -> PublicationOutcome:
        """保存快照并审核草案；业务不确定返回隔离，系统故障上抛，不伪装成功。"""
        if not request_id:
            raise ValueError("request_id 必填")
        LOGGER.info(json.dumps({"requestId": request_id, "event": "policy_started", "oid": scope.oid}, ensure_ascii=False))
        try:
            self.snapshots.save(snapshot)
        except (SnapshotStorageError, OSError) as e:
            LOGGER.exception(json.dumps({"requestId": request_id, "event": "snapshot_storage_failed", "oid": scope.oid}))
            raise PublicationError("源快照无法可靠落盘，任务未发布") from e
        parsed = parse_snapshot(snapshot)
        record = {"oid": scope.oid, "requestId": request_id, "status": "quarantined",
                  "source": {"url": snapshot.url, "sha256": snapshot.sha256, "fetchedAt": snapshot.fetched_at.isoformat(),
                             "finalUrl": snapshot.final_url or None,
                             "textSha256": hashlib.sha256(parsed.text.encode("utf-8")).hexdigest()},
                  "verifierVersion": VERIFIER_VERSION, "reason": "", "checks": []}
        try:
            if parsed.status in (ParseStatus.FAILED, ParseStatus.UNSUPPORTED):
                raise PolicyValidationError("正文解析失败或载体不支持")
            if len(parsed.text) > 50_000:
                raise PolicyValidationError("单页正文超过当前提取预算，需拆分受控范围")
            record["scopeEvidence"] = _scope_evidence(scope, snapshot, parsed.text, parsed.title)
            draft = self.extractor.structure(parsed.text)
            record["rawDraft"] = draft.raw_tree
            record["extractorVersion"] = draft.prompt_version
            record["generation"] = {"model": draft.model_id, "temperature": draft.temperature,
                                    "attempts": draft.attempts, "usage": None,
                                    "usageStatus": "not_reported" if draft.attempts else "no_model_in_this_run"}
            record["parserVersion"] = "html-notice-v1"
            record["checks"] = [asdict(check) for check in draft.gate_report]
            if draft.status != "structured" or draft.raw_tree is None:
                raise PolicyValidationError("提取未通过草案校验；本次机会未发布")
            # 重新从原始树和原始正文验证，不信任提取者返回的status/claims/spec。
            validate_tree_budget(draft.raw_tree)
            tree, claims = bind_claims(draft.raw_tree, parsed.text)
            verify_application_tree(tree, claims, parsed.text)
            window, window_claims = extract_registration_window(parsed.text)
            tree, evidence = _bind_evidence(tree, claims + window_claims, snapshot, parsed.text)
            condition = codec.build(tree)
            if not isinstance(condition, AndGroup):
                condition = AndGroup((condition,), label="申请条件")
            opportunity = Opportunity(scope.oid, scope.school, scope.department, scope.program, scope.batch,
                                      scope.entry_year, scope.degree_type, (window,), condition, evidence=evidence)
            section = application_section(parsed.text)
            record["nonEligibilityText"] = parsed.text[section.end:]
            record["opportunity"] = opportunity_to_record(opportunity)
            record["boundTree"] = tree
            record["fieldAssessments"] = [
                {"fieldPath": claim.path, "originalTextVerdict": "supported",
                 "claimVerdict": "unknown" if _node_at_path(tree, claim.path).get("type") == "unstructured" else "supported",
                 "evidenceRefs": [e.eid for e in evidence if e.located_at.endswith("/" + claim.path)]}
                for claim in claims
            ]
            record["fieldAssessments"].extend(
                {"fieldPath": f"window.{name}", "claimVerdict": "supported" if value is not None else "unknown",
                 "evidenceRefs": [e.eid for e in evidence if e.located_at.endswith(f"/window.{name}")]}
                for name, value in (("start", window.start), ("end", window.end))
            )
            partial = any(c.get("type") == "unstructured" for c in _nodes(tree)) or window.start is None or window.end is None
            record["status"] = "published_partial" if partial else "published"
            record["checks"].append({"name": "independent_policy_gate", "passed": True,
                                      "detail": "单页范围、申请阶段、完整性与受支持数值/关系已核验；未知原文保留"})
        except PolicyValidationError as e:
            record["reason"] = str(e)
        record["publicationStatus"] = "quarantined" if record["status"] == "quarantined" else "verified_published"
        record["completeness"] = "partial" if record["status"] == "published_partial" else ("supported_subset" if record["status"] == "published" else "unpublished")
        try:
            self.publications.snapshots.read(snapshot.sha256)
            version = self.publications.write(record, expected_version=expected_version)
        except (PublicationError, SnapshotStorageError) as e:
            LOGGER.exception(json.dumps({"requestId": request_id, "event": "policy_publish_failed", "oid": scope.oid}))
            raise PublicationError("事实发布失败，不能报告成功") from e
        log = LOGGER.warning if record["status"] == "quarantined" else LOGGER.info
        log(json.dumps({"requestId": request_id, "event": "policy_completed", "oid": scope.oid,
                        "status": record["status"], "version": version}, ensure_ascii=False))
        return PublicationOutcome(record["status"], version, record["reason"])


def _nodes(tree: dict):
    yield tree
    for child in tree.get("conditions", []):
        yield from _nodes(child)


def _node_at_path(tree: dict, path: str) -> dict:
    node = tree
    for index in re.findall(r"\.conditions\[(\d+)\]", path):
        node = node["conditions"][int(index)]
    return node
