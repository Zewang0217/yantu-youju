"""候选字段清单的边界、失败分母及源证据测试；不调用网络或模型。"""

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from yantu.acquisition.artifact_files import canonical_json
from yantu.acquisition.document_bundle import DocumentBudget, DocumentBundleCollector, DocumentSeed
from yantu.acquisition.fetcher import FetchError, Snapshot
from yantu.acquisition.parse_models import ParseResult, ParseStatus
from yantu.acquisition.parsers import HtmlNoticeParser, ParserRegistry, parse_snapshot
from yantu.acquisition.policy_inventory import FIELD_NAMES, inventory_document
from yantu.acquisition.store import SnapshotStore, SnapshotStorageError
from yantu.acquisition.source_evidence import SourceClaim, claim_record, source_quality

ROOT = Path(__file__).resolve().parents[1]
URL = "https://unit.example/main"


def source(content):
    return Snapshot(URL, datetime(2026, 11, 5), 200, "text/html", f'<html><title>推免通知</title><body><article>2026-07-01 {content}</article></body></html>'.encode(), URL)


def inspect(content):
    snapshot = source(content)
    return inventory_document(snapshot, parse_snapshot(snapshot))


def all_claims(value):
    if isinstance(value, dict):
        if {"quote", "start", "end", "path"} <= value.keys():
            yield value
        for child in value.values():
            yield from all_claims(child)
    elif isinstance(value, list):
        for child in value:
            yield from all_claims(child)


class TestPolicyInventory(unittest.TestCase):
    def test_long_related_list_keeps_budget_gap(self):
        items = ' '.join(f'{number}. 请通过系统报名。' for number in range(1, 258))
        result = inspect(f'<p>一、其他说明</p><p>{items}</p>')
        self.assertIn('ITEM_BUDGET_EXCEEDED', result['fields']['channels']['issues'])
        self.assertEqual(result['fields']['channels']['status'], 'not_attempted')

    def test_long_primary_list_keeps_section_budget_gap(self):
        items = ' '.join(f'{number}. 申请条件。' for number in range(1, 258))
        result = inspect(f'<p>一、报名条件</p><p>{items}</p>')
        self.assertIn('ITEM_BUDGET_EXCEEDED', result['fields']['conditions']['sections'][0]['issues'])
        self.assertEqual(result['fields']['conditions']['status'], 'candidate_excerpt')

    def test_related_list_sequence_gap_is_visible(self):
        result = inspect('<p>一、其他说明</p><p>1. 系统报名。 3. 系统报名。</p>')
        self.assertIn('ITEM_SEQUENCE_UNCERTAIN', result['fields']['channels']['issues'])

    def test_revoked_parent_is_reachable_from_channel_excerpt(self):
        result = inspect('<p>一、其他说明</p><p>以下安排已废止。</p><p>（一）注意事项</p><p>1. 在旧系统报名。</p>')
        excerpt = result['fields']['channels']['relatedExcerpts'][0]
        claims = {claim['path']: claim for claim in all_claims(result)}
        contexts = [claims[path] for path in excerpt['contextRefs']]
        self.assertTrue(any('以下安排已废止' in claim['quote'] for claim in contexts))
        self.assertTrue(all(claim['quote'] == result['text'][claim['start']:claim['end']] for claim in contexts))

    def test_doctoral_parent_context_keeps_pdf_page_coordinates(self):
        from yantu.acquisition.parse_models import PageText
        text = '一、仅限直博\n以下仅限直博申请人。\n（二）报名材料\n1. 请提交证明。'
        split = text.index('（二）')
        pages = (PageText(1, text[:split], 0, split, 'text_extracted'),
                 PageText(2, text[split:], split, len(text), 'text_extracted'))
        snap = Snapshot(URL, datetime(2026, 11, 5), 200, 'application/pdf', b'fixture', URL)
        result = inventory_document(snap, ParseResult(ParseStatus.COMPLETE, text=text, pages=pages))
        section = result['fields']['materials']['sections'][0]
        claims = {claim['path']: claim for claim in all_claims(result)}
        contexts = [claims[path] for path in section['contextRefs']]
        self.assertTrue(any('仅限直博' in claim['quote'] and claim['pages'] == [1] for claim in contexts))
        self.assertEqual(section['claim']['pages'], [2])
        self.assertTrue(all(claim['quote'] == text[claim['start']:claim['end']] for claim in all_claims(result)))

    def test_mixed_headings_with_long_whitespace_stay_unclassified(self):
        headings = ('报名 条件及材料', '报名条件\n及材料', '报名条件' + '\n' * 80 + '及材料')
        for heading in headings:
            with self.subTest(heading=heading):
                result = inspect(f'<p>一、{heading}</p><p>1. 混合条款。</p>')
                self.assertTrue(result['unclassifiedSections'])
                self.assertFalse(result['fields']['conditions']['sections'])
                self.assertFalse(result['fields']['channels']['sections'])

    def test_mixed_heading_beyond_lookahead_in_raw_text_stays_unclassified(self):
        text = '一、报名条件' + '\n' * 80 + '及材料\n1. 混合条款。'
        parsed = ParseResult(ParseStatus.COMPLETE, content_text=text, content_scope='article')
        result = inventory_document(source('正文'), parsed)
        self.assertEqual(len(result['unclassifiedSections']), 1)
        self.assertFalse(result['fields']['conditions']['sections'])
        self.assertFalse(result['fields']['channels']['sections'])

    def test_unclassified_claim_path_matches_its_output_index(self):
        result = inspect('<p>一、报名条件</p><p>1. 条件。</p><p>二、自定义政策</p><p>其他原文。</p>')
        claim = result['unclassifiedSections'][0]['claim']
        self.assertEqual(claim['path'], 'unclassifiedSections[0]')

    def test_custom_parser_does_not_inherit_parent_manifest(self):
        class CustomParser(HtmlNoticeParser):
            def parse(self, snapshot):
                return ParseResult(ParseStatus.PARTIAL, content_text='自定义正文')
        registry = ParserRegistry()
        registry.register(CustomParser())
        manifest = parse_snapshot(source('正文'), registry).parser_info
        self.assertTrue(manifest['identity'].endswith('CustomParser'))
        self.assertIsNone(manifest['version'])
        self.assertEqual(manifest['configurationStatus'], 'not_reported')

    def test_explicit_custom_parser_manifest_is_preserved(self):
        class ConfiguredParser(HtmlNoticeParser):
            def manifest(self):
                return {'identity': 'custom', 'version': 'test-v1', 'configuration': {'limit': 5}, 'configurationStatus': 'reported'}
        registry = ParserRegistry()
        registry.register(ConfiguredParser())
        manifest = parse_snapshot(source('正文'), registry).parser_info
        self.assertEqual(manifest['version'], 'test-v1')
        self.assertEqual(manifest['configuration'], {'limit': 5})
        self.assertTrue(manifest['adapterClass'].endswith('ConfiguredParser'))

    def test_fixed_field_denominator_never_claims_verified(self):
        result = inspect('<p>一、报名条件</p><p>1. 可以申请。</p>')
        self.assertEqual(set(result["fields"]), set(FIELD_NAMES))
        self.assertEqual(result["publicationStatus"], "not_published")
        self.assertEqual((result["applicability"], result["coverage"]), ("not_evaluated", "not_established"))
        self.assertEqual(result["fields"]["materials"]["status"], "not_found")

    def test_hidden_and_footer_content_does_not_become_article_evidence(self):
        body = '<html><title>推免通知</title><body>2026-07-01 <div hidden><p>一、报名材料</p><p>1. 虚假材料</p></div><article><p>一、报名条件</p><p>1. 英语证书优先。</p></article><footer>二、报名材料 1. 页脚</footer></body></html>'
        snapshot = Snapshot(URL, datetime(2026, 11, 5), 200, "text/html", body.encode(), URL)
        result = inventory_document(snapshot, parse_snapshot(snapshot))
        self.assertEqual(result["fields"]["materials"]["status"], "not_found")
        self.assertEqual(result["view"], "html_content_text")
        self.assertNotIn("虚假", result["text"])
        for claim in all_claims(result):
            self.assertEqual(claim["quote"], result["text"][claim["start"]:claim["end"]])

    def test_material_review_is_not_application_material_list(self):
        result = inspect('<p>一、综合考核</p><p>（一）材料审核</p><p>择优确定名单。</p>')
        self.assertEqual(result["fields"]["materials"]["status"], "not_found")
        self.assertIn("材料审核", result["fields"]["assessment"]["sections"][0]["claim"]["quote"])

    def test_nested_items_remain_in_parent_and_qualifiers_are_retained(self):
        result = inspect('<p>一、报名材料</p><p>仅适用于直博生：</p><p>1. 可选证明（1）论文（2）专利，不超过10MB。</p><p>2. 无需重复提交。</p>')
        section = result["fields"]["materials"]["sections"][0]
        numbered = [item for item in section["items"] if item["number"] is not None]
        self.assertEqual(len(numbered), 2)
        self.assertIn("（2）专利", numbered[0]["claim"]["quote"])
        self.assertIn("仅适用于直博生", section["claim"]["quote"])
        self.assertIn("无需", numbered[1]["claim"]["quote"])

    def test_skipped_and_duplicate_numbers_are_explicit(self):
        for items in ('1. A 3. B', '1. A 1. B', '2. A'):
            with self.subTest(items=items):
                result = inspect(f'<p>一、报名条件</p><p>{items}</p>')
                self.assertIn("ITEM_SEQUENCE_UNCERTAIN", result["fields"]["conditions"]["sections"][0]["issues"])

    def test_repeated_titles_and_quotes_keep_distinct_positions(self):
        result = inspect('<p>一、报名条件</p><p>1. 相同原文。</p><p>二、报名条件</p><p>1. 相同原文。</p>')
        field = result["fields"]["conditions"]
        self.assertIn("MULTIPLE_SECTIONS_SCOPE_UNRESOLVED", field["issues"])
        a, b = [section["items"][0]["claim"] for section in field["sections"]]
        self.assertLess(a["start"], b["start"])
        self.assertTrue(all(claim["quote"] == result["text"][claim["start"]:claim["end"]] for claim in all_claims(result)))

    def test_unknown_and_mixed_headers_bound_earlier_section(self):
        result = inspect('<p>一、报名条件</p><p>1. 条件。</p><p>二、报名条件及材料</p><p>1. 混合语义。</p>')
        self.assertEqual(result["fields"]["materials"]["status"], "not_found")
        self.assertNotIn("混合语义", result["fields"]["conditions"]["sections"][0]["claim"]["quote"])
        self.assertTrue(result["unclassifiedSections"])

    def test_no_numbering_and_empty_sections_are_not_claimed_complete(self):
        for body, issue in (("请提交成绩单。", "NUMBERED_LIST_NOT_IDENTIFIED"), ("", "EMPTY_SECTION")):
            result = inspect(f'<p>一、报名材料</p><p>{body}</p><p>二、其他说明</p><p>详情待公告。</p>')
            self.assertIn(issue, result["fields"]["materials"]["sections"][0]["issues"])
            self.assertEqual(result["coverage"], "not_established")

    def test_later_channel_and_assessment_mentions_keep_conditional_context(self):
        result = inspect('<p>一、报名流程</p><p>通过学校系统报名。</p><p>二、其他说明</p><p>1. 错过预报名的，可在系统开通后报名，是否再次复试待定。</p><p>2. 复试预计9月举行，以邮件为准。</p>')
        excerpts = result["fields"]["channels"]["relatedExcerpts"]
        self.assertIn("是否再次复试待定", excerpts[0]["claim"]["quote"])
        self.assertEqual(result["fields"]["assessment"]["status"], "related_excerpt_only")

    def test_table_text_does_not_authorize_row_relationships(self):
        result = inspect('<p>一、招生学科专业</p><table><tr><td>085405-02</td><td>3</td></tr></table>')
        self.assertIn("TABLE_RELATIONSHIPS_NOT_INTERPRETED", result["fields"]["scope"]["issues"])

    def test_unreadable_empty_and_unmarked_body_keep_missing_states(self):
        snap = source("")
        unreadable = inventory_document(snap, ParseResult(ParseStatus.UNSUPPORTED))
        self.assertTrue(all(field["status"] == "source_unreadable" for field in unreadable["fields"].values()))
        fallback = inventory_document(snap, ParseResult(ParseStatus.COMPLETE, content_text="一、报名材料 1. 虚构", content_scope="body_fallback"))
        self.assertTrue(all(field["status"] == "not_attempted" for field in fallback["fields"].values()))
        self.assertIn("BODY_SCOPE_UNCERTAIN", fallback["issues"])

    def test_heading_budget_is_not_reported_as_no_requirements_found(self):
        result = inspect("<p>一、报名条件</p><p>1. 条件</p>" * 129)
        self.assertIn("SECTION_BUDGET_EXCEEDED", result["issues"])
        self.assertTrue(all(field["status"] == "not_attempted" for field in result["fields"].values()))

    def test_common_evidence_rejects_wrong_quote_and_view(self):
        from yantu.acquisition.parse_models import PageText
        with self.assertRaises(ValueError):
            claim_record(SourceClaim("field", "wrong", 0, 4), "text")
        with self.assertRaises(ValueError):
            claim_record(SourceClaim("field", "text", 0, 4), "text", (PageText(1, "other", 0, 4, "text_extracted"),))
        self.assertEqual(source_quality(None)["parseStatus"], None)


class TestInventoryApplication(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name)
        self.store = SnapshotStore(self.output / "snapshots")
        spec = importlib.util.spec_from_file_location("inspect_policy_fields", ROOT / "scripts/inspect_policy_fields.py")
        self.script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.script)

    def bundle(self, *, failed=False):
        snap = source('<p>一、报名条件</p><p>1. 原始条件。</p><a href="/extra.pdf">政策文件</a>')
        def fetch(url, **kwargs):
            if url != URL:
                raise FetchError(url, "unavailable")
            return snap
        bundle = DocumentBundleCollector(self.store, allowed_hosts=("unit.example",), fetcher=fetch,
                                         budget=DocumentBudget(max_documents=8 if failed else 1)).collect((DocumentSeed(URL, "policy"),), request_id="fields-test")
        path = self.output / "bundle.json"
        bundle.save(path)
        return path, snap

    def test_failed_and_budget_sources_stay_in_denominator(self):
        for failed in (False, True):
            path, _ = self.bundle(failed=failed)
            report = self.script.inspect_fields(path)
            self.assertEqual(report["counts"]["reportedSources"], 2)
            for field in FIELD_NAMES:
                self.assertEqual(sum(report["counts"]["fields"][field].values()), 2)
                self.assertEqual(report["results"][1]["fields"][field]["status"], "source_unavailable")
            self.assertTrue(report["inputGaps"])
            self.assertEqual((report["usage"]["modelCalls"], report["usage"]["networkCalls"]), (0, 0))

    def test_cached_text_tampering_cannot_replace_original_source(self):
        path, _ = self.bundle()
        record = json.loads(path.read_text(encoding="utf-8"))
        record["documents"][0]["parse_result"]["content_text"] = "一、报名条件 1. 虚假的缓存条件。"
        record.pop("recordDigest")
        record["recordDigest"] = hashlib.sha256(canonical_json(record)).hexdigest()
        path.write_bytes(canonical_json(record))
        report = self.script.inspect_fields(path)
        self.assertIn("原始条件", report["results"][0]["text"])
        self.assertNotIn("虚假的", report["results"][0]["text"])

    def test_corrupt_bytes_fail_instead_of_becoming_empty_fields(self):
        path, snap = self.bundle()
        self.store.path_of(snap.sha256).write_bytes(b"damaged")
        with self.assertRaises(SnapshotStorageError):
            self.script.inspect_fields(path)

    def test_programming_error_propagates(self):
        path, _ = self.bundle()
        with patch("yantu.acquisition.field_inventory_service.inventory_document", side_effect=TypeError("implementation bug")), self.assertRaises(TypeError):
            self.script.inspect_fields(path)

    def test_service_log_omits_exception_and_cause_messages(self):
        path, _ = self.bundle()
        error = TypeError('harmless_outer_marker https://unit.example/?query=outer_marker')
        error.__cause__ = ValueError('harmless_cause_marker')
        with self.assertLogs('yantu.acquisition.field_inventory_service', level='ERROR') as logs:
            with patch('yantu.acquisition.field_inventory_service.inventory_document', side_effect=error):
                with self.assertRaises(TypeError) as caught:
                    self.script.inspect_fields(path)
        self.assertIs(caught.exception, error)
        output = '\n'.join(logs.output)
        self.assertNotIn('outer_marker', output)
        self.assertNotIn('cause_marker', output)
        self.assertNotIn('?query=', output)
        self.assertIn('TypeError', output)
        self.assertIn('ValueError', output)
        self.assertIn('line', output)
        self.assertIn('field_inventory_service.py', output)

    def test_cli_failure_is_nonzero_without_exception_messages(self):
        runner = '''import runpy, sys
from unittest.mock import patch
def fail(*args, **kwargs):
    try:
        raise ValueError("harmless_cause_marker")
    except ValueError as cause:
        raise TypeError("harmless_outer_marker https://unit.example/?query=outer_marker") from cause
sys.argv = ["inspect_policy_fields.py", "unused.json"]
with patch("yantu.acquisition.field_inventory_service.inspect_fields", side_effect=fail):
    runpy.run_path("scripts/inspect_policy_fields.py", run_name="__main__")
'''
        result = subprocess.run([sys.executable, '-c', runner], cwd=ROOT, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('outer_marker', result.stderr)
        self.assertNotIn('cause_marker', result.stderr)
        self.assertNotIn('?query=', result.stderr)
        self.assertIn('TypeError', result.stderr)
        self.assertIn('ValueError', result.stderr)
        self.assertIn('line', result.stderr)

    def test_reparse_manifest_and_quality_preserve_old_limit(self):
        path, _ = self.bundle()
        report = self.script.inspect_fields(path)
        row = report["results"][0]
        self.assertEqual(row["sourceQuality"]["parser"]["identity"], "html-notice")
        self.assertEqual(row["inputParseQuality"]["parser"]["identity"], "html-notice")
        self.assertNotIn("pages", row["inputParseQuality"])
