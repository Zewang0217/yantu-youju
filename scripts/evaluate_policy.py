"""隔离回放政策发布回归；不加载密钥或调用真实模型。"""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime
from uuid import uuid4

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from yantu.evaluation.policy_regression import run_regression


def main() -> int:
    """输出MD/JSON报告；任一回归失败或执行失败则以非零退出。"""
    output = ROOT / "var" / "policy_eval" / f"{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:8]}"
    report = run_regression(ROOT / "data/pilot/trusted_policy/manifest.json", output)
    print(json.dumps({"planned": report["planned"], "counts": report["counts"], "realModelCalls": 0,
                      "report": str(output / "report.md")}, ensure_ascii=False))
    return 0 if report["counts"].get("regression_pass", 0) == report["planned"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
