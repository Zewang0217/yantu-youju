"""迁移与只读入口的真实子进程检查；不触碰现有资料库。"""

import json
import os
import sqlite3
from contextlib import closing
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_trusted_pipeline import SCOPE, make_snapshot
from yantu.acquisition.publications import PublicationRepository
from yantu.acquisition.store import SnapshotStore
from yantu.acquisition.trusted_pipeline import ConservativeExtractor, TrustedPolicyPipeline

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'publication_library.py'


class PublicationLibraryCliTests(unittest.TestCase):
    def invoke(self, *args):
        env = os.environ.copy()
        env.pop('PYTHONPATH', None)
        env['PYTHONIOENCODING'] = 'utf-8'
        return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], cwd=ROOT,
                              env=env, capture_output=True, text=True, encoding='utf-8')

    def test_migrate_then_read_in_fresh_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshots = SnapshotStore(root / 'snapshots')
            source = PublicationRepository(root / 'facts', snapshots)
            outcome = TrustedPolicyPipeline(snapshots, source, ConservativeExtractor()).run(
                make_snapshot(), SCOPE, request_id='cli-test')
            args = ('--db', root / 'tasks.sqlite', '--snapshots', root / 'snapshots')
            for _ in range(2):
                migrated = self.invoke('migrate', '--source', root / 'facts', *args)
                self.assertEqual(migrated.returncode, 0, migrated.stderr)
                self.assertEqual(json.loads(migrated.stdout)['current'], 1)
            shown = self.invoke('show', *args, '--oid', SCOPE.oid)
            self.assertEqual(shown.returncode, 0, shown.stderr)
            result = json.loads(shown.stdout)
            self.assertEqual(result['version'], outcome.version)
            self.assertEqual(result['record']['oid'], SCOPE.oid)

    def test_show_and_migrate_reject_unrelated_sqlite_without_modification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / 'unrelated.sqlite'
            with closing(sqlite3.connect(database)) as connection:
                connection.execute('CREATE TABLE unrelated (value TEXT)')
                connection.execute("INSERT INTO unrelated VALUES ('kept')")
                connection.commit()
            before = database.read_bytes()
            (root / 'snapshots').mkdir()
            (root / 'facts').mkdir()
            args = ('--db', database, '--snapshots', root / 'snapshots')
            for command in (('show', *args, '--oid', 'sample'),
                            ('migrate', '--source', root / 'facts', *args)):
                result = self.invoke(*command)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(database.read_bytes(), before)
                with closing(sqlite3.connect(database)) as connection:
                    self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 0)
                    self.assertEqual(connection.execute('SELECT value FROM unrelated').fetchone()[0], 'kept')


if __name__ == '__main__':
    unittest.main()
