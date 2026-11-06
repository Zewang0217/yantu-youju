"""离线资料核验任务适配器；冻结输入摘要，不读取模型配置或写正式发布库。"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from ..acquisition.applicability_service import inspect_applicability
from ..acquisition.document_bundle import load_bundle_record
from ..acquisition.policy_applicability import APPLICABILITY_VERSION, ApplicabilityTarget
from ..acquisition.store import SnapshotStore, SnapshotStorageError
from .models import Budget, StepFailure, StepResult, StepSpec, Usage
from .runner import HandlerRegistry, StepHandler, TaskContext
from .store import TaskStore

WORKFLOW_VERSION = 'offline-scope-v1'
PLAN = (StepSpec('bundle_manifest', 'validate_bundle', 'v1'),
        StepSpec('applicability', 'inspect_scope', APPLICABILITY_VERSION))


def _validated_bundle(payload: dict) -> dict:
    if payload.get('workflow_version') != WORKFLOW_VERSION:
        raise StepFailure('HANDLER_UNAVAILABLE', usage=Usage())
    path = Path(payload['bundle_path'])
    if not path.is_file():
        raise StepFailure('INPUT_MISSING', usage=Usage())
    try:
        record = load_bundle_record(path, SnapshotStore(path.parent / 'snapshots'))
    except FileNotFoundError as error:
        raise StepFailure('INPUT_MISSING', usage=Usage()) from error
    except (SnapshotStorageError, ValueError) as error:
        raise StepFailure('SOURCE_INTEGRITY_FAILED', usage=Usage()) from error
    if record['recordDigest'] != payload['bundle_digest']:
        raise StepFailure('INPUT_CHANGED', usage=Usage())
    return record


def submit_policy_task(store: TaskStore, bundle_path: Path, *, idempotency_key: str, budget: Budget = Budget(),
                       target: ApplicabilityTarget | None = None, parent_task_id: str | None = None) -> str:
    """冻结bundle/目标/版本并入队；以后同一路径改内容也不能偷换正在恢复的任务。"""
    path = bundle_path.resolve()
    record = load_bundle_record(path, SnapshotStore(path.parent / 'snapshots'))
    payload = {'workflow_version': WORKFLOW_VERSION, 'bundle_path': str(path), 'bundle_digest': record['recordDigest'],
               'target': asdict(target) if target else None}
    return store.submit(request_id=record['requestId'], idempotency_key=idempotency_key, payload=payload,
                        plan=PLAN, budget=budget, parent_task_id=parent_task_id)


def policy_registry() -> HandlerRegistry:
    """登记两个真实只读操作；成本/token已知为0，不把未知供应商usage写成0。"""
    registry = HandlerRegistry()

    def validate(context: TaskContext, payload: dict) -> StepResult:
        context.check()
        record = _validated_bundle(payload)
        return StepResult({'bundleDigest': record['recordDigest'], 'plannedSources': len(record['documents'])})

    def inspect(context: TaskContext, payload: dict) -> StepResult:
        context.check()
        _validated_bundle(payload)
        prior = context.previous('bundle_manifest')
        if prior is None or prior['bundleDigest'] != payload['bundle_digest']:
            raise StepFailure('INPUT_CHANGED', usage=Usage())
        target = ApplicabilityTarget(**payload['target']) if payload['target'] is not None else None
        report = inspect_applicability(Path(payload['bundle_path']), target=target)
        if report['inputBundleDigest'] != payload['bundle_digest']:
            raise StepFailure('INPUT_CHANGED', usage=Usage())
        context.check()
        return StepResult(report, partial=report['inputBundleStatus'] != 'completed')

    registry.register(StepHandler('validate_bundle', 'v1', validate, replay_safe=True, effect='read_only'))
    registry.register(StepHandler('inspect_scope', APPLICABILITY_VERSION, inspect, replay_safe=True, effect='read_only'))
    return registry
