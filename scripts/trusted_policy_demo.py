"""冻结华东师大官方快照→自动原文保留→核验发布→重载判定排序。

默认不访问网络或模型；--live-fetch重新获取配置的同一官方URL，
--llm显式选择模型提取（会消耗模型额度），配置缺失时直接报错。
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
from datetime import datetime
from uuid import uuid4

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from yantu.acquisition.fetcher import fetch
from yantu.acquisition.publications import PublicationRepository
from yantu.acquisition.store import SnapshotStore
from yantu.acquisition.structuring_agent import StructuringAgent
from yantu.acquisition.trusted_pipeline import ConservativeExtractor, OpportunityScope, TrustedPolicyPipeline
from yantu.evaluation.policy_regression import evidence_checks, load_source
from yantu.domain.models import StudentProfile
from yantu.domain.ranking import rank
from yantu.llm import load_client
from yantu.tasks.publications import TaskPublicationRepository
from yantu.tasks.store import TaskStore


def main() -> int:
    """使用本次提取发布的版本，输出回放边界、待确认点与可回溯证据。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-fetch", action="store_true")
    parser.add_argument("--llm", action="store_true")
    parser.add_argument("--store", choices=("file", "sqlite"), default="file",
                        help="选择隔离演示仓储；sqlite 使用任务租约与事务发布")
    parser.add_argument("--as-of", help="显式北京时间，无时区偏移；历史值只用于标注清楚的回放")
    args = parser.parse_args()
    if args.store == "sqlite" and args.llm:
        raise SystemExit("SQLite发布任务当前仅支持离线保守提取；不能使用 --llm")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    fixture_root = ROOT / "data/pilot/trusted_policy"
    manifest = json.loads((fixture_root / "manifest.json").read_text(encoding="utf-8"))
    source = manifest["sources"]["ecnu"]
    scope = OpportunityScope(**source["scope"])
    extractor = ConservativeExtractor()
    if args.llm:
        client = load_client(ROOT / "config/llm.json")
        if client is None:
            raise SystemExit("已选择真实模型，但缺少有效模型配置；未运行、无质量报告")
        print("将运行真实模型，最多2次调用、每次最多配置的max_tokens；本次不代表完整模型eval。")
        extractor = StructuringAgent(client)
    snapshot = fetch(scope.source_url, allowed_hosts=(scope.allowed_host,)) if args.live_fetch else load_source(fixture_root, source)
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else snapshot.fetched_at
    if as_of.tzinfo is not None:
        raise SystemExit("当前演示要求北京时间naive datetime，不能混用带偏移时间")
    run_root = ROOT / "var" / "trusted_demo" / uuid4().hex
    snapshots = SnapshotStore(run_root / "snapshots")
    if args.store == "sqlite":
        store = TaskStore(run_root / "tasks.sqlite3")
        repo = TaskPublicationRepository(store, snapshots)
        task_id = repo.submit(snapshot, scope, request_id=run_root.name, idempotency_key=run_root.name)
        task = repo.run(task_id, owner="demo")
        output = store.output(task_id, "publish")
        if output is None or task["status"] not in ("completed", "partial"):
            raise RuntimeError("任务未提交发布结果")
        record = repo.read_record(output["publicationVersion"])
        publication_status, version, reason = record["status"], output["publicationVersion"], record["reason"]
    else:
        repo = PublicationRepository(run_root / "facts", snapshots)
        outcome = TrustedPolicyPipeline(snapshots, repo, extractor).run(snapshot, scope, request_id=run_root.name)
        publication_status, version, reason = outcome.status, outcome.version, outcome.reason
    op = repo.load(scope.oid)
    result = {"mode": "live-source-read" if args.live_fetch else "frozen-source-replay", "timezone": "Asia/Shanghai",
              "asOf": as_of.isoformat(), "sourceCapturedAt": snapshot.fetched_at.isoformat(),
              "evidenceAvailableByAsOf": snapshot.fetched_at <= as_of, "publicationStatus": publication_status,
              "version": version, "reason": reason, "manualSeedUsed": False, "store": args.store,
              "extractor": "llm" if args.llm else "conservative-original-text-no-llm"}
    if op:
        item = rank([op], StudentProfile(school="示意学校", major="软件工程", grad_year=2027, cet6=500, ranking_percent=10), as_of)[0]
        result.update(eligibility=item.eligibility.verdict.value, opening=item.open_state.value, included=item.included,
                      excludedReason=item.excluded_reason, unknownPoints=item.eligibility.unknown_points,
                      evidence=[{"eid": e.eid, "url": e.url, "quote": e.quote, "snapshot": e.snapshot_sha256} for e in op.evidence],
                      evidenceFailures=evidence_checks(op, snapshot, snapshots))
    (run_root / "demo_report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"报告：{run_root / 'demo_report.json'}")
    return 0 if op and not result["evidenceFailures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
