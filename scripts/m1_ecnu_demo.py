"""M1 端到端演示:华东师大真实通知 → 快照/解析 → 种子数据 → 判定/排序/证据。

运行: python scripts/m1_ecnu_demo.py
- 真实抓取失败时自动降级(只跑种子判定),如实标注,不假装抓取成功。
- 条款来自人工种子数据(data/pilot/ecnu_2027_seed.json),非自动结构化。
"""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from yantu.acquisition import HtmlNoticeParser, fetch  # noqa: E402
from yantu.domain import condition_codec as codec  # noqa: E402
from yantu.domain.eligibility import evaluate  # noqa: E402
from yantu.domain.models import Evidence, Opportunity, StudentProfile, Window  # noqa: E402
from yantu.domain.ranking import rank  # noqa: E402

SEED = ROOT / "data" / "pilot" / "ecnu_2027_seed.json"

LIVE_URLS = [
    ("软院预报名", "https://sei.ecnu.edu.cn/c4/0d/c33171a771085/page.htm"),
    ("软院考核细则", "https://sei.ecnu.edu.cn/d7/c3/c33171a776131/page.htm"),
    ("CS考核细则", "https://cs.ecnu.edu.cn/d7/bf/c33066a776127/page.htm"),
]


def section(title: str) -> None:
    print(f"\n{'=' * 12} {title} {'=' * 12}")


def phase1_live_fetch() -> dict[str, str]:
    """真实抓取,返回 url->sha 摘要;失败降级。"""
    section("① 真实抓取与快照")
    shas: dict[str, str] = {}
    for name, url in LIVE_URLS:
        try:
            snap = fetch(url)
            r = HtmlNoticeParser().parse(snap)
            print(f"[OK] {name}: HTTP {snap.status_code} {len(snap.body)}B "
                  f"sha256={snap.sha256[:12]}… 解析={r.status.value}")
            print(f"     标题: {r.title[:52]}")
            shas[url] = snap.sha256[:12]
        except Exception as e:  # FetchError等:降级,不中断演示
            print(f"[降级] {name}: {type(e).__name__}: {e}")
    if not shas:
        print("(全部抓取失败:本次仅演示种子数据判定,证据哈希采用种子文件内2026-11-05记录)")
    return shas


def load_opportunities() -> list[Opportunity]:
    data = json.loads(SEED.read_text(encoding="utf-8"))
    out: list[Opportunity] = []
    for o in data["opportunities"]:
        windows = tuple(
            Window(
                entry_name=w["entry_name"],
                required=w.get("required", True),
                start=datetime.fromisoformat(w["start"]) if w.get("start") else None,
                end=datetime.fromisoformat(w["end"]) if w.get("end") else None,
            )
            for w in o["windows"]
        )
        evidence = tuple(Evidence(**e) for e in o.get("evidence", []))
        out.append(
            Opportunity(
                oid=o["oid"],
                school=o["school"],
                department=o["department"],
                program=o["program"],
                batch=o["batch"],
                entry_year=o["entry_year"],
                degree_type=o["degree_type"],
                windows=windows,
                conditions=codec.build(o["conditions_spec"]),
                priorities=tuple(codec.build_priority(p) for p in o.get("priorities_spec", [])),
                evidence=evidence,
            )
        )
    return out


def phase2_judge(ops: list[Opportunity]) -> None:
    section("② 画像判定(两位示意学生)")
    students = {
        "学生A(8/120≈前6.7%,CET6=500)": StudentProfile(
            school="某双一流", major="软件工程", grad_year=2027,
            ranking_percent=6.7, ranking_basis="前6学期/专业总人数",
            cet6=500, target_directions=("软件工程",),
        ),
        "学生B(前25%,CET6=400)": StudentProfile(
            school="某双一流", major="软件工程", grad_year=2027,
            ranking_percent=25.0, cet6=400, target_directions=("计算机",),
        ),
    }
    for name, p in students.items():
        print(f"\n-- {name} --")
        for op in ops:
            r = evaluate(op, p)
            print(f"  {op.oid}: {r.verdict.value}({r.human})")
            for d in r.details:
                mark = {"pass": "✓", "fail": "✗", "unknown": "⚠"}[d.verdict.value]
                print(f"    {mark} {d.label}")
    return None


def phase3_rank(ops: list[Opportunity]) -> None:
    section("③ 排序与排除(两个判定时点)")
    p = StudentProfile(
        school="某双一流", major="软件工程", grad_year=2027,
        ranking_percent=6.7, cet6=500,
        target_directions=("软件工程",), preferences={"region": ["华东师范"]},
    )
    for as_of in (datetime(2026, 8, 15, 12, 0), datetime(2026, 11, 5, 12, 0)):
        print(f"\n-- as_of={as_of.date()} --")
        items = rank(ops, p, as_of)
        for i in items:
            tag = "推荐" if i.included else f"排除:{i.excluded_reason}"
            print(f"  [{tag}] {i.opportunity.oid} score={i.score:.2f}")
        included = [i for i in items if i.included]
        if not included:
            print("  → 正文0条(REC-15):存在条件符合但窗口未知/已过的机会,走末尾提示,不造推荐")


def phase4_evidence(ops: list[Opportunity]) -> None:
    section("④ 证据链(判定可回溯到原文)")
    for op in ops:
        print(f"\n-- {op.oid} --")
        for e in op.evidence:
            print(f"  {e.eid}: {e.source_name}")
            print(f"    {e.url}")
            print(f"    “{e.quote[:58]}…” @ {e.located_at} (版本{e.version_time})")


def main() -> None:
    print("研途有据 M1 端到端演示 — 华东师大软院真实通知(2026-11-05)")
    phase1_live_fetch()
    ops = load_opportunities()
    phase2_judge(ops)
    phase3_rank(ops)
    phase4_evidence(ops)
    print("\n(种子数据为人工结构化;条款自动结构化与复核门禁属后续采集Agent任务)")


if __name__ == "__main__":
    main()
