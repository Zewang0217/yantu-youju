"""幂等只读步骤的同步调度；退避落盘，工具调用不占用数据库事务。"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Callable

from ..acquisition.error_events import log_failure
from .models import TERMINAL, Attempt, Lease, LeaseLost, StepFailure, StepResult, TaskStorageError, identifier
from .store import TaskStore

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class TaskContext:
    """处理器的协作式检查点；取消/超时不依赖模型主动配合解释错误。"""

    store: TaskStore
    lease: Lease
    attempt: Attempt
    lease_seconds: float

    def check(self) -> None:
        """检查提交权及deadline并续租；当前同步函数不会被强制杀死。"""
        if not self.store.heartbeat(self.lease, lease_seconds=self.lease_seconds):
            raise LeaseLost('任务已停止')

    def previous(self, step_name: str) -> dict | None:
        """读取已提交的步骤产物；损坏时失败，不偷偷重跑已完成工具。"""
        return self.store.output(self.lease.task_id, step_name)


@dataclass(frozen=True)
class StepHandler:
    """已审查的只读幂等操作；此接口不接受正式发布等不可安全重放的副作用。"""

    name: str
    version: str
    function: Callable[[TaskContext, dict], StepResult]
    replay_safe: bool
    effect: str

    def __post_init__(self) -> None:
        identifier(self.name)
        identifier(self.version)
        if self.replay_safe is not True or self.effect != 'read_only' or not callable(self.function):
            raise ValueError('仅支持明确声明可安全重放的只读处理器')


class HandlerRegistry:
    """显式版本分发；旧任务不能悄悄改用不同版本或发布处理器。"""

    def __init__(self) -> None:
        self._handlers: dict[tuple[str, str], StepHandler] = {}

    def register(self, handler: StepHandler) -> None:
        key = handler.name, handler.version
        if key in self._handlers:
            raise ValueError('处理器版本已登记')
        self._handlers[key] = handler

    def get(self, name: str, version: str) -> StepHandler | None:
        return self._handlers.get((name, version))


class TaskRunner:
    """执行已登记的步骤，保留失败历史并在可查询检查点结束；不后台sleep或发布政策。"""

    def __init__(self, store: TaskStore, registry: HandlerRegistry, *, lease_seconds: float = 60):
        self.store, self.registry, self.lease_seconds = store, registry, lease_seconds

    def run(self, task_id: str, *, owner: str | None = None, max_steps: int = 32) -> dict:
        """推进任务至终态、退避或步骤上限；编程异常安全记录后仍重抛原异常。"""
        if type(max_steps) is not int or not 1 <= max_steps <= 32:
            raise ValueError('无效步骤执行上限')
        lease = self.store.claim(task_id, owner=owner or uuid.uuid4().hex, lease_seconds=self.lease_seconds)
        if lease is None:
            return self.store.inspect(task_id)
        trace = self.store.inspect(task_id)
        for _ in range(max_steps):
            attempt = None
            try:
                if not self.store.heartbeat(lease, lease_seconds=self.lease_seconds):
                    break
                attempt = self.store.start_step(lease)
                if attempt is None:
                    break
                handler = self.registry.get(attempt.handler, attempt.version)
                if handler is None:
                    raise StepFailure('HANDLER_UNAVAILABLE', sent=False)
                if not self.store.dispatched(lease, attempt):
                    break
                context = TaskContext(self.store, lease, attempt, self.lease_seconds)
                result = handler.function(context, self.store.payload(task_id))
                self.store.finish(lease, attempt, result)
            except LeaseLost:
                return self.store.inspect(task_id)
            except StepFailure as failure:
                try:
                    self.store.fail(lease, attempt, failure)
                except LeaseLost:
                    pass
                return self.store.inspect(task_id)
            except TaskStorageError as error:
                self._log(error, trace, attempt)
                raise
            except Exception as error:
                self._log(error, trace, attempt)
                if attempt is not None:
                    try:
                        self.store.fail(lease, attempt, StepFailure('INVARIANT_BROKEN'))
                    except LeaseLost:
                        pass
                raise
            snapshot = self.store.inspect(task_id)
            if snapshot['status'] in TERMINAL:
                return snapshot
        snapshot = self.store.inspect(task_id)
        if snapshot['status'] == 'running':
            try:
                self.store.release(lease)
            except LeaseLost:
                pass
        return self.store.inspect(task_id)

    def _log(self, error: BaseException, trace: dict, attempt: Attempt | None) -> None:
        log_failure(LOGGER, error, event='task_execution_failed', task_id=trace['id'], request_id=trace['request_id'], run_id=trace['run_id'],
                    step_id=attempt.step_name if attempt else None, attempt=attempt.number if attempt else None)
