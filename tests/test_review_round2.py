"""M1续阶段 subagent review 发现项的回归测试(P1×6 + P2高价值)。"""

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
from yantu.acquisition.structurer import extract_window_candidates, merge_deadline
from yantu.acquisition.text_dates import find_datetimes
from yantu.domain.models import Window


def _snap(body: bytes, url="https://x.edu.cn/a.htm", ctype="text/html") -> Snapshot:
    return Snapshot(url=url, fetched_at=datetime(2026, 11, 5, 12, 0),
                    status_code=200, content_type=ctype, body=body)


class TestP1TimeParsing(unittest.TestCase):
    def test_pm_with_wide_whitespace(self):
        """P1-1:多空白/换行后仍能识别下午。"""
        for text in ("截止2026年9月10日  下午5:00", "截止2026年9月10日\n\n下午5:00"):
            hits = find_datetimes(text)
            self.assertEqual(hits[0].dt.hour, 17, msg=text)

    def test_2400_maps_next_day(self):
        """P1-2:24:00 按ISO语义记次日00:00,不整条丢弃。"""
        hits = find_datetimes("报名截止2026年9月10日24:00")
        self.assertEqual(hits[0].dt, datetime(2026, 9, 11, 0, 0))
        hits2 = find_datetimes("截止2026年9月10日24点")
        self.assertEqual(hits2[0].dt, datetime(2026, 9, 11, 0, 0))

    def test_invalid_time_falls_back_to_date_level(self):
        """P1-2:时刻非法(12:60)但日期合法→回退日级,不丢弃。"""
        hits = find_datetimes("截止2026年9月10日12:60")
        self.assertEqual(hits[0].dt, datetime(2026, 9, 10))

    def test_range_phrase_no_bleed(self):
        """P1-3:'X日至Y日9:00'的时刻不得串扰到前一日期。"""
        hits = find_datetimes("2026年9月7日至2026年9月8日9:00")
        by_day = {h.dt.date(): h.dt for h in hits}
        self.assertEqual(by_day[datetime(2026, 9, 7).date()].hour, 0)
        self.assertEqual(by_day[datetime(2026, 9, 8).date()].hour, 9)

    def test_no_minute_form(self):
        hits = find_datetimes("截止2026年9月10日9时")
        self.assertEqual((hits[0].dt.hour, hits[0].dt.minute), (9, 0))


class TestP1StructurerDedupe(unittest.TestCase):
    def test_exact_survives_after_candidate(self):
        """P1-4:同日期先candidate后exact,去重保留exact。"""
        text = "开放截止;材料暂定2026年9月10日。报名截止时间为2026年9月10日。"
        cands = extract_window_candidates(text)
        dead = [c for c in cands if c.kind == "deadline" and c.date == datetime(2026, 9, 10)]
        self.assertEqual(len(dead), 1)
        self.assertEqual(dead[0].confidence, "exact")

    def test_merge_multiple_exacts_takes_earliest(self):
        """P2:多exact取最早(保守),不取最晚。"""
        text = "A批次截止2026年9月1日。B批次截止2026年9月10日。"
        merged = merge_deadline(Window("w"), extract_window_candidates(text))
        self.assertEqual(merged.end, datetime(2026, 9, 1))


HTML = ("<html><head><title>通知</title></head><body>通知 报名截止时间:2026年9月10日 12:00</body></html>"
        ).encode("utf-8")


class TestP1HarvestPersistence(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self.store = SnapshotStore(root / "snaps")
        self.book_path = root / "entries.json"
        self.book = EntryBook(self.book_path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_collect_persists_entry_book(self):
        """P1-5:collect 后入口库落盘(不再依赖调用方save)。"""
        svc = HarvestService(self.store, self.book, fetch_fn=lambda url: _snap(HTML))
        svc.collect("https://x.edu.cn/a.htm", name="通知页")
        self.assertTrue(self.book_path.exists())
        reloaded = EntryBook(self.book_path)
        self.assertEqual(reloaded.get("https://x.edu.cn/a.htm").last_status, "ok")

    def test_fetch_failed_keeps_history(self):
        """P1-6:失败登记不抹掉此前的哈希/时间等入口资产。"""
        svc = HarvestService(self.store, self.book, fetch_fn=lambda url: _snap(HTML))
        svc.collect("https://x.edu.cn/a.htm", name="通知页")
        ok_sha = self.book.get("https://x.edu.cn/a.htm").last_sha256
        self.assertTrue(ok_sha)

        def boom(url):
            raise FetchError(url, "HTTP 403")

        svc2 = HarvestService(self.store, EntryBook(self.book_path), fetch_fn=boom)
        out = svc2.collect("https://x.edu.cn/a.htm", name="通知页")
        self.assertFalse(out.ok)
        rec = EntryBook(self.book_path).get("https://x.edu.cn/a.htm")
        self.assertEqual(rec.last_status, "fetch_failed")
        self.assertEqual(rec.last_sha256, ok_sha)  # 历史哈希保留

    def test_unexpected_exception_registered_then_raised(self):
        """P2:非FetchError异常→登记后上抛,不吞。"""
        def boom(url):
            raise KeyError("config")

        svc = HarvestService(self.store, self.book, fetch_fn=boom)
        with self.assertRaises(KeyError):
            svc.collect("https://x.edu.cn/b.htm", name="坏页")
        self.assertEqual(self.book.get("https://x.edu.cn/b.htm").last_status, "fetch_failed")

    def test_unsupported_snapshot_still_saved(self):
        """P2:UNSUPPORTED载体:ok=True、快照已落盘、状态可从parse_result读出。"""
        svc = HarvestService(self.store, self.book,
                             fetch_fn=lambda url: _snap(b"PDF", ctype="application/pdf"))
        out = svc.collect("https://x.edu.cn/a.pdf", name="附件")
        self.assertTrue(out.ok)
        self.assertEqual(out.parse_result.status, ParseStatus.UNSUPPORTED)
        self.assertTrue(out.snapshot_path.exists())


if __name__ == "__main__":
    unittest.main()
