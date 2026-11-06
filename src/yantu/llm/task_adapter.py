"""模型响应到任务结果的显式转换；未知账单保留预留并记录供应商部分计量。"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .client import ChatResult
from ..tasks.models import StepResult, Usage, identifier, nonnegative_integer


@dataclass(frozen=True)
class MeasuredCost:
    """独立可信账单的整数成本及单位；调用方负责核验来源，不能使用模型生成的数字。"""

    units: int
    unit: str
    source: str

    def __post_init__(self) -> None:
        nonnegative_integer(self.units)
        identifier(self.unit)
        identifier(self.source)


def chat_result_to_step(result: ChatResult, *, cost: MeasuredCost | None = None, budget_unit: str | None = None) -> StepResult:
    """只在token和独立账单均已知且单位一致时完整结算；否则标usage_pending。

    返回值保留可核验的供应商字段及成本来源，供后续callId对账；不执行网络调用。
    """
    if not isinstance(result, ChatResult) or (cost is not None and not isinstance(cost, MeasuredCost)):
        raise ValueError('无效模型调用计量')
    if cost is not None and (not isinstance(budget_unit, str) or cost.unit != budget_unit):
        raise ValueError('成本单位与预算不一致')
    total = result.usage.total_tokens if result.usage.status == 'complete' else None
    usage = Usage(tool_calls=1, tokens=total, cost_units=cost.units) if total is not None and cost is not None else None
    return StepResult({'content': result.content, 'providerUsage': asdict(result.usage),
                       'measuredCost': asdict(cost) if cost else None}, usage=usage)
