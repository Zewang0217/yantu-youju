"""常用具体条件(判定内核的可复用条件类型)。

对应领域模型 §8"条件类型注册表"的首批实现(F4:_ThresholdCondition 吸收
"字段缺失→UNKNOWN + 阈值比较"骨架);新院校新条件经 condition_codec.register
登记即可被解析侧使用,不影响引擎(开闭扩展)。
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from .models import Condition, StudentProfile, Verdict


@dataclass
class _ThresholdCondition(Condition):
    """阈值条件骨架:画像字段缺失→UNKNOWN(REC-04,不设默认值),否则按比较判定。"""

    threshold: float
    label: str = ""
    evidence_refs: tuple[str, ...] = field(default=(), kw_only=True)

    def evaluate(self, profile: StudentProfile) -> Verdict:
        value = self._value(profile)
        if value is None:
            return Verdict.UNKNOWN
        return Verdict.PASS if self._compare(value, self.threshold) else Verdict.FAIL

    @abstractmethod
    def _value(self, profile: StudentProfile) -> Optional[float]: ...

    def _compare(self, value: float, threshold: float) -> bool:
        return value >= threshold


@dataclass
class RankingAtMost(_ThresholdCondition):
    """排名百分位门槛:profile.ranking_percent ≤ threshold 视为满足。"""

    label: str = "排名要求"

    def _value(self, profile: StudentProfile) -> Optional[float]:
        return profile.ranking_percent

    def _compare(self, value: float, threshold: float) -> bool:
        return value <= threshold


@dataclass
class Cet6AtLeast(_ThresholdCondition):
    threshold: float = 425
    label: str = "CET-6"

    def _value(self, profile: StudentProfile) -> Optional[float]:
        return profile.cet6


@dataclass
class IeltsAtLeast(_ThresholdCondition):
    threshold: float = 6.5
    label: str = "IELTS"

    def _value(self, profile: StudentProfile) -> Optional[float]:
        return profile.ielts


@dataclass
class ResearchHighlight(Condition):
    """科研突出(作为放宽条款的触发条件时使用)。"""

    label: str = "科研突出"
    evidence_refs: tuple[str, ...] = field(default=(), kw_only=True)

    def evaluate(self, profile: StudentProfile) -> Verdict:
        if profile.research_highlight is None:
            return Verdict.UNKNOWN  # 未判定/材料不足 → 未知,不解释为院系认可(REC-05)
        return Verdict.PASS if profile.research_highlight else Verdict.FAIL


@dataclass
class IsC9(Condition):
    """确定性放宽触发:本科为C9。未提供(None)→unknown,不默认非C9(P0修复)。"""

    label: str = "C9高校"
    evidence_refs: tuple[str, ...] = field(default=(), kw_only=True)

    def evaluate(self, profile: StudentProfile) -> Verdict:
        if profile.undergrad_c9 is None:
            return Verdict.UNKNOWN
        return Verdict.PASS if profile.undergrad_c9 else Verdict.FAIL


@dataclass
class HasCet6(Condition):
    """优先项触发:"已通过CET-6"(≥425)。"者优先"条款按通过口径解释;
    仅"持有成绩单"不算通过。"""

    label: str = "已通过CET-6"
    threshold: int = 425
    evidence_refs: tuple[str, ...] = field(default=(), kw_only=True)

    def evaluate(self, profile: StudentProfile) -> Verdict:
        if profile.cet6 is None:
            return Verdict.UNKNOWN
        return Verdict.PASS if profile.cet6 >= self.threshold else Verdict.FAIL


@dataclass
class UnstructuredCondition(Condition):
    """未结构化条款(Null Object,INV-13降级路径的一等公民)。

    恒 UNKNOWN,不参与pass/fail;携带原文片段与定位,供诊断/详情展示与
    人工复核。解析侧遇到未知条件类型或不可读载体时生成此类,而不是丢弃条款。
    """

    label: str = "条款未完成结构化"
    evidence_refs: tuple[str, ...] = field(default=(), kw_only=True)
    quote: str = ""  # 原文片段
    located_at: str = ""  # 定位(页/段/表格)
    reason: str = ""  # 未结构化原因(未知类型/载体不可读/冲突待裁)

    def evaluate(self, profile: StudentProfile) -> Verdict:
        return Verdict.UNKNOWN
