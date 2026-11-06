"""R2 text_dates 与 R3 HarvestService 的离线测试(fetch 注入,无真实网络)。"""

import pathlib
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from yantu.acquisition.fetcher import FetchError, Snapshot
from yantu.acquisition.harvest import HarvestService
from yantu.acquisition.parsers import ParseStatus
from yantu.acquisition.store import EntryBook, SnapshotStore
from yantu.acquisition.text_dates import find_datetimes


class TestTextDates(unittest.TestCase):
    def test_both_formats_with_time(self):
        hits = find_datetimes("截止:2026年9月7日中午12:00 与 2026-09-10 17:30")
        self.assertEqual([h.dt for h in hits], [datetime(2026, 9, 7, 12, 0), datetime(2026, 9, 10, 17, 30)])

    def test_pm_adds_twelve(self):
        hits = find_datetimes("下午5:00 前提交 2026年9月10日下午5:00")
        self.assertEqual(hits[-1].dt.hour, 17)

    def test_invalid_skipped_and_spans_ordered(self):
        hits = find_datetimes("2026年13月40日 无效 与 2026年7月24日 有效")
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0].start < hits[0].end)

    def test_dedupe_positions(self):
        hits = find_datetimes("2026年7月24日 落款 与 2026年7月24日 开放")
        self.assertEqual(len(hits), 2)  # 两处位置不同,各自命中


def _snap(body: bytes, url="https://x.edu.cn/a.htm", ctype="text/html") -> Snapshot:
    return Snapshot(url=url, fetched_at=datetime(2026, 11, 5, 12, 0),
                    status_code=200, content_type=ctype, body=body)


HTML = ("<html><head><title>通知</title></head><body>通知 报名截止时间:2026年9月10日 12:00</body></html>"
        ).encode("utf-8")


class TestHarvestService(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self.store = SnapshotStore(root / "snaps")
        self.book = EntryBook(root / "entries.json")

    def tearDown(self):
        self._tmp.cleanup()

    def test_collect_ok(self):
        svc = HarvestService(self.store, self.book, fetch_fn=lambda url: _snap(HTML))
        out = svc.collect("https://x.edu.cn/a.htm", name="通知页", school="测试大学")
        self.assertTrue(out.ok)
        self.assertEqual(out.parse_result.status, ParseStatus.COMPLETE)
        self.assertTrue(out.snapshot_path.exists())
        rec = self.book.get("https://x.edu.cn/a.htm")
        self.assertEqual(rec.last_status, "ok")
        self.assertEqual(rec.last_sha256, out.sha256)

    def test_collect_fetch_failed(self):
        def boom(url):
            raise FetchError(url, "HTTP 403")

        svc = HarvestService(self.store, self.book, fetch_fn=boom)
        out = svc.collect("https://x.edu.cn/forbidden", name="失败页")
        self.assertFalse(out.ok)
        self.assertIn("403", out.error)
        self.assertEqual(self.book.get("https://x.edu.cn/forbidden").last_status, "fetch_failed")
        self.assertFalse(any(self.store.path_of(out.sha256).exists() for _ in [0]))  # 无快照

    def test_collect_unsupported_carrier(self):
        svc = HarvestService(self.store, self.book, fetch_fn=lambda url: _snap(b"TIFF", ctype="image/tiff"))
        out = svc.collect("https://x.edu.cn/a.pdf", name="附件")
        self.assertTrue(out.ok)  # 抓取成功
        self.assertEqual(out.parse_result.status, ParseStatus.UNSUPPORTED)  # 解析层如实降级
        self.assertEqual(self.book.get("https://x.edu.cn/a.pdf").last_status, "ok")


if __name__ == "__main__":
    unittest.main()
