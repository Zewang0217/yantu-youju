"""一次性模型任务离线测试，不访问任何外部服务。"""

import io
import json
import pathlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace

from yantu.llm.client import ChatClient
from yantu.llm.one_shot import run_model_task, submit_model_task
from yantu.tasks.models import Budget, LeaseLost, StepFailure, StepResult, StepSpec, Usage
from yantu.tasks.runner import HandlerRegistry, TaskRunner
from yantu.tasks.store import TaskStore


class Clock:
    def __init__(self):
        self.now = 1000
    def __call__(self):
        return self.now


class FakeTransport:
    def __init__(self):
        self.calls = 0
        self.callback = None
        self.error = None
        self.sent = []
    def __call__(self, req, timeout):
        self.calls += 1
        if self.callback:
            self.callback()
        self.sent.append(json.loads(req.data))
        if self.error:
            raise self.error
        return io.BytesIO(json.dumps({'choices': [{'message': {'content': 'ok'}}],
            'usage': {'prompt_tokens': 5, 'completion_tokens': 3}}).encode())


class OneShotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.path = pathlib.Path(self.tmp.name) / 'tasks.sqlite'
        self.store = TaskStore(self.path, clock=self.clock)
        self.transport = FakeTransport()
        self.client = ChatClient('https://example.invalid', 'secret-credential', 'test', transport=self.transport)
        self.messages = [{'role': 'user', 'content': 'private prompt'}]
        self.estimate = Usage(1, 100, 10)
        self.budget = Budget(tool_calls=1, tokens=100, cost_units=10)

    def submit(self, **overrides):
        args = dict(request_id='request', idempotency_key='unique', cost_unit='credits', estimate=self.estimate, budget=self.budget)
        args.update(overrides)
        return submit_model_task(self.store, self.client, self.messages, **args)

    def run_task(self, task, **overrides):
        args = dict(cost_unit='credits')
        args.update(overrides)
        return run_model_task(self.store, task, self.client, self.messages, **args)

    def test_success_persists_meter_but_holds_estimate(self):
        task = self.submit()
        self.assertEqual(task, self.submit())
        result = self.run_task(task)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['budget']['usagePending'], 1)
        self.assertEqual(result['budget']['held'], {'tool_calls': 1, 'tokens': 100, 'cost_units': 10})
        self.assertEqual(self.store.output(task)['providerUsage']['total_tokens'], 8)
        call_id = result['attempts'][0]['call_id']
        with self.assertRaises(ValueError):
            self.store.reconcile(call_id, Usage(1, 8, 4))
        with self.assertRaises(ValueError):
            self.store.reconcile(call_id, Usage(1, 8, 4), cost_unit='other')
        self.store.reconcile(call_id, Usage(1, 8, 4), cost_unit='credits')
        self.assertEqual(self.store.inspect(task)['budget']['usagePending'], 0)
        self.run_task(task)
        self.assertEqual(self.transport.calls, 1)
        with closing(sqlite3.connect(self.path)) as connection:
            payload, plan = connection.execute('SELECT input_json, plan_json FROM tasks').fetchone()
        self.assertNotIn(b'secret-credential', payload + plan)
        self.assertNotIn(b'private prompt', payload + plan)
        self.assertEqual(json.loads(plan)['steps'][0]['cost_unit'], 'credits')

    def test_regular_runner_cannot_claim(self):
        task = self.submit()
        result = TaskRunner(self.store, HandlerRegistry()).run(task)
        self.assertEqual(result['status'], 'queued')
        self.assertEqual(self.transport.calls, 0)
        self.assertEqual(result['attempts'], [])

    def test_expired_after_dispatch_cannot_replay_even_via_regular_runner(self):
        task = self.submit()
        lease = self.store.claim(task, owner='first', lease_seconds=1, replay_policy='at_most_once')
        attempt = self.store.start_step(lease)
        self.store.dispatched(lease, attempt, replay_policy='at_most_once', cost_unit='credits')
        self.clock.now += 2
        self.assertEqual(TaskRunner(self.store, HandlerRegistry()).run(task)['status'], 'running')
        result = self.run_task(task)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['budget']['usagePending'], 1)
        self.assertEqual(self.transport.calls, 0)
        with self.assertRaises(LeaseLost):
            self.store.finish(lease, attempt, StepResult({'unexpected': True}))
        self.run_task(task)
        self.assertEqual(len(self.store.inspect(task)['attempts']), 1)

    def test_reserved_not_dispatched_can_recover(self):
        task = self.submit()
        self.store.start_step(self.store.claim(task, owner='first', lease_seconds=1, replay_policy='at_most_once'))
        self.clock.now += 2
        result = self.run_task(task)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.transport.calls, 1)
        self.assertEqual([call['status'] for call in result['calls']], ['released', 'usage_pending'])

    def test_dispatched_failure_cannot_release_or_retry(self):
        task = self.submit()
        lease = self.store.claim(task, owner='first', replay_policy='at_most_once')
        attempt = self.store.start_step(lease)
        self.store.dispatched(lease, attempt, replay_policy='at_most_once', cost_unit='credits')
        self.store.fail(lease, attempt, StepFailure('NETWORK_TIMEOUT', sent=False))
        result = self.store.inspect(task)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['budget']['usagePending'], 1)
        self.run_task(task)
        self.assertEqual(self.transport.calls, 0)

    def test_network_error_and_cancel_do_not_replay(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                task = self.submit(request_id=f'request-{cancel}', idempotency_key=f'key-{cancel}')
                if cancel:
                    self.transport.callback = lambda: self.store.cancel(task)
                else:
                    self.transport.error = TimeoutError('secret')
                result = self.run_task(task)
                self.assertEqual(result['status'], 'cancelled' if cancel else 'failed')
                self.assertEqual(result['budget']['usagePending'], 1)
                self.assertIsNone(self.store.output(task))
                before = self.transport.calls
                self.run_task(task)
                self.assertEqual(self.transport.calls, before)
                self.transport.callback = self.transport.error = None

    def test_config_input_and_cost_unit_are_frozen(self):
        task = self.submit()
        self.messages[0]['content'] = 'changed'
        with self.assertRaises(ValueError):
            self.run_task(task)
        self.messages[0]['content'] = 'private prompt'
        with self.assertRaises(ValueError):
            self.run_task(task, cost_unit='different')
        with self.assertRaises(ValueError):
            run_model_task(self.store, task, replace(self.client, model='changed'), self.messages, cost_unit='credits')
        self.assertEqual(self.transport.calls, 0)

    def test_messages_are_snapshotted_before_dispatch(self):
        task = self.submit()
        self.transport.callback = lambda: self.messages[0].update(content='changed')
        self.run_task(task)
        self.assertEqual(self.transport.sent[0]['messages'][0]['content'], 'private prompt')

    def test_unexpected_transport_exception_is_classified_without_leak_or_retry(self):
        task = self.submit()
        self.transport.error = RuntimeError('secret-credential')
        result = self.run_task(task)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['reason'], 'INVARIANT_BROKEN')
        self.assertEqual(result['budget']['usagePending'], 1)
        self.assertNotIn('secret-credential', str(result))
        self.run_task(task)
        self.assertEqual(self.transport.calls, 1)

    def test_budget_insufficient_blocks_dispatch(self):
        task = self.submit(budget=Budget(tool_calls=1, tokens=99, cost_units=10))
        result = self.run_task(task)
        self.assertEqual(result['reason'], 'BUDGET_EXHAUSTED')
        self.assertEqual(self.transport.calls, 0)

    def test_mixed_plan_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.submit(request_id='mixed', idempotency_key='mixed', payload={}, budget=self.budget,
                plan=(StepSpec('once', 'model', 'v1', self.estimate, 'at_most_once', 'credits'),
                      StepSpec('safe', 'read', 'v1')))


if __name__ == '__main__':
    unittest.main()
