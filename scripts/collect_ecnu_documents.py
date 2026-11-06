"""采集ECNU政策关系资料包；默认冻结官方快照回放，--live才访问官网。"""

from __future__ import annotations

import argparse
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path

from yantu.acquisition.document_bundle import DocumentBundleCollector, DocumentSeed, load_bundle_record
from yantu.acquisition.fetcher import FetchError, fetch
from yantu.acquisition.store import SnapshotStore
from yantu.evaluation.policy_regression import load_source

ROOT = Path(__file__).resolve().parents[1]
SEED = "https://sei.ecnu.edu.cn/d7/c3/c33171a776131/page.htm"


def replay_sources() -> dict:
    """读取三份冻结官方来源；下载时间/最终URL未知字段保持原记录。"""
    definitions = (
        ("trusted_policy", "ecnu_rules.html", "ecnu_rules_source.json"),
        ("ecnu_documents", "prenotice.html", "prenotice_html_source.json"),
        ("ecnu_documents", "prenotice.pdf", "prenotice_pdf_source.json"),
    )
    result = {}
    for folder, file, meta in definitions:
        snapshot = load_source(ROOT / "data/pilot" / folder, {"file": file, "metadata": meta})
        result[snapshot.url] = snapshot
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="显式访问白名单官网；不调用模型")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    request_id = uuid.uuid4().hex
    output = ROOT / "var/document_bundles" / request_id
    output.mkdir(parents=True, exist_ok=False)
    snapshots = SnapshotStore(output / "snapshots")
    if args.live:
        source_fetcher = fetch
    else:
        sources = replay_sources()

        def source_fetcher(url, *, timeout, max_bytes, allowed_hosts):
            if url not in sources:
                raise FetchError(url, "冻结数据集未包含此来源")
            snapshot = sources[url]
            if len(snapshot.body) > max_bytes:
                raise FetchError(url, "冻结快照超过当前预算")
            return snapshot

    bundle = DocumentBundleCollector(snapshots, allowed_hosts=("sei.ecnu.edu.cn",), fetcher=source_fetcher).collect(
        (DocumentSeed(SEED, "assessment_policy"),), request_id=request_id)
    bundle.save(output / "bundle.json")
    load_bundle_record(output / "bundle.json", snapshots)
    lines = ["# ECNU政策关联资料包", "", f"> author: Codex；created/updated: {datetime.now().astimezone().isoformat()}；status: 运行证据，非需求决策", "",
             f"模式：{'官网实时采集' if args.live else '冻结官网快照离线回放（非实时复核）'}；requestId：{request_id}", "",
             f"资料范围终态：{bundle.status}；推荐事实适用性：{bundle.recommendation_readiness}。", "",
             "覆盖仅限配置政策种子与可识别HTML关系；不表示全校资料或所有字段完整。名单/公示链接保留但不纳入本政策包下载。", "",
             "| 来源 | 获取/解析 | 页数 | 快照SHA256 |", "|---|---|---|---|"]
    for document in bundle.documents:
        parsed = document.parse_result
        lines.append(f"| {document.url} | {document.status}/{parsed.status.value if parsed else document.reason} | {parsed.declared_page_count if parsed else '-'} | {document.snapshot_sha256} |")
    lines += ["", "## 缺损与使用量", "", "```json", json.dumps({"gaps": bundle.gaps, "usage": bundle.usage}, ensure_ascii=False, indent=2), "```", "",
              "详细原文、页偏移、关系定位与排除理由见同目录bundle.json；各通知独立保存，不自动合并不同阶段规则。"]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "status": bundle.status, "documents": len(bundle.documents),
                      "edges": len(bundle.edges), "gaps": len(bundle.gaps)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
