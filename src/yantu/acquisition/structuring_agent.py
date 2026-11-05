"""LLM 条款结构化 Agent(采集线核心)+ 字段发布门禁。

流程:政策正文 →(LLM,受控JSON)→ 条件 spec 草案 → **门禁**(可构建性
codec.build + 逐字证据 grounding:quote 必须逐字出现在原文) → 通过才输出;
门禁失败带错误反馈重试一次,再失败整体降级(conditions=None → 引擎走
UnstructuredCondition 路径,INV-13)。确定性窗口提取始终并行参与,
不依赖 LLM。

诚实边界:门禁保证的是"结构可解析+摘录可回溯",不保证语义完全正确——
语义抽验属 08 评估协议(golden/judge/人工复标)。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

from ..domain import condition_codec as codec
from ..llm import ChatClient
from .structurer import WindowCandidate, extract_window_candidates

PROMPT_VERSION = "structuring-v1"

_SYSTEM_PROMPT = """你是推免招生政策的结构化助手。把给定政策正文整理为条件树JSON。
可用节点:
- 组合器: {"type":"and","conditions":[...]} / {"type":"or","conditions":[...]} / {"type":"relief","base":{...},"relief":{...},"discretionary":true|false}
- 叶子条件类型(仅限以下,参数按需): {LEAF_TYPES}
- 未结构化条款: {"type":"unstructured","label":"...","quote":"原文逐字摘录","located_at":"位置","reason":"为何无法结构化"}
规则:
1. 只输出正文明确支持的内容;数值门槛必须与原文一致,禁止推算或补默认值。
2. 每个叶子条件可带 "quote" 字段=支持该条件的原文**逐字**片段(必须能在正文中找到)。
3. 优先项(如"XX者优先")不要放进条件树——那是门槛之外的提示项。
4. 不确定的条款一律用 unstructured 类型,附 quote。
5. 只输出一个JSON对象: {{"conditions": <条件树>, "notes": ["..."]}} ,不要输出其他文字。"""


@dataclass
class GateCheck:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class StructuringResult:
    source_chars: int
    status: str = "skipped"  # structured / degraded / skipped
    conditions_spec: Optional[dict] = None
    windows: list[WindowCandidate] = field(default_factory=list)
    gate_report: list[GateCheck] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    prompt_version: str = PROMPT_VERSION
    attempts: int = 0


def _normalize(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _strip_quotes(node):
    """递归摘出叶子上的 quote 字段(供grounding校验),返回(清理后的树, quotes)。

    递归覆盖 dict / list(conditions 是数组)两种容器。
    """
    quotes: list[str] = []
    if isinstance(node, list):
        cleaned = []
        for item in node:
            child, cq = _strip_quotes(item)
            cleaned.append(child)
            quotes.extend(cq)
        return cleaned, quotes
    if isinstance(node, dict):
        node = dict(node)
        q = node.pop("quote", None)
        if q:
            quotes.append(q)
        for key in ("conditions", "base", "relief"):
            if key in node:
                child, cq = _strip_quotes(node[key])
                node[key] = child
                quotes.extend(cq)
        if isinstance(node.get("condition"), (dict, list)):  # priority 嵌套(防御)
            child, cq = _strip_quotes(node["condition"])
            node["condition"] = child
            quotes.extend(cq)
    return node, quotes


class StructuringAgent:
    def __init__(self, client: Optional[ChatClient] = None, max_attempts: int = 2):
        self._client = client
        self._max_attempts = max_attempts

    def structure(self, text: str) -> StructuringResult:
        result = StructuringResult(source_chars=len(text))
        result.windows = extract_window_candidates(text)

        if self._client is None:
            result.status = "skipped"
            result.notes.append("未配置模型(config/llm.json),仅执行确定性窗口提取")
            return result

        feedback = ""
        for attempt in range(1, self._max_attempts + 1):
            result.attempts = attempt
            try:
                raw = self._client.chat(
                    [
                        {"role": "system", "content": self._system_prompt()},
                        {"role": "user", "content": f"政策正文:\n{text}"},
                    ]
                    + ([{"role": "user", "content": f"上次输出未通过校验:{feedback}请修正后重新输出JSON。"}] if feedback else [])
                )
            except Exception as e:  # LLMError 等:降级,不重试网络
                result.gate_report.append(GateCheck("llm_call", False, str(e)[:160]))
                break

            spec, checks = self._gates(raw, text)
            result.gate_report = checks
            if spec is not None:
                result.conditions_spec = spec
                result.status = "structured"
                return result
            feedback = "; ".join(f"{c.name}:{c.detail}" for c in checks if not c.passed)

        result.status = "degraded"
        result.conditions_spec = None
        result.notes.append("门禁未通过或调用失败,整体降级为未结构化(INV-13);确定性窗口提取仍保留")
        return result

    # —— 门禁 ——

    def _gates(self, raw: str, text: str) -> tuple[Optional[dict], list[GateCheck]]:
        checks: list[GateCheck] = []

        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            checks.append(GateCheck("json_parse", False, "输出中无JSON对象"))
            return None, checks
        try:
            payload = json.loads(m.group(0))
        except json.JSONDecodeError as e:
            checks.append(GateCheck("json_parse", False, str(e)[:120]))
            return None, checks
        checks.append(GateCheck("json_parse", True))

        tree = payload.get("conditions")
        if not isinstance(tree, dict):
            checks.append(GateCheck("has_conditions", False, "缺少 conditions 字段"))
            return None, checks
        checks.append(GateCheck("has_conditions", True))

        cleaned, quotes = _strip_quotes(tree)

        try:
            codec.build(cleaned)
            checks.append(GateCheck("codec_build", True))
        except Exception as e:
            checks.append(GateCheck("codec_build", False, f"{type(e).__name__}: {e}"[:160]))
            return None, checks

        normalized_text = _normalize(text)
        bad = [q for q in quotes if _normalize(q) not in normalized_text]
        if bad:
            checks.append(
                GateCheck("quote_grounding", False, f"{len(bad)}/{len(quotes)} 条quote未在原文找到,如:{bad[0][:40]!r}")
            )
            return None, checks
        checks.append(GateCheck("quote_grounding", True, f"{len(quotes)}条quote全部回溯到原文"))

        return cleaned, checks

    @staticmethod
    def _system_prompt() -> str:
        leaf_types = ", ".join(f'"{name}"' for name in sorted(codec._CONDITION_TYPES))
        return _SYSTEM_PROMPT.replace("{LEAF_TYPES}", leaf_types)
