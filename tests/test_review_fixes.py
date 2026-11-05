"""subagent 全量 review 发现项的回归测试(P0/P1修复+P2高价值项)。"""

import pathlib
import sys
import unittest
from dataclasses import dataclass
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from yantu.domain import condition_codec as codec
from yantu.domain.conditions import Cet6AtLeast, HasCet6, IsC9, RankingAtMost, UnstructuredCondition
from yantu.domain.eligibility import evaluate, open_state_as_of, window_state
from yantu.domain.models import (
    AndGroup,
    Condition,
    Opportunity,
    OrGroup,
    PriorityItem,
    ReliefClause,
    StudentProfile,
    Verdict,
    Window,
)
from yantu.domain.ranking import rank

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


class TestP0MissingSchoolNotFail(unittest.TestCase):
    """P0:学校信息缺失时,C9确定性放宽不得把整体判成明确不符合。"""

    def test_missing_c9_unknown(self):
        p = profile(school="", undergrad_c9=None)
        clause = ReliefClause(
            RankingAtMost(20.0), IsC9(), discretionary=False, label="C9确定性放宽"
        )
        r = evaluate(open_opp("a", AndGroup((clause,))), p)
        self.assertEqual(r.verdict, Verdict.UNKNOWN)

    def test_explicit_non_c9_relief_fails(self):
        p = profile(undergrad_c9=False)
        self.assertEqual(IsC9().evaluate(p), Verdict.FAIL)


class TestP1EmptyGroups(unittest.TestCase):
    def test_empty_and_rejected(self):
        with self.assertRaises(ValueError):
            AndGroup(())

    def test_empty_or_rejected(self):
        with self.assertRaises(ValueError):
            OrGroup(())

    def test_codec_rejects_empty_conditions(self):
        with self.assertRaises(ValueError):
            codec.build({"type": "and", "conditions": []})


class TestP1SingleBoundaryWindows(unittest.TestCase):
    def test_end_known_start_missing_closed(self):
        w = Window("入口", True, None, datetime(2026, 9, 1))
        self.assertEqual(window_state(w, AS_OF), "closed")

    def test_start_known_end_missing_not_yet(self):
        w = Window("入口", True, datetime(2026, 9, 15), None)
        self.assertEqual(window_state(w, AS_OF), "not_yet_open")

    def test_both_unknown_still_unknown(self):
        self.assertEqual(window_state(Window("入口"), AS_OF), "unknown")

    def test_boundaries_inclusive(self):
        w = Window("入口", True, datetime(2026, 9, 10), datetime(2026, 9, 20))
        self.assertEqual(window_state(w, datetime(2026, 9, 10)), "open")
        self.assertEqual(window_state(w, datetime(2026, 9, 20)), "open")

    def test_required_false_documented_m1_semantics(self):
        """M1 口径:非必需入口不参与聚合(Window docstring)。"""
        o = Opportunity(
            oid="a", school="X", department="Y", program="Z", batch="预推免",
            entry_year=2027, degree_type="专硕",
            windows=(
                Window("必需", True, datetime(2026, 9, 10), datetime(2026, 9, 20)),
                Window("非必需", False, datetime(2026, 9, 10), datetime(2026, 9, 11)),  # 已截止但不参与
            ),
        )
        self.assertEqual(open_state_as_of(o, AS_OF), "open")


class TestP1RegistryAlias(unittest.TestCase):
    def test_alias_registration_rejected(self):
        with self.assertRaises(ValueError):
            codec.register("c9_alias", IsC9)


class TestP1PrioritySerialization(unittest.TestCase):
    def test_spec_priority_roundtrip(self):
        item = codec.build_priority(
            {"type": "priority", "label": "六级者优先", "condition": {"type": "has_cet6"}}
        )
        again = codec.build_priority(codec.spec_priority(item))
        self.assertEqual(again, item)


class TestP2CodecHardening(unittest.TestCase):
    def test_missing_key_structured_valueerror(self):
        with self.assertRaises(ValueError):
            codec.build({"type": "and"})
        with self.assertRaises(ValueError):
            codec.build({"type": "relief", "base": {"type": "is_c9"}})

    def test_type_field_registration_rejected(self):
        """含保留字段名 type 的条件类无法注册(会破坏 spec 回环)。"""

        @dataclass
        class DegreeIs(Condition):
            type: str  # 故意与保留键同名
            label: str = "学位类型"

            def evaluate(self, p):
                return Verdict.PASS

        with self.assertRaises(ValueError):
            codec.register("degree_is", DegreeIs)


class TestP2NestedDetailsFlatten(unittest.TestCase):
    def test_nested_leaf_evidence_and_path(self):
        @dataclass
        class EvidencedEnglish(Condition):
            threshold: int = 425
            label: str = "CET-6(带证据)"
            evidence_refs: tuple = ("ev-eng-1",)

            def evaluate(self, p):
                if p.cet6 is None:
                    return Verdict.UNKNOWN
                return Verdict.PASS if p.cet6 >= self.threshold else Verdict.FAIL

        tree = AndGroup(
            (
                OrGroup((EvidencedEnglish(), Cet6AtLeast(425)), label="英语"),
                RankingAtMost(30.0),
            )
        )
        r = evaluate(open_opp("a", tree), profile(cet6=None))
        # 注:路径标签可能重复(如两个同名条款),展示层不得以label为唯一键
        by_label = {d.label: d for d in r.details}
        self.assertIn("英语/CET-6(带证据)", by_label)  # 路径前缀保留组合器语境
        self.assertEqual(by_label["英语/CET-6(带证据)"].evidence_refs, ("ev-eng-1",))
        self.assertEqual(r.verdict, Verdict.UNKNOWN)
        self.assertTrue(any("英语" in u for u in r.unknown_points))

    def test_relief_clause_single_row(self):
        clause = ReliefClause(
            RankingAtMost(20.0, label="排名前20%"),
            IsC9(),
            discretionary=True,
            label="科研突出可放宽",
        )
        r = evaluate(open_opp("a", AndGroup((clause,))), profile(undergrad_c9=True))
        labels = [d.label for d in r.details]
        self.assertEqual(labels, ["科研突出可放宽"])  # 不下钻base,避免误导展示
        self.assertEqual(r.verdict, Verdict.UNKNOWN)


class TestP2ReliefBaseUnknown(unittest.TestCase):
    def test_base_unknown_relief_pass_still_unknown(self):
        clause = ReliefClause(RankingAtMost(20.0), IsC9(), discretionary=False)
        p = profile(ranking_percent=None, undergrad_c9=True)
        self.assertEqual(clause.evaluate(p), Verdict.UNKNOWN)  # base未知,放宽不参与结论

    def test_base_unknown_relief_fail_still_unknown(self):
        clause = ReliefClause(RankingAtMost(20.0), IsC9(), discretionary=False)
        p = profile(ranking_percent=None, undergrad_c9=False)
        self.assertEqual(clause.evaluate(p), Verdict.UNKNOWN)


class TestP2RankExclusionReasons(unittest.TestCase):
    def test_open_unknown_excluded_with_reason(self):
        o = open_opp("a", AndGroup((RankingAtMost(30.0),)))
        o.windows = (Window("入口", True, datetime(2026, 9, 10), None),)  # 截止未知
        items = rank([o], profile(), AS_OF)
        self.assertEqual(items[0].excluded_reason, "开放状态未确认")  # REC-11路径


class TestP2HasCet6Semantics(unittest.TestCase):
    def test_failed_cet6_not_pass(self):
        self.assertEqual(HasCet6().evaluate(profile(cet6=380)), Verdict.FAIL)
        self.assertEqual(HasCet6().evaluate(profile(cet6=500)), Verdict.PASS)


if __name__ == "__main__":
    unittest.main()
