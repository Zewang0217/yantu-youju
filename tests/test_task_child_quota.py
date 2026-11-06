"""单层子任务额度与恢复反例；隔离 SQLite，无外部调用。"""

import tempfile
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from test_task_runtime import Clock
from yantu.tasks.models import Budget, LeaseLost, StepResult, StepSpec, TaskConflict, Usage
from yantu.tasks.runner import HandlerRegistry, TaskRunner
from yantu.tasks.store import TaskStore


class ChildQuotaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.store = TaskStore(Path(self.tmp.name) / 'tasks.sqlite', clock=self.clock)
        self.parent = self.store.submit(request_id='root', idempotency_key='root-key', payload={},
            plan=(StepSpec('root', 'local', 'v1'),), budget=Budget(tool_calls=4, tokens=20, cost_units=10))
        self.lease = self.store.claim(self.parent, owner='parent')

    def child(self, key='child-key', quota=Usage(1, 4, 2)):
        return self.store.submit_child(self.lease, idempotency_key=key, payload={'source': key},
            plan=(StepSpec('child', 'local', 'v1', Usage(1, 4, 2)),), quota=quota, cost_unit='credits')

    def begin(self, task, owner='child'):
        lease = self.store.claim(task, owner=owner)
        attempt = self.store.start_step(lease)
        self.assertIsNotNone(attempt)
        self.store.dispatched(lease, attempt)
        return lease, attempt

    def test_idempotency_and_frozen_grant(self):
        child = self.child()
        self.assertEqual(self.child(), child)
        with self.assertRaises(TaskConflict):
            self.child(quota=Usage(2, 4, 2))
        self.assertEqual(self.store.inspect(child)['run_id'], self.store.inspect(self.parent)['run_id'])

    def test_root_and_child_keep_legacy_plan_bytes_and_idempotency(self):
        child = self.child()
        plan = (StepSpec('child', 'local', 'v1', Usage(1, 4, 2)),)
        root = self.store.submit(request_id='other', idempotency_key='other-root', payload={}, plan=plan)
        expected = b'{"steps":[{"estimate":{"cost_units":2,"tokens":4,"tool_calls":1},"handler":"local","name":"child","version":"v1"}]}'
        with closing(sqlite3.connect(self.store.path)) as connection:
            for task in (root, child):
                self.assertEqual(connection.execute('SELECT plan_json FROM tasks WHERE id=?', (task,)).fetchone()[0], expected)
        self.assertEqual(self.child(), child)
        self.assertEqual(self.store.submit(request_id='other', idempotency_key='other-root', payload={}, plan=plan), root)

    def test_exact_remaining_grant_is_allowed_and_next_unit_is_rejected(self):
        self.child('first', Usage(3, 16, 8))
        self.child('exact', Usage(1, 4, 2))
        with self.assertRaises(TaskConflict):
            self.child('excess', Usage(1, 0, 0))

    def test_concurrent_grants_cannot_oversubscribe(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.child, key, Usage(3, 15, 7)) for key in ('a', 'b')]
            results = []
            for future in futures:
                try:
                    results.append(future.result())
                except TaskConflict:
                    pass
        self.assertEqual(len(results), 1)

    def test_root_cannot_spend_child_reservation(self):
        self.child(quota=Usage(4, 20, 10))
        self.assertIsNone(self.store.start_step(self.lease))
        self.assertEqual(self.store.inspect(self.parent)['reason'], 'BUDGET_EXHAUSTED')

    def test_child_cannot_spend_beyond_grant(self):
        child = self.child(quota=Usage(1, 3, 2))
        lease = self.store.claim(child, owner='worker')
        self.assertIsNone(self.store.start_step(lease))
        self.assertEqual(self.store.inspect(child)['reason'], 'BUDGET_EXHAUSTED')

    def test_root_waits_and_child_completion_finishes_parent(self):
        child = self.child()
        attempt = self.store.start_step(self.lease)
        self.store.dispatched(self.lease, attempt)
        self.store.finish(self.lease, attempt, StepResult({'root': True}))
        self.assertEqual(self.store.inspect(self.parent)['status'], 'waiting_input')
        self.assertEqual(self.store.inspect(self.parent)['waitReason'], 'children_pending')
        self.assertEqual(self.store.inspect(self.parent)['pendingChildren'], 1)
        self.assertIsNone(self.store.claim(self.parent, owner='ordinary'))
        lease, attempt = self.begin(child)
        self.store.finish(lease, attempt, StepResult({'child': True}, Usage(1, 4, 2)))
        self.assertEqual(self.store.inspect(self.parent)['status'], 'completed')

    def test_cancel_parent_revokes_child_and_holds_inflight(self):
        child = self.child()
        lease, attempt = self.begin(child)
        self.store.cancel(self.parent)
        self.assertEqual(self.store.inspect(child)['status'], 'cancelled')
        self.assertEqual(self.store.inspect(self.parent)['budget']['usagePending'], 1)
        with self.assertRaises(LeaseLost):
            self.store.finish(lease, attempt, StepResult({'late': True}))

    def test_deadline_stops_both(self):
        child = self.child()
        lease, attempt = self.begin(child)
        self.clock.advance(301)
        self.assertFalse(self.store.finish(lease, attempt, StepResult({'late': True})))
        self.assertEqual(self.store.inspect(self.parent)['reason'], 'DEADLINE_EXCEEDED')
        self.assertEqual(self.store.inspect(child)['reason'], 'DEADLINE_EXCEEDED')

    def test_grandchild_and_inconsistent_units_rejected(self):
        child = self.child()
        child_lease = self.store.claim(child, owner='worker')
        with self.assertRaises(TaskConflict):
            self.store.submit_child(child_lease, idempotency_key='grandchild', payload={},
                                    plan=(StepSpec('a', 'local', 'v1'),), quota=Usage(), cost_unit='credits')
        with self.assertRaises(TaskConflict):
            self.store.submit_child(self.lease, idempotency_key='other', payload={},
                                    plan=(StepSpec('a', 'local', 'v1'),), quota=Usage(), cost_unit='currency')

    def test_old_parent_id_remains_separate_run(self):
        retry = self.store.submit(request_id='retry', idempotency_key='retry', payload={},
                                  plan=(StepSpec('root', 'local', 'v1'),), parent_task_id=self.parent)
        self.assertNotEqual(self.store.inspect(retry)['run_id'], self.store.inspect(self.parent)['run_id'])

    def test_child_actual_overrun_partial_and_prevents_more_calls(self):
        child = self.child()
        lease, attempt = self.begin(child)
        self.store.finish(lease, attempt, StepResult({'ok': True}, Usage(1, 5, 2)))
        self.assertEqual(self.store.inspect(child)['status'], 'partial')
        self.assertEqual(self.store.inspect(child)['childQuota']['used']['tokens'], 5)
        self.assertEqual(self.store.inspect(child)['childQuota']['grant']['tokens'], 4)

    def test_pending_remains_held_after_child_terminal_and_requires_unit(self):
        child = self.child()
        lease, attempt = self.begin(child)
        self.store.finish(lease, attempt, StepResult({'ok': True}, None))
        budget = self.store.inspect(self.parent)['budget']
        self.assertEqual(budget['held']['tokens'], 4)
        self.assertEqual(budget['usagePending'], 1)
        for unit in (None, 'other'):
            with self.assertRaises(ValueError):
                self.store.reconcile(attempt.call_id, Usage(1, 5, 3), cost_unit=unit)
        self.store.reconcile(attempt.call_id, Usage(1, 5, 3), cost_unit='credits')
        self.assertEqual(self.store.inspect(self.parent)['budget']['used']['tokens'], 5)
        self.assertTrue(self.store.inspect(child)['childQuota']['overrun'])
        self.assertEqual(self.store.inspect(child)['status'], 'completed')
        self.assertEqual(self.store.inspect(self.parent)['budget']['childOverruns'], [child])

    def test_expired_child_submission_commits_parent_and_child_stop(self):
        child = self.child()
        self.clock.advance(301)
        self.assertIsNone(self.child(key='late'))
        self.assertEqual(self.store.inspect(self.parent)['reason'], 'DEADLINE_EXCEEDED')
        self.assertEqual(self.store.inspect(child)['reason'], 'DEADLINE_EXCEEDED')

    def test_cancel_child_after_deadline_stops_parent_for_deadline(self):
        child = self.child()
        attempt = self.store.start_step(self.lease)
        self.store.dispatched(self.lease, attempt)
        self.store.finish(self.lease, attempt, StepResult({'root': True}))
        self.clock.advance(301)
        self.store.cancel(child)
        self.assertEqual(self.store.inspect(self.parent)['reason'], 'DEADLINE_EXCEEDED')

    def test_legacy_unlabelled_cost_cannot_be_given_unit_afterward(self):
        self.parent = self.store.submit(request_id='legacy', idempotency_key='legacy-root', payload={},
            plan=(StepSpec('one', 'local', 'v1'), StepSpec('two', 'local', 'v1')),
            budget=Budget(tool_calls=4, tokens=20, cost_units=10))
        self.lease = self.store.claim(self.parent, owner='legacy')
        attempt = self.store.start_step(self.lease)
        self.store.dispatched(self.lease, attempt)
        self.store.finish(self.lease, attempt, StepResult({'root': True}, Usage(1, 0, 2)))
        with self.assertRaises(TaskConflict):
            self.child()

    def test_unlabelled_root_cost_is_rejected_after_grant(self):
        self.child()
        attempt = self.store.start_step(self.lease)
        self.store.dispatched(self.lease, attempt)
        with self.assertRaises(TaskConflict):
            self.store.finish(self.lease, attempt, StepResult({'root': True}, Usage(1, 0, 2)))
        self.assertIsNone(self.store.output(self.parent))

    def test_real_process_death_keeps_child_pending(self):
        child = self.child()
        script = '''
import os, sys
from pathlib import Path
from yantu.tasks.store import TaskStore
store = TaskStore(Path(sys.argv[1]), clock=lambda: 1000.0)
lease = store.claim(sys.argv[2], owner='dead')
attempt = store.start_step(lease)
store.dispatched(lease, attempt)
os._exit(71)
'''
        child_process = subprocess.run([sys.executable, '-c', script, str(self.store.path), child],
                                       capture_output=True, timeout=20)
        self.assertEqual(child_process.returncode, 71, child_process.stderr.decode(errors='replace'))
        self.clock.advance(61)
        reopened = TaskStore(self.store.path, clock=self.clock)
        lease = reopened.claim(child, owner='recovery')
        self.assertIsNone(reopened.start_step(lease))
        self.assertEqual(reopened.inspect(child)['budget']['usagePending'], 1)
        self.assertEqual(reopened.inspect(child)['childQuota']['held']['tokens'], 4)

    def test_existing_v1_database_gets_child_table_and_keeps_old_task(self):
        with closing(sqlite3.connect(self.store.path)) as connection:
            connection.execute('DROP TABLE child_grants')
            connection.commit()
        reopened = TaskStore(self.store.path, clock=self.clock)
        self.assertEqual(reopened.inspect(self.parent)['status'], 'running')
        self.store = reopened
        child = self.child()
        self.assertEqual(reopened.inspect(child)['parent_id'], self.parent)


if __name__ == '__main__':
    unittest.main()
