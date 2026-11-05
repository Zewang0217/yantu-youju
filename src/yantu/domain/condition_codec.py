"""条件类型注册表与编解码(F1,领域模型§8"开放条件类型注册表"的代码实现)。

职责边界:
- 本模块负责 dict(spec) ↔ Condition 树 的双向转换;领域类自身不携带序列化方法
  (领域纯净性),07 数据契约的"规则版本"以 spec 形态落库。
- 新条件类型通过 register() 登记(开闭);未登记类型 build() 抛
  UnknownConditionTypeError,由解析侧决定降级为 UnstructuredCondition(INV-13),
  而不是在这里吞掉。
- 优先项(PriorityItem)不是资格条件,单独提供 build_priority。
"""

from __future__ import annotations

import dataclasses
from typing import Any

from .conditions import (
    Cet6AtLeast,
    HasCet6,
    IeltsAtLeast,
    IsC9,
    RankingAtMost,
    ResearchHighlight,
    UnstructuredCondition,
)
from .models import (
    AndGroup,
    Condition,
    OrGroup,
    PriorityItem,
    ReliefClause,
)

_AND = "and"
_OR = "or"
_RELIEF = "relief"
_UNSTRUCTURED = "unstructured"
_PRIORITY = "priority"


class UnknownConditionTypeError(ValueError):
    """spec 中的 type 未注册。调用方(解析侧)应降级为 UnstructuredCondition。"""

    def __init__(self, type_name: str):
        super().__init__(f"未注册的条件类型: {type_name!r}")
        self.type_name = type_name


_CONDITION_TYPES: dict[str, type[Condition]] = {}


def register(name: str, cls: type[Condition]) -> None:
    """登记条件类型(开闭扩展入口)。配置错误快速失败:
    保留字冲突、同名不同类、同类不同名(别名会导致回环改写 type,
    污染 07 规则版本 diff)均拒绝。"""
    if name in (_AND, _OR, _RELIEF, _UNSTRUCTURED, _PRIORITY):
        raise ValueError(f"类型名 {name!r} 为组合器保留字")
    if name in _CONDITION_TYPES and _CONDITION_TYPES[name] is not cls:
        raise ValueError(f"类型名 {name!r} 已登记给 {_CONDITION_TYPES[name].__name__}")
    for existing_name, existing_cls in _CONDITION_TYPES.items():
        if existing_cls is cls and existing_name != name:
            raise ValueError(
                f"类 {cls.__name__} 已登记为 {existing_name!r},拒绝别名 {name!r}"
            )
    if dataclasses.is_dataclass(cls) and any(
        f.name == "type" for f in dataclasses.fields(cls)
    ):
        raise ValueError(
            f"条件类 {cls.__name__} 含保留字段名 'type',会破坏 spec 回环;请改名"
        )
    _CONDITION_TYPES[name] = cls


def _register_builtin() -> None:
    for name, cls in {
        "ranking_at_most": RankingAtMost,
        "cet6_at_least": Cet6AtLeast,
        "ielts_at_least": IeltsAtLeast,
        "research_highlight": ResearchHighlight,
        "is_c9": IsC9,
        "has_cet6": HasCet6,
    }.items():
        register(name, cls)


_register_builtin()


def _require(spec: dict[str, Any], key: str, type_name: str) -> Any:
    """必填键缺失抛结构化数据错误(与'类型未注册'两类错误分开)。"""
    if key not in spec:
        raise ValueError(f"{type_name!r} 组合器缺少必填键 {key!r}: {spec}")
    return spec[key]


def build(spec: dict[str, Any]) -> Condition:
    """dict → Condition 树。组合器递归;叶子按注册表实例化,多余键交给类构造
    (未知参数由 dataclass 抛 TypeError,属于数据错误而非降级场景)。"""
    type_name = spec.get("type")
    if type_name == _AND:
        return AndGroup(
            tuple(build(c) for c in _require(spec, "conditions", _AND)),
            label=spec.get("label", "必要条件(全部满足)"),
        )
    if type_name == _OR:
        return OrGroup(
            tuple(build(c) for c in _require(spec, "conditions", _OR)),
            label=spec.get("label", "替代条件(满足其一)"),
        )
    if type_name == _RELIEF:
        return ReliefClause(
            base=build(_require(spec, "base", _RELIEF)),
            relief_condition=build(_require(spec, "relief", _RELIEF)),
            discretionary=spec.get("discretionary", True),
            label=spec.get("label", "放宽条款"),
        )
    if type_name == _UNSTRUCTURED:
        params = {k: v for k, v in spec.items() if k != "type"}
        return UnstructuredCondition(**params)
    cls = _CONDITION_TYPES.get(type_name)
    if cls is None:
        raise UnknownConditionTypeError(type_name)
    params = {k: v for k, v in spec.items() if k != "type"}
    return cls(**params)


def build_priority(spec: dict[str, Any]) -> PriorityItem:
    """dict → 优先项(T04:不参与资格判断,仅随结果提示)。"""
    if spec.get("type") != _PRIORITY:
        raise ValueError(f"非优先项 spec: {spec.get('type')!r}")
    return PriorityItem(
        label=spec.get("label", "优先项"),
        condition=build(_require(spec, "condition", _PRIORITY)),
    )


def spec_priority(item: PriorityItem) -> dict[str, Any]:
    """优先项 → dict(与 build_priority 互逆,07 契约落库用)。"""
    return {"type": _PRIORITY, "label": item.label, "condition": spec(item.condition)}


def spec(cond: Condition) -> dict[str, Any]:
    """Condition 树 → dict(可落库/版本化/diff,07 规则版本)。"""
    if isinstance(cond, AndGroup):
        return {
            "type": _AND,
            "conditions": [spec(c) for c in cond.conditions],
            "label": cond.label,
        }
    if isinstance(cond, OrGroup):
        return {
            "type": _OR,
            "conditions": [spec(c) for c in cond.conditions],
            "label": cond.label,
        }
    if isinstance(cond, ReliefClause):
        return {
            "type": _RELIEF,
            "base": spec(cond.base),
            "relief": spec(cond.relief_condition),
            "discretionary": cond.discretionary,
            "label": cond.label,
        }
    name = _reverse_lookup(type(cond))
    payload = {f.name: getattr(cond, f.name) for f in dataclasses.fields(cond)}
    return {"type": name, **payload}


def _reverse_lookup(cls: type[Condition]) -> str:
    for name, registered in _CONDITION_TYPES.items():
        if registered is cls:
            return name
    if cls is UnstructuredCondition:
        return _UNSTRUCTURED
    raise UnknownConditionTypeError(cls.__name__)
