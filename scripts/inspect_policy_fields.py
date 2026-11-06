"""输出逐文档候选字段清单；离线读取已保存来源，不调用网络/模型。"""

import argparse
import json
import logging
from pathlib import Path

from yantu.acquisition.artifact_files import atomic_write, canonical_json
from yantu.acquisition.field_inventory_service import inspect_fields
from yantu.acquisition.policy_inventory import FIELD_NAMES
from yantu.acquisition.error_events import log_failure


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    report = inspect_fields(args.bundle)
    output = args.bundle.parent / "policy_fields.json"
    atomic_write(output, canonical_json(report))
    lines = ["# 逐文档候选政策字段清单", "", f"> author: Codex（AI助手）；created/updated: {report['created']}；status: 未语义核验、未发布", "",
             "本清单保留候选原文与缺损；not_found不表示官方没有要求。各来源独立，未合并招生阶段或裁定专业适用性。", "",
             "| 来源 | 条件 | 材料 | 考核 | 通道 | 范围 | 其他说明 |", "|---|---|---|---|---|---|---|"]
    for row in report["results"]:
        lines.append("| " + row["url"] + " | " + " | ".join(row["fields"][name]["status"] for name in FIELD_NAMES) + " |")
    lines += ["", "原文引用、页码、hash、解析状态、未分类章节与缺损见同目录policy_fields.json。", "",
              f"输入资料包状态：{report['inputBundleStatus']}；来源分母：{report['counts']['reportedSources']}/{report['counts']['plannedSources']}；模型调用：0。"]
    atomic_write(output.with_suffix(".md"), ("\n".join(lines) + "\n").encode("utf-8"))
    print(json.dumps({"output": str(output.resolve()), "counts": report["counts"]}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # 程序异常仍使CLI非零失败；避免解释器把任意异常message直接打印到stderr。
        log_failure(logging.getLogger(__name__), error, event="field_inventory_cli_failed")
        raise SystemExit(1) from None
