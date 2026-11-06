"""字段清单应用编排：校验源快照、重解析、日志与完整分母；无事实发布权限。"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path

from .document_bundle import load_bundle_record
from .fetcher import Snapshot
from .error_events import log_failure
from .parsers import DEFAULT_REGISTRY, ParserRegistry, parse_snapshot
from .policy_inventory import FIELD_NAMES, INVENTORY_VERSION, inventory_document
from .source_evidence import source_quality
from .store import SnapshotStore
from .parse_models import ParseResult

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReparsedSource:
    """同次校验的原始快照与解析结果；留在内存，不把源字节复制进JSON报告。"""

    snapshot: Snapshot
    parsed: ParseResult


def inspect_fields_with_sources(bundle_path: Path, *, registry: ParserRegistry = DEFAULT_REGISTRY) -> tuple[dict, list[ReparsedSource | None]]:
    """以原始字节为本次证据基底，不信任资料包缓存的正文；旧缺损仍保留。"""
    started = time.monotonic()
    store = SnapshotStore(bundle_path.parent / "snapshots")
    bundle = load_bundle_record(bundle_path, store)
    results, contexts, parse_calls = [], [], 0
    request_id = bundle["requestId"]
    LOGGER.info(json.dumps({"requestId": request_id, "event": "field_inventory_started", "sources": len(bundle["documents"])}))
    for document in bundle["documents"]:
        source_id = hashlib.sha256(document["url"].encode("utf-8")).hexdigest()[:16]
        if document["status"] != "parsed":
            contexts.append(None)
            row = {"url": document["url"], "snapshotSha256": document.get("snapshot_sha256", ""),
                   "fields": {field: {"status": "source_unavailable", "sections": [], "relatedExcerpts": [],
                                      "issues": [document.get("reason") or "SOURCE_NOT_PARSED"]} for field in FIELD_NAMES},
                   "coverage": "not_established", "publicationStatus": "not_published", "semanticVerification": "not_evaluated", "applicability": "not_evaluated"}
        else:
            try:
                source = Snapshot(document["url"], datetime.fromisoformat(document["fetched_at"]), document["http_status"],
                                  document["content_type"], store.read(document["snapshot_sha256"]), document.get("final_url", ""))
                parsed = parse_snapshot(source, registry)
                parse_calls += 1
                row = inventory_document(source, parsed)
                contexts.append(ReparsedSource(source, parsed))
            except Exception as error:
                log_failure(LOGGER, error, event="field_inventory_failed", request_id=request_id, source_id=source_id)
                raise
        row["inputStatus"] = document["status"]
        row["inputReason"] = document.get("reason", "")
        row["inputParseQuality"] = source_quality(document.get("parse_result"))
        row["inputGaps"] = [gap for gap in bundle["gaps"] if gap.get("url") == document["url"]]
        results.append(row)
        LOGGER.info(json.dumps({"requestId": request_id, "event": "field_inventory_source_done", "sourceId": source_id,
                                "statuses": {field: item["status"] for field, item in row["fields"].items()}}))
    counts = {field: {} for field in FIELD_NAMES}
    for row in results:
        for name, field in row["fields"].items():
            counts[name][field["status"]] = counts[name].get(field["status"], 0) + 1
    LOGGER.info(json.dumps({"requestId": request_id, "event": "field_inventory_completed", "sources": len(results), "parseCalls": parse_calls}))
    report = {"schemaVersion": "policy-field-inventory-v1", "author": "Codex（AI助手）", "created": datetime.now().astimezone().isoformat(),
            "requestId": request_id, "inputBundleDigest": bundle["recordDigest"], "inputBundleStatus": bundle["status"], "inputGaps": bundle["gaps"],
            "publicationStatus": "not_published", "semanticVerification": "not_evaluated", "applicability": "not_evaluated", "coverage": "not_established",
            "extractorVersion": INVENTORY_VERSION, "evidenceBasis": "reparsed_snapshot_bytes", "results": results,
            "counts": {"plannedSources": len(bundle["documents"]), "reportedSources": len(results), "fields": counts},
            "usage": {"parseCalls": parse_calls, "modelCalls": 0, "networkCalls": 0, "elapsedSeconds": time.monotonic() - started},
            "limitation": "候选段落及相关提及，未转换可计算规则/通道组合/专业归属；not_found只表示受限标题策略未识别，原资料缺损继续保留"}
    return report, contexts


def inspect_fields(bundle_path: Path, *, registry: ParserRegistry = DEFAULT_REGISTRY) -> dict:
    """兼容字段报告入口；解析上下文只供同次核验使用，不序列化原始字节。"""
    return inspect_fields_with_sources(bundle_path, registry=registry)[0]
