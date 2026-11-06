"""离线核对资料包的阶段与专业适用表述；输出证据和禁止自动绑定的原因。"""

import argparse
import json
import logging
from pathlib import Path

from yantu.acquisition.applicability_service import inspect_applicability
from yantu.acquisition.artifact_files import atomic_write, canonical_json
from yantu.acquisition.error_events import log_failure
from yantu.acquisition.policy_applicability import ApplicabilityTarget


def main() -> None:
    """生成未发布的JSON及Markdown；异常由入口安全记录并非零退出。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--target", type=Path, help="含六轴显式目标的JSON；查询值不补原文缺失范围")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    target = ApplicabilityTarget(**json.loads(args.target.read_text(encoding='utf-8'))) if args.target else None
    report = inspect_applicability(args.bundle, target=target)
    output = args.bundle.parent / "policy_applicability.json"
    atomic_write(output, canonical_json(report))
    lines = ["# 招生阶段与专业适用表述核验", "", f"> author: Codex（AI助手）；created/updated: {report['created']}；status: 未发布，机会适用性待核验", "",
             "表述支持不表示当前有效或适用于学生；不同来源、阶段、方向不自动合并。", "",
             "| 来源 | 状态 | 资格表述 | 机会绑定 | 逐条款目标判断 |", "|---|---|---|---|---|"]
    for source in report["results"]:
        review = source["applicabilityReview"]
        wordings = ", ".join(row["wording"] or "unresolved" for row in review["qualification"])
        bindings = ", ".join(row['status'] for row in review['targetBindings']) or "not_requested_or_no_clause"
        lines.append(f"| {source['url']} | {review['status']} | {wordings} | {review['bindingDecision']} | {bindings} |")
    lines += ["", "原文、上下文、阶段观察、方向码、限定及阻断原因见同目录policy_applicability.json。", "",
              f"来源：{report['counts']['reportedSources']}/{report['counts']['plannedSources']}；跨源差异提示：{len(report['crossSourceReview'])}；模型/网络调用：0。"]
    atomic_write(output.with_suffix('.md'), ('\n'.join(lines) + '\n').encode('utf-8'))
    print(json.dumps({"output": str(output.resolve()), "counts": report["counts"]}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        log_failure(logging.getLogger(__name__), error, event="applicability_cli_failed")
        raise SystemExit(1) from None
