"""资格判定引擎与开放状态判定。纯函数,无 I/O(全局规范)。

纯度契约:evaluate 会对同一棵条件树求值两次(明细逐条 + 整树聚合),
条件实现必须永远纯净无副作用;引入缓存前不可破坏此假设。
时间契约:调用方负责统一时区——本模块按 naive datetime 比较(建议统一为
北京时间),aware/naive 混用会抛 TypeError(SRS §2"判定时点与明确时区")。
"""

from __future__ import annotations

from datetime import datetime

from .conditions import UnstructuredCondition
from .models import (
    AndGroup,
    Condition,
    ConditionResult,
    EligibilityResult,
    OpenState,
    Opportunity,
    OrGroup,
    ReliefClause,
    StudentProfile,
    Verdict,
    Window,
)


def window_state(window: Window, as_of: datetime) -> OpenState:
    """单入口窗口状态。未知时间不以未知当开放(REC-10),但单边界已知时
    保守推导可确定的状态:as_of>end(已知)→closed;as_of<start(已知)→not_yet。
    """
    if window.end is not None and as_of > window.end:
        return OpenState.CLOSED
    if window.start is not None and as_of < window.start:
        return OpenState.NOT_YET_OPEN
    if window.start is None or window.end is None:
        return OpenState.UNKNOWN
    return OpenState.OPEN


def open_state_as_of(opportunity: Opportunity, as_of: datetime) -> OpenState:
    """机会整体开放状态:所有必需入口的窗口聚合。

    T08:多入口均必需时,一个入口已截止即整体 closed,不以另一入口开放冒充。
    聚合优先级:closed > unknown > not_yet_open > open。
    required=False 的入口不参与聚合(Window docstring 的 M1 口径)。
    """
    required = [w for w in opportunity.windows if w.required]
    if not required:
        return OpenState.UNKNOWN  # 无已结构化窗口=未知,不默认开放
    states = [window_state(w, as_of) for w in required]
    if OpenState.CLOSED in states:
        return OpenState.CLOSED
    if OpenState.UNKNOWN in states:
        return OpenState.UNKNOWN
    if OpenState.NOT_YET_OPEN in states:
        return OpenState.NOT_YET_OPEN
    return OpenState.OPEN


def _condition_group(opportunity: Opportunity) -> AndGroup:
    """条件组缺省为"未结构化"占位(Null Object,INV-13),引擎无 None 特判。"""
    if opportunity.conditions is None:
        return AndGroup((UnstructuredCondition(),), label="申请条件")
    return opportunity.conditions


def _iter_leaf_details(
    cond: Condition, profile: StudentProfile, prefix: str
):
    """递归展开明细到叶子条件,路径前缀保留组合器语境(如"英语/CET-6"),
    使嵌套叶子的证据引用与待确认点可达 ConditionResult(REC-14/16)。
    ReliefClause 不下钻:输出条款级单行(其 base 失败但放宽待认定时,
    下钻 base 的 fail 会误导展示)。
    """
    label = f"{prefix}/{cond.label}" if prefix else cond.label
    if isinstance(cond, (AndGroup, OrGroup)):
        for child in cond.conditions:
            yield from _iter_leaf_details(child, profile, label)
        return
    yield ConditionResult(
        label=label,
        verdict=cond.evaluate(profile),
        evidence_refs=getattr(cond, "evidence_refs", ()),
    )


def evaluate(opportunity: Opportunity, profile: StudentProfile) -> EligibilityResult:
    """逐项条件判定并聚合(SRS §4:替代/放宽先算,再聚合资格)。

    明细从根组的直接子项展开(根组自身标签不进路径前缀)。
    """
    result = EligibilityResult(oid=opportunity.oid, verdict=Verdict.UNKNOWN)

    group = _condition_group(opportunity)
    for child in group.conditions:
        result.details.extend(_iter_leaf_details(child, profile, prefix=""))

    result.verdict = group.evaluate(profile)
    return result
