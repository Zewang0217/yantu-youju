"""任务事务、预算与真实中断恢复反例；时钟注入，不联网或调用模型。"""

import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

from yantu.tasks.models import (Budget, LeaseLost, StepFailure, StepResult, StepSpec, TaskConflict, TaskStorageError, Usage)
from yantu.tasks.runner import HandlerRegistry, StepHandler, TaskRunner
from yantu.tasks.store import TaskStore

ROOT = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, value):
        self.now += value


class Operation:
    def __init__(self, outcomes=None, usage=Usage()):
        self.calls = []
        self.outcomes = list(outcomes or [])
        self.usage = usage

    def __call__(self, context, payload):
        self.calls.append(context.attempt.step_name)
        if self.outcomes:
            result = self.outcomes.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return StepResult({'step': context.attempt.step_name}, self.usage)


class FailStepAudit:
    def __init__(self, original):
        self.original = original

    def __call__(self, connection, task, event, now, **kwargs):
        if event == 'step_completed':
            raise sqlite3.OperationalError('synthetic audit failure')
        return self.original(connection, task, event, now, **kwargs)


class AdvanceOnStepAudit:
    def __init__(self, original, clock, delta):
        self.original, self.clock, self.delta = original, clock, delta

    def __call__(self, connection, task, event, now, **kwargs):
        result = self.original(connection, task, event, now, **kwargs)
        if event == 'step_completed':
            self.clock.advance(self.delta)
        return result


class TestTaskRuntime(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'tasks.sqlite3'
        self.clock = Clock()
        self.store = TaskStore(self.path, clock=self.clock)

    def submit(self, **changes):
        values = dict(request_id='task-test', idempotency_key='task-key', payload={'source': 'frozen'}, plan=(StepSpec('first', 'operation', 'v1'),))
        values.update(changes)
        return self.store.submit(**values)

    def runner(self, operation=None, store=None, lease_seconds=60):
        registry = HandlerRegistry()
        registry.register(StepHandler('operation', 'v1', operation or Operation(), True, 'read_only'))
        return TaskRunner(store or self.store, registry, lease_seconds=lease_seconds)

    def active(self, **changes):
        task = self.submit(**changes)
        lease = self.store.claim(task, owner='first-worker')
        attempt = self.store.start_step(lease)
        return task, lease, attempt

    def finish_after_waiting_for_writer(self, delta, budget=Budget()):
        task, lease, attempt = self.active(budget=budget)
        self.store.dispatched(lease, attempt)
        entered, original = threading.Event(), self.store._transaction

        @contextmanager
        def signal_transaction(**kwargs):
            entered.set()
            with original(**kwargs) as connection:
                yield connection

        def finish():
            try:
                return self.store.finish(lease, attempt, StepResult({'stale': True}))
            except LeaseLost as error:
                return error

        with closing(sqlite3.connect(self.path, isolation_level=None)) as blocker:
            blocker.execute('BEGIN IMMEDIATE')
            with patch.object(self.store, '_transaction', side_effect=signal_transaction):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(finish)
                    if not entered.wait(5):
                        blocker.rollback()
                        raise RuntimeError('worker did not reach transaction')
                    self.clock.advance(delta)
                    blocker.commit()
                    outcome = future.result(timeout=5)
        return task, outcome

    def test_submit_is_idempotent_without_new_run_or_event(self):
        first = self.submit()
        self.assertEqual(first, self.submit())
        self.assertEqual(len(self.store.list_tasks()), 1)
        self.assertEqual(len(self.store.inspect(first)['events']), 1)

    def test_same_key_rejects_changed_payload_plan_and_budget(self):
        self.submit()
        for change in ({'payload': {'source': 'changed'}}, {'budget': Budget(tool_calls=9)},
                       {'plan': (StepSpec('first', 'operation', 'v2'),)}):
            with self.subTest(change=change):
                with self.assertRaises(TaskConflict):
                    self.submit(**change)

    def test_terminal_task_is_not_executed_twice(self):
        operation, task = Operation(), self.submit()
        self.assertEqual(self.runner(operation).run(task)['status'], 'completed')
        self.assertEqual(self.runner(operation).run(task)['status'], 'completed')
        self.assertEqual(operation.calls, ['first'])

    def test_checkpoint_survives_reopening_without_repeating_completed_step(self):
        operation = Operation()
        task = self.submit(plan=(StepSpec('first', 'operation', 'v1'), StepSpec('second', 'operation', 'v1')))
        checkpoint = self.runner(operation).run(task, max_steps=1)
        self.assertEqual(checkpoint['status'], 'queued')
        reopened = TaskStore(self.path, clock=self.clock)
        result = self.runner(operation, reopened).run(task)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(operation.calls, ['first', 'second'])
        self.assertEqual(reopened.output(task), {'step': 'second'})

    def test_unsent_reservation_is_released_on_recovery(self):
        task, _, _ = self.active(budget=Budget(tool_calls=1))
        self.clock.advance(61)
        result = self.runner().run(task)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual([call['status'] for call in result['calls']], ['released', 'settled'])
        self.assertEqual(result['budget']['used']['tool_calls'], 1)

    def test_dispatched_unknown_usage_stays_reserved_after_recovery(self):
        task, lease, attempt = self.active(budget=Budget(tool_calls=2))
        self.store.dispatched(lease, attempt)
        self.clock.advance(61)
        result = self.runner().run(task)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['budget']['usagePending'], 1)
        self.assertEqual(result['budget']['held']['tool_calls'], 1)
        self.assertEqual(result['budget']['used']['tool_calls'], 1)

    def test_pending_usage_cannot_be_spent_again_after_restart(self):
        task, lease, attempt = self.active(budget=Budget(tool_calls=1))
        self.store.dispatched(lease, attempt)
        self.clock.advance(61)
        operation = Operation()
        result = self.runner(operation).run(task)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['reason'], 'BUDGET_EXHAUSTED')
        self.assertEqual(operation.calls, [])
        self.assertEqual(result['budget']['held']['tool_calls'], 1)

    def test_success_with_unknown_usage_keeps_reservation_until_reconciled(self):
        task = self.submit(plan=(StepSpec('first', 'operation', 'v1', Usage(1, 6, 3)),),
                           budget=Budget(tokens=10, cost_units=10))
        result = self.runner(Operation(outcomes=[StepResult({'answer': 'available'}, usage=None)])).run(task)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.store.output(task, 'first'), {'answer': 'available'})
        self.assertEqual(result['budget']['usagePending'], 1)
        self.assertEqual(result['budget']['held'], {'tool_calls': 1, 'tokens': 6, 'cost_units': 3})
        call_id = result['attempts'][0]['call_id']
        self.store.reconcile(call_id, Usage(1, 5, 2))
        result = self.store.inspect(task)
        self.assertEqual(result['budget']['usagePending'], 0)
        self.assertEqual(result['budget']['used'], {'tool_calls': 1, 'tokens': 5, 'cost_units': 2})

    def test_expired_lease_cannot_commit_even_without_new_owner(self):
        task, lease, attempt = self.active()
        self.store.dispatched(lease, attempt)
        self.clock.advance(61)
        with self.assertRaises(LeaseLost):
            self.store.finish(lease, attempt, StepResult({'unauthorized': True}))
        self.assertIsNone(self.store.output(task))

    def test_lease_expiring_while_waiting_for_write_lock_cannot_commit(self):
        task, outcome = self.finish_after_waiting_for_writer(61)
        self.assertIsInstance(outcome, LeaseLost)
        self.assertIsNone(self.store.output(task))

    def test_deadline_crossed_while_waiting_for_lock_is_persisted(self):
        task, outcome = self.finish_after_waiting_for_writer(2, Budget(wall_seconds=1))
        self.assertFalse(outcome)
        self.assertEqual(self.store.inspect(task)['reason'], 'DEADLINE_EXCEEDED')
        self.assertIsNone(self.store.output(task))

    def test_lease_expiring_during_artifact_audit_rolls_back_everything(self):
        task, lease, attempt = self.active()
        self.store.dispatched(lease, attempt)
        with patch.object(self.store, '_event', AdvanceOnStepAudit(self.store._event, self.clock, 61)):
            with self.assertRaises(LeaseLost):
                self.store.finish(lease, attempt, StepResult({'stale': True}))
        self.assertIsNone(self.store.output(task))
        self.assertEqual(self.store.inspect(task)['calls'][0]['status'], 'dispatched')

    def test_deadline_crossing_inside_transaction_rolls_back_then_recovers(self):
        task, lease, attempt = self.active(budget=Budget(wall_seconds=1))
        self.store.dispatched(lease, attempt)
        with patch.object(self.store, '_event', AdvanceOnStepAudit(self.store._event, self.clock, 2)):
            with self.assertRaises(LeaseLost):
                self.store.finish(lease, attempt, StepResult({'stale': True}))
        self.assertIsNone(self.store.output(task))
        self.assertEqual(self.runner().run(task)['reason'], 'DEADLINE_EXCEEDED')

    def test_new_epoch_blocks_old_worker_result(self):
        task, lease, attempt = self.active()
        self.store.dispatched(lease, attempt)
        self.clock.advance(61)
        successor = self.store.claim(task, owner='new-worker')
        with self.assertRaises(LeaseLost):
            self.store.finish(lease, attempt, StepResult({'stale': True}))
        self.assertGreater(successor.epoch, lease.epoch)
        self.assertIsNone(self.store.output(task))

    def test_expired_worker_cannot_revive_itself_by_heartbeat(self):
        _, lease, _ = self.active()
        self.clock.advance(61)
        with self.assertRaises(LeaseLost):
            self.store.heartbeat(lease)

    def test_cancel_queued_task_never_calls_handler(self):
        task, operation = self.submit(), Operation()
        self.store.cancel(task)
        result = self.runner(operation).run(task)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(operation.calls, [])

    def test_cancel_inflight_revokes_output_and_keeps_pending_usage(self):
        task, lease, attempt = self.active()
        self.store.dispatched(lease, attempt)
        self.assertTrue(self.store.cancel(task))
        with self.assertRaises(LeaseLost):
            self.store.finish(lease, attempt, StepResult({'late': True}))
        result = self.store.inspect(task)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['budget']['usagePending'], 1)
        self.assertIsNone(self.store.output(task))

    def test_cancel_during_handler_does_not_commit_returned_value(self):
        def cancel(context, payload):
            context.store.cancel(context.lease.task_id)
            return StepResult({'too_late': True})
        task = self.submit()
        self.assertEqual(self.runner(cancel).run(task)['status'], 'cancelled')
        self.assertIsNone(self.store.output(task))

    def test_completed_task_cannot_be_rewritten_as_cancelled(self):
        task = self.submit()
        self.runner().run(task)
        self.assertFalse(self.store.cancel(task))
        self.assertEqual(self.store.inspect(task)['status'], 'completed')

    def test_deadline_prevents_new_dispatch(self):
        task, operation = self.submit(), Operation()
        self.clock.advance(301)
        result = self.runner(operation).run(task)
        self.assertEqual(result['reason'], 'DEADLINE_EXCEEDED')
        self.assertEqual(result['attempts'], [])
        self.assertEqual(operation.calls, [])

    def test_return_after_deadline_does_not_commit_result(self):
        def late(context, payload):
            self.clock.advance(301)
            return StepResult({'too_late': True})
        task = self.submit()
        result = self.runner(late).run(task)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['reason'], 'DEADLINE_EXCEEDED')
        self.assertIsNone(self.store.output(task))

    def test_retry_after_is_persisted_and_not_run_before_due(self):
        operation = Operation([StepFailure('RATE_LIMITED', usage=Usage(), retry_after=5), StepResult({'retried': True})])
        task = self.submit()
        first = self.runner(operation).run(task)
        self.assertEqual(first['status'], 'retry_wait')
        self.assertEqual(first['next_at'], 1005)
        self.assertEqual(self.runner(operation).run(task)['status'], 'retry_wait')
        self.assertEqual(len(operation.calls), 1)
        self.clock.advance(5)
        result = self.runner(operation).run(task)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(result['attempts']), 2)
        self.assertEqual(result['budget']['used']['tool_calls'], 2)

    def test_retry_after_above_policy_is_not_truncated(self):
        task = self.submit()
        operation = Operation([StepFailure('RATE_LIMITED', usage=Usage(), retry_after=1000)])
        result = self.runner(operation).run(task)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(result['attempts']), 1)

    def test_forbidden_is_not_retried(self):
        task = self.submit()
        operation = Operation([StepFailure('FORBIDDEN', usage=Usage())])
        result = self.runner(operation).run(task)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['reason'], 'FORBIDDEN')
        self.assertEqual(len(result['attempts']), 1)

    def test_retry_attempt_limit_stops_persistent_failure(self):
        task = self.submit(budget=Budget(max_attempts=2))
        operation = Operation([StepFailure('TEMP_UNAVAILABLE', usage=Usage()), StepFailure('TEMP_UNAVAILABLE', usage=Usage())])
        self.assertEqual(self.runner(operation).run(task)['status'], 'retry_wait')
        self.clock.advance(1)
        result = self.runner(operation).run(task)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(result['attempts']), 2)
        self.assertEqual(self.runner(operation).run(task)['status'], 'failed')
        self.assertEqual(len(operation.calls), 2)

    def test_cancelling_retry_wait_does_not_send_next_attempt(self):
        task = self.submit()
        operation = Operation([StepFailure('RATE_LIMITED', usage=Usage())])
        self.runner(operation).run(task)
        self.store.cancel(task)
        self.clock.advance(5)
        result = self.runner(operation).run(task)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(len(operation.calls), 1)

    def test_unknown_usage_is_reconciled_idempotently(self):
        task = self.submit(plan=(StepSpec('first', 'operation', 'v1', Usage(1, 6, 3)),), budget=Budget(tokens=10, cost_units=10))
        result = self.runner(Operation([StepFailure('FORBIDDEN')])).run(task)
        call_id = result['calls'][0]['id']
        self.store.reconcile(call_id, Usage(1, 5, 2))
        self.store.reconcile(call_id, Usage(1, 5, 2))
        result = self.store.inspect(task)
        self.assertEqual(result['budget']['usagePending'], 0)
        self.assertEqual(result['budget']['used'], {'tool_calls': 1, 'tokens': 5, 'cost_units': 2})
        with self.assertRaises(TaskConflict):
            self.store.reconcile(call_id, Usage(1, 4, 2))

    def test_reconciliation_cannot_release_a_still_active_call(self):
        _, lease, attempt = self.active()
        self.store.dispatched(lease, attempt)
        with self.assertRaises(TaskConflict):
            self.store.reconcile(attempt.call_id, Usage(0, 0, 0))

    def test_actual_overestimate_is_recorded_and_blocks_next_step(self):
        task = self.submit(plan=(StepSpec('first', 'operation', 'v1', Usage(1, 0, 1)), StepSpec('second', 'operation', 'v1')),
                           budget=Budget(cost_units=1))
        operation = Operation(usage=Usage(1, 0, 2))
        result = self.runner(operation).run(task)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['reason'], 'BUDGET_EXHAUSTED')
        self.assertTrue(result['budget']['overrun'])
        self.assertEqual(result['budget']['used']['cost_units'], 2)
        self.assertEqual(operation.calls, ['first'])

    def test_programming_error_is_rethrown_and_messages_are_omitted(self):
        task = self.submit()
        error = TypeError('harmless_task_marker https://unit.example/?query=marker')
        error.__cause__ = ValueError('harmless_cause_marker')
        with self.assertLogs('yantu.tasks.runner', level='ERROR') as logs:
            with self.assertRaises(TypeError) as caught:
                self.runner(Operation([error])).run(task)
        self.assertIs(caught.exception, error)
        self.assertNotIn('harmless_task_marker', '\n'.join(logs.output))
        self.assertNotIn('harmless_cause_marker', '\n'.join(logs.output))
        self.assertIn('runId', '\n'.join(logs.output))
        self.assertIn('requestId', '\n'.join(logs.output))
        self.assertEqual(self.store.inspect(task)['reason'], 'INVARIANT_BROKEN')

    def test_audit_failure_rolls_back_artifact_usage_and_step_commit(self):
        task, lease, attempt = self.active()
        self.store.dispatched(lease, attempt)
        with patch.object(self.store, '_event', FailStepAudit(self.store._event)):
            with self.assertRaises(TaskStorageError):
                self.store.finish(lease, attempt, StepResult({'atomic': True}))
        result = self.store.inspect(task)
        self.assertEqual(result['steps'][0]['status'], 'running')
        self.assertEqual(result['calls'][0]['status'], 'dispatched')
        self.assertIsNone(self.store.output(task))
        self.store.finish(lease, attempt, StepResult({'atomic': True}))
        self.assertEqual(self.store.output(task), {'atomic': True})

    def test_duplicate_dispatch_is_rejected(self):
        _, lease, attempt = self.active()
        self.store.dispatched(lease, attempt)
        with self.assertRaises(TaskConflict):
            self.store.dispatched(lease, attempt)

    def test_output_cannot_be_committed_before_dispatch(self):
        _, lease, attempt = self.active()
        with self.assertRaises(TaskConflict):
            self.store.finish(lease, attempt, StepResult({'not_sent': True}))

    def test_corrupt_output_is_not_silently_recomputed(self):
        task = self.submit()
        self.runner().run(task)
        with sqlite3.connect(self.path) as connection:
            connection.execute('UPDATE artifacts SET body=?', (b'{}',))
        connection.close()
        with self.assertRaises(TaskStorageError):
            self.store.output(task)

    def test_unknown_handler_version_fails_without_charging_dispatch(self):
        task = self.submit(plan=(StepSpec('first', 'operation', 'v2'),))
        result = self.runner().run(task)
        self.assertEqual(result['reason'], 'HANDLER_UNAVAILABLE')
        self.assertEqual(result['calls'][0]['status'], 'released')

    def test_publishing_and_non_idempotent_handlers_are_rejected(self):
        with self.assertRaises(ValueError):
            StepHandler('publish', 'v1', Operation(), True, 'publish')
        with self.assertRaises(ValueError):
            StepHandler('unsafe', 'v1', Operation(), False, 'read_only')

    def test_only_one_worker_can_dispatch_against_single_call_budget(self):
        entered, release = threading.Event(), threading.Event()

        def blocked(context, payload):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test release was not signalled')
            return StepResult({'worker': 'first'})

        task = self.submit(budget=Budget(tool_calls=1))
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.runner(blocked).run, task)
            self.assertTrue(entered.wait(5))
            try:
                second = self.runner(blocked).run(task)
                self.assertEqual(second['status'], 'running')
                self.assertEqual(len(second['attempts']), 1)
            finally:
                release.set()
            result = first.result(timeout=5)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['budget']['used']['tool_calls'], 1)

    def test_process_exit_is_recovered_with_unknown_call_retained(self):
        task = self.submit(budget=Budget(tool_calls=3))
        program = '''import os,sys
from pathlib import Path
from yantu.tasks.store import TaskStore
from yantu.tasks.runner import HandlerRegistry,StepHandler,TaskRunner
store=TaskStore(Path(sys.argv[1]),clock=lambda:1000)
registry=HandlerRegistry()
registry.register(StepHandler('operation','v1',lambda context,payload:os._exit(17),True,'read_only'))
TaskRunner(store,registry).run(sys.argv[2])
'''
        result = subprocess.run([sys.executable, '-c', program, str(self.path), task], cwd=ROOT, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 17)
        self.clock.advance(61)
        resumed = self.runner().run(task)
        self.assertEqual(resumed['status'], 'completed')
        self.assertEqual(resumed['budget']['usagePending'], 1)
        self.assertEqual(len(resumed['attempts']), 2)

    def test_invalid_budget_values_are_rejected(self):
        for change in ({'tool_calls': True}, {'tokens': -1}, {'wall_seconds': float('nan')}, {'max_attempts': 0}, {'retry_base_seconds': 0}):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    replace(Budget(), **change)
