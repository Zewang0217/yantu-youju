"""对已定位的申请条件做保守核验；无法证明的结构不作为确定事实发布。

仅支持少量明确的数值句式。任意自然语言的蕴含、复杂放宽和跨通知冲突
仍需独立语义核验；本模块不会以引文存在替代这些判断。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .source_evidence import SourceClaim


class PolicyValidationError(ValueError):
    """草案缺证据、与原文矛盾或超出当前确定性核验能力。"""


def validate_tree_budget(tree: dict) -> None:
    """约束外部条件树的深度与规模，防止不合法模型输出耗尽递归资源。"""
    pending = [(tree, 0)]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if depth > 16 or count > 256:
            raise PolicyValidationError("条件树超过深度或节点预算")
        if not isinstance(node, dict):
            raise PolicyValidationError("条件节点必须是对象")
        kind = node.get("type")
        if not isinstance(kind, str):
            raise PolicyValidationError("条件type必须是字符串")
        if kind in ("and", "or"):
            children = node.get("conditions")
            if not isinstance(children, list):
                raise PolicyValidationError("组合器conditions必须是数组")
            pending.extend((child, depth + 1) for child in children)
        elif kind == "relief":
            if not isinstance(node.get("discretionary", True), bool):
                raise PolicyValidationError("放宽裁量标记必须是布尔值")
            pending.extend((node.get(key), depth + 1) for key in ("base", "relief"))


def locate_quote(text: str, quote: str) -> tuple[int, int]:
    """允许排版空白差异，返回实际原文区间；缺失或多次命中时拒绝。"""
    if not isinstance(quote, str) or not quote.strip():
        raise PolicyValidationError("每项条件必须提供非空 quote")
    positions = [i for i, ch in enumerate(text) if not ch.isspace()]
    compact = "".join(text[i] for i in positions)
    needle = re.sub(r"\s+", "", quote)
    start = compact.find(needle)
    if start < 0:
        raise PolicyValidationError("quote 未在原文找到")
    if compact.find(needle, start + 1) >= 0:
        raise PolicyValidationError("quote 多次出现，需扩大引文以唯一定位")
    return positions[start], positions[start + len(needle) - 1] + 1


_NUMERIC_PATTERNS = {
    "ranking_at_most": re.compile(r"(?:本科)?(?:前[一二三四五六七八\d]+学期)?(?:成绩)?排名(?:为)?前(\d+(?:\.\d+)?)%"),
    "cet6_at_least": re.compile(r"(?:全国大学英语六级(?:考试)?(?:成绩)?|CET-?6(?:成绩)?)(\d+(?:\.\d+)?)(?:分)?(?:及以上|以上)"),
    "ielts_at_least": re.compile(r"(?:雅思|IELTS)(?:成绩)?(\d+(?:\.\d+)?)(?:分)?(?:及以上|以上)"),
}
SUPPORTED_NUMERIC_TYPES = tuple(_NUMERIC_PATTERNS)


def verify_numeric(node: dict, quote: str) -> None:
    """核对数值、比较方向及限定语；不支持的句式要求保留未知。"""
    kind = node.get("type")
    if kind == "unstructured":
        return
    pattern = _NUMERIC_PATTERNS.get(kind)
    if pattern is None:
        raise PolicyValidationError(f"{kind} 尚无独立确定性核验器，请保留未结构化条款")
    value = node.get("threshold")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PolicyValidationError("数值条件必须给出有限的显式 threshold")
    compact = re.sub(r"\s+", "", quote)
    if re.search(r"原则上|一般|优先|放宽|无需|不要求|不得|不超过|不高于|低于|免除", compact):
        raise PolicyValidationError("引文含裁量、优先或相反方向，不能直接作为数值硬门槛")
    match = pattern.fullmatch(compact.strip("，,。；;：:"))
    if not match or float(match.group(1)) != value:
        raise PolicyValidationError("threshold/比较方向未获完整引文支持，需保留未知或独立复核")
    if kind == "ranking_at_most" and not 0 <= value <= 100:
        raise PolicyValidationError("排名百分比超出0至100")


def bind_claims(tree: dict, text: str, path: str = "conditions") -> tuple[dict, list[SourceClaim]]:
    """校验树形证据并保留未结构化原文；返回 codec 可消费的草案与区间。

    模型自带的 evidence_refs 不可信，引用由入库边界重新生成。
    OR/放宽组合必须提供覆盖子项的原文；普通 AND 关系由后续完整性核验约束。
    """
    if not isinstance(tree, dict):
        raise PolicyValidationError("条件节点必须是对象")
    node = dict(tree)
    node.pop("evidence_refs", None)
    kind = node.get("type")
    quote = node.pop("quote", None)
    claims: list[SourceClaim] = []
    if kind in ("and", "or", "relief"):
        if quote:
            start, end = locate_quote(text, quote)
            claims.append(SourceClaim(path, text[start:end], start, end))
        if kind in ("or", "relief") and not quote:
            raise PolicyValidationError("OR/放宽组合必须提供关系原文 quote")
        if kind in ("and", "or"):
            children = node.get("conditions")
            if not isinstance(children, list) or not children:
                raise PolicyValidationError("组合条件不能为空")
            built = [bind_claims(child, text, f"{path}.conditions[{i}]") for i, child in enumerate(children)]
            node["conditions"] = [child for child, _ in built]
            claims.extend(claim for _, child_claims in built for claim in child_claims)
        else:
            for key in ("base", "relief"):
                node[key], child_claims = bind_claims(node.get(key), text, f"{path}.{key}")
                claims.extend(child_claims)
    else:
        start, end = locate_quote(text, quote)
        actual_quote = text[start:end]
        verify_numeric(node, actual_quote)
        if kind == "unstructured":
            node["quote"] = actual_quote
        claims.append(SourceClaim(path, actual_quote, start, end))
    return node, claims


@dataclass(frozen=True)
class PolicySection:
    """完整解析文本中已知编号段落的边界；未找到时禁止假定整页都是申请门槛。"""

    text: str
    start: int
    end: int
    clauses: tuple[str, ...]
    preamble: str


def application_section(text: str) -> PolicySection:
    """定位中文一级标题的报名/申请条件段落，保守支持明确的编号条目。"""
    header = re.search(r"[一二三四五六七八九十]+、(?:报名条件|申请条件)", text)
    if not header:
        raise PolicyValidationError("未定位明确的申请条件章节")
    next_header = re.search(r"[一二三四五六七八九十]+、", text[header.end():])
    end = header.end() + next_header.start() if next_header else len(text)
    section = text[header.end():end]
    parts = re.split(r"(?:^|\s)\d+[.．、]\s*", section)
    if len(parts) < 2:
        raise PolicyValidationError("申请条件未按当前支持的编号条目组织")
    preamble = parts[0].strip().strip("；;。 ")
    clauses = tuple(part.strip().strip("；;。 ") for part in parts if part.strip().strip("；;。 "))
    return PolicySection(section, header.end(), end, clauses, preamble)


def verify_application_tree(tree: dict, claims: list[SourceClaim], text: str) -> None:
    """验证申请阶段、条目完整性及有限的逻辑关系；复杂放宽暂不自动发布。"""
    section = application_section(text)
    if any(c.start < section.start or c.end > section.end for c in claims):
        raise PolicyValidationError("申请条件树引用了考核/录取或其他章节")
    def leaf_paths(node: dict, path: str):
        if node.get("type") in ("and", "or"):
            for i, child in enumerate(node["conditions"]):
                yield from leaf_paths(child, f"{path}.conditions[{i}]")
        elif node.get("type") == "relief":
            yield from leaf_paths(node["base"], f"{path}.base")
            yield from leaf_paths(node["relief"], f"{path}.relief")
        else:
            yield path

    paths = set(leaf_paths(tree, "conditions"))
    leaf_claims = [claim for claim in claims if claim.path in paths]
    clause_bounds = []
    if section.preamble and any(node["type"] != "unstructured" for node in _leaf_nodes(tree)):
        raise PolicyValidationError("申请条件前言含未核验的范围/组合语义，请整段保留未知")
    if re.search(r"任一|其一|之一|任选|至少一", section.text) and any(node["type"] != "unstructured" for node in _leaf_nodes(tree)):
        raise PolicyValidationError("替代关系超出当前明确‘或’句式，需独立语义复核")
    for clause in section.clauses:
        local_start, local_end = locate_quote(section.text, clause)
        start, end = section.start + local_start, section.start + local_end
        clause_bounds.append((start, end))
        if re.search(r"或", clause) and re.search(r"且|并且|同时", clause) and any(node["type"] != "unstructured" for node in _leaf_nodes(tree)):
            raise PolicyValidationError("同条目混合替代与合取，分组语义不明确，请保留未知")
        uncovered = "".join(text[i] for i in range(start, end) if not any(c.start <= i < c.end for c in leaf_claims))
        remainder = re.sub(r"或|并且|同时|且|[\s，,。；;：:]", "", uncovered)
        if remainder:
            raise PolicyValidationError("申请条件条目缺失或被截断，不能发布不完整条件树")
    claim_by_path = {c.path: c for c in claims}

    def visit(node: dict, path: str) -> tuple[int, int]:
        kind = node["type"]
        if kind == "relief":
            raise PolicyValidationError("复杂放宽逻辑暂不自动发布，应整体保留未结构化")
        if kind not in ("and", "or"):
            claim = claim_by_path[path]
            if re.search(r"考核总成绩|确认待录取|系统填报志愿", claim.quote):
                raise PolicyValidationError("未来流程义务不能用作当前申请资格")
            return claim.start, claim.end
        spans = [visit(child, f"{path}.conditions[{i}]") for i, child in enumerate(node["conditions"])]
        if len(spans) > 1:
            ordered = sorted(spans)
            if any(b[0] < a[1] for a, b in zip(ordered, ordered[1:])):
                raise PolicyValidationError("多个子条件证据重叠，无法证明其逻辑关系")
            bridges = [text[a[1]:b[0]] for a, b in zip(ordered, ordered[1:])]
            alternative = [bool(re.search(r"或|任选|任一|其一", bridge)) for bridge in bridges]
            if kind == "or" and not all(alternative):
                raise PolicyValidationError("OR 条件之间未找到明确替代关系")
            if kind == "and" and (any(alternative) or re.search(r"满足.*(?:任一|其一)|任选", section.preamble)):
                raise PolicyValidationError("原文替代关系被改写成 AND")
        start, end = min(a for a, _ in spans), max(b for _, b in spans)
        if kind == "or":
            if not any(clause_start <= start and end <= clause_end for clause_start, clause_end in clause_bounds):
                raise PolicyValidationError("OR不得跨独立编号条目重组必要条件")
            claim = claim_by_path[path]
            if claim.start > start or claim.end < end:
                raise PolicyValidationError("OR 组合引文未包含所有替代分支")
        return start, end

    visit(tree, "conditions")


def _leaf_nodes(node: dict):
    if node.get("type") in ("and", "or"):
        for child in node["conditions"]:
            yield from _leaf_nodes(child)
    elif node.get("type") == "relief":
        yield from _leaf_nodes(node["base"])
        yield from _leaf_nodes(node["relief"])
    else:
        yield node
