"""运行冻结快照/合成草案的发布回归，输出真实落盘终态而非模型自评。

所有case隔离存储。此runner不访问网络、密钥或在线数据，结果仅代表
发布安全边界与工作流回归；真实LLM质量和人工金标校准仍需独立实验。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from ..acquisition.fetcher import Snapshot
from ..acquisition.parsers import parse_snapshot
from ..acquisition.publications import PublicationRepository
from ..acquisition.store import SnapshotStore
from ..acquisition.structuring_agent import StructuringAgent
from ..acquisition.trusted_pipeline import ConservativeExtractor, OpportunityScope, TrustedPolicyPipeline
from ..domain.models import StudentProfile
from ..domain.ranking import rank


class FixtureDraftExtractor:
    """冻结草案的替身；仅用于回放及消费边界测试，不冒充真实模型调用。"""

    def __init__(self, payload: dict):
        self.payload = payload

    def structure(self, text: str):
        result = StructuringAgent.validate_output(json.dumps(self.payload, ensure_ascii=False), text)
        result.prompt_version = "fixture-draft-replay-v1-no-llm"
        return result


def _fixture_path(root: Path, name: str) -> Path:
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("fixture路径越过数据集目录")
    return path


def load_source(root: Path, definition: dict, *, html_replace: list[str] | None = None) -> Snapshot:
    """校验冻结官方快照摘要；只允许修改显式合成源。"""
    body = _fixture_path(root, definition.get("file") or definition["html"]).read_bytes()
    if definition.get("metadata"):
        meta = json.loads(_fixture_path(root, definition["metadata"]).read_text(encoding="utf-8"))
        if hashlib.sha256(body).hexdigest() != meta["sha256"]:
            raise ValueError("官方fixture快照摘要不匹配")
        url, fetched_at, content_type = meta["url"], meta["fetched_at"], meta["content_type"]
    else:
        url, fetched_at, content_type = definition["url"], definition["fetchedAt"], "text/html; charset=utf-8"
    if html_replace:
        if definition["provenance"] != "synthetic":
            raise ValueError("不允许改写官方fixture后仍称官方原文")
        body = body.decode("utf-8").replace(*html_replace).encode("utf-8")
    final_url = meta.get("final_url", "") if definition.get("metadata") else ""
    status = meta.get("status_code", 200) if definition.get("metadata") else 200
    return Snapshot(url, datetime.fromisoformat(fetched_at), status, content_type, body, final_url)


def evidence_checks(opportunity, snapshot: Snapshot, snapshots: SnapshotStore) -> list[str]:
    """检查落盘后所有条件引用、原文区间和快照摘要，不使用提取器自述。"""
    from ..domain.eligibility import evaluate

    failures = []
    snapshot = replace(snapshot, body=snapshots.read(snapshot.sha256))
    text = parse_snapshot(snapshot).text
    by_id = {e.eid: e for e in opportunity.evidence}
    if len(by_id) != len(opportunity.evidence):
        failures.append("重复证据ID")
    for detail in evaluate(opportunity, StudentProfile()).details:
        if not detail.evidence_refs or any(ref not in by_id for ref in detail.evidence_refs):
            failures.append("条件缺少有效证据引用")
    for evidence in opportunity.evidence:
        if (evidence.snapshot_sha256 != snapshot.sha256 or evidence.text_sha256 != hashlib.sha256(text.encode("utf-8")).hexdigest()
                or text[evidence.start_offset:evidence.end_offset] != evidence.quote):
            failures.append("证据落盘后不再回溯到同一快照原文")
    return failures


def run_regression(manifest_path: Path, output_root: Path) -> dict:
    """运行全部预分配用例，保留失败与分母；禁止复用已有任务目录。"""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    output_root.mkdir(parents=True, exist_ok=False)
    results = []
    for case in manifest["cases"]:
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", case["id"]):
            raise ValueError("非法case ID")
        source = manifest["sources"][case["source"]]
        case_root = output_root / case["id"]
        row = {"id": case["id"], "category": case["category"], "provenance": source["provenance"],
               "annotationStatus": case["annotationStatus"], "expected": case["expected"], "failures": []}
        try:
            snapshot = load_source(manifest_path.parent, source, html_replace=case.get("htmlReplace"))
            scope = OpportunityScope(**source["scope"])
            if case.get("scopeOverride"):
                scope = replace(scope, **case["scopeOverride"])
            extractor = ConservativeExtractor() if case["extractor"] == "conservative" else FixtureDraftExtractor(case["draft"])
            snapshots = SnapshotStore(case_root / "snapshots")
            repo = PublicationRepository(case_root / "facts", snapshots)
            outcome = TrustedPolicyPipeline(snapshots, repo, extractor).run(
                snapshot, scope, request_id=f"regression-{case['id']}")
            observed = {"status": outcome.status}
            opportunity = repo.load(scope.oid)
            if opportunity:
                item = rank([opportunity], StudentProfile(**case["profile"]), datetime.fromisoformat(case["asOf"]))[0]
                observed.update(eligibility=item.eligibility.verdict.value, opening=item.open_state.value, included=item.included,
                                firstLabel=item.eligibility.details[0].label if item.eligibility.details else "")
                row["failures"].extend(evidence_checks(opportunity, snapshot, snapshots))
                row["evidenceCount"] = len(opportunity.evidence)
            elif outcome.status != "quarantined":
                row["failures"].append("发布终态没有可重载机会")
            row.update(observed=observed, version=outcome.version, reason=outcome.reason, snapshotSha256=snapshot.sha256)
            row["failures"].extend(f"{key}: expected={value!r}, actual={observed.get(key)!r}"
                                   for key, value in case["expected"].items() if observed.get(key) != value)
            row["result"] = "regression_fail" if row["failures"] else "regression_pass"
        except (ValueError, OSError, RuntimeError) as e:
            row["result"] = "execution_failed"
            row["errorType"] = type(e).__name__
            row["failures"].append(str(e))
        results.append(row)
    counts = Counter(row["result"] for row in results)
    report = {"datasetVersion": manifest["version"], "manifestSha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
              "mode": "offline-safeguard-regression", "realModelCalls": 0, "modelQuality": "N/A",
              "annotationStatus": "AI-authored-pending-human", "planned": len(manifest["cases"]),
              "counts": dict(counts), "results": results,
              "limits": ["冻结官方快照和合成草案回放，不代表新提示词的真实模型效果", "AI预标注待人工终审，不代表院校分布或全网覆盖率"]}
    (output_root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rows = ["# 可信政策发布离线回归报告", "", f"数据集：{manifest['version']}；计划用例：{report['planned']}。",
            f"计数：{dict(counts)}。真实模型调用：0；模型质量：N/A。", "", "AI预标注待人工终审；此报告检验落盘与门禁，不是模型准确率。", "",
            "| 用例 | 数据来源 | 回归结果 | 实际终态 |", "|---|---|---|---|"]
    rows.extend(f"| {row['id']} | {row['provenance']} | {row['result']} | {row.get('observed', {})} |" for row in results)
    (output_root / "report.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return report
