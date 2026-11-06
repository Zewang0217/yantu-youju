"""LLM 条款结构化草案及有界返工；发布权归独立的可信资料流水线。

流程:政策正文 →(LLM,受控JSON)→ 条件 spec 草案 → **门禁**(可构建性
codec.build + 逐字证据 grounding:quote 必须逐字出现在原文) → 通过才输出;
门禁失败带错误反馈重试一次，再失败保留降级结果，禁止当作已核验事实。
确定性窗口提取始终并行参与，
不依赖 LLM。

草案层验证结构、逐项摘录及受支持的数值句式。申请阶段、完整性、组合关系
和入库授权由trusted_pipeline再次核验；真实模型质量需独立eval。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

from ..domain import condition_codec as codec
from ..llm import ChatClient
from ..llm.client import LLMError
from .policy_verification import PolicyValidationError, SourceClaim, SUPPORTED_NUMERIC_TYPES, bind_claims, validate_tree_budget
from .structurer import WindowCandidate, extract_window_candidates

PROMPT_VERSION = "structuring-v2-evidence"

_SYSTEM_PROMPT = """你是推免招生政策的结构化助手。把给定政策正文整理为条件树JSON。
可用节点:
- 组合器: {"type":"and","conditions":[...]} / {"type":"or","conditions":[...]} / {"type":"relief","base":{...},"relief":{...},"discretionary":true|false}
- 叶子条件类型(仅限以下,参数按需): {LEAF_TYPES}
- 未结构化条款: {"type":"unstructured","label":"...","quote":"原文逐字摘录","located_at":"位置","reason":"为何无法结构化"}
规则:
1. 只输出正文明确支持的内容;数值门槛必须与原文一致,禁止推算或补默认值。
2. 每个叶子条件必须带非空 "quote" 字段=支持该条件的原文逐字片段，OR及放宽组合也必须附关系原文。数值必须显式给threshold，不能省略成默认值。
3. 优先项(如"XX者优先")不要放进条件树——那是门槛之外的提示项。
4. 不确定的条款一律用 unstructured 类型,附 quote。
5. 仅整理报名/申请条件，考核成绩、确认待录取等未来义务不进入当前条件树；完整保留每条编号条件，不确定就以整条原文标unstructured。
6. 只输出一个JSON对象: {"conditions": <条件树>, "notes": ["..."]} ,不要输出其他文字。
数值叶子参数示例：ranking_at_most.threshold=排名前百分比；cet6_at_least.threshold=六级分数；ielts_at_least.threshold=雅思分数。"""


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
    claims: list[SourceClaim] = field(default_factory=list)
    raw_tree: Optional[dict] = None
    model_id: Optional[str] = None
    temperature: Optional[float] = None


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"非标准JSON常量: {value}")


class StructuringAgent:
    """生成有据草案；structured 只表示草案校验通过，不授予事实发布权。"""

    def __init__(self, client: Optional[ChatClient] = None, max_attempts: int = 2):
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 2:
            raise ValueError("max_attempts 必须是1或2")
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
        result.model_id = getattr(self._client, "model", None)
        result.temperature = 0.1
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
            except LLMError:
                result.gate_report.append(GateCheck("llm_call", False, "模型调用失败；未发布任何条件"))
                break

            validated = self.validate_output(raw, text)
            validated.attempts = attempt
            validated.model_id = getattr(self._client, "model", None)
            validated.temperature = 0.1
            if validated.status == "structured":
                return validated
            result = validated
            feedback = "; ".join(f"{c.name}:{c.detail}" for c in result.gate_report if not c.passed)

        result.status = "degraded"
        result.conditions_spec = None
        result.notes.append("门禁未通过或调用失败,整体降级为未结构化(INV-13);确定性窗口提取仍保留")
        return result

    # —— 门禁 ——

    def _gates(self, raw: str, text: str) -> tuple[Optional[dict], list[GateCheck]]:
        result = self.validate_output(raw, text)
        return result.conditions_spec, result.gate_report

    @staticmethod
    def validate_output(raw: str, text: str) -> StructuringResult:
        """只读校验候选输出；保留原始树与逐项引文，供独立发布门禁再次核验。"""
        result = StructuringResult(source_chars=len(text), status="degraded")
        result.windows = extract_window_candidates(text)
        checks = result.gate_report

        m = re.search(r"\{.*\}", raw, re.S) if isinstance(raw, str) and len(raw) <= 100_000 else None
        if not m:
            checks.append(GateCheck("json_parse", False, "输出中无JSON对象"))
            return result
        try:
            payload = json.loads(m.group(0), parse_constant=_reject_json_constant)
        except (ValueError, RecursionError) as e:
            checks.append(GateCheck("json_parse", False, str(e)[:120]))
            return result
        checks.append(GateCheck("json_parse", True))

        tree = payload.get("conditions") if isinstance(payload, dict) else None
        if not isinstance(tree, dict):
            checks.append(GateCheck("has_conditions", False, "缺少 conditions 字段"))
            return result
        checks.append(GateCheck("has_conditions", True))
        try:
            validate_tree_budget(tree)
            result.raw_tree = tree
            cleaned, claims = bind_claims(tree, text)
        except PolicyValidationError as e:
            checks.append(GateCheck("quote_grounding", False, str(e)))
            return result
        try:
            codec.build(cleaned)
            checks.append(GateCheck("codec_build", True))
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            checks.append(GateCheck("codec_build", False, f"{type(e).__name__}: {e}"[:160]))
            return result
        checks.append(GateCheck("quote_grounding", True, f"{len(claims)}项引文已定位，数值句式已核对"))
        result.conditions_spec = cleaned
        result.claims = claims
        result.status = "structured"
        return result

    @staticmethod
    def _system_prompt() -> str:
        leaf_types = ", ".join(f'"{name}"' for name in SUPPORTED_NUMERIC_TYPES)
        return _SYSTEM_PROMPT.replace("{LEAF_TYPES}", leaf_types)
