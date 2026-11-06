"""冻结官网资料的持久任务与CLI集成；任务终态与政策发布保持独立。"""

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from datetime import datetime

from yantu.acquisition.artifact_files import canonical_json
from yantu.acquisition.document_bundle import DocumentBundleCollector, DocumentSeed
from yantu.acquisition.fetcher import FetchError, Snapshot
from yantu.acquisition.policy_applicability import ApplicabilityTarget
from yantu.acquisition.store import SnapshotStore
from yantu.evaluation.policy_regression import load_source
from yantu.tasks.models import Budget, TaskConflict
from yantu.tasks.policy_workflow import policy_registry, submit_policy_task
from yantu.tasks.runner import TaskRunner
from yantu.tasks.store import TaskStore

ROOT = Path(__file__).resolve().parents[1]
PDF_REQUIRED = unittest.skipUnless(importlib.util.find_spec('pypdf') is not None, '真实PDF回归需要pypdf，跳过不计通过')


@PDF_REQUIRED
class TestPolicyTasks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name)
        self.db = self.output / 'tasks.sqlite3'
        self.store = TaskStore(self.db)
        self.target = ApplicabilityTarget(2027, '华东师范大学软件工程学院', '085405', '02', 'masters', 'pre_registration')
        definitions = (('trusted_policy', 'ecnu_rules.html', 'ecnu_rules_source.json'),
                       ('ecnu_documents', 'prenotice.html', 'prenotice_html_source.json'),
                       ('ecnu_documents', 'prenotice.pdf', 'prenotice_pdf_source.json'))
        sources = [load_source(ROOT / 'data/pilot' / folder, {'file': filename, 'metadata': metadata}) for folder, filename, metadata in definitions]
        by_url = {source.url: source for source in sources}

        def fetch(url, **kwargs):
            if url not in by_url:
                raise FetchError(url, 'fixture not provided')
            return by_url[url]

        self.snapshots = SnapshotStore(self.output / 'snapshots')
        self.bundle = DocumentBundleCollector(self.snapshots, allowed_hosts=('sei.ecnu.edu.cn',), fetcher=fetch).collect(
            (DocumentSeed(sources[0].url, 'assessment_policy'),), request_id='policy-task-test')
        self.bundle_path = self.output / 'bundle.json'
        self.bundle.save(self.bundle_path)

    def submit(self, **changes):
        values = dict(idempotency_key='frozen-policy', target=self.target, budget=Budget(tool_calls=2))
        values.update(changes)
        return submit_policy_task(self.store, self.bundle_path, **values)

    def cli(self, *arguments):
        return subprocess.run([sys.executable, 'scripts/policy_tasks.py', '--db', str(self.db), *arguments],
                              cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=30)

    def test_real_scope_workflow_resumes_checkpoint_and_preserves_partial_input(self):
        task = self.submit()
        first = TaskRunner(self.store, policy_registry()).run(task, max_steps=1)
        self.assertEqual(first['status'], 'queued')
        reopened = TaskStore(self.db)
        result = TaskRunner(reopened, policy_registry()).run(task)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual([step['attempts'] for step in result['steps']], [1, 1])
        self.assertEqual(result['budget']['used']['tool_calls'], 2)
        report = reopened.output(task, 'applicability')
        self.assertEqual(report['counts']['reportedSources'], 3)
        self.assertEqual(report['counts']['targetBindings'], {'applicable': 12, 'not_applicable': 8, 'unresolved': 1})
        self.assertEqual(report['publicationStatus'], 'not_published')
        self.assertEqual(report['bindingDecision'], 'hold')

    def test_business_scope_unknown_does_not_fail_a_completed_execution(self):
        directory = self.output / 'complete-input'
        url = 'https://unit.example/notice'
        source = Snapshot(url, datetime(2026, 11, 6), 200, 'text/html',
                          '<title>政策通知</title><article>2026-07-01 一、报名条件 1. 具体要求另行说明。</article>'.encode(), url)
        collector = DocumentBundleCollector(SnapshotStore(directory / 'snapshots'), allowed_hosts=('unit.example',),
                                           fetcher=lambda url, **kwargs: source)
        bundle = collector.collect((DocumentSeed(url, 'policy'),), request_id='business-unknown-test')
        path = directory / 'bundle.json'
        bundle.save(path)
        task = submit_policy_task(self.store, path, idempotency_key='business-unknown')
        result = TaskRunner(self.store, policy_registry()).run(task)
        self.assertEqual(result['status'], 'completed')
        report = self.store.output(task, 'applicability')
        self.assertEqual(report['finalDirectoryStatus'], 'unresolved')
        self.assertEqual(report['bindingDecision'], 'hold')

    def test_rerunning_terminal_workflow_does_not_consume_more_budget(self):
        task = self.submit()
        first = TaskRunner(self.store, policy_registry()).run(task)
        second = TaskRunner(TaskStore(self.db), policy_registry()).run(task)
        self.assertEqual(second['budget'], first['budget'])
        self.assertEqual(len(second['attempts']), 2)

    def test_changed_target_is_not_reused_with_same_key(self):
        self.submit()
        with self.assertRaises(TaskConflict):
            self.submit(target=replace(self.target, direction_code='68'))

    def test_changed_source_bytes_fail_without_reusing_cached_text(self):
        task = self.submit()
        TaskRunner(self.store, policy_registry()).run(task, max_steps=1)
        document = self.bundle.documents[0]
        self.snapshots.path_of(document.snapshot_sha256).write_bytes(b'damaged')
        result = TaskRunner(self.store, policy_registry()).run(task)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['reason'], 'SOURCE_INTEGRITY_FAILED')
        self.assertIsNone(self.store.output(task, 'applicability'))

    def test_recomputed_bundle_digest_does_not_replace_frozen_task_input(self):
        task = self.submit()
        record = json.loads(self.bundle_path.read_text(encoding='utf-8'))
        record['requestId'] = 'other-request'
        record.pop('recordDigest')
        record['recordDigest'] = hashlib.sha256(canonical_json(record)).hexdigest()
        self.bundle_path.write_bytes(canonical_json(record))
        result = TaskRunner(self.store, policy_registry()).run(task)
        self.assertEqual(result['reason'], 'INPUT_CHANGED')
        self.assertIsNone(self.store.output(task, 'applicability'))

    def test_missing_input_is_a_locatable_terminal_failure(self):
        task = self.submit()
        self.bundle_path.unlink()
        result = TaskRunner(self.store, policy_registry()).run(task)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['reason'], 'INPUT_MISSING')

    def test_budget_stop_does_not_export_manifest_as_final_report(self):
        task = self.submit(budget=Budget(tool_calls=1))
        result = TaskRunner(self.store, policy_registry()).run(task)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['reason'], 'BUDGET_EXHAUSTED')
        exported = self.cli('export', task)
        self.assertEqual(exported.returncode, 1)
        self.assertFalse((self.db.parent / 'exports' / f'{task}.json').exists())

    def test_cli_submit_resume_show_and_export_real_report(self):
        target_path = self.output / 'target.json'
        target_path.write_text(json.dumps({'enrollment_year': 2027, 'unit': self.target.unit, 'base_code': '085405',
            'direction_code': '02', 'degree': 'masters', 'stage': 'pre_registration'}, ensure_ascii=False), encoding='utf-8')
        submitted = self.cli('submit', str(self.bundle_path), '--key', 'cli-policy', '--target', str(target_path))
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        task = json.loads(submitted.stdout)['taskId']
        resumed = self.cli('resume', task)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(json.loads(resumed.stdout)['status'], 'partial')
        self.assertEqual(self.cli('show', task).returncode, 0)
        exported = self.cli('export', task)
        self.assertEqual(exported.returncode, 0, exported.stderr)
        report = json.loads(Path(json.loads(exported.stdout)['output']).read_text(encoding='utf-8'))
        self.assertEqual(report['counts']['reportedSources'], 3)
        self.assertEqual(report['publicationStatus'], 'not_published')

    def test_cli_cancel_keeps_task_queryable(self):
        task = self.submit()
        cancelled = self.cli('cancel', task)
        self.assertEqual(cancelled.returncode, 0)
        self.assertEqual(json.loads(cancelled.stdout)['status'], 'cancelled')
        self.assertEqual(json.loads(self.cli('show', task).stdout)['status'], 'cancelled')
