"""F1-F5 抽象的行为规格:注册表/编解码回环、NullObject、Strategy、证据关联。"""

import sys
import pathlib
import unittest
from dataclasses import dataclass
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from yantu.domain import condition_codec as codec
from yantu.domain.conditions import (
    Cet6AtLeast,
    IeltsAtLeast,
    RankingAtMost,
    ResearchHighlight,
    UnstructuredCondition,
)
from yantu.domain.eligibility import evaluate
from yantu.domain.models import (
    AndGroup,
    Condition,
    ConditionResult,
    Opportunity,
    OrGroup,
    PriorityItem,
    ReliefClause,
    StudentProfile,
    Verdict,
    Window,
)
from yantu.domain.ranking import BaselineA, RankedItem, rank

AS_OF = datetime(2026, 9, 12, 12, 0)


def profile(**kw) -> StudentProfile:
    base = dict(school="华东师大", ranking_percent=25.0, cet6=500)
    base.update(kw)
    return StudentProfile(**base)


def open_opp(oid: str, conditions) -> Opportunity:
    return Opportunity(
        oid=oid,
        school="测试大学",
        department="软件学院",
        program="软件工程",
        batch="预推免",
        entry_year=2027,
        degree_type="专硕",
        windows=(Window("入口", True, datetime(2026, 9, 10), datetime(2026, 9, 20)),),
        conditions=conditions,
    )


class TestCodecRoundTrip(unittest.TestCase):
    def _tree(self) -> AndGroup:
        relief = ReliefClause(
            base=RankingAtMost(20.0, label="排名前20%"),
            relief_condition=ResearchHighlight(),
            discretionary=True,
            label="科研突出可放宽",
        )
        english = OrGroup((Cet6AtLeast(425), IeltsAtLeast(6.5)), label="英语")
        return AndGroup((relief, english))

    def test_spec_build_spec_roundtrip(self):
        tree = self._tree()
        self.assertEqual(codec.spec(codec.build(codec.spec(tree))), codec.spec(tree))

    def test_built_tree_evaluates_identically(self):
        tree = self._tree()
        rebuilt = codec.build(codec.spec(tree))
        p = profile(ranking_percent=25.0, research_highlight=True, ielts=None)
        self.assertEqual(rebuilt.evaluate(p), tree.evaluate(p))
        self.assertEqual(rebuilt.evaluate(p), Verdict.UNKNOWN)  # 裁量放宽→待院系确认

    def test_unstructured_roundtrip(self):
        cond = UnstructuredCondition(quote="成绩名列前茅者优先", located_at="第3节")
        rebuilt = codec.build(codec.spec(cond))
        self.assertEqual(rebuilt, cond)
        self.assertEqual(rebuilt.evaluate(profile()), Verdict.UNKNOWN)

    def test_priority_roundtrip(self):
        item = codec.build_priority(
            {"type": "priority", "label": "六级者优先", "condition": {"type": "has_cet6"}}
        )
        self.assertIsInstance(item, PriorityItem)
        self.assertEqual(item.condition.evaluate(profile(cet6=None)), Verdict.UNKNOWN)


class TestRegistryErrors(unittest.TestCase):
    def test_unknown_type_raises(self):
        with self.assertRaises(codec.UnknownConditionTypeError):
            codec.build({"type": "no_such_kind", "threshold": 1})

    def test_reserved_name_rejected(self):
        with self.assertRaises(ValueError):
            codec.register("and", RankingAtMost)

    def test_duplicate_conflict_rejected(self):
        with self.assertRaises(ValueError):
            codec.register("ranking_at_most", Cet6AtLeast)

    def test_register_new_type_open_closed(self):
        @dataclass
        class FakeNewPolicy(Condition):
            label: str = "新院校新条款"

            def evaluate(self, p):
                return Verdict.PASS

        codec.register("fake_new_policy_2026", FakeNewPolicy)
        try:
            cond = codec.build({"type": "fake_new_policy_2026"})
            self.assertEqual(cond.evaluate(profile()), Verdict.PASS)
            self.assertEqual(codec.spec(cond)["type"], "fake_new_policy_2026")
        finally:
            codec._CONDITION_TYPES.pop("fake_new_policy_2026", None)


class TestNullObject(unittest.TestCase):
    def test_unstructured_in_tree_keeps_quote(self):
        cond = AndGroup(
            (UnstructuredCondition(quote="名列前茅", located_at="附件2"), Cet6AtLeast(425))
        )
        p = profile(cet6=None)
        r = evaluate(open_opp("a", cond), p)
        self.assertEqual(r.verdict, Verdict.UNKNOWN)
        self.assertTrue(any("未完成结构化" in u for u in r.unknown_points))

    def test_none_conditions_uses_null_object(self):
        r = evaluate(open_opp("a", None), profile())
        self.assertEqual(r.verdict, Verdict.UNKNOWN)
        self.assertEqual(len(r.details), 1)


class TestStrategyReplaceable(unittest.TestCase):
    def test_custom_strategy_changes_order_not_gating(self):
        class PreferFailures:
            """恶意策略:给fail打高分,验证硬门槛独立于算法(T12/REC-13)。"""

            def score(self, opportunity, eligibility, profile):
                return {Verdict.FAIL: 1.0, Verdict.UNKNOWN: 0.5, Verdict.PASS: 0.0}[
                    eligibility.verdict
                ]

        p = profile()
        pass_a = open_opp("pass_a", AndGroup((RankingAtMost(30.0),)))
        pass_b = open_opp("pass_b", AndGroup((RankingAtMost(40.0),)))
        fail_c = open_opp("fail_c", AndGroup((RankingAtMost(10.0),)))
        items = rank([pass_a, fail_c, pass_b], p, AS_OF, strategy=PreferFailures())
        included = [i.opportunity.oid for i in items if i.included]
        excluded = {i.opportunity.oid: i.excluded_reason for i in items if not i.included}
        self.assertEqual(set(included), {"pass_a", "pass_b"})  # fail 仍被排除
        self.assertEqual(excluded["fail_c"], "条件明确不符合")

    def test_baseline_a_orders_by_score(self):
        p = profile(target_directions=("软件工程",), preferences={"region": ["测试"]})
        strong = open_opp("strong", AndGroup((RankingAtMost(30.0),)))
        unknown = open_opp(
            "unknown",
            AndGroup((RankingAtMost(30.0), OrGroup((Cet6AtLeast(425), IeltsAtLeast(6.5)), label="英语"))),
        )
        p_no_eng = profile(target_directions=("软件工程",), preferences={"region": ["测试"]}, cet6=None, ielts=None)
        items = rank([unknown, strong], p_no_eng, AS_OF)
        self.assertEqual(
            [i.opportunity.oid for i in items if i.included], ["strong", "unknown"]
        )

    def test_default_strategy_is_baseline_a(self):
        items = rank([open_opp("a", AndGroup((RankingAtMost(30.0),)))], profile(), AS_OF)
        self.assertTrue(items[0].included)
        self.assertGreater(items[0].score, 0.0)


class TestEvidenceRefs(unittest.TestCase):
    def test_evidence_refs_propagated_to_result(self):
        @dataclass
        class EvidencedRanking(Condition):
            threshold: float
            label: str = "排名(带证据)"
            evidence_refs: tuple = ("ev-rank-1",)

            def evaluate(self, p):
                if p.ranking_percent is None:
                    return Verdict.UNKNOWN
                return Verdict.PASS if p.ranking_percent <= self.threshold else Verdict.FAIL

        r = evaluate(open_opp("a", AndGroup((EvidencedRanking(30.0),))), profile())
        self.assertEqual(r.details[0].evidence_refs, ("ev-rank-1",))

    def test_builtin_without_refs_yields_empty(self):
        r = evaluate(open_opp("a", AndGroup((RankingAtMost(30.0),))), profile())
        self.assertEqual(r.details[0].evidence_refs, ())


if __name__ == "__main__":
    unittest.main()
