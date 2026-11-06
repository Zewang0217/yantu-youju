"""正文链接与PDF页级解析的离线契约测试，合成PDF仅在内存创建。"""

from __future__ import annotations

import builtins
import importlib.util
import io
import pathlib
import unittest
import tempfile
from datetime import datetime
from unittest.mock import patch

from yantu.acquisition.fetcher import Snapshot
from yantu.acquisition.html_links import discover_document_links
from yantu.acquisition.parsers import HtmlNoticeParser, ParseStatus, PdfTextParser, parse_snapshot
from yantu.acquisition.store import SnapshotStore, SnapshotStorageError


def pdf_bytes(texts: tuple[str, ...], *, password: str | None = None, unsupported_page: int | None = None) -> bytes:
    """构造无需字体下载的英文文字层/空白页，避免依赖外部测试生成器。"""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    for index, text in enumerate(texts):
        page = writer.add_blank_page(width=300, height=400)
        if text:
            font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
            page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 20 200 Td ({text}) Tj ET".encode("ascii"))
            if index == unsupported_page:
                stream[NameObject("/Filter")] = NameObject("/UnsupportedFilter")
            page[NameObject("/Contents")] = writer._add_object(stream)
    if password:
        writer.encrypt(password)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def snapshot(body: bytes, carrier: str = "application/pdf") -> Snapshot:
    return Snapshot("https://unit.example/notice/index.htm", datetime(2026, 11, 5), 200, carrier, body)


class TestNoticeLinks(unittest.TestCase):
    def test_embedded_webplus_pdf_is_discovered_without_following_template_images(self):
        html = '<body><img src="/assets/logo.png"><div class="wp_articlecontent"><div pdfsrc="/notice.pdf" sudyfile-attr="{\'title\':\'政策正文\'}"></div></div></body>'
        result = discover_document_links(html, "https://unit.example/a.htm")
        self.assertEqual([(link.url, link.kind, link.label) for link in result.links], [("https://unit.example/notice.pdf", "embedded_document", "政策正文")])
        self.assertEqual(result.content_scope, "wp_articlecontent")

    def test_anchor_text_and_relative_url_preserve_origin(self):
        result = discover_document_links('<body><article><a href="../rules.pdf#page=1">考核<span>细则</span></a></article></body>', "https://unit.example/notices/a.htm")
        self.assertEqual((result.links[0].url, result.links[0].label), ("https://unit.example/rules.pdf", "考核细则"))

    def test_body_image_is_a_missing_carrier_not_successful_ocr(self):
        result = HtmlNoticeParser().parse(snapshot(b'<html><title>Notice</title><body><article><img src="/notice.png">2026-07-01</article></body></html>', "text/html"))
        self.assertEqual((result.status, result.links[0].kind), (ParseStatus.PARTIAL, "body_image"))

    def test_non_http_and_hidden_tracking_candidates_are_not_followable(self):
        html = '<body><article><a href="javascript:alert(1)">bad</a><img src="/counter" width="0"><div style="display:none"><img src="/hidden.png"></div><a href="mailto:a@example.com">mail</a></article></body>'
        self.assertEqual(discover_document_links(html, "https://unit.example/").links, ())

    def test_navigation_budget_does_not_starve_article_discovery(self):
        html = '<body><a href="/nav1">a</a><a href="/nav2">b</a><article><div pdfsrc="/body.pdf"></div></article></body>'
        result = discover_document_links(html, "https://unit.example/", max_links=1)
        self.assertEqual(([link.url for link in result.links], result.truncated), (["https://unit.example/body.pdf"], False))

    def test_article_candidate_budget_is_explicit(self):
        result = discover_document_links('<body><article><a href="/a.pdf">a</a><a href="/b.pdf">b</a></article></body>', "https://unit.example/", max_links=1)
        self.assertEqual((len(result.links), result.truncated), (1, True))

    def test_malformed_anchor_does_not_hide_later_valid_pdf(self):
        result = discover_document_links('<body><article><a href="http://[">bad</a><div pdfsrc="/valid.pdf"></div></article></body>', "https://unit.example/")
        self.assertEqual([link.url for link in result.links], ["https://unit.example/valid.pdf"])

    def test_hidden_article_does_not_override_visible_body_fallback(self):
        result = discover_document_links('<body><div hidden><article></article></div><div pdfsrc="/valid.pdf"></div></body>', "https://unit.example/")
        self.assertEqual((result.content_scope, [link.url for link in result.links]), ("body_fallback", ["https://unit.example/valid.pdf"]))

    def test_precise_article_scope_excludes_main_template_images(self):
        result = discover_document_links('<body><main><img src="/template.png"><div class="wp_articlecontent"><div pdfsrc="/valid.pdf"></div></div></main></body>', "https://unit.example/")
        self.assertEqual([link.url for link in result.links], ["https://unit.example/valid.pdf"])


@unittest.skipUnless(importlib.util.find_spec("pypdf"), "需要可选pdf依赖组")
class TestPdfTextParser(unittest.TestCase):
    def test_pdf_text_pages_have_reproducible_offsets(self):
        result = parse_snapshot(snapshot(pdf_bytes(("First policy page", "Second policy page"))))
        self.assertEqual((result.status, result.declared_page_count), (ParseStatus.COMPLETE, 2))
        self.assertEqual(result.text[result.pages[1].start_offset:result.pages[1].end_offset], result.pages[1].text)

    def test_empty_text_layer_does_not_report_complete(self):
        result = PdfTextParser().parse(snapshot(pdf_bytes(("",))))
        self.assertEqual((result.status, result.pages[0].status), (ParseStatus.UNSUPPORTED, "no_text"))

    def test_partial_text_pdf_preserves_unreadable_page(self):
        result = PdfTextParser().parse(snapshot(pdf_bytes(("Visible", ""))))
        self.assertEqual((result.status, len(result.pages), result.pages[1].status), (ParseStatus.PARTIAL, 2, "no_text"))

    def test_encrypted_pdf_is_explicitly_unsupported(self):
        result = PdfTextParser().parse(snapshot(pdf_bytes(("Secret",), password="fixture-password")))
        self.assertEqual(result.status, ParseStatus.UNSUPPORTED)

    def test_malformed_pdf_is_a_parse_failure(self):
        self.assertEqual(PdfTextParser().parse(snapshot(b"%PDF-1.7 broken")).status, ParseStatus.FAILED)

    def test_unsupported_pdf_filter_is_a_capability_gap_not_uncaught_error(self):
        result = PdfTextParser().parse(snapshot(pdf_bytes(("Unsupported",), unsupported_page=0)))
        self.assertEqual((result.status, result.pages[0].status), (ParseStatus.UNSUPPORTED, "unsupported"))

    def test_unsupported_page_does_not_discard_other_pages(self):
        result = PdfTextParser().parse(snapshot(pdf_bytes(("Unsupported", "Readable"), unsupported_page=0)))
        self.assertEqual((result.status, result.pages[1].status), (ParseStatus.PARTIAL, "text_extracted"))

    def test_page_budget_keeps_total_page_count_and_partial_state(self):
        result = PdfTextParser(max_pages=1).parse(snapshot(pdf_bytes(("one", "two"))))
        self.assertEqual((result.status, result.declared_page_count, len(result.pages)), (ParseStatus.PARTIAL, 2, 1))

    def test_character_budget_includes_page_separators(self):
        result = PdfTextParser(max_total_chars=4).parse(snapshot(pdf_bytes(("abc", "def"))))
        self.assertLessEqual(len(result.text), 4)
        self.assertEqual(result.status, ParseStatus.PARTIAL)

    def test_real_ecnu_pdf_text_layer_contains_six_conditions_and_explicit_range(self):
        path = pathlib.Path(__file__).resolve().parents[1] / "data/pilot/ecnu_documents/prenotice.pdf"
        result = PdfTextParser().parse(snapshot(path.read_bytes()))
        compact = "".join(result.text.split())
        self.assertEqual((result.status, len(result.pages)), (ParseStatus.COMPLETE, 2))
        self.assertIn("已获得或预计获得", compact)
        self.assertIn("2026年7月27日09：00", compact)
        self.assertIn("者优先", compact)


class TestMissingPdfDependency(unittest.TestCase):
    def test_optional_dependency_absence_is_explicit(self):
        original = builtins.__import__

        def missing(name, *args, **kwargs):
            if name == "pypdf":
                raise ModuleNotFoundError("optional pypdf missing", name="pypdf")
            return original(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=missing):
            result = PdfTextParser().parse(snapshot(b"%PDF-1.7"))
        self.assertEqual(result.status, ParseStatus.UNSUPPORTED)


class TestBinarySnapshotCompatibility(unittest.TestCase):
    def test_new_pdf_snapshot_uses_binary_neutral_extension(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SnapshotStore(pathlib.Path(directory))
            source = snapshot(b"pdf fixture bytes")
            path = store.save(source)
            self.assertEqual((path.suffix, store.read(source.sha256)), (".blob", source.body))

    def test_legacy_html_snapshot_is_verified_and_reused_without_rename(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = snapshot(b"historical bytes")
            legacy = root / f"{source.sha256}.html"
            legacy.write_bytes(source.body)
            store = SnapshotStore(root)
            self.assertEqual(store.save(source), legacy)
            self.assertFalse((root / f"{source.sha256}.blob").exists())

    def test_corrupt_legacy_snapshot_is_not_hidden_by_new_blob(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = snapshot(b"historical bytes")
            (root / f"{source.sha256}.html").write_bytes(b"corrupt")
            with self.assertRaises(SnapshotStorageError):
                SnapshotStore(root).save(source)
