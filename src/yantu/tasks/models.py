"""持久任务的值与不变量；不读取存储、时钟或记录日志。"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass

TERMINAL = frozenset(('completed', 'partial', 'failed', 'cancelled'))
ERROR_CODES = frozenset(('INPUT_MISSING', 'INPUT_CHANGED', 'INPUT_INVALID', 'SOURCE_INTEGRITY_FAILED',
    'RATE_LIMITED', 'NETWORK_TIMEOUT', 'TEMP_UNAVAILABLE', 'FORBIDDEN', 'AUTH_REQUIRED', 'NOT_FOUND',
    'HANDLER_UNAVAILABLE', 'INVARIANT_BROKEN', 'RESULT_INVALID', 'WORKER_LOST', 'BUDGET_EXHAUSTED',
    'DEADLINE_EXCEEDED', 'CANCELLED', 'RETRY_EXHAUSTED', 'RETRY_AFTER_EXCEEDS_POLICY', 'PUBLISH_CONFLICT'))
RETRYABLE_CODES = frozenset(('RATE_LIMITED', 'NETWORK_TIMEOUT', 'TEMP_UNAVAILABLE'))


class TaskStorageError(RuntimeError):
    """本地账本无法确认事务结果；调用者不得伪造任务成功。"""


class TaskConflict(RuntimeError):
    """幂等键与输入或配置不一致，或任务状态不允许当前操作。"""


class LeaseLost(TaskConflict):
    """worker已无提交权，返回值不得写成步骤产物。"""


def identifier(value: str) -> str:
    """关联ID仅接受短的机器标识，不接受URL或任意错误消息。"""
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', value):
        raise ValueError('无效任务标识')
    return value


def nonnegative_integer(value: int) -> None:
    if type(value) is not int or not 0 <= value <= 10**12:
        raise ValueError('用量必须为有界非负整数')


def seconds(value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('时间必须为有限非负秒数')


@dataclass(frozen=True)
class Usage:
    """工具次数、token和同一配置成本单位的已知用量；未知使用None，不能填0。"""

    tool_calls: int = 1
    tokens: int = 0
    cost_units: int = 0

    def __post_init__(self) -> None:
        for value in asdict(self).values():
            nonnegative_integer(value)


@dataclass(frozen=True)
class Budget:
    """单运行共享额度与绝对截止策略；成本单位由接入方统一约定，不估算供应商价格。"""

    tool_calls: int = 10
    tokens: int = 0
    cost_units: int = 0
    wall_seconds: float = 300
    max_attempts: int = 3
    retry_base_seconds: float = 1
    max_retry_seconds: float = 60

    def __post_init__(self) -> None:
        for value in (self.tool_calls, self.tokens, self.cost_units):
            nonnegative_integer(value)
        for value in (self.wall_seconds, self.retry_base_seconds, self.max_retry_seconds):
            seconds(value)
        if (self.wall_seconds <= 0 or self.retry_base_seconds <= 0 or self.max_retry_seconds < self.retry_base_seconds
                or type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 10):
            raise ValueError('无效任务预算或重试策略')


@dataclass(frozen=True)
class StepSpec:
    """一个版本固定的幂等安全操作；预算预留不得由模型输出绕过。"""

    name: str
    handler: str
    version: str
    estimate: Usage = Usage()
    replay_policy: str = 'replay_safe'
    cost_unit: str | None = None

    def __post_init__(self) -> None:
        for value in (self.name, self.handler, self.version):
            identifier(value)
        if not isinstance(self.estimate, Usage) or self.estimate.tool_calls < 1:
            raise ValueError('每次操作必须预留至少一次工具额度')
        if self.replay_policy not in ('replay_safe', 'at_most_once'):
            raise ValueError('无效重放策略')
        if self.replay_policy == 'at_most_once':
            identifier(self.cost_unit)
            if self.estimate.tokens <= 0 or self.estimate.cost_units <= 0:
                raise ValueError('一次性调用必须预留token和成本')
        elif self.cost_unit is not None:
            raise ValueError('成本单位仅用于一次性调用')


@dataclass(frozen=True)
class Lease:
    """单机worker提交权；epoch与owner必须同时匹配且租约未过期。"""

    task_id: str
    owner: str
    epoch: int


@dataclass(frozen=True)
class Attempt:
    """一次已预留预算的步骤尝试；call_id独立于重试次数用于结算幂等。"""

    task_id: str
    step_name: str
    number: int
    call_id: str
    handler: str
    version: str


@dataclass(frozen=True)
class StepResult:
    """步骤数据及用量；None表示已发送但实际用量待核对，partial不授予发布权限。"""

    value: dict
    usage: Usage | None = Usage()
    partial: bool = False


class StepFailure(Exception):
    """可分类的工具失败；只持有错误码，不接收可能含凭据的任意消息。"""

    def __init__(self, code: str, *, sent: bool = True, usage: Usage | None = None, retry_after: float = 0):
        if code not in ERROR_CODES or type(sent) is not bool or (usage is not None and not isinstance(usage, Usage)):
            raise ValueError('无效步骤错误')
        seconds(retry_after)
        self.code, self.sent, self.usage, self.retry_after = code, sent, usage, retry_after
        super().__init__(code)
