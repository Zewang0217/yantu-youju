"""真实冻结通知的适用矩阵与结构反例；篡改输入均为明确的合成反例。"""

import copy
import json
import importlib.util
import tempfile
import unittest
import subprocess
import sys
from datetime import datetime
from dataclasses import replace
from pathlib import Path

from yantu.acquisition.applicability_service import inspect_applicability
from yantu.acquisition.document_bundle import DocumentBundleCollector, DocumentSeed
from yantu.acquisition.fetcher import FetchError, Snapshot
from yantu.acquisition.notice_scope import resolve_notice_scope
from yantu.acquisition.notice_structure import notice_structure
from yantu.acquisition.parsers import parse_snapshot
from yantu.acquisition.policy_applicability import ApplicabilityTarget
from yantu.acquisition.policy_inventory import inventory_document
from yantu.acquisition.store import SnapshotStore
from yantu.acquisition.source_evidence import EvidenceResolver
from yantu.acquisition.html_text import html_title
from yantu.evaluation.policy_regression import load_source

ROOT = Path(__file__).resolve().parents[1]
PDF_REQUIRED = unittest.skipUnless(importlib.util.find_spec('pypdf') is not None, '冻结PDF回归需要pypdf，跳过不计通过')


def frozen_sources():
    return {
        'rules': load_source(ROOT / 'data/pilot/trusted_policy', {'file': 'ecnu_rules.html', 'metadata': 'ecnu_rules_source.json'}),
        'entry': load_source(ROOT / 'data/pilot/ecnu_documents', {'file': 'prenotice.html', 'metadata': 'prenotice_html_source.json'}),
        'pre': load_source(ROOT / 'data/pilot/ecnu_documents', {'file': 'prenotice.pdf', 'metadata': 'prenotice_pdf_source.json'}),
    }


class TestNativeNoticeStructure(unittest.TestCase):
    def setUp(self):
        self.source = frozen_sources()['rules']

    def structure(self, mutate=None):
        snapshot = self.source if mutate is None else replace(self.source, body=mutate(self.source.body.decode()).encode())
        parsed = parse_snapshot(snapshot)
        return notice_structure(snapshot, parsed)

    def test_real_table_preserves_five_program_rows_and_degrees(self):
        structure = self.structure()
        self.assertEqual(len(structure['programRows']), 5)
        self.assertEqual([(row['baseCode'], row['directionCode'], row['degree']) for row in structure['programRows']],
            [('083500', None, 'direct_doctorate'), ('085405', '01', 'masters'), ('085405', '02', 'masters'),
             ('085405', '03', 'masters'), ('085405', '68', 'masters')])
        self.assertEqual(structure['issues'], [])

    def test_merged_cells_are_not_interpreted_as_flat_rows(self):
        structure = self.structure(lambda text: text.replace('<td width="61"', '<td rowspan="2" width="61"', 1))
        self.assertEqual(structure['programRows'], [])
        self.assertIn('MERGED_CELLS', structure['issues'])

    def test_invisible_table_cells_do_not_authorize_a_program(self):
        structure = self.structure(lambda text: text.replace('<table cellspacing', '<table hidden cellspacing', 1))
        self.assertEqual(structure['programRows'], [])

    def test_changed_header_is_unresolved(self):
        structure = self.structure(lambda text: text.replace('推免招生计划</span>', '统招招生计划</span>', 1))
        self.assertEqual(structure['programRows'], [])
        self.assertIn('UNSUPPORTED_TABLE_HEADER', structure['issues'])

    def test_conflicting_titles_do_not_select_first_identity(self):
        snapshot = replace(self.source, body=self.source.body.replace(b'</title>', '</title><title>其他大学软件学院2028年推免生综合考核实施细则</title>'.encode(), 1))
        parsed = parse_snapshot(snapshot)
        structure = notice_structure(snapshot, parsed)
        scope = resolve_notice_scope(inventory_document(snapshot, parsed), structure)
        self.assertEqual(parsed.title, '')
        self.assertNotIn('html_title', structure['views'])
        self.assertEqual(scope['status'], 'unresolved')

    def test_commented_and_script_title_tags_do_not_change_actual_title(self):
        html = '<!-- <title>伪标题</title> --><script>const x = "<title>脚本伪标题</title>";</script><title>真实标题</title>'
        self.assertEqual(html_title(html), '真实标题')

    def test_unclosed_title_does_not_authorize_identity(self):
        self.assertEqual(html_title('<title>真实标题'), '')

    def test_only_unique_h1_is_a_title_fallback(self):
        self.assertEqual(html_title('<h1>标题一</h1><h1>标题二</h1>'), '')
        self.assertEqual(html_title('<h1>唯一标题</h1>'), '唯一标题')

    def test_hidden_title_still_makes_metadata_identity_ambiguous(self):
        self.assertEqual(html_title('<title hidden>冲突标题</title><title>真实标题</title>'), '')

    def test_every_structure_claim_has_its_own_snapshot_view(self):
        structure = self.structure()
        for view in structure['views'].values():
            self.assertEqual(view['snapshotSha256'], self.source.sha256)
            for claim in view['claims']:
                self.assertEqual(claim['quote'], view['text'][claim['start']:claim['end']])

    def test_added_program_text_outside_table_blocks_scope(self):
        snapshot = replace(self.source, body=self.source.body.replace('二、报名条件'.encode(), '另含其他专业。二、报名条件'.encode()))
        parsed = parse_snapshot(snapshot)
        scope = resolve_notice_scope(inventory_document(snapshot, parsed), notice_structure(snapshot, parsed))
        self.assertEqual(scope['status'], 'unresolved')

    def test_program_name_qualifiers_are_not_treated_as_neutral_names(self):
        for qualifier in ('仅限直博', '仅限专项'):
            with self.subTest(qualifier=qualifier):
                structure = self.structure(lambda text: text.replace('085405-02 &nbsp;软件工程', f'085405-02 &nbsp;软件工程（{qualifier}）'))
                self.assertIn('UNSUPPORTED_PROGRAM_LABEL', structure['issues'])

    def test_plan_column_qualifiers_and_zero_do_not_authorize_membership(self):
        for plan in ('不招收推免生', '仅限直博', '约3（仅限专项）', '0'):
            with self.subTest(plan=plan):
                structure = self.structure(lambda text: text.replace('约3</span>', f'{plan}</span>'))
                self.assertIn('UNSUPPORTED_PROGRAM_PLAN', structure['issues'])

    def test_uninterpreted_parser_issue_blocks_natural_scope(self):
        parsed = replace(parse_snapshot(self.source), issues=('link_budget_exhausted',))
        row = inventory_document(self.source, parsed)
        scope = resolve_notice_scope(row, notice_structure(self.source, parsed))
        self.assertEqual(scope['status'], 'unresolved')
        self.assertIn('UNINTERPRETED_SOURCE_STRUCTURE', scope['blockers'])


class TestUnreadableNoticeSources(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name)
        self.url = 'https://unit.example/good.html'
        self.bad_url = 'https://unit.example/bad.html'

    def bundle(self):
        sources = {
            self.url: Snapshot(self.url, datetime(2026, 11, 6), 200, 'text/html', '<title>通知</title><article>2026-07-01 一、报名条件 1. 可以报名。</article>'.encode(), self.url),
            self.bad_url: Snapshot(self.bad_url, datetime(2026, 11, 6), 200, 'text/html', b'\xff', self.bad_url),
        }
        collector = DocumentBundleCollector(SnapshotStore(self.output / 'snapshots'), allowed_hosts=('unit.example',),
                                           fetcher=lambda url, **kwargs: sources[url])
        bundle = collector.collect((DocumentSeed(self.url, 'policy'), DocumentSeed(self.bad_url, 'policy')), request_id='unreadable-source-test')
        path = self.output / 'bundle.json'
        bundle.save(path)
        return path

    def test_failed_decode_stays_in_source_and_field_denominators(self):
        report = inspect_applicability(self.bundle())
        self.assertEqual(report['counts']['reportedSources'], 2)
        row = next(row for row in report['results'] if row['url'] == self.bad_url)
        self.assertTrue(all(field['status'] == 'source_unreadable' for field in row['fields'].values()))
        self.assertEqual(row['applicabilityReview']['status'], 'not_attempted')
        self.assertIn('SOURCE_UNREADABLE', row['noticeStructure']['issues'])

    def test_cli_records_partial_unreadable_input_without_aborting_other_sources(self):
        path = self.bundle()
        process = subprocess.run([sys.executable, 'scripts/inspect_policy_applicability.py', str(path)], cwd=ROOT,
                                 capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(process.returncode, 0, process.stderr)
        report = json.loads(path.with_name('policy_applicability.json').read_text(encoding='utf-8'))
        self.assertEqual(report['counts']['reportedSources'], 2)
        self.assertEqual(report['inputBundleStatus'], 'partial')


@PDF_REQUIRED
class TestRealNoticeScopeMatrix(unittest.TestCase):
    def setUp(self):
        self.sources = frozen_sources()
        self.target = ApplicabilityTarget(2027, '华东师范大学软件工程学院', '085405', '02', 'masters', 'pre_registration')

    def report(self, target=None, mutate_rules=None, missing_pre=False, without_target=False):
        sources = dict(self.sources)
        if mutate_rules:
            original = sources['rules']
            sources['rules'] = replace(original, body=mutate_rules(original.body.decode()).encode())
        by_url = {snapshot.url: snapshot for snapshot in sources.values()}

        def fetch(url, **kwargs):
            if url not in by_url or (missing_pre and url == sources['pre'].url):
                raise FetchError(url, 'synthetic missing source')
            return by_url[url]

        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            bundle = DocumentBundleCollector(SnapshotStore(output / 'snapshots'), allowed_hosts=('sei.ecnu.edu.cn',), fetcher=fetch).collect(
                (DocumentSeed(sources['rules'].url, 'assessment_policy'),), request_id='real-scope-test')
            path = output / 'bundle.json'
            bundle.save(path)
            return inspect_applicability(path, target=None if without_target else self.target if target is None else target)

    def source_row(self, report, name):
        return next(row for row in report['results'] if row['url'] == self.sources[name].url)

    def binding(self, report, source_name, kind):
        return next(row for row in self.source_row(report, source_name)['applicabilityReview']['targetBindings'] if row.get('kind') == kind)

    def test_real_preliminary_expected_qualification_is_applicable(self):
        report = self.report()
        binding = self.binding(report, 'pre', 'qualification')
        self.assertEqual(binding['status'], 'applicable')
        self.assertEqual(binding['wording'], 'acquired_or_expected')
        self.assertEqual(binding['sourceScope']['enrollment_year'], 2027)
        self.assertEqual(binding['sourceScope']['stage'], 'pre_registration')
        self.assertEqual(report['publicationStatus'], 'not_published')
        self.assertEqual(report['finalDirectoryStatus'], 'unresolved')

    def test_later_acquired_condition_is_not_assumed_to_replace_preliminary(self):
        binding = self.binding(self.report(), 'rules', 'qualification')
        self.assertEqual(binding['status'], 'unresolved')
        self.assertEqual(binding['wording'], 'acquired')
        self.assertEqual(binding['reason'], 'ASSESSMENT_CONDITION_OTHER_STAGE_UNPROVEN')

    def test_real_assessment_qualification_and_details_are_applicable(self):
        report = self.report(target=replace(self.target, stage='assessment'))
        self.assertEqual(self.binding(report, 'rules', 'qualification')['status'], 'applicable')
        self.assertEqual(self.binding(report, 'rules', 'assessment_details')['status'], 'applicable')
        self.assertEqual(self.binding(report, 'pre', 'qualification')['status'], 'not_applicable')

    def test_real_formal_procedure_requires_acquired_not_expected(self):
        report = self.report(target=replace(self.target, stage='formal_admission'))
        self.assertEqual(self.binding(report, 'pre', 'formal_qualification')['status'], 'applicable')
        self.assertEqual(self.binding(report, 'pre', 'formal_qualification')['wording'], 'acquired')
        self.assertEqual(self.binding(report, 'pre', 'qualification')['status'], 'not_applicable')
        self.assertEqual(self.binding(report, 'rules', 'formal_procedure')['status'], 'applicable')

    def test_real_late_channel_remains_conditional_and_does_not_prove_open(self):
        report = self.report(target=replace(self.target, stage='late_application'))
        binding = self.binding(report, 'pre', 'conditional_direct_application')
        self.assertEqual(binding['status'], 'applicable')
        self.assertTrue(binding['conditional'])
        self.assertEqual(binding['currentAvailability'], 'not_evaluated')

    def test_real_02_joint_restriction_does_not_apply_to_68(self):
        report = self.report(target=replace(self.target, direction_code='68'))
        binding = self.binding(report, 'pre', 'joint_program_restriction')
        self.assertEqual(binding['status'], 'not_applicable')
        self.assertEqual(binding['mismatchedAxes'], ['direction_code'])
        self.assertEqual(self.binding(report, 'pre', 'qualification')['status'], 'applicable')

    def test_real_masters_and_doctoral_forms_have_separate_scope(self):
        report = self.report(target=replace(self.target, base_code='083500', direction_code=None, degree='direct_doctorate'))
        forms = [row for row in self.source_row(report, 'pre')['applicabilityReview']['targetBindings'] if row['kind'] == 'degree_form_requirement']
        self.assertEqual([row['status'] for row in forms], ['applicable', 'not_applicable'])
        self.assertEqual(self.binding(report, 'pre', 'qualification')['status'], 'applicable')

    def test_explicit_wrong_year_and_unit_are_not_applicable(self):
        for target in (replace(self.target, enrollment_year=2028), replace(self.target, unit='其他大学软件学院')):
            with self.subTest(target=target):
                report = self.report(target=target)
                self.assertEqual(self.binding(report, 'pre', 'qualification')['status'], 'not_applicable')

    def test_unannounced_program_is_not_filled_from_target(self):
        binding = self.binding(self.report(target=replace(self.target, direction_code='99')), 'pre', 'qualification')
        self.assertEqual(binding['status'], 'unresolved')
        self.assertEqual(binding['reason'], 'PROGRAM_MEMBERSHIP_UNPROVEN')

    def test_different_year_catalog_is_not_reused(self):
        report = self.report(mutate_rules=lambda text: text.replace('2027年推免生综合考核实施细则</title>', '2028年推免生综合考核实施细则</title>'))
        self.assertEqual(self.binding(report, 'pre', 'qualification')['status'], 'unresolved')

    def test_new_global_revocation_blocks_real_natural_binding(self):
        report = self.report(target=replace(self.target, stage='assessment'), mutate_rules=lambda text: text.replace('五、其他说明', '五、其他说明：以上所有条款已废止。'))
        row = self.source_row(report, 'rules')
        self.assertEqual(row['noticeScope']['status'], 'unresolved')
        self.assertTrue(all(binding['status'] == 'unresolved' for binding in row['applicabilityReview']['targetBindings']))

    def test_new_global_restriction_in_known_sibling_section_is_not_ignored(self):
        report = self.report(target=replace(self.target, stage='assessment'), mutate_rules=lambda text: text.replace('1. 推免生须保证提交材料真实', '1. 本通知仅限直博，硕士不适用。推免生须保证提交材料真实'))
        self.assertEqual(self.source_row(report, 'rules')['noticeScope']['status'], 'unresolved')
        self.assertTrue(all(binding['status'] == 'unresolved' for binding in self.source_row(report, 'rules')['applicabilityReview']['targetBindings']))

    def test_table_local_qualifiers_block_master_assessment_authorization(self):
        for mutate in (lambda text: text.replace('085405-02 &nbsp;软件工程', '085405-02 &nbsp;软件工程（仅限直博）'),
                       lambda text: text.replace('约3</span>', '不招收推免生</span>')):
            with self.subTest(mutate=mutate):
                report = self.report(target=replace(self.target, stage='assessment'), mutate_rules=mutate)
                row = self.source_row(report, 'rules')
                self.assertEqual(row['noticeScope']['status'], 'unresolved')
                self.assertTrue(all(binding['status'] == 'unresolved' for binding in row['applicabilityReview']['targetBindings']))

    def test_unresolved_body_image_blocks_natural_scope_and_catalog_reuse(self):
        report = self.report(target=replace(self.target, stage='assessment'), mutate_rules=lambda text: '<img src="/scope-limits.png"/>一、招生学科专业'.join(text.rsplit('一、招生学科专业', 1)))
        row = self.source_row(report, 'rules')
        self.assertIn('UNRESOLVED_BODY_CARRIER', row['noticeStructure']['issues'])
        self.assertEqual(row['noticeScope']['status'], 'unresolved')
        self.assertEqual(report['declaredPrograms'], [])
        self.assertTrue(all(binding['status'] == 'unresolved' for binding in row['applicabilityReview']['targetBindings']))

    def test_missing_pdf_stays_in_source_and_field_denominator(self):
        report = self.report(missing_pre=True)
        self.assertEqual(report['counts']['reportedSources'], 3)
        row = self.source_row(report, 'pre')
        self.assertEqual(row['applicabilityReview']['status'], 'not_attempted')
        self.assertEqual(row['noticeScope']['status'], 'not_attempted')

    def test_empty_html_does_not_inherit_pdf_identity_or_conditions(self):
        row = self.source_row(self.report(), 'entry')
        self.assertEqual(row['noticeScope']['status'], 'unresolved')
        self.assertEqual(row['applicabilityReview']['targetBindings'], [])

    def test_cross_source_catalog_reference_is_evidence_not_copied_conditions(self):
        report = self.report()
        binding = self.binding(report, 'pre', 'qualification')
        self.assertTrue(any(ref['view'].startswith('html_program_table') for ref in binding['supportingEvidence']))
        self.assertEqual(binding['wording'], 'acquired_or_expected')
        self.assertEqual(report['crossSourceReview'][0]['mergeDecision'], 'blocked')

    def test_no_target_still_resolves_source_scopes_without_inventing_query(self):
        report = self.report(without_target=True)
        sources = report['results']
        self.assertEqual([row['noticeScope']['status'] for row in sources], ['supported_notice_scope', 'unresolved', 'supported_notice_scope'])
        self.assertIsNone(report['target'])
        self.assertTrue(all(not row['applicabilityReview']['targetBindings'] for row in sources))

    def test_scope_resolution_does_not_mutate_inventory(self):
        snap = self.sources['rules']
        parsed = parse_snapshot(snap)
        row = inventory_document(snap, parsed)
        before = copy.deepcopy(row)
        resolve_notice_scope(row, notice_structure(snap, parsed))
        self.assertEqual(row, before)

    def test_all_cross_view_references_resolve_within_their_recorded_sources(self):
        report = self.report()
        resolver = EvidenceResolver(report['results'])
        for row in report['results']:
            for binding in row['applicabilityReview']['targetBindings']:
                for ref in binding.get('supportingEvidence', ()):
                    claim = resolver.resolve(ref)
                    self.assertTrue(claim['quote'])

    def test_reference_cannot_use_body_path_in_title_view(self):
        report = self.report()
        resolver = EvidenceResolver(report['results'])
        with self.assertRaises(ValueError):
            resolver.resolve({'sourceIndex': 0, 'view': 'html_title', 'path': 'conditions.sections[0]'})

    def test_structure_view_cannot_claim_another_snapshot(self):
        report = self.report()
        report['results'][0]['noticeStructure']['views']['html_title']['snapshotSha256'] = '0' * 64
        with self.assertRaises(ValueError):
            EvidenceResolver(report['results']).resolve({'sourceIndex': 0, 'view': 'html_title', 'path': 'claims[0]'})

    def test_invalid_source_index_is_not_accepted_as_a_boolean_or_negative_index(self):
        resolver = EvidenceResolver(self.report()['results'])
        for index in (True, -1, 99):
            with self.subTest(index=index):
                with self.assertRaises(ValueError):
                    resolver.resolve({'sourceIndex': index, 'view': 'html_title', 'path': 'claims[0]'})

    def test_modified_view_text_is_rejected(self):
        report = self.report()
        report['results'][0]['noticeStructure']['views']['html_title']['text'] += '篡改'
        with self.assertRaises(ValueError):
            EvidenceResolver(report['results']).resolve({'sourceIndex': 0, 'view': 'html_title', 'path': 'claims[0]'})
