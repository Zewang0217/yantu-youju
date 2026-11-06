"""迁移使用合成库，验证历史、指针和故障事务；不触碰真实库。"""
import copy
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

from test_trusted_pipeline import DraftExtractor, SCOPE, TREE, make_snapshot
from yantu.acquisition.artifact_files import canonical_json
from yantu.acquisition.publications import PublicationError, PublicationRepository, publication_version
from yantu.acquisition.store import SnapshotStore
from yantu.acquisition.trusted_pipeline import TrustedPolicyPipeline
from yantu.tasks.models import TaskStorageError
from yantu.tasks.publication_migration import migrate_file_publications
from yantu.tasks.publications import TaskPublicationRepository
from yantu.tasks.store import TaskStore


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.snapshots = SnapshotStore(self.root / 'snapshots')
        self.source = PublicationRepository(self.root / 'source', self.snapshots)
        self.store = TaskStore(self.root / 'target.db')
        self.target = TaskPublicationRepository(self.store, self.snapshots)
        self.good = self.run_source(TREE)
        bad = copy.deepcopy(TREE)
        bad['conditions'][0]['threshold'] = 300
        self.bad = self.run_source(bad, expected=self.good.version)

    def run_source(self, tree, expected=None):
        return TrustedPolicyPipeline(self.snapshots, self.source, DraftExtractor(tree)).run(
            make_snapshot(), SCOPE, request_id='test-request', expected_version=expected)

    def test_import_all_and_repeat_without_changing_current(self):
        first = migrate_file_publications(self.source, self.target)
        self.assertEqual(first, migrate_file_publications(self.source, self.target))
        self.assertEqual((first['versions'], first['current']), (2, 1))
        self.assertEqual(self.target.current_version(SCOPE.oid), self.good.version)
        self.assertEqual(self.target.read_record(self.bad.version)['status'], 'quarantined')
        self.assertEqual(self.target.load(SCOPE.oid), self.source.load(SCOPE.oid))
        with self.store._transaction(write=False) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM publication_imports').fetchone()[0], 1)
            row = db.execute('SELECT manifest,body FROM publication_import_manifests').fetchone()
            self.assertEqual(hashlib.sha256(row['body']).hexdigest(), row['manifest'])
            manifest = json.loads(row['body'])
            self.assertEqual(manifest['current'], {SCOPE.oid: self.good.version})
            self.assertEqual(manifest['versions'][self.good.version], hashlib.sha256(canonical_json(self.source.read_record(self.good.version))).hexdigest())

    def test_existing_import_table_gets_manifest_backfilled(self):
        first = migrate_file_publications(self.source, self.target)
        with self.store._transaction() as db:
            db.execute('DROP TABLE publication_import_manifests')
        self.assertEqual(migrate_file_publications(self.source, self.target), first)
        with self.store._transaction(write=False) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM publication_imports').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM publication_import_manifests').fetchone()[0], 1)

    def test_corrupt_existing_import_manifest_rejected(self):
        migrate_file_publications(self.source, self.target)
        with self.store._transaction() as db:
            db.execute('UPDATE publication_import_manifests SET body=?', (b'corrupt',))
        with self.assertRaises(PublicationError):
            migrate_file_publications(self.source, self.target)

    def test_corrupt_existing_import_counts_rejected(self):
        migrate_file_publications(self.source, self.target)
        with self.store._transaction() as db:
            db.execute('UPDATE publication_imports SET version_count=999')
        with self.assertRaises(PublicationError):
            migrate_file_publications(self.source, self.target)

    def test_wrong_pointer_or_quarantined_current_rejected(self):
        self.source._pointer(SCOPE.oid).write_text(json.dumps({'version': self.bad.version}))
        with self.assertRaises(PublicationError):
            migrate_file_publications(self.source, self.target)
        self.assertIsNone(self.target.current_version(SCOPE.oid))

    def test_corrupt_version_rejected(self):
        (self.source.root / 'versions' / (self.good.version + '.json')).write_text('{}')
        with self.assertRaises(PublicationError):
            migrate_file_publications(self.source, self.target)

    def test_missing_target_snapshot_rejected(self):
        target = TaskPublicationRepository(self.store, SnapshotStore(self.root / 'empty'))
        with self.assertRaises(PublicationError):
            migrate_file_publications(self.source, target)
        self.assertIsNone(self.target.current_version(SCOPE.oid))

    def test_snapshot_disappearing_before_transaction_rolls_back(self):
        original = self.store._transaction
        @contextmanager
        def remove_snapshot_then_begin(*, write=True):
            self.snapshots.path_of(make_snapshot().sha256).unlink()
            with original(write=write) as db:
                yield db
        with patch.object(self.store, '_transaction', side_effect=remove_snapshot_then_begin):
            with self.assertRaises(PublicationError):
                migrate_file_publications(self.source, self.target)
        with original(write=False) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM publication_versions').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM publication_current').fetchone()[0], 0)

    def test_manifest_covers_full_audit_record(self):
        first = migrate_file_publications(self.source, self.target)
        path = self.source.root / 'versions' / (self.good.version + '.json')
        record = self.source.read_record(self.good.version)
        record['requestId'] = 'different-audit-request'
        self.assertEqual(publication_version(record), self.good.version)
        path.write_bytes(canonical_json(record))
        other = TaskPublicationRepository(TaskStore(self.root / 'other.db'), self.snapshots)
        second = migrate_file_publications(self.source, other)
        self.assertNotEqual(first['manifest'], second['manifest'])

    def test_both_repositories_reject_quarantined_current(self):
        migrate_file_publications(self.source, self.target)
        self.source._pointer(SCOPE.oid).write_bytes(canonical_json({'version': self.bad.version}))
        with self.store._transaction() as db:
            db.execute('UPDATE publication_current SET version=? WHERE oid=?', (self.bad.version, SCOPE.oid))
        for repository in (self.source, self.target):
            with self.subTest(repository=type(repository).__name__), self.assertRaises(PublicationError):
                repository.load(SCOPE.oid)

    def test_both_repositories_reject_missing_current_snapshot(self):
        migrate_file_publications(self.source, self.target)
        self.snapshots.path_of(make_snapshot().sha256).unlink()
        for repository in (self.source, self.target):
            with self.subTest(repository=type(repository).__name__), self.assertRaises(PublicationError):
                repository.load(SCOPE.oid)

    def test_conflicting_current_rolls_back_import(self):
        with self.store._transaction() as db:
            db.execute('INSERT INTO publication_versions VALUES(?,?,?)',
                       (self.bad.version, SCOPE.oid, canonical_json(self.source.read_record(self.bad.version))))
            db.execute('INSERT INTO publication_current VALUES(?,?)', (SCOPE.oid, self.bad.version))
        with self.assertRaises(PublicationError):
            migrate_file_publications(self.source, self.target)
        with self.store._transaction(write=False) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM publication_versions').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT version FROM publication_current').fetchone()[0], self.bad.version)

    def test_failed_pointer_insert_rolls_back_records(self):
        with self.store._transaction() as db:
            db.execute("CREATE TRIGGER reject_import BEFORE INSERT ON publication_current BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(TaskStorageError):
            migrate_file_publications(self.source, self.target)
        with self.store._transaction(write=False) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM publication_versions').fetchone()[0], 0)

    def test_source_writer_lock_excludes_import(self):
        with self.source._writer(), self.assertRaises(PublicationError):
            migrate_file_publications(self.source, self.target)

    def test_records_without_current_are_preserved(self):
        self.source._pointer(SCOPE.oid).unlink()
        self.assertEqual(migrate_file_publications(self.source, self.target)['current'], 0)
        self.assertEqual(self.target.read_record(self.good.version)['status'], 'published')
        self.assertIsNone(self.target.load(SCOPE.oid))

    def test_extra_target_pointer_rejected_if_source_removed_pointer(self):
        migrate_file_publications(self.source, self.target)
        self.source._pointer(SCOPE.oid).unlink()
        with self.assertRaises(PublicationError):
            migrate_file_publications(self.source, self.target)
        self.assertEqual(self.target.current_version(SCOPE.oid), self.good.version)

    def test_process_death_rolls_back_sqlite_and_leaves_source_lock(self):
        script = '''
import os, sys
from contextlib import contextmanager
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from yantu.acquisition.publications import PublicationRepository
from yantu.acquisition.store import SnapshotStore
from yantu.tasks.store import TaskStore
from yantu.tasks.publications import TaskPublicationRepository
from yantu.tasks.publication_migration import migrate_file_publications
root = Path(sys.argv[1])
snapshots = SnapshotStore(root / 'snapshots')
store = TaskStore(root / 'target.db')
original = store._transaction
@contextmanager
def die_during_pointer(*, write=True):
    with original(write=write) as connection:
        def on_statement(statement):
            if statement.startswith('INSERT INTO publication_current'):
                os._exit(73)
        connection.set_trace_callback(on_statement)
        yield connection
store._transaction = die_during_pointer
migrate_file_publications(PublicationRepository(root / 'source', snapshots), TaskPublicationRepository(store, snapshots))
'''
        src = Path(__file__).resolve().parents[1] / 'src'
        child = subprocess.run([sys.executable, '-c', script, str(self.root), str(src)],
                               capture_output=True, timeout=20)
        self.assertEqual(child.returncode, 73, child.stderr.decode(errors='replace'))
        with closing(sqlite3.connect(self.root / 'target.db')) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM publication_versions').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT count(*) FROM publication_current').fetchone()[0], 0)
        lock = self.source.root / 'writer.lock'
        self.assertTrue(lock.is_file())
        with self.assertRaises(PublicationError):
            migrate_file_publications(self.source, self.target)
        # 子进程已结束，且锁只位于当前测试创建的临时目录。
        lock.unlink()
        self.assertEqual(migrate_file_publications(self.source, self.target)['current'], 1)
