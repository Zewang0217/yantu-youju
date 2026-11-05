"""判定内核单元测试:直接把 SRS v0.4 的验收语义写成可执行规格。

覆盖:SRS §4 OR替代示例、T01院系差异、T02资格不全、T03放宽裁量、
T04优先非门槛、T08多入口窗口、REC-10排序排除。
"""

import pathlib
import sys
import unittest
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from yantu.domain.conditions import (
    Cet6AtLeast,
    HasCet6,
    IeltsAtLeast,
    IsC9,
    RankingAtMost,
    ResearchHighlight,
)
from yantu.domain.eligibility import evaluate, open_state_as_of
from yantu.domain.models import (
    AndGroup,
    EligibilityResult,
    OpenState,
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


def profile_25pct(**kw) -> StudentProfile:
    """T01原型用户:排名前25%(8/120≈6.7%口径换算为示意值25)。"""
    base = dict(
        school="华东师大",
        major="软件工程",
        grad_year=2027,
        ranking_percent=25.0,
        cet6=500,
        ielts=None,
    )
    base.update(kw)
    return StudentProfile(**base)


def opp(oid: str, conditions, windows=None, priorities=()) -> Opportunity:
    return Opportunity(
        oid=oid,
        school="测试大学",
        department="软件学院",
        program="软件工程",
        batch="预推免",
        entry_year=2027,
        degree_type="专硕",
        windows=windows
        if windows is not None
        else (
            Window(
                entry_name="院系系统",
                start=datetime(2026, 9, 10),
                end=datetime(2026, 9, 18, 12, 0),
            ),
        ),
        conditions=conditions,
        priorities=priorities,
    )


class TestOrSubstitution(unittest.TestCase):
    """SRS §4:CET6不达标但IELTS满足同一OR要求,不得淘汰。"""

    def test_cet6_fail_ielts_pass(self):
        cond = OrGroup((Cet6AtLeast(425), IeltsAtLeast(6.5)), label="英语")
        p = profile_25pct(cet6=400, ielts=6.5)
        self.assertEqual(cond.evaluate(p), Verdict.PASS)

    def test_cet6_fail_ielts_unknown(self):
        cond = OrGroup((Cet6AtLeast(425), IeltsAtLeast(6.5)), label="英语")
        p = profile_25pct(cet6=400, ielts=None)
        self.assertEqual(cond.evaluate(p), Verdict.UNKNOWN)

    def test_all_fail(self):
        cond = OrGroup((Cet6AtLeast(425), IeltsAtLeast(6.5)), label="英语")
        p = profile_25pct(cet6=400, ielts=5.5)
        self.assertEqual(cond.evaluate(p), Verdict.FAIL)


class TestT01CollegeDifference(unittest.TestCase):
    def test_front20_fail_front30_pass(self):
        p = profile_25pct()
        strict = AndGroup((RankingAtMost(20.0, label="排名前20%"),))
        loose = AndGroup((RankingAtMost(30.0, label="排名前30%"),))
        self.assertEqual(evaluate(opp("a", strict), p).verdict, Verdict.FAIL)
        self.assertEqual(evaluate(opp("b", loose), p).verdict, Verdict.PASS)


class TestT02MissingEnglish(unittest.TestCase):
    def test_unknown_not_fail_not_zero(self):
        p = profile_25pct(cet6=None, ielts=None)
        conds = AndGroup(
            (RankingAtMost(30.0), OrGroup((Cet6AtLeast(425), IeltsAtLeast(6.5)), label="英语"))
        )
        r = evaluate(opp("a", conds), p)
        self.assertEqual(r.verdict, Verdict.UNKNOWN)
        self.assertIn("英语", "".join(r.unknown_points))


class TestT03Relief(unittest.TestCase):
    def _conditions(self, relief) -> AndGroup:
        base = RankingAtMost(20.0, label="排名前20%")
        clause = ReliefClause(base, relief, discretionary=True, label="科研突出可放宽(需院系认可)")
        return AndGroup((clause,))

    def test_discretionary_relief_unknown(self):
        p = profile_25pct(ranking_percent=25.0, research_highlight=True)
        r = evaluate(opp("a", self._conditions(ResearchHighlight())), p)
        self.assertEqual(r.verdict, Verdict.UNKNOWN)  # 待院系确认,不直接pass

    def test_no_relief_clause_fail(self):
        p = profile_25pct(ranking_percent=25.0)
        r = evaluate(opp("a", AndGroup((RankingAtMost(20.0),))), p)
        self.assertEqual(r.verdict, Verdict.FAIL)

    def test_relief_not_met_fail(self):
        p = profile_25pct(ranking_percent=25.0, research_highlight=False)
        r = evaluate(opp("a", self._conditions(ResearchHighlight())), p)
        self.assertEqual(r.verdict, Verdict.FAIL)

    def test_deterministic_relief_pass(self):
        p = profile_25pct(ranking_percent=25.0, undergrad_c9=True)
        base = RankingAtMost(20.0, label="排名前20%")
        clause = ReliefClause(base, IsC9(), discretionary=False, label="C9确定性放宽")
        r = evaluate(opp("a", AndGroup((clause,))), p)
        self.assertEqual(r.verdict, Verdict.PASS)


class TestT04PriorityNotGate(unittest.TestCase):
    def test_priority_does_not_affect_verdict(self):
        priorities = (PriorityItem("六级者优先", HasCet6()),)
        conds = AndGroup((RankingAtMost(30.0),))
        no_cet6 = profile_25pct(cet6=None)
        r = evaluate(opp("a", conds, priorities=priorities), no_cet6)
        self.assertEqual(r.verdict, Verdict.PASS)  # 无六级不因此fail或unknown


class TestT08MultiEntryWindows(unittest.TestCase):
    def _two_entries(self, end2=None, start2=None):
        return (
            Window("院系系统", True, datetime(2026, 9, 10), datetime(2026, 9, 18, 12, 0)),
            Window("校系统", True, start2 or datetime(2026, 9, 10), end2),
        )

    def test_one_closed_means_closed(self):
        o = opp("a", AndGroup((RankingAtMost(30.0),)), windows=self._two_entries(end2=datetime(2026, 9, 8)))
        self.assertEqual(open_state_as_of(o, AS_OF), OpenState.CLOSED)

    def test_unknown_deadline_means_unknown(self):
        o = opp("a", AndGroup((RankingAtMost(30.0),)), windows=self._two_entries(end2=None))
        self.assertEqual(open_state_as_of(o, AS_OF), OpenState.UNKNOWN)

    def test_all_open(self):
        o = opp("a", AndGroup((RankingAtMost(30.0),)), windows=self._two_entries(end2=datetime(2026, 9, 20)))
        self.assertEqual(open_state_as_of(o, AS_OF), OpenState.OPEN)

    def test_not_yet_open(self):
        o = opp(
            "a",
            AndGroup((RankingAtMost(30.0),)),
            windows=(Window("唯一入口", True, datetime(2026, 9, 15), datetime(2026, 9, 25)),),
        )
        self.assertEqual(open_state_as_of(o, AS_OF), OpenState.NOT_YET_OPEN)

    def test_no_windows_unknown(self):
        o = opp("a", AndGroup((RankingAtMost(30.0),)), windows=())
        self.assertEqual(open_state_as_of(o, AS_OF), OpenState.UNKNOWN)


class TestRankingRec10(unittest.TestCase):
    def test_fail_and_closed_excluded_unknown_kept(self):
        p = profile_25pct(cet6=None, preferences={"region": ["测试"]})
        pass_op = opp("pass", AndGroup((RankingAtMost(30.0),)))
        fail_op = opp("fail", AndGroup((RankingAtMost(20.0),)))
        unknown_op = opp("unknown", AndGroup((RankingAtMost(30.0), OrGroup((Cet6AtLeast(425), IeltsAtLeast(6.5)), label="英语"))))
        items = rank([fail_op, unknown_op, pass_op], p, AS_OF)

        included = [i for i in items if i.included]
        excluded = [i for i in items if not i.included]
        self.assertEqual([i.opportunity.oid for i in included], ["pass", "unknown"])
        self.assertEqual(
            {i.opportunity.oid: i.excluded_reason for i in excluded}["fail"], "条件明确不符合"
        )
        # unknown 保留但分数低于 pass
        self.assertLess(
            next(i.score for i in included if i.opportunity.oid == "unknown"),
            next(i.score for i in included if i.opportunity.oid == "pass"),
        )

    def test_closed_opportunity_excluded(self):
        p = profile_25pct()
        closed_op = opp(
            "closed",
            AndGroup((RankingAtMost(30.0),)),
            windows=(Window("唯一入口", True, datetime(2026, 9, 1), datetime(2026, 9, 5)),),
        )
        items = rank([closed_op], p, AS_OF)
        self.assertFalse(items[0].included)
        self.assertEqual(items[0].excluded_reason, "已截止")


class TestUnstructuredFallback(unittest.TestCase):
    """INV-13/ACQ-13:不可结构化条款降级为整体unknown+待确认点。"""

    def test_conditions_none_unknown(self):
        r = evaluate(opp("raw", None), profile_25pct())
        self.assertEqual(r.verdict, Verdict.UNKNOWN)
        self.assertTrue(any("未完成结构化" in u for u in r.unknown_points))


if __name__ == "__main__":
    unittest.main()
