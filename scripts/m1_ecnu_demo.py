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

from yantu.acquisition.harvest import HarvestService  # noqa: E402
from yantu.acquisition.store import (  # noqa: E402
    EntryBook,
    OpportunityRepository,
    SnapshotStore,
    record_to_opportunity,
)
from yantu.acquisition.structurer import extract_window_candidates  # noqa: E402
from yantu.domain.eligibility import evaluate  # noqa: E402
from yantu.domain.models import Opportunity, StudentProfile  # noqa: E402
from yantu.domain.ranking import rank  # noqa: E402

SEED = ROOT / "data" / "pilot" / "ecnu_2027_seed.json"
VAR = ROOT / "var"  # 运行时产物(快照/入口库/机会库),不入库

LIVE_URLS = [
    ("软院预报名", "https://sei.ecnu.edu.cn/c4/0d/c33171a771085/page.htm"),
    ("软院考核细则", "https://sei.ecnu.edu.cn/d7/c3/c33171a776131/page.htm"),
    ("CS考核细则", "https://cs.ecnu.edu.cn/d7/bf/c33066a776127/page.htm"),
]


def section(title: str) -> None:
    print(f"\n{'=' * 12} {title} {'=' * 12}")


def phase1_live_fetch() -> None:
    """R3 门面:一次调用完成 抓取→解析→快照落盘→入口库登记;失败降级。"""
    section("① 真实抓取/快照落盘/入口库(HarvestService)")
    svc = HarvestService(
        SnapshotStore(VAR / "snapshots"), EntryBook(VAR / "entries.json")
    )
    parsed_texts: dict[str, str] = {}
    for name, url in LIVE_URLS:
        outcome = svc.collect(url, name=name, school="华东师范大学")
        if outcome.ok:
            r = outcome.parse_result
            print(f"[OK] {name}: {len(outcome.snapshot.body)}B sha256={outcome.sha256[:12]}… "
                  f"解析={r.status.value} 已落盘")
            print(f"     标题: {r.title[:52]}")
            parsed_texts[name] = r.text
        else:
            print(f"[降级] {name}: {outcome.error}")
    if parsed_texts:
        _phase1b_auto_extract(parsed_texts)
        _phase1c_llm_structuring(parsed_texts)
    else:
        print("(全部抓取失败:本次仅演示种子数据判定,证据哈希采用种子文件内2026-11-05记录)")


def _phase1b_auto_extract(parsed_texts: dict[str, str]) -> None:
    """确定性窗口候选提取,与种子数据交叉验证(基线自检)。"""
    section("①b 自动截止提取(确定性基线)与种子交叉验证")
    candidates = extract_window_candidates(parsed_texts.get("软院考核细则", ""))
    for c in candidates:
        print(f"  [{c.confidence}/{c.kind}] {c.date} ← “{c.quote[:34]}…”")
    if not candidates:
        print("  (无句式命中的截止/开放候选——保守降级为不提取)")
    seed_end = datetime.fromisoformat("2026-09-07T12:00:00")
    exact_ends = [c.date for c in candidates if c.confidence == "exact" and c.kind == "deadline"]
    if exact_ends:
        verdict = "一致 ✓" if max(exact_ends) == seed_end else "不一致 ✗(以种子为准,启发式需复核)"
        print(f"  交叉验证: 提取截止 max={max(exact_ends)} vs 种子={seed_end} → {verdict}")


def _phase1c_llm_structuring(parsed_texts: dict[str, str]) -> None:
    section("①c LLM条款结构化(发布门禁:可构建+quote逐字回溯)")
    from yantu.llm import load_client

    client = load_client(ROOT / "config" / "llm.json")
    if client is None:
        print("  未检测到有效 config/llm.json → 跳过LLM结构化(确定性基线不受影响)")
        print("  配置: 复制 config/llm.example.json 为 config/llm.json,填 api_key(该文件不入库)")
        return
    from yantu.acquisition import StructuringAgent

    r = StructuringAgent(client=client).structure(parsed_texts.get("软院考核细则", ""))
    print(f"  状态={r.status} 尝试={r.attempts} 提示词={r.prompt_version}")
    for c in r.gate_report:
        print(f"  [{'✓' if c.passed else '✗'}] {c.name}: {c.detail or '通过'}")
    if r.conditions_spec:
        s = json.dumps(r.conditions_spec, ensure_ascii=False)
        print(f"  条件树: {s[:200]}…")
    for n in r.notes:
        print(f"  注: {n}")


def load_opportunities() -> list[Opportunity]:
    """种子 → 机会库落盘 → 重载使用(验证 spec 往返,而非直接用内存对象)。"""
    data = json.loads(SEED.read_text(encoding="utf-8"))
    ops = [record_to_opportunity(o) for o in data["opportunities"]]
    repo = OpportunityRepository(VAR / "opportunities.json")
    repo.save(ops)
    reloaded = repo.load()
    assert [o.oid for o in reloaded] == [o.oid for o in ops]
    return reloaded


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
    print("\n(运行产物在 var/(不入库);条款LLM自动结构化与复核门禁属后续采集Agent任务)")


if __name__ == "__main__":
    main()
