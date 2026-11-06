"""采集模块单元测试:确定性部分,不含真实网络(真网属integration,SRS §5.1)。"""

import pathlib
import sys
import unittest
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from yantu.acquisition.fetcher import FetchError, Snapshot
from yantu.acquisition.parsers import (
    HtmlNoticeParser,
    ParseStatus,
    ParserRegistry,
    parse_snapshot,
    extract_dates,
)

FIXTURE_HTML = """<html><head><meta charset="utf-8"><title>软件工程学院2027年预报名通知</title></head>
<body><h1>软件工程学院2027年预报名通知</h1>
<script>var x=1;</script>
<p>报名截止时间:2026年9月10日 12:00。</p>
<p>系统开放:2026-07-24 10:00 至 2026-09-10 12:00。</p>
<p>落款:2026年7月24日</p>
</body></html>"""


def make_snapshot(body: bytes, content_type: str = "text/html; charset=utf-8") -> Snapshot:
    return Snapshot(
        url="https://example.edu.cn/page.htm",
        fetched_at=datetime(2026, 11, 5, 12, 0),
        status_code=200,
        content_type=content_type,
        body=body,
    )


class TestSnapshot(unittest.TestCase):
    def test_sha256_deterministic(self):
        a = make_snapshot("同内容".encode("utf-8"))
        b = make_snapshot("同内容".encode("utf-8"))
        c = make_snapshot("不同内容".encode("utf-8"))
        self.assertEqual(a.sha256, b.sha256)
        self.assertNotEqual(a.sha256, c.sha256)

    def test_carrier_strips_parameters(self):
        self.assertEqual(make_snapshot(b"x").carrier, "text/html")

    def test_fetch_error_structure(self):
        e = FetchError("https://x", "HTTP 403")
        self.assertEqual((e.url, e.reason), ("https://x", "HTTP 403"))


class TestHtmlNoticeParser(unittest.TestCase):
    def setUp(self):
        self.parser = HtmlNoticeParser()

    def test_complete_parse(self):
        r = self.parser.parse(make_snapshot(FIXTURE_HTML.encode("utf-8")))
        self.assertEqual(r.status, ParseStatus.COMPLETE)
        self.assertIn("预报名", r.title)
        self.assertIn("报名截止", r.text)
        self.assertIn(datetime(2026, 9, 10, 12, 0), r.dates)
        self.assertIn(datetime(2026, 7, 24, 10, 0), r.dates)
        self.assertNotIn("var x=1", r.text)  # script被剔除

    def test_no_dates_partial(self):
        html = "<html><head><title>只有标题</title></head><body>无日期正文</body></html>"
        r = self.parser.parse(make_snapshot(html.encode("utf-8")))
        self.assertEqual(r.status, ParseStatus.PARTIAL)
        self.assertTrue(any("日期候选" in n for n in r.notes))

    def test_non_html_unsupported_by_dispatcher(self):
        """R1:载体分发由 parse_snapshot 决定;无解析器→UNSUPPORTED。"""
        r = parse_snapshot(make_snapshot(b"unknown binary", "image/tiff"))
        self.assertEqual(r.status, ParseStatus.UNSUPPORTED)
        self.assertTrue(any("载体" in n for n in r.notes))

    def test_navigation_cut(self):
        """R4:导航菜单被削掉,正文从标题处开始。"""
        html = (
            "<html><head><title>软件工程学院2027年预报名通知</title></head><body>"
            "首页 学院概况 师资队伍 Toggle navigation "
            "软件工程学院2027年预报名通知 报名截止时间:2026年9月10日 12:00。"
            "</body></html>"
        )
        r = self.parser.parse(make_snapshot(html.encode("utf-8")))
        self.assertTrue(r.text.startswith("软件工程学院"), msg=r.text[:40])
        self.assertNotIn("学院概况", r.text)
        self.assertIn(datetime(2026, 9, 10, 12, 0), r.dates)


class TestParserRegistry(unittest.TestCase):
    def test_register_and_dispatch(self):
        class FakePdfParser:
            def supports(self, carrier):
                return carrier == "application/pdf"

            def parse(self, snapshot):
                return ParseStatus.COMPLETE  # 测试桩:非ParseResult仅验证分发

        registry = ParserRegistry()
        registry.register(FakePdfParser())
        self.assertIsInstance(registry.select("application/pdf"), FakePdfParser)
        self.assertIsNone(registry.select("text/html"))

    def test_default_registry_has_html(self):
        from yantu.acquisition.parsers import DEFAULT_REGISTRY

        self.assertIsNotNone(DEFAULT_REGISTRY.select("text/html"))


class TestHtmlNoticeParserTail(TestHtmlNoticeParser):
    """归位:gbk 解码与 supports 属于 HtmlNoticeParser 行为。"""

    def test_gbk_decoding(self):
        html = "<html><head><meta charset=\"gbk\"><title>中文标题GBK</title></head><body>2026年9月1日</body></html>"
        r = self.parser.parse(make_snapshot(html.encode("gbk")))
        self.assertEqual(r.status, ParseStatus.COMPLETE)
        self.assertIn("GBK", r.title)

    def test_supports(self):
        self.assertTrue(self.parser.supports("text/html"))
        self.assertFalse(self.parser.supports("application/pdf"))


class TestDateExtraction(unittest.TestCase):
    def test_invalid_date_ignored(self):
        self.assertEqual(extract_dates("2026-13-99 与 2026年2月30日"), [])

    def test_both_formats(self):
        self.assertEqual(
            extract_dates("2026-09-10 与 2026年9月10日"),
            [datetime(2026, 9, 10)],
        )


if __name__ == "__main__":
    unittest.main()
