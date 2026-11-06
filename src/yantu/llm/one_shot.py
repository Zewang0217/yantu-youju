"""一次性模型调用原型；预算预留是本地估算，不是供应商硬限额。"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid

from ..acquisition.artifact_files import canonical_json
from ..tasks.models import Budget, LeaseLost, StepFailure, StepSpec, TaskStorageError, Usage, identifier
from ..tasks.store import TaskStore
from .client import ChatClient, LLMError
from .task_adapter import chat_result_to_step

_HANDLER = 'llm_once'
_VERSION = 'v1'


def _snapshot_client(client: ChatClient) -> ChatClient:
    if not isinstance(client, ChatClient):
        raise ValueError('必须显式注入客户端')
    return ChatClient(client.base_url, client.api_key, client.model, client.timeout, client.max_tokens, client.transport)


def _commitment(client: ChatClient, messages: list[dict], temperature: float, request_id: str) -> str:
    """冻结输入与配置；任务账本不保存提示词、URL、凭据或其明文副本。"""
    if not isinstance(client, ChatClient) or not client.is_configured or client.transport is None:
        raise ValueError('必须显式注入已配置客户端和传输')
    if not isinstance(messages, list) or not all(isinstance(message, dict) for message in messages):
        raise ValueError('模型输入无效')
    data = canonical_json({'request_id': request_id, 'messages': messages, 'temperature': temperature,
        'base_url': client.base_url, 'model': client.model, 'timeout': client.timeout, 'max_tokens': client.max_tokens})
    return hmac.new(client.api_key.encode('utf-8'), data, hashlib.sha256).hexdigest()


def submit_model_task(store: TaskStore, client: ChatClient, messages: list[dict], *, request_id: str,
                      idempotency_key: str, cost_unit: str, estimate: Usage, budget: Budget,
                      temperature: float = 0.1) -> str:
    """只保存不可逆输入承诺；估算不限制供应商实际费用，调用者需独立控制风险。"""
    identifier(cost_unit)
    commitment = _commitment(_snapshot_client(client), json.loads(canonical_json(messages)), temperature, request_id)
    return store.submit(request_id=request_id, idempotency_key=idempotency_key,
        payload={'commitment': commitment, 'cost_unit': cost_unit},
        plan=(StepSpec('model', _HANDLER, _VERSION, estimate, 'at_most_once', cost_unit),), budget=budget)


def run_model_task(store: TaskStore, task_id: str, client: ChatClient, messages: list[dict], *,
                   cost_unit: str, temperature: float = 0.1, owner: str | None = None,
                   lease_seconds: float = 60) -> dict:
    """显式提供客户端及原始输入；发送后失败一律保留未知用量且永不自动重放。"""
    identifier(cost_unit)
    snapshot = store.inspect(task_id)
    payload = store.payload(task_id)
    frozen_messages = json.loads(canonical_json(messages))
    frozen_client = _snapshot_client(client)
    expected = _commitment(frozen_client, frozen_messages, temperature, snapshot['request_id'])
    if payload != {'commitment': expected, 'cost_unit': cost_unit}:
        raise ValueError('输入、客户端配置或成本单位与冻结任务不一致')
    lease = store.claim(task_id, owner=owner or uuid.uuid4().hex, lease_seconds=lease_seconds, replay_policy='at_most_once')
    if lease is None:
        return store.inspect(task_id)
    attempt = None
    try:
        attempt = store.start_step(lease)
        if attempt is None:
            return store.inspect(task_id)
        if (attempt.handler, attempt.version, attempt.step_name) != (_HANDLER, _VERSION, 'model'):
            store.fail(lease, attempt, StepFailure('HANDLER_UNAVAILABLE', sent=False))
            return store.inspect(task_id)
        if not store.dispatched(lease, attempt, replay_policy='at_most_once', cost_unit=cost_unit):
            return store.inspect(task_id)
        try:
            result = frozen_client.chat_with_usage(frozen_messages, temperature)
        except (LLMError, TaskStorageError):
            raise
        except Exception:
            raise LLMError('传输异常', code='INVARIANT_BROKEN') from None
        store.finish(lease, attempt, chat_result_to_step(result))
    except LeaseLost:
        pass
    except LLMError as error:
        if attempt is not None:
            try:
                # 即使客户端判断未发送，也不撤销已落盘的dispatch；没有重试。
                store.fail(lease, attempt, StepFailure(error.code, sent=True))
            except LeaseLost:
                pass
    return store.inspect(task_id)
