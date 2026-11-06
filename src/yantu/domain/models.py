"""领域对象:三值判定语义与机会/画像/条件模型。

语义依据 SRS v0.4 §4:
- 资格判断 pass/fail/unknown;仅"不可被替代/放宽的必要条件明确失败"→fail,
  无此失败但有影响资格的未知→unknown,unknown 不伪装 pass。
- 开放状态 open/not_yet_open/closed/unknown,不从"网页还在"推导 open。
- 优先项不是门槛、不参与资格判断(SRS §2、T04)。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Verdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"  # 待确认/待补充/待院系认定


class OpenState(str, Enum):
    OPEN = "open"
    NOT_YET_OPEN = "not_yet_open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


@dataclass
class StudentProfile:
    """学生画像快照。None 一律表示"未提供"(缺失),与明确"没有"区分。"""

    school: str = ""
    major: str = ""
    grad_year: Optional[int] = None
    # 排名:百分位(0~100,越小越靠前)。附口径说明文本;None=未提供
    ranking_percent: Optional[float] = None
    ranking_basis: str = ""  # 如"前6学期/总人数口径"
    gpa: Optional[float] = None
    gpa_scale: Optional[float] = None  # 量程,如 4.0
    # 英语:None=未提供;0 不作为缺省值(不自动设0, REC-04)
    cet6: Optional[int] = None
    ielts: Optional[float] = None
    toefl: Optional[int] = None
    # 科研:None=未判定/未提供;False=明确无;True=有(突出判定另行由标记给出)
    research_highlight: Optional[bool] = None
    # 本科是否C9(用于确定性放宽条款):None=未提供(判断为unknown,不默认非C9)
    undergrad_c9: Optional[bool] = None
    target_directions: tuple[str, ...] = ()
    preferences: dict = field(default_factory=dict)  # 如 {"region": ["上海", "杭州"]}


class Condition(ABC):
    """一条申请条件。evaluate 只做领域判断,不做 I/O。

    evidence_refs 默认空;需要逐条举证的条件子类将其声明为自身字段
    (ACQ-05:每项结论关联支持证据)。
    """

    evidence_refs: tuple[str, ...] = ()

    @abstractmethod
    def evaluate(self, profile: StudentProfile) -> Verdict: ...


@dataclass
class AndGroup(Condition):
    """全部必要条件的合取。

    聚合规则(SRS §4):任一明确 fail → fail;无 fail 但有 unknown → unknown;
    否则 pass。fail 仅当"不可替代/放宽的必要条件"明确失败——替代由 OrGroup
    表达,放宽由 ReliefClause 表达。空组是数据错误(结构化为空≠放行),构造即拒。
    """

    conditions: tuple[Condition, ...]
    label: str = "必要条件(全部满足)"
    evidence_refs: tuple[str, ...] = field(default=(), kw_only=True)

    def __post_init__(self) -> None:
        if not self.conditions:
            raise ValueError("AndGroup.conditions 不能为空;无条件数据应使用 UnstructuredCondition")

    def evaluate(self, profile: StudentProfile) -> Verdict:
        verdicts = [c.evaluate(profile) for c in self.conditions]
        if Verdict.FAIL in verdicts:
            return Verdict.FAIL
        if Verdict.UNKNOWN in verdicts:
            return Verdict.UNKNOWN
        return Verdict.PASS


@dataclass
class OrGroup(Condition):
    """替代关系:满足任一即通过(如 CET-6 与 IELTS 互替)。

    任一 pass → pass;全部明确 fail → fail;其余(有 unknown 兜底可能)→ unknown。
    空组是数据错误,构造即拒(避免空真语义静默放行/误杀)。
    """

    conditions: tuple[Condition, ...]
    label: str = "替代条件(满足其一)"
    evidence_refs: tuple[str, ...] = field(default=(), kw_only=True)

    def __post_init__(self) -> None:
        if not self.conditions:
            raise ValueError("OrGroup.conditions 不能为空")

    def evaluate(self, profile: StudentProfile) -> Verdict:
        verdicts = [c.evaluate(profile) for c in self.conditions]
        if Verdict.PASS in verdicts:
            return Verdict.PASS
        if Verdict.UNKNOWN in verdicts:
            return Verdict.UNKNOWN
        return Verdict.FAIL


@dataclass
class ReliefClause(Condition):
    """放宽条款:base 不达标时的替代达标路径。

    discretionary=True(需院系认定,如"科研突出者可放宽") → 满足放宽条件时
    判 unknown(待院系确认),不得直接 pass(T03);
    discretionary=False(确定性放宽,如"C9高校可放宽") → 满足即 pass。
    """

    base: Condition
    relief_condition: Condition
    discretionary: bool = True
    label: str = "放宽条款"
    evidence_refs: tuple[str, ...] = field(default=(), kw_only=True)

    def evaluate(self, profile: StudentProfile) -> Verdict:
        base_verdict = self.base.evaluate(profile)
        if base_verdict == Verdict.PASS:
            return Verdict.PASS
        relief_verdict = self.relief_condition.evaluate(profile)
        if base_verdict == Verdict.FAIL:
            if relief_verdict == Verdict.PASS:
                return Verdict.UNKNOWN if self.discretionary else Verdict.PASS
            if relief_verdict == Verdict.UNKNOWN:
                return Verdict.UNKNOWN
            return Verdict.FAIL
        # base unknown:仍可能直接通过,放宽不参与结论
        return Verdict.UNKNOWN


@dataclass
class PriorityItem:
    """优先项(如"六级者优先"):仅随结果提示,不参与资格判断(T04)。"""

    label: str
    condition: Condition

    def evaluate(self, profile: StudentProfile) -> Verdict:
        return self.condition.evaluate(profile)


@dataclass
class Window:
    """一个报名入口的窗口。start/end 为 None 表示未知(不以未知当开放)。

    required 语义(M1 口径):True=必需入口,参与整体开放状态聚合(T08);
    False=非必需入口,M1 **不参与**聚合、也不支持"多入口二选一"的替代语义——
    替代入口(报名系统二选一)推迟到 M2 按 07 契约显式建模(窗口组 OR),届时
    本字段语义升级并配测试。
    """

    entry_name: str
    required: bool = True
    start: Optional[datetime] = None
    end: Optional[datetime] = None


@dataclass
class Evidence:
    """证据。eid 为库内主键,Condition.evidence_refs 引用它(F5 证据链闭合)。"""

    eid: str = ""
    source_name: str = ""
    url: str = ""
    quote: str = ""  # 原文片段
    located_at: str = ""  # 定位(页/段/表格)
    version_time: Optional[datetime] = None  # 文档版本时间
    verified_at: Optional[datetime] = None  # 最近成功复核时间
    snapshot_sha256: str = ""
    text_sha256: str = ""
    start_offset: Optional[int] = None
    end_offset: Optional[int] = None


@dataclass
class Opportunity:
    """招生机会:可单独判断条件与报名窗口的申请对象(SRS §2 已采纳口径)。"""

    oid: str
    school: str
    department: str  # 培养单位(可与报名单位不同)
    program: str  # 专业/方向
    batch: str  # 批次(夏令营/预推免/…)
    entry_year: int  # 入学年份(规范字段;原文表述留在证据里)
    degree_type: str  # 学硕/专硕/直博/…
    windows: tuple[Window, ...] = ()
    conditions: Optional[AndGroup] = None  # None=条件未结构化
    priorities: tuple[PriorityItem, ...] = ()
    evidence: tuple[Evidence, ...] = ()


@dataclass
class ConditionResult:
    label: str
    verdict: Verdict
    note: str = ""
    evidence_refs: tuple[str, ...] = ()  # 支持该条结论的证据引用(ACQ-05)


@dataclass
class EligibilityResult:
    oid: str
    verdict: Verdict
    details: list[ConditionResult] = field(default_factory=list)

    @property
    def unknown_points(self) -> list[str]:
        """待确认点从明细派生,单一真相(DRY)。"""
        return [d.label for d in self.details if d.verdict == Verdict.UNKNOWN]

    @property
    def human(self) -> str:
        return {
            Verdict.PASS: "条件符合",
            Verdict.FAIL: "明确不符合",
            Verdict.UNKNOWN: "待确认(条件无明确不符)",
        }[self.verdict]
