"""发布事务反例：合成资料、隔离 SQLite 和快照，不访问网络。"""
import copy
from dataclasses import replace
from datetime import timedelta
import sqlite3
import tempfile
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from test_trusted_pipeline import DraftExtractor, SCOPE, TREE, make_snapshot
from yantu.acquisition.publications import PublicationError
from yantu.acquisition.store import SnapshotStore
from yantu.tasks.models import Budget, LeaseLost, StepResult, StepSpec, TaskConflict, TaskStorageError, Usage
from yantu.tasks.publications import TaskPublicationRepository, publication_input
from yantu.acquisition.trusted_pipeline import ConservativeExtractor
from yantu.tasks.store import TaskStore


class TaskPublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1000.0
        self.root = Path(self.tmp.name)
        self.store = TaskStore(self.root / 'tasks.db', clock=lambda: self.now)
        self.repo = TaskPublicationRepository(self.store, SnapshotStore(self.root / 'snapshots'))
        self.counter = 0

    def active(self, request='test-request', budget=Budget(), expected=None):
        self.counter += 1
        task = self.store.submit(request_id=request, idempotency_key=f'key-{self.counter}', payload={'publication': publication_input(make_snapshot(), SCOPE, expected)},
                                 plan=(StepSpec('publish', 'policy_publish', 'v1'),), budget=budget)
        lease = self.store.claim(task, owner='worker', lease_seconds=20)
        attempt = self.store.start_step(lease)
        self.store.dispatched(lease, attempt)
        return task, lease, attempt

    def publish(self, lease, attempt, tree=TREE, expected=None, usage=Usage()):
        with patch.object(ConservativeExtractor, 'structure', side_effect=DraftExtractor(tree).structure):
            return self.repo.publish(lease, attempt, make_snapshot(), SCOPE, ConservativeExtractor(),
                                     request_id='test-request', expected_version=expected, usage=usage)

    def test_atomic_publish_and_reopen(self):
        task, lease, attempt = self.active()
        result = self.publish(lease, attempt)
        self.assertEqual(result.status, 'published')
        self.assertEqual(self.store.inspect(task)['status'], 'completed')
        self.assertEqual(self.store.output(task)['publicationVersion'], result.version)
        self.assertEqual(self.store.inspect(task)['calls'][0]['status'], 'settled')
        reopened = TaskPublicationRepository(TaskStore(self.root / 'tasks.db'), SnapshotStore(self.root / 'snapshots'))
        self.assertEqual(reopened.current_version(SCOPE.oid), result.version)
        self.assertEqual(reopened.load(SCOPE.oid).oid, SCOPE.oid)

    def test_quarantine_does_not_advance_current(self):
        _, lease, attempt = self.active()
        good = self.publish(lease, attempt)
        _, lease, attempt = self.active(expected=good.version)
        bad = copy.deepcopy(TREE)
        bad['conditions'][0]['threshold'] = 300
        result = self.publish(lease, attempt, bad, expected=good.version)
        self.assertEqual(result.status, 'quarantined')
        self.assertEqual(self.repo.current_version(SCOPE.oid), good.version)
        self.assertEqual(self.repo.read_record(result.version)['status'], 'quarantined')

    def test_stale_and_cancelled_attempt_never_publish(self):
        task, old, attempt = self.active()
        self.now += 21
        replacement = self.store.claim(task, owner='new-worker')
        with self.assertRaises(LeaseLost):
            self.publish(old, attempt)
        self.assertIsNone(self.repo.current_version(SCOPE.oid))
        new_attempt = self.store.start_step(replacement)
        self.store.dispatched(replacement, new_attempt)
        self.store.cancel(task)
        with self.assertRaises(LeaseLost):
            self.publish(replacement, new_attempt)
        self.assertIsNone(self.repo.current_version(SCOPE.oid))

    def test_expiry_during_commit_rolls_back_everything(self):
        task, lease, attempt = self.active()
        original = self.store._event
        def expire(connection, task_row, event, now, **kwargs):
            original(connection, task_row, event, now, **kwargs)
            if event == 'publication_committed':
                self.now += 21
        with patch.object(self.store, '_event', side_effect=expire), self.assertRaises(LeaseLost):
            self.publish(lease, attempt)
        self.assertIsNone(self.repo.current_version(SCOPE.oid))
        self.assertIsNone(self.store.output(task))
        self.assertEqual(self.store.inspect(task)['calls'][0]['status'], 'dispatched')

    def test_audit_failure_rolls_back_everything(self):
        task, lease, attempt = self.active()
        original = self.store._event
        def fail(connection, task_row, event, now, **kwargs):
            if event == 'step_completed':
                raise sqlite3.OperationalError('synthetic')
            original(connection, task_row, event, now, **kwargs)
        with patch.object(self.store, '_event', side_effect=fail), self.assertRaises(TaskStorageError):
            self.publish(lease, attempt)
        self.assertIsNone(self.repo.current_version(SCOPE.oid))
        self.assertIsNone(self.store.output(task))
        self.assertEqual(self.store.inspect(task)['calls'][0]['status'], 'dispatched')

    def test_idempotent_same_version_and_cas_rejects_change(self):
        _, lease, attempt = self.active()
        first = self.publish(lease, attempt)
        _, lease, attempt = self.active()
        second = self.publish(lease, attempt, expected=None)
        self.assertEqual(first.version, second.version)
        _, lease, attempt = self.active()
        bad = copy.deepcopy(TREE)
        bad['conditions'][0]['threshold'] = 300
        with self.assertRaises(PublicationError):
            self.publish(lease, attempt, bad, expected=None)
        self.assertEqual(self.repo.current_version(SCOPE.oid), first.version)

    def test_run_records_expected_cas_conflict(self):
        first_snapshot = make_snapshot()
        second_snapshot = replace(first_snapshot, fetched_at=first_snapshot.fetched_at + timedelta(hours=1))
        first = self.repo.submit(first_snapshot, SCOPE, request_id='test-request', idempotency_key='cas-first')
        second = self.repo.submit(second_snapshot, SCOPE, request_id='test-request', idempotency_key='cas-second')
        self.repo.run(first, owner='first')
        version = self.repo.current_version(SCOPE.oid)
        with self.assertRaises(PublicationError):
            self.repo.run(second, owner='second')
        self.assertEqual(self.store.inspect(second)['reason'], 'PUBLISH_CONFLICT')
        self.assertEqual(self.repo.current_version(SCOPE.oid), version)

    def test_unknown_usage_remains_reserved_for_publish_and_finish(self):
        task, lease, attempt = self.active(budget=Budget(tool_calls=2, tokens=10))
        self.publish(lease, attempt, usage=None)
        info = self.store.inspect(task)
        self.assertEqual(info['calls'][0]['status'], 'usage_pending')
        self.assertEqual(info['budget']['held']['tool_calls'], 1)
        self.store.reconcile(attempt.call_id, Usage(tokens=2))
        self.assertEqual(self.store.inspect(task)['calls'][0]['status'], 'settled')
        task, lease, attempt = self.active()
        self.store.finish(lease, attempt, StepResult({'ok': True}, None))
        self.assertEqual(self.store.inspect(task)['calls'][0]['status'], 'usage_pending')

    def test_snapshot_damage_blocks_read(self):
        _, lease, attempt = self.active()
        self.publish(lease, attempt)
        self.repo.snapshots.path_of(make_snapshot().sha256).write_bytes(b'damaged')
        with self.assertRaises(PublicationError):
            self.repo.load(SCOPE.oid)

    def test_single_step_entry_and_recovery_before_and_after_commit(self):
        task = self.repo.submit(make_snapshot(), SCOPE, request_id='test-request', idempotency_key='entry')
        original = self.store._event
        def crash(connection, task_row, event, now, **kwargs):
            original(connection, task_row, event, now, **kwargs)
            if event == 'publication_committed':
                raise KeyboardInterrupt()
        with patch.object(self.store, '_event', side_effect=crash), self.assertRaises(KeyboardInterrupt):
            self.repo.run(task, owner='first', lease_seconds=20)
        self.assertIsNone(self.repo.current_version(SCOPE.oid))
        self.now += 21
        state = self.repo.run(task, owner='recovery')
        self.assertEqual(state['status'], 'partial')
        self.assertEqual(state['calls'][0]['status'], 'usage_pending')
        self.assertEqual(state['calls'][1]['status'], 'settled')
        version = self.repo.current_version(SCOPE.oid)
        self.assertEqual(version, self.store.output(task)['publicationVersion'])
        self.assertEqual(self.repo.run(task, owner='again')['status'], 'partial')
        self.assertEqual(self.repo.current_version(SCOPE.oid), version)

    def test_crash_after_commit_reads_checkpoint_without_duplicate_attempt(self):
        task = self.repo.submit(make_snapshot(), SCOPE, request_id='test-request', idempotency_key='after-commit')
        publish = self.repo.publish
        def crash_after(*args, **kwargs):
            publish(*args, **kwargs)
            raise KeyboardInterrupt()
        with patch.object(self.repo, 'publish', side_effect=crash_after), self.assertRaises(KeyboardInterrupt):
            self.repo.run(task, owner='first')
        first = self.store.inspect(task)
        self.assertEqual(first['status'], 'partial')
        self.assertEqual(len(first['attempts']), 1)
        again = self.repo.run(task, owner='again')
        self.assertEqual(len(again['attempts']), 1)
        self.assertEqual(self.repo.current_version(SCOPE.oid), self.store.output(task)['publicationVersion'])

    def test_corrupt_version_cannot_be_loaded(self):
        _, lease, attempt = self.active()
        published = self.publish(lease, attempt)
        with self.store._transaction() as connection:
            connection.execute('UPDATE publication_versions SET body=? WHERE version=?', (b'{}', published.version))
        with self.assertRaises(PublicationError):
            self.repo.load(SCOPE.oid)

    def test_expiry_while_waiting_for_sqlite_lock_rejects_commit(self):
        task, lease, attempt = self.active()
        entered, proceed = threading.Event(), threading.Event()
        original = self.repo._commit
        def gated_commit(*args, **kwargs):
            entered.set()
            self.assertTrue(proceed.wait(3))
            return original(*args, **kwargs)
        with patch.object(self.repo, '_commit', side_effect=gated_commit):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.publish, lease, attempt)
                self.assertTrue(entered.wait(3))
                with self.store._transaction() as blocking:
                    proceed.set()
                    self.now += 21
                    blocking.commit()
                with self.assertRaises(LeaseLost):
                    future.result(timeout=5)
        self.assertIsNone(self.repo.current_version(SCOPE.oid))
        self.assertIsNone(self.store.output(task))

    def test_binding_and_extractor_are_checked_before_extraction(self):
        task, lease, attempt = self.active()
        with patch.object(ConservativeExtractor, 'structure') as structure:
            with self.assertRaises(TaskConflict):
                self.repo.publish(lease, attempt, make_snapshot(), SCOPE, ConservativeExtractor(),
                                  request_id='test-request', expected_version='wrong')
            structure.assert_not_called()
        with self.assertRaises(ValueError):
            self.repo.publish(lease, attempt, make_snapshot(), SCOPE, DraftExtractor(TREE), request_id='test-request')
        self.store.cancel(task)
        with patch.object(ConservativeExtractor, 'structure') as structure:
            with self.assertRaises(LeaseLost):
                self.repo.publish(lease, attempt, make_snapshot(), SCOPE, ConservativeExtractor(), request_id='test-request')
            structure.assert_not_called()

    def test_deadline_stops_task_without_publication(self):
        task, lease, attempt = self.active(budget=Budget(wall_seconds=10))
        self.now += 11
        with self.assertRaises(LeaseLost):
            self.publish(lease, attempt)
        self.assertEqual(self.store.inspect(task)['reason'], 'DEADLINE_EXCEEDED')
        self.assertIsNone(self.repo.current_version(SCOPE.oid))
