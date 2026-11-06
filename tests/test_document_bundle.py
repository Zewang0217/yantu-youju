"""有界资料包的访问、覆盖与证据回放测试；不联网、不调用模型。"""

import importlib.util
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, Mock

from yantu.acquisition.document_bundle import DocumentBudget, DocumentBundleCollector, DocumentSeed, load_bundle_record
from yantu.acquisition.artifact_files import atomic_write
from yantu.acquisition.fetcher import FetchError, Snapshot, _ScopedRedirect, fetch
from yantu.acquisition.html_links import discover_document_links
from yantu.acquisition.source_urls import validate_source_url
from yantu.acquisition.store import SnapshotStore
from yantu.evaluation.policy_regression import load_source

HOST = "unit.example"
ROOT_URL = f"https://{HOST}/root"


def html(url=ROOT_URL, content="政策正文", final=True):
    body = f'<html><title>推免通知</title><body><article>2026-07-01 {content}</article></body></html>'.encode()
    return Snapshot(url, datetime(2026, 11, 5), 200, "text/html", body, url if final else "")


class TestPlaintextReferences(unittest.TestCase):
    def test_plaintext_url_has_replayable_text_locator(self):
        source = '<body><article><p>详见《预报名通知》（网址：https://unit.example/pre.htm）。</p><p>后续说明</p></article></body>'
        result = discover_document_links(source, ROOT_URL)
        link = result.links[0]
        self.assertEqual(link.url, "https://unit.example/pre.htm")
        self.assertEqual(result.content_text[link.text_start:link.text_end], link.raw_value)
        self.assertEqual((link.locator_kind, link.line), ("content_text", 0))
        self.assertIn("预报名通知", link.label)

    def test_hidden_text_navigation_and_duplicate_urls_are_not_extra_sources(self):
        source = '<body>https://nav.example/<article><div hidden>https://hide.example/</div><a href="/pre">推免 https://unit.example/pre</a></article></body>'
        self.assertEqual(len(discover_document_links(source, ROOT_URL).links), 1)

    def test_plaintext_urls_obey_shared_budget(self):
        result = discover_document_links('<body><article>推免通知 https://unit.example/a https://unit.example/b</article></body>', ROOT_URL, max_links=1)
        self.assertEqual((len(result.links), result.truncated), (1, True))


class TestSourceBoundary(unittest.TestCase):
    def test_unsafe_urls_are_rejected_even_if_host_is_configured(self):
        for url in ("http://unit.example/x", "https://user@unit.example/x", "https://unit.example:444/x", "https://127.0.0.1/x", "https://unit.example.evil/x", "https://unit.example/\nx"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_source_url(url, (HOST, "127.0.0.1"))
        self.assertEqual(validate_source_url("https://UNIT.example:443/x#p", (HOST,)), "https://unit.example/x")

    def test_redirect_is_rejected_without_reading_response_body(self):
        response = Mock()
        request = Mock(full_url=ROOT_URL)
        for url in ("https://unit.example/new", "https://evil.example/x"):
            with self.assertRaises(FetchError):
                _ScopedRedirect((HOST,)).redirect_request(request, response, 302, "", {}, url)
        response.read.assert_not_called()
        self.assertEqual(response.close.call_count, 2)

    def test_fetch_reads_at_most_limit_plus_probe_and_records_final_url(self):
        response = Mock(status=200, headers={"Content-Type": "text/html"})
        response.geturl.return_value = ROOT_URL
        response.read.return_value = b"1234"
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(FetchError):
                fetch(ROOT_URL, max_bytes=3)
        response.read.assert_called_once_with(4)
        response.read.return_value = b"123"
        with patch("urllib.request.urlopen", return_value=response):
            self.assertEqual(fetch(ROOT_URL).final_url, ROOT_URL)
        for timeout in (float("nan"), float("inf"), True, 0):
            with self.assertRaises(ValueError):
                fetch(ROOT_URL, timeout=timeout)


class TestDocumentBundle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SnapshotStore(Path(self.tmp.name) / "sources")
        self.calls = []

    def collect(self, sources, *, budget=DocumentBudget(), clock=None, seeds=None):
        def fake(url, **kwargs):
            self.calls.append((url, kwargs))
            item = sources[url]
            if isinstance(item, Exception):
                raise item
            return item
        kwargs = {"clock": clock} if clock else {}
        return DocumentBundleCollector(self.store, allowed_hosts=(HOST,), fetcher=fake, budget=budget, **kwargs).collect(
            seeds or (DocumentSeed(ROOT_URL, "policy"),), request_id="test-request")

    def test_missing_embedded_source_stays_partial_and_bytes_unknown(self):
        url = f"https://{HOST}/body.pdf"
        bundle = self.collect({ROOT_URL: html(content='<div pdfsrc="/body.pdf"></div>'), url: FetchError(url, "offline")})
        self.assertEqual(bundle.status, "partial")
        self.assertGreater(bundle.usage["reservedUnknownBytes"], 0)
        self.assertEqual(bundle.documents[1].status, "access_failed")

    def test_cycles_and_duplicate_links_fetch_each_url_once(self):
        child = f"https://{HOST}/child"
        bundle = self.collect({ROOT_URL: html(content='<a href="/child">推免通知</a><a href="/child">政策</a>'), child: html(child, '<a href="/root">推免通知</a>')})
        self.assertEqual((bundle.status, len(self.calls)), ("completed", 2))

    def test_empty_wrapper_cycle_is_not_body_coverage(self):
        body = b'<html><title>Policy</title><body><article><div pdfsrc="/root"></div></article></body></html>'
        bundle = self.collect({ROOT_URL: replace(html(), body=body)})
        self.assertEqual(bundle.status, "partial")
        self.assertIn("BODY_NOT_LOCATED_OR_UNRESOLVED", [g["code"] for g in bundle.gaps])

    def test_budget_skips_remaining_sources_and_keeps_gaps(self):
        child = f"https://{HOST}/child"
        for budget in (DocumentBudget(max_documents=1), DocumentBudget(max_depth=0), DocumentBudget(max_total_bytes=len(html(content='<a href="/child">推免通知</a>').body))):
            with self.subTest(budget=budget):
                bundle = self.collect({ROOT_URL: html(content='<a href="/child">推免通知</a>'), child: html(child)}, budget=budget)
                self.assertEqual((bundle.status, bundle.documents[1].status, bundle.usage["attempts"]), ("partial", "not_run", 1))

    def test_elapsed_budget_after_fetch_prevents_parse(self):
        times = iter([0, 0, 100, 100])
        bundle = self.collect({ROOT_URL: html()}, clock=lambda: next(times))
        self.assertEqual((bundle.status, bundle.documents[0].reason), ("partial", "DEADLINE_AFTER_FETCH"))

    def test_response_scope_status_and_adapter_overread_rejected(self):
        cases = (replace(html(), final_url="https://evil.example/x"), replace(html(), final_url=f"https://{HOST}/other"), replace(html(), status_code=404), replace(html(), url=f"https://{HOST}/other"))
        for source in cases:
            with self.subTest(source=source), patch.object(self.store, "save") as save:
                self.assertEqual(self.collect({ROOT_URL: source}).documents[0].reason, "RESPONSE_SCOPE_MISMATCH")
                save.assert_not_called()
        self.assertEqual(self.collect({ROOT_URL: html()}, budget=DocumentBudget(max_file_bytes=1)).documents[0].reason, "FETCH_ADAPTER_BYTE_LIMIT_VIOLATION")

    def test_blocked_policy_relation_is_gap_roster_exclusion_is_auditable(self):
        source = html(content='<a href="/list.pdf">考核名单</a><a href="https://other.example/policy.pdf">预报名通知</a>')
        bundle = self.collect({ROOT_URL: source})
        self.assertEqual((len(self.calls), bundle.status, len(bundle.edges)), (1, "partial", 2))
        self.assertTrue(all(not edge.selected for edge in bundle.edges))
        self.assertIn("POLICY_REFERENCE_BLOCKED", [gap["code"] for gap in bundle.gaps])

    def test_unsupported_body_images_and_inline_images_remain_gaps(self):
        url = f"https://{HOST}/notice.png"
        bundle = self.collect({ROOT_URL: html(content='<img src="/notice.png">'), url: Snapshot(url, datetime(2026, 11, 5), 200, "image/png", b"PNG", url)})
        self.assertEqual(bundle.status, "partial")
        bundle = self.collect({ROOT_URL: html(content='<img src="data:image/png;base64,xx">')})
        self.assertEqual(bundle.status, "partial")

    def test_programming_errors_and_corrupt_snapshot_fail_loudly(self):
        with self.assertRaises(TypeError):
            self.collect({ROOT_URL: TypeError("bug")})
        source = html()
        self.store.save(source).write_bytes(b"corrupt")
        with self.assertRaises(RuntimeError):
            self.collect({ROOT_URL: source})

    def test_record_and_source_bytes_round_trip(self):
        source = html(final=False)
        bundle = self.collect({ROOT_URL: source})
        path = Path(self.tmp.name) / "bundle.json"
        bundle.save(path)
        record = load_bundle_record(path, self.store)
        self.assertEqual(record["recommendationReadiness"], "not_evaluated")
        self.assertEqual(self.store.read(record["documents"][0]["snapshot_sha256"]), source.body)
        self.assertFalse(bundle.gaps[0]["required"])

    def test_prior_roster_sentence_does_not_suppress_policy_url(self):
        url = f"https://{HOST}/policy.pdf"
        source = html(content=f"考核名单已发布；另请查看推免政策通知：{url}")
        bundle = self.collect({ROOT_URL: source, url: html(url)})
        self.assertEqual((len(self.calls), bundle.status), (2, "completed"))
        self.assertNotIn("名单", bundle.edges[0].label)

    def test_mixed_roster_policy_label_is_unresolved_not_safely_excluded(self):
        for label in ("推免政策及考核名单", "名单及夏令营通知", "名单及推免通知", "名单与招生通知", "公示及报名细则"):
            with self.subTest(label=label):
                bundle = self.collect({ROOT_URL: html(content=f'<a href="/mixed.pdf">{label}</a>')})
                self.assertEqual(bundle.status, "partial")
                self.assertIn("AMBIGUOUS_POLICY_RELATION", [gap["code"] for gap in bundle.gaps])

    def test_unmarked_footer_is_not_proof_of_body_coverage(self):
        source = replace(html(), body=b'<html><title>Notice</title><body>2026-07-01 Copyright footer</body></html>')
        bundle = self.collect({ROOT_URL: source})
        self.assertEqual(bundle.status, "partial")
        self.assertIn("BODY_SCOPE_UNCERTAIN", [gap["code"] for gap in bundle.gaps])

    def test_same_seed_url_with_two_roles_preserves_input_without_refetch(self):
        seeds = (DocumentSeed(ROOT_URL, "main_notice"), DocumentSeed(ROOT_URL, "assessment_rules"))
        bundle = self.collect({ROOT_URL: html()}, seeds=seeds)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual([seed["role"] for seed in bundle.to_record()["configuredSeeds"]], ["main_notice", "assessment_rules"])

    def test_bundle_or_source_corruption_blocks_reuse(self):
        source = html()
        bundle = self.collect({ROOT_URL: source})
        path = Path(self.tmp.name) / "bundle.json"
        bundle.save(path)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["status"] = "invented"
        path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(RuntimeError):
            load_bundle_record(path, self.store)
        bundle.save(path)
        self.store.path_of(source.sha256).write_bytes(b"damaged")
        with self.assertRaises(RuntimeError):
            load_bundle_record(path, self.store)

    def test_atomic_save_failure_preserves_previous_artifact(self):
        path = Path(self.tmp.name) / "bundle.json"
        path.write_bytes(b"old")
        with patch("yantu.acquisition.artifact_files.os.replace", side_effect=OSError("failed")):
            with self.assertRaises(OSError):
                atomic_write(path, b"new")
        self.assertEqual(path.read_bytes(), b"old")
        self.assertEqual(list(path.parent.glob("*.pending")), [])

    @unittest.skipUnless(importlib.util.find_spec("pypdf"), "需要可选pdf依赖组")
    def test_three_official_sources_follow_plaintext_then_embedded_pdf(self):
        base = Path(__file__).resolve().parents[1] / "data/pilot"
        sources = {}
        for folder, file, meta in (("trusted_policy", "ecnu_rules.html", "ecnu_rules_source.json"), ("ecnu_documents", "prenotice.html", "prenotice_html_source.json"), ("ecnu_documents", "prenotice.pdf", "prenotice_pdf_source.json")):
            source = load_source(base / folder, {"file": file, "metadata": meta})
            sources[source.url] = source
        calls = []
        def replay(url, **kwargs):
            calls.append(url)
            return sources[url]
        bundle = DocumentBundleCollector(self.store, allowed_hosts=("sei.ecnu.edu.cn",), fetcher=replay).collect(
            (DocumentSeed(next(iter(sources)), "assessment_policy"),), request_id="official-replay")
        # 两个跨主机政策/门户引用尚未核验，不为让样例通过而隐藏覆盖缺损。
        self.assertEqual((bundle.status, len(calls), len(bundle.documents)), ("partial", 3, 3))
        self.assertEqual(sum(gap["required"] for gap in bundle.gaps), 2)
        pdf = bundle.documents[-1].parse_result
        self.assertEqual(pdf.declared_page_count, 2)
        self.assertTrue(all(page.status == "text_extracted" for page in pdf.pages))
        self.assertEqual(next(edge for edge in bundle.edges if edge.selected).locator_kind, "content_text")
        for document in bundle.documents:
            self.assertEqual(self.store.read(document.snapshot_sha256), sources[document.url].body)


if __name__ == "__main__":
    unittest.main()
