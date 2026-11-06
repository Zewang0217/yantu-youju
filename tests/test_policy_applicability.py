"""分源适用性表述与绑定阻断回归；不联网、不读取模型配置。"""

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from yantu.acquisition.applicability_service import inspect_applicability
from yantu.acquisition.document_bundle import DocumentBudget, DocumentBundleCollector, DocumentSeed
from yantu.acquisition.fetcher import FetchError, Snapshot
from yantu.acquisition.parsers import ParserRegistry, parse_snapshot
from yantu.acquisition.pdf_parser import PdfTextParser
from yantu.acquisition.policy_applicability import ApplicabilityTarget, compare_qualification_wording, review_document_applicability
from yantu.acquisition.policy_inventory import inventory_document
from yantu.acquisition.store import SnapshotStore
from yantu.evaluation.policy_regression import load_source

ROOT = Path(__file__).resolve().parents[1]
URL = 'https://unit.example/policy'
ACQUIRED = '已获得就读学校推荐免试资格的优秀应届本科毕业生'
EXPECTED = '已获得或预计获得就读学校推荐免试资格的优秀应届本科毕业生'
PDF_REQUIRED = unittest.skipUnless(importlib.util.find_spec('pypdf') is not None, '真实PDF回归需要pypdf，跳过不计通过')


def source(body):
    return Snapshot(URL, datetime(2026, 11, 6), 200, 'text/html',
                    f'<html><title>通知</title><article>2026-07-01 {body}</article></html>'.encode(), URL)


def inventory(body):
    snap = source(body)
    return inventory_document(snap, parse_snapshot(snap))


def reviewed(body):
    row = inventory(body)
    row['applicabilityReview'] = review_document_applicability(row)
    return row


class TestApplicabilityWording(unittest.TestCase):
    def test_incomplete_and_decimal_direction_codes_remain_unknown(self):
        for code in ('085405（02', '085405（02.5）', '085405-02.5', '085405（021）'):
            with self.subTest(code=code):
                result = reviewed(f'<p>一、招生专业</p><p>{code}</p>')['applicabilityReview']
                self.assertTrue(all(row['directionCode'] is None for row in result['programs']))

    def test_local_wording_difference_is_not_cross_source_difference(self):
        row = reviewed(f'<p>一、报名条件</p><p>1. {ACQUIRED}。</p><p>2. {EXPECTED}。</p>')
        self.assertEqual(compare_qualification_wording([row]), [])
        self.assertIn('LOCAL_QUALIFICATION_WORDING_DIFFERS', row['applicabilityReview']['blockers'])

    def test_qualification_wordings_are_separate_and_cannot_authorize_binding(self):
        first = reviewed(f'<p>一、报名条件</p><p>1. {ACQUIRED}；</p>')
        second = reviewed(f'<p>一、报名条件</p><p>1. {EXPECTED}；</p>')
        differences = compare_qualification_wording([first, second])
        self.assertEqual(first['applicabilityReview']['qualification'][0]['wording'], 'acquired')
        self.assertEqual(second['applicabilityReview']['qualification'][0]['wording'], 'acquired_or_expected')
        self.assertEqual(differences[0]['mergeDecision'], 'blocked')
        self.assertEqual(differences[0]['replacementStatus'], 'not_established')
        self.assertEqual(first['applicabilityReview']['bindingDecision'], 'hold')
        self.assertEqual(first['applicabilityReview']['identityBinding']['enrollmentYear'], 'unresolved')

    def test_negated_quoted_and_revoked_qualification_sentences_are_not_supported(self):
        for text in (f'不要求{ACQUIRED}', f'“{ACQUIRED}”仅为示例', f'原要求{ACQUIRED}，现已废止',
                     f'{ACQUIRED}，但本次不适用', f'若非{ACQUIRED}也可申请'):
            with self.subTest(text=text):
                result = reviewed(f'<p>一、报名条件</p><p>1. {text}。</p>')['applicabilityReview']
                self.assertEqual(result['qualification'][0]['status'], 'unresolved')
                self.assertIsNone(result['qualification'][0]['wording'])

    def test_revoked_parent_cannot_activate_supported_local_wording(self):
        row = reviewed(f'<p>一、旧政策</p><p>以下已废止。</p><p>（一）报名条件</p><p>1. {ACQUIRED}。</p>')
        result = row['applicabilityReview']
        self.assertEqual(result['qualification'][0]['status'], 'supported_wording')
        self.assertTrue(result['qualification'][0]['contextRefs'])
        self.assertEqual(result['qualification'][0]['stageBinding'], 'unresolved')
        self.assertEqual(result['bindingDecision'], 'hold')
        self.assertIn('INVENTORY_GAPS_REQUIRE_REVIEW', result['blockers'])

    def test_same_source_two_wordings_keep_local_ambiguity(self):
        result = reviewed(f'<p>一、报名条件</p><p>1. {ACQUIRED}。</p><p>2. {EXPECTED}。</p>')['applicabilityReview']
        self.assertEqual(len(result['qualification']), 2)
        self.assertIn('LOCAL_QUALIFICATION_WORDING_DIFFERS', result['blockers'])

    def test_equal_wording_is_not_a_cross_source_equivalence_verdict(self):
        row = reviewed(f'<p>一、报名条件</p><p>1. {ACQUIRED}。</p>')
        self.assertEqual(compare_qualification_wording([row, copy.deepcopy(row)]), [])
        self.assertIn('CROSS_SOURCE_SCOPE_NOT_VERIFIED', row['applicabilityReview']['blockers'])

    def test_same_url_different_snapshots_remain_separate_observations(self):
        first = reviewed(f'<p>一、报名条件</p><p>1. {ACQUIRED}。</p>')
        second = reviewed(f'<p>一、报名条件</p><p>1. {EXPECTED}。</p>')
        self.assertEqual(first['url'], second['url'])
        self.assertNotEqual(first['snapshotSha256'], second['snapshotSha256'])
        differences = compare_qualification_wording([first, second])
        self.assertEqual([row['sourceIndex'] for row in differences[0]['observations']], [0, 1])

    def test_multiple_stages_and_conditional_late_application_do_not_imply_open(self):
        result = reviewed('<p>一、报名方式</p><p>预报名请使用校内系统。错过预报名的考生在系统开放后直接报名，是否再次复试待定。正式程序须确认待录取。</p>')['applicabilityReview']
        stages = set(result['stages'][0]['observedStages'])
        self.assertEqual(stages, {'pre_registration', 'assessment', 'formal_admission', 'late_application'})
        self.assertTrue(result['stages'][0]['conditional'])
        self.assertEqual(result['currentAvailability'], 'not_evaluated')
        self.assertIn('MULTIPLE_STAGE_USES_DO_NOT_PROPAGATE_DOCUMENT_STAGE', result['blockers'])

    def test_doctoral_parent_and_masters_text_keep_unresolved_local_scopes(self):
        result = reviewed('<p>一、仅限直博</p><p>（一）报名材料</p><p>1. 成绩单。</p><p>二、其他说明</p><p>硕士申请人填写研究领域。</p>')['applicabilityReview']
        self.assertTrue(any('direct_doctorate' in row['observedDegrees'] for row in result['degreeScopes']))
        self.assertTrue(any('masters' in row['observedDegrees'] for row in result['degreeScopes']))
        self.assertTrue(all(row['bindingStatus'] == 'unresolved' for row in result['degreeScopes']))

    def test_program_directions_are_not_merged_by_base_code_or_country(self):
        result = reviewed('<p>一、招生专业</p><p>085405（02 信息技术创新）与085405-68为不同方向，均涉及法国。</p>')['applicabilityReview']
        self.assertEqual({(row['baseCode'], row['directionCode']) for row in result['programs']}, {('085405', '02'), ('085405', '68')})
        self.assertTrue(all(row['relationshipStatus'] == 'unresolved' for row in result['programs']))

    def test_table_and_final_directory_dependency_block_binding(self):
        result = reviewed('<p>一、招生专业</p><table><tr><td>硕士</td><td>085405-68</td><td>1</td></tr></table><p>最终以专业目录为准。</p>')['applicabilityReview']
        # 紧邻单元格的文字层为085405-681；不能猜最后的1属于计划人数。
        self.assertIsNone(result['programs'][0]['directionCode'])
        self.assertEqual(result['programs'][0]['relationshipStatus'], 'unresolved')
        self.assertIn('INVENTORY_GAPS_REQUIRE_REVIEW', result['blockers'])
        self.assertIn('FINAL_PROGRAM_DIRECTORY_NOT_VERIFIED', result['blockers'])

    def test_input_inventory_is_not_mutated(self):
        row = inventory(f'<p>一、报名条件</p><p>1. {EXPECTED}。</p>')
        original = copy.deepcopy(row)
        review_document_applicability(row)
        self.assertEqual(row, original)


class TestApplicabilityApplication(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name)
        self.store = SnapshotStore(self.output / 'snapshots')

    def bundle(self, max_documents=1):
        snap = source(f'<p>一、报名条件</p><p>1. {EXPECTED}。</p><a href="/extra.pdf">政策文件</a>')

        def fetch(url, **kwargs):
            if url != URL:
                raise FetchError(url, 'unavailable')
            return snap

        bundle = DocumentBundleCollector(self.store, allowed_hosts=('unit.example',), fetcher=fetch,
                    budget=DocumentBudget(max_documents=max_documents)).collect((DocumentSeed(URL, 'policy'),), request_id='applicability-test')
        path = self.output / 'bundle.json'
        bundle.save(path)
        return path

    def test_budget_unexecuted_source_stays_in_denominator(self):
        report = inspect_applicability(self.bundle())
        self.assertEqual(report['counts']['reportedSources'], 2)
        self.assertEqual(report['results'][1]['applicabilityReview']['status'], 'not_attempted')
        self.assertEqual(sum(report['counts']['applicability'].values()), 2)
        self.assertEqual(report['usage']['networkCalls'], 0)
        self.assertEqual(report['usage']['modelCalls'], 0)

    def test_failed_source_stays_in_denominator(self):
        report = inspect_applicability(self.bundle(max_documents=8))
        self.assertEqual(report['counts']['reportedSources'], 2)
        self.assertEqual(report['results'][1]['applicabilityReview']['status'], 'not_attempted')
        self.assertIn('SOURCE_NOT_USABLE', report['results'][1]['applicabilityReview']['blockers'])

    def test_service_rethrows_same_exception_without_message_leak(self):
        error = TypeError('harmless_applicability_marker')
        with self.assertLogs('yantu.acquisition.applicability_service', level='ERROR') as logs:
            with patch('yantu.acquisition.applicability_service.review_document_applicability', side_effect=error):
                with self.assertRaises(TypeError) as caught:
                    inspect_applicability(self.bundle())
        self.assertIs(caught.exception, error)
        self.assertNotIn('harmless_applicability_marker', '\n'.join(logs.output))
        self.assertIn('TypeError', '\n'.join(logs.output))

    def test_cli_writes_both_artifacts_and_never_publishes(self):
        path = self.bundle()
        process = subprocess.run([sys.executable, 'scripts/inspect_policy_applicability.py', str(path)],
                                 cwd=ROOT, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(process.returncode, 0, process.stderr)
        report = json.loads(path.with_name('policy_applicability.json').read_text(encoding='utf-8'))
        self.assertEqual(report['publicationStatus'], 'not_published')
        self.assertEqual(report['bindingDecision'], 'hold')
        self.assertTrue(path.with_name('policy_applicability.md').exists())

    def test_cli_failure_is_nonzero_without_exception_message(self):
        runner = '''import runpy, sys
from unittest.mock import patch
sys.argv = ['inspect_policy_applicability.py', 'unused.json']
with patch('yantu.acquisition.applicability_service.inspect_applicability', side_effect=TypeError('harmless_cli_marker')):
    runpy.run_path('scripts/inspect_policy_applicability.py', run_name='__main__')
'''
        result = subprocess.run([sys.executable, '-c', runner], cwd=ROOT, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('harmless_cli_marker', result.stderr)
        self.assertIn('TypeError', result.stderr)


class TestExplicitTargetBinding(unittest.TestCase):
    """显式标注的合成正向资料验证有限语法，不代表真实样本已完成机会绑定。"""

    def setUp(self):
        self.target = ApplicabilityTarget(2027, '华东师范大学软件工程学院', '085405', '02', 'masters', 'pre_registration')
        self.statement = ('本条仅适用于：入学年份：2027；培养单位：华东师范大学软件工程学院；'
                          '专业代码：085405；方向码：02；学位：硕士；招生阶段：预报名；'
                          f'适用条款：{EXPECTED}。')

    def review(self, statement=None, *, parent='', target=None):
        text = self.statement if statement is None else statement
        heading = '<p>（一）报名条件</p>' if parent else '<p>一、报名条件</p>'
        row = inventory(parent + heading + f'<p>1. {text}</p>')
        return review_document_applicability(row, self.target if target is None else target)

    def test_complete_source_scope_matches_target_for_this_clause_only(self):
        result = self.review()
        self.assertEqual(result['targetBindings'][0]['status'], 'applicable')
        self.assertEqual(result['targetBindings'][0]['sourceScope']['direction_code'], '02')
        self.assertEqual(result['bindingDecision'], 'hold')
        self.assertEqual(result['publicationStatus'], 'not_published')

    def test_cli_target_is_used_without_granting_publication(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            snap = source(f'<p>一、报名条件</p><p>1. {self.statement}</p>')
            collector = DocumentBundleCollector(SnapshotStore(output / 'snapshots'), allowed_hosts=('unit.example',),
                fetcher=lambda url, **kwargs: snap, budget=DocumentBudget(max_documents=1))
            bundle = collector.collect((DocumentSeed(URL, 'policy'),), request_id='explicit-target-test')
            path, target_path = output / 'bundle.json', output / 'target.json'
            bundle.save(path)
            target_path.write_text(json.dumps(asdict(self.target), ensure_ascii=False), encoding='utf-8')
            process = subprocess.run([sys.executable, 'scripts/inspect_policy_applicability.py', str(path), '--target', str(target_path)],
                                     cwd=ROOT, capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(process.returncode, 0, process.stderr)
            report = json.loads((output / 'policy_applicability.json').read_text(encoding='utf-8'))
            self.assertEqual(report['counts']['targetBindings']['applicable'], 1)
            self.assertEqual(report['target']['direction_code'], '02')
            self.assertEqual(report['publicationStatus'], 'not_published')
            self.assertEqual(report['bindingDecision'], 'hold')

    def test_each_explicit_scope_axis_can_exclude_this_clause(self):
        for change in ({'enrollment_year': 2028}, {'unit': '其他大学软件学院'}, {'base_code': '083500'},
                       {'direction_code': '68'}, {'degree': 'direct_doctorate'}, {'stage': 'formal_admission'}):
            with self.subTest(change=change):
                result = self.review(target=replace(self.target, **change))
                self.assertEqual(result['targetBindings'][0]['status'], 'not_applicable')
                self.assertEqual(result['targetBindings'][0]['mismatchedAxes'], list(change))

    def test_missing_scope_is_not_filled_from_target(self):
        result = self.review(EXPECTED + '。')
        self.assertEqual(result['targetBindings'][0]['status'], 'unresolved')
        self.assertIsNone(result['targetBindings'][0]['sourceScope'])

    def test_negated_or_quoted_scope_cannot_bind(self):
        for text in ('不适用：' + self.statement, '“' + self.statement + '”仅为举例', self.statement + '本次不执行。'):
            with self.subTest(text=text):
                self.assertEqual(self.review(text)['targetBindings'][0]['status'], 'unresolved')

    def test_revoked_or_unknown_parent_blocks_fully_scoped_clause(self):
        for parent in ('<p>一、旧政策</p><p>以下已废止。</p>', '<p>一、仅限专项</p>'):
            with self.subTest(parent=parent):
                result = self.review(parent=parent)
                self.assertEqual(result['targetBindings'][0]['status'], 'unresolved')

    def test_scope_does_not_propagate_to_next_numbered_clause(self):
        row = inventory(f'<p>一、报名条件</p><p>1. {self.statement}</p><p>2. {ACQUIRED}。</p>')
        result = review_document_applicability(row, self.target)
        self.assertEqual(result['targetBindings'][1]['status'], 'unresolved')
        self.assertIsNone(result['targetBindings'][1]['sourceScope'])

    def test_parse_gap_blocks_fully_scoped_clause(self):
        row = inventory(f'<p>一、报名条件</p><p>1. {self.statement}</p><a href="/missing.pdf">政策文件</a>')
        result = review_document_applicability(row, self.target)
        self.assertEqual(result['targetBindings'][0]['status'], 'unresolved')

    def test_invalid_target_values_are_rejected(self):
        for change in ({'enrollment_year': True}, {'direction_code': 2}, {'base_code': '85405'},
                       {'unit': ''}, {'unit': '未知'}, {'unit': '待确认'}, {'degree': 'unknown'}, {'stage': 'all'}):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    replace(self.target, **change)

    def test_later_global_revocation_or_degree_restriction_blocks_binding(self):
        for note in ('上述全部条款已废止，本通知不再执行。', '本通知仅限本科直博申请人，硕士申请人不适用。',
                     '本通知仅为示例，全部条款不适用。'):
            with self.subTest(note=note):
                row = inventory(f'<p>一、报名条件</p><p>1. {self.statement}</p><p>二、其他说明</p><p>1. {note}</p>')
                result = review_document_applicability(row, self.target)
                self.assertEqual(result['targetBindings'][0]['status'], 'unresolved')

    def test_source_unit_placeholder_is_not_a_complete_identity(self):
        result = self.review(self.statement.replace('华东师范大学软件工程学院', '未知'))
        self.assertEqual(result['targetBindings'][0]['status'], 'unresolved')
        self.assertEqual(result['targetBindings'][0]['reason'], 'SOURCE_IDENTITY_PLACEHOLDER')

    def test_real_source_without_explicit_complete_scope_stays_unknown_for_target(self):
        snap = load_source(ROOT / 'data/pilot/trusted_policy', {'file': 'ecnu_rules.html', 'metadata': 'ecnu_rules_source.json'})
        row = inventory_document(snap, parse_snapshot(snap))
        result = review_document_applicability(row, self.target)
        self.assertEqual(result['targetBindings'][0]['status'], 'unresolved')
        self.assertIsNone(result['targetBindings'][0]['sourceScope'])


class TestFrozenApplicabilityReference(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = {
            'rules': load_source(ROOT / 'data/pilot/trusted_policy', {'file': 'ecnu_rules.html', 'metadata': 'ecnu_rules_source.json'}),
            'entry': load_source(ROOT / 'data/pilot/ecnu_documents', {'file': 'prenotice.html', 'metadata': 'prenotice_html_source.json'}),
            'prenotice': load_source(ROOT / 'data/pilot/ecnu_documents', {'file': 'prenotice.pdf', 'metadata': 'prenotice_pdf_source.json'}),
        }

    def review(self, name, registry=None):
        snap = self.sources[name]
        parsed = parse_snapshot(snap) if registry is None else parse_snapshot(snap, registry)
        row = inventory_document(snap, parsed)
        row['applicabilityReview'] = review_document_applicability(row)
        return row

    @PDF_REQUIRED
    def test_real_qualification_difference_does_not_rewrite_either_source(self):
        rules, preliminary = self.review('rules'), self.review('prenotice')
        differences = compare_qualification_wording([rules, preliminary])
        self.assertEqual(rules['applicabilityReview']['qualification'][0]['wording'], 'acquired')
        self.assertEqual(preliminary['applicabilityReview']['qualification'][0]['wording'], 'acquired_or_expected')
        self.assertEqual(differences[0]['mergeDecision'], 'blocked')
        self.assertEqual(differences[0]['replacementStatus'], 'not_established')

    @PDF_REQUIRED
    def test_real_joint_restriction_keeps_02_separate_from_68(self):
        preliminary = self.review('prenotice')['applicabilityReview']
        restrictions = [row for row in preliminary['restrictions'] if row['status'] == 'supported_wording']
        self.assertEqual(len(restrictions), 1)
        self.assertEqual(restrictions[0]['program'], {'baseCode': '085405', 'directionCode': '02'})
        self.assertEqual(restrictions[0]['recommendationProject'], '法国蔚蓝海岸大学')
        self.assertEqual(restrictions[0]['otherProjectsStage'], 'unified_exam')
        rules = self.review('rules')['applicabilityReview']
        self.assertIn(('085405', '68'), {(row['baseCode'], row['directionCode']) for row in rules['programs']})
        self.assertFalse(any(row['status'] == 'supported_wording' for row in rules['restrictions']))

    def test_real_shell_does_not_borrow_pdf_scope(self):
        result = self.review('entry')['applicabilityReview']
        self.assertEqual(result['qualification'], [])
        self.assertEqual(result['programs'], [])
        self.assertIn('BODY_TEXT_MISSING', result['blockers'])

    @PDF_REQUIRED
    def test_truncated_pdf_keeps_identity_and_scope_unresolved(self):
        registry = ParserRegistry()
        registry.register(PdfTextParser(max_total_chars=180))
        result = self.review('prenotice', registry)['applicabilityReview']
        self.assertEqual(result['bindingDecision'], 'hold')
        self.assertTrue(all(row['status'] == 'unresolved' for row in result['qualification']))
        self.assertIn('SOURCE_GAPS_REQUIRE_REVIEW', result['blockers'])

    @PDF_REQUIRED
    def test_all_observation_paths_resolve_within_their_own_frozen_source(self):
        for name in self.sources:
            row = self.review(name)
            claims = {}

            def visit(value):
                if isinstance(value, dict):
                    if {'path', 'quote', 'start', 'end'} <= value.keys():
                        claims[value['path']] = value
                    for child in value.values():
                        visit(child)
                elif isinstance(value, list):
                    for child in value:
                        visit(child)

            visit(row)
            for group in ('stages', 'qualification', 'programs', 'restrictions', 'degreeScopes'):
                for finding in row['applicabilityReview'][group]:
                    for ref in [finding['evidenceRef'], *finding['contextRefs']]:
                        claim = claims[ref]
                        self.assertEqual(claim['quote'], row['text'][claim['start']:claim['end']])
