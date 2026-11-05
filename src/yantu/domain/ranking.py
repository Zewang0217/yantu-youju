"""排序:硬门槛过滤 + 可替换评分策略。

REC-13:硬门槛约束独立于算法——fail/closed/开放状态排除固定在 rank(),
任何 RankingStrategy 都不能让被排除项复活(T12)。评分策略按 OPS-07
以统一契约接入(08 评估协议的基线A/B/C即插即比)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .eligibility import evaluate, open_state_as_of
from .models import EligibilityResult, OpenState, Opportunity, StudentProfile, Verdict

_EXCLUDE_REASON = {
    OpenState.CLOSED: "已截止",
    OpenState.NOT_YET_OPEN: "未开始",
    OpenState.UNKNOWN: "开放状态未确认",
}


class RankingStrategy(Protocol):
    """评分策略契约:只做评分,不参与过滤(过滤由 rank() 固定执行)。"""

    def score(
        self, opportunity: Opportunity, eligibility: EligibilityResult, profile: StudentProfile
    ) -> float: ...


@dataclass
class BaselineA:
    """排序基线A(初版)。权重为工程假设,以《评估与持续改进设计》冻结金标校准。

    已知简化(记录于 HLD 决策文档):①REC-13 的"官方历史"因子推迟到 M2
    历史统计数据接入(ACQ-09)后再加;②方向/偏好采用子串匹配(如"软件工程"
    会命中一切含该串的专业名),作为基线假设接受,金标校准时重审。
    时间契约:as_of 按 naive datetime(建议北京时间),由调用方统一。
    """

    w_condition: float = 0.5
    w_direction: float = 0.3
    w_preference: float = 0.2

    def score(
        self, opportunity: Opportunity, eligibility: EligibilityResult, profile: StudentProfile
    ) -> float:
        return (
            self.w_condition * self._condition_score(eligibility)
            + self.w_direction * self._direction_score(opportunity, profile)
            + self.w_preference * self._preference_score(opportunity, profile)
        )

    @staticmethod
    def _condition_score(eligibility: EligibilityResult) -> float:
        return {Verdict.PASS: 1.0, Verdict.UNKNOWN: 0.5, Verdict.FAIL: 0.0}[eligibility.verdict]

    @staticmethod
    def _direction_score(opportunity: Opportunity, profile: StudentProfile) -> float:
        if not profile.target_directions:
            return 0.5  # 未表达方向:中性,不奖不罚
        return 1.0 if any(d in opportunity.program for d in profile.target_directions) else 0.0

    @staticmethod
    def _preference_score(opportunity: Opportunity, profile: StudentProfile) -> float:
        regions = profile.preferences.get("region")
        if not regions:
            return 0.5
        return 1.0 if any(r in opportunity.school for r in regions) else 0.0


@dataclass
class RankedItem:
    opportunity: Opportunity
    eligibility: EligibilityResult
    open_state: OpenState
    score: float
    excluded_reason: str = ""  # 非空表示被排除,不进入推荐正文

    @property
    def included(self) -> bool:
        return not self.excluded_reason


def rank(
    opportunities: list[Opportunity],
    profile: StudentProfile,
    as_of: datetime,
    strategy: RankingStrategy | None = None,
) -> list[RankedItem]:
    """正常推荐(REC-10):仅含已核实当前可申请机会。

    - 资格 fail → 排除(明确不符合淘汰,REC-07)
    - 开放 closed/not_yet_open/unknown → 排除并注明(开放未知走 REC-11 末尾提示,
      由调用方按未确认列表另行呈现)
    - 资格 unknown → 保留,与 pass 统一排序,前端标"待确认"
    """
    strategy = strategy if strategy is not None else BaselineA()

    items: list[RankedItem] = []
    for opp in opportunities:
        eligibility = evaluate(opp, profile)
        state = open_state_as_of(opp, as_of)
        item = RankedItem(
            opportunity=opp,
            eligibility=eligibility,
            open_state=state,
            score=0.0,
        )
        if eligibility.verdict == Verdict.FAIL:
            item.excluded_reason = "条件明确不符合"
        elif state != OpenState.OPEN:
            item.excluded_reason = _EXCLUDE_REASON[state]
        else:
            item.score = strategy.score(opp, eligibility, profile)
        items.append(item)

    included = sorted(
        (i for i in items if i.included), key=lambda i: i.score, reverse=True
    )
    excluded = [i for i in items if not i.included]
    return included + excluded  # 排除项附后,供解释与末尾提示使用
