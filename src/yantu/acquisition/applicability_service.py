"""适用性检查编排；复用原始字节重解析，不添加采集或事实发布权限。"""

from __future__ import annotations

import logging
import json
import time
from dataclasses import asdict
from pathlib import Path

from .error_events import log_failure
from .field_inventory_service import inspect_fields_with_sources
from .parsers import DEFAULT_REGISTRY, ParserRegistry
from .policy_applicability import APPLICABILITY_VERSION, ApplicabilityTarget, compare_qualification_wording, review_document_applicability
from .notice_structure import notice_structure
from .notice_scope import resolve_notice_scope
from .notice_bindings import apply_notice_bindings, declared_programs
from .source_evidence import EvidenceResolver

LOGGER = logging.getLogger(__name__)


def _validate_scope_evidence(sources: list[dict], contexts: list) -> None:
    resolver = EvidenceResolver(sources, [context.parsed.pages if context else None for context in contexts])
    for index, source in enumerate(sources):
        resolver.body_claims(index)
        for name, view in source['noticeStructure']['views'].items():
            for claim in view['claims']:
                resolver.resolve({'sourceIndex': index, 'view': name, 'path': claim['path']})
        for ref in source['noticeScope'].get('identityEvidence', ()):
            resolver.resolve(dict(ref, sourceIndex=index))
        review = source['applicabilityReview']
        for group in ('stages', 'qualification', 'programs', 'restrictions', 'degreeScopes', 'targetBindings'):
            for row in review[group]:
                for path in [row['evidenceRef'], *row.get('contextRefs', ())]:
                    resolver.resolve({'sourceIndex': index, 'view': source['view'], 'path': path})
                for ref in row.get('supportingEvidence', ()):
                    resolver.resolve(ref)


def inspect_applicability(bundle_path: Path, *, registry: ParserRegistry = DEFAULT_REGISTRY, target: ApplicabilityTarget | None = None) -> dict:
    """校验/重解析资料包后核对分源表述；未完成适用性绑定的资料不能发布。"""
    started = time.monotonic()
    report, contexts = inspect_fields_with_sources(bundle_path, registry=registry)
    try:
        for source, context in zip(report["results"], contexts):
            source["applicabilityReview"] = review_document_applicability(source, target)
            source['noticeStructure'] = notice_structure(context.snapshot, context.parsed) if context else {'views': {}, 'programRows': [], 'issues': []}
            source['noticeScope'] = resolve_notice_scope(source, source['noticeStructure']) if context else {'status': 'not_attempted'}
        programs = declared_programs(report['results'])
        for index, (source, context) in enumerate(zip(report['results'], contexts)):
            if context:
                apply_notice_bindings(source, context.parsed, target, programs, index)
        report["crossSourceReview"] = compare_qualification_wording(report["results"])
        _validate_scope_evidence(report['results'], contexts)
    except Exception as error:
        log_failure(LOGGER, error, event="applicability_review_failed", request_id=report["requestId"])
        raise
    report["schemaVersion"] = "policy-applicability-review-v2"
    report["analyzerVersion"] = APPLICABILITY_VERSION
    report["bindingDecision"] = "hold"
    report["target"] = asdict(target) if target is not None else None
    report['declaredPrograms'] = programs
    report['scopeReviewStatus'] = 'completed'
    report['finalDirectoryStatus'] = 'unresolved'
    report["counts"]["applicability"] = {
        status: sum(source["applicabilityReview"]["status"] == status for source in report["results"])
        for status in ("unresolved", "not_attempted")
    }
    report["counts"]["targetBindings"] = {
        status: sum(binding["status"] == status for source in report["results"]
                    for binding in source["applicabilityReview"]["targetBindings"])
        for status in ("applicable", "not_applicable", "unresolved")
    }
    report["limitation"] = "分源通知及本地招生表支持逐条适用判断；未知格式/缺损不猜测，最终目录、当前有效性与开放窗口独立待核验，禁止自动合并或发布"
    report["usage"]["elapsedSeconds"] = time.monotonic() - started
    LOGGER.info(json.dumps({"event": "applicability_review_completed", "requestId": report["requestId"],
                            "sources": len(report["results"]), "differences": len(report["crossSourceReview"])}))
    return report
