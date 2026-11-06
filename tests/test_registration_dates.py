"""日期精度和报名区间的证据验证；真实PDF仅冻结回放。"""

import importlib.util
import unittest
import tempfile
from datetime import datetime
from pathlib import Path

from yantu.acquisition.text_dates import find_datetimes
from yantu.acquisition.registration_window import extract_registration_window
from yantu.acquisition.policy_verification import PolicyValidationError
from yantu.acquisition.parsers import parse_snapshot
from yantu.evaluation.policy_regression import load_source


class TestDatePrecision(unittest.TestCase):
    def test_pdf_spacing_fullwidth_colon_and_linebreak_preserve_evidence(self):
        text = "起止时间为 2026 年 7 月 27 日 09：00——2026 年 9 月 7 日中午 12：\n00"
        hits = find_datetimes(text)
        self.assertEqual([hit.dt for hit in hits], [datetime(2026, 7, 27, 9), datetime(2026, 9, 7, 12)])
        self.assertTrue(all(hit.precision == "minute" and hit.time_status == "valid" for hit in hits))
        self.assertTrue(text[hits[1].start:hits[1].value_end].endswith("12：\n00"))

    def test_day_hour_minute_and_invalid_time_are_distinct(self):
        cases = (("2026-09-07", "day", "missing"), ("2026-09-07 9时", "hour", "valid"), ("2026-09-07 9时30分", "minute", "valid"),
                 ("2026-09-07 12:60", "day", "invalid"), ("2026-09-07 9:", "day", "invalid"), ("2026-09-07 9:000", "day", "invalid"),
                 ("2026-09-07 9:00:30", "day", "invalid"), ("2026-09-07 中午1:00", "day", "invalid"))
        for text, precision, status in cases:
            with self.subTest(text=text):
                hit = find_datetimes(text)[0]
                self.assertEqual((hit.precision, hit.time_status), (precision, status))

    def test_clock_does_not_come_from_later_event_or_numbered_item(self):
        for text in ("2026年9月7日。面试9:00", "2026年9月7日 开会9:00", "2026年9月7日至2026年9月8日9:00"):
            hit = find_datetimes(text)[0]
            self.assertEqual((hit.precision, hit.time_status), ("day", "missing"))
        self.assertEqual(find_datetimes("2026-09-07 9时\n4.材料要求")[0].precision, "hour")

    def test_2400_preserves_raw_date_and_rolls_value(self):
        text = "2026-09-07 24：00"
        hit = find_datetimes(text)[0]
        self.assertEqual(hit.dt, datetime(2026, 9, 8))
        self.assertEqual(text[hit.start:hit.end], "2026-09-07")
        self.assertEqual(text[hit.start:hit.value_end], text)
        self.assertEqual(find_datetimes("9999-12-31 24:00")[0].time_status, "invalid")


class TestRegistrationWindow(unittest.TestCase):
    def test_range_is_supported_by_complete_original_quote(self):
        text = "一、报名条件\n优秀。二、报名流程\n预报名的起止时间为 2026 年 7 月 27 日 09：00——2026 年 9 月 7 日中午 12：\n00。三、其他说明"
        window, claims = extract_registration_window(text)
        self.assertEqual((window.start, window.end), (datetime(2026, 7, 27, 9), datetime(2026, 9, 7, 12)))
        self.assertEqual([claim.path for claim in claims], ["window.start", "window.end"])
        for claim in claims:
            self.assertEqual(text[claim.start:claim.end], claim.quote)
            self.assertIn("起止时间", claim.quote)

    def test_missing_or_invalid_precision_never_becomes_a_midnight_window(self):
        for expression in ("2026年9月7日", "2026年9月7日9时", "2026年9月7日12:60", "2026年9月7日12:", "2026年13月7日12:00"):
            with self.subTest(expression=expression), self.assertRaises(PolicyValidationError):
                extract_registration_window(f"一、报名条件。二、报名：报名截止时间为{expression}。三、其他")

    def test_range_order_missing_endpoint_and_multiple_claims_block(self):
        for expression in ("2026年9月8日09:00至2026年9月7日09:00", "2026年9月7日09:00", "2026年9月7日09:00至2026年9月8日", "2026年9月7日09:00。其他事件2026年9月8日09:00"):
            with self.subTest(expression=expression), self.assertRaises(PolicyValidationError):
                extract_registration_window(f"二、报名流程：预报名起止时间为{expression}。三、其他说明")
        with self.assertRaises(PolicyValidationError):
            extract_registration_window("二、报名流程：报名截止时间2026年9月7日09:00。报名截止时间2026年9月8日09:00。三、其他")

    def test_unrelated_publication_date_is_not_opening(self):
        window, claims = extract_registration_window("二、报名流程：请及时关注报名系统。三、其他说明 2026年7月24日09:00")
        self.assertEqual((window.start, window.end, claims), (None, None, []))

    def test_unsupported_clock_suffix_cannot_publish_a_valid_prefix(self):
        for clock in ("9时30分 20秒", "09:00:", "09:00:xx", "09:00:30", "09:00pm"):
            with self.subTest(clock=clock):
                text = f"二、报名流程：报名截止时间为2026年9月7日{clock}。三、其他"
                self.assertEqual(find_datetimes(text)[0].time_status, "invalid")
                with self.assertRaises(PolicyValidationError):
                    extract_registration_window(text)

    def test_cancelled_or_qualified_statement_remains_unresolved(self):
        for prefix, suffix in (("已取消的", ""), ("不再采用以下", ""), ("暂定", ""), ("", "以后"), ("", "左右"), ("", "前后"), ("", "之前"),
                               ("", "的安排无效"), ("本通知不采用", "的安排"), ("此前的", "，此时间已无效"), ("请问是否", ""), ("", "，这只是示例")):
            with self.subTest(prefix=prefix, suffix=suffix), self.assertRaises(PolicyValidationError):
                extract_registration_window(f"二、报名流程：{prefix}报名截止时间为2026年9月7日09:00{suffix}。三、其他")

    def test_unknown_context_is_not_authorized_by_missing_negative_keywords(self):
        with self.assertRaises(PolicyValidationError):
            extract_registration_window("二、报名：示例甲说报名截止时间为2026年9月7日09:00。三、其他")

    def test_additional_required_date_is_not_hidden_by_lookback_or_long_quote(self):
        for separator in ("。材料说明如下，" + "材料需真实有效，" * 10, "，"):
            for extra in ("2026年8月5日12:00", "8月5日12:00", "2026年13月5日12:00"):
                with self.subTest(separator=separator, extra=extra), self.assertRaises(PolicyValidationError):
                    extract_registration_window("二、报名流程：预报名起止时间为2026年8月1日09:00至2026年9月7日12:00" + separator + f"另需在{extra}前通过院系表单完成材料提交。三、其他")

    def test_evidence_keeps_full_statement_context(self):
        text = "二、报名流程：本院今年的报名截止时间为2026年9月7日09:00，请提交完整资料。三、其他"
        _, claims = extract_registration_window(text)
        self.assertIn("本院今年的", claims[0].quote)
        self.assertIn("请提交完整资料", claims[0].quote)

    @unittest.skipUnless(importlib.util.find_spec("pypdf"), "需要可选pdf依赖组")
    def test_official_pdf_range_has_exact_text_offsets(self):
        root = Path(__file__).resolve().parents[1] / "data/pilot/ecnu_documents"
        source = load_source(root, {"file": "prenotice.pdf", "metadata": "prenotice_pdf_source.json"})
        parsed = parse_snapshot(source)
        window, claims = extract_registration_window(parsed.text)
        self.assertEqual((window.start, window.end), (datetime(2026, 7, 27, 9), datetime(2026, 9, 7, 12)))
        self.assertEqual(len(claims), 2)
        for claim in claims:
            self.assertEqual(parsed.text[claim.start:claim.end], claim.quote)
            self.assertTrue(parsed.pages[0].start_offset <= claim.start < claim.end <= parsed.pages[0].end_offset)

    @unittest.skipUnless(importlib.util.find_spec("pypdf"), "需要可选pdf依赖组")
    def test_field_report_preserves_partial_source_and_unprocessed_pages(self):
        from yantu.acquisition.document_bundle import DocumentBundleCollector, DocumentSeed
        from yantu.acquisition.parsers import ParserRegistry, PdfTextParser
        from yantu.acquisition.store import SnapshotStore

        root = Path(__file__).resolve().parents[1]
        source = load_source(root / "data/pilot/ecnu_documents", {"file": "prenotice.pdf", "metadata": "prenotice_pdf_source.json"})
        registry = ParserRegistry()
        registry.register(PdfTextParser(max_pages=1))
        spec = importlib.util.spec_from_file_location("inspect_registration_windows", root / "scripts/inspect_registration_windows.py")
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            bundle = DocumentBundleCollector(SnapshotStore(output / "snapshots"), allowed_hosts=("sei.ecnu.edu.cn",),
                                             registry=registry, fetcher=lambda url, **kwargs: source).collect(
                (DocumentSeed(source.url, "prenotice"),), request_id="partial-pdf-report")
            bundle.save(output / "bundle.json")
            report = script.inspect_windows(output / "bundle.json")
            row = report["results"][0]
            self.assertEqual((row["status"], row["sourceParseStatus"], row["unprocessedPageCount"]), ("extracted", "partial", 1))
            self.assertEqual((report["inputBundleStatus"], report["publicationStatus"]), ("partial", "not_published"))
            self.assertTrue(report["inputGaps"])


if __name__ == "__main__":
    unittest.main()
