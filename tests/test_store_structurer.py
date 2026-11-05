"""存储与确定性结构化的单元测试(全部离线,用例文本取自ECNU官方页真实句式)。"""

import pathlib
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from yantu.acquisition.fetcher import Snapshot
from yantu.acquisition.store import (
    EntryBook,
    EntryRecord,
    OpportunityRepository,
    SnapshotStore,
    opportunity_to_record,
    record_to_opportunity,
)
from yantu.acquisition.structurer import (
    extract_window_candidates,
    merge_deadline,
)
from yantu.domain import condition_codec as codec
from yantu.domain.models import AndGroup, Opportunity, Window

REAL_TEXT = (
    "（二）报名截止时间 学院推免预报名截止时间为：2026年9月7日中午12:00 "
    "四、综合考核 我院将组织专家对推免生的材料进行审核。系统开放:2026年7月24日。"
    "落款:2026年7月24日"
)


class TestStructurer(unittest.TestCase):
    def test_exact_deadline_from_real_text(self):
        cands = extract_window_candidates(REAL_TEXT)
        exact = [c for c in cands if c.confidence == "exact"]
        self.assertTrue(exact)
        self.assertEqual(exact[0].date, datetime(2026, 9, 7, 12, 0))  # 中午12:00
        self.assertIn("截止时间为", exact[0].quote)

    def test_open_start_candidate(self):
        cands = extract_window_candidates(REAL_TEXT)
        opens = [c for c in cands if c.kind == "open_start"]
        self.assertTrue(any(c.date == datetime(2026, 7, 24) for c in opens))

    def test_luo_kuan_not_extracted(self):
        """落款日期(无截止/开放句式)不得被提取——保守优先。"""
        cands = extract_window_candidates("特此通知。落款:2026年7月24日")
        self.assertEqual(cands, [])

    def test_pm_time(self):
        cands = extract_window_candidates("报名截止时间:2026年9月10日下午5:00")
        self.assertEqual(cands[0].date.hour, 17)

    def test_invalid_date_ignored(self):
        self.assertEqual(extract_window_candidates("截止时间为:2026年13月40日"), [])

    def test_merge_fills_missing_end(self):
        w = Window("系统", True, None, None)
        merged = merge_deadline(w, extract_window_candidates(REAL_TEXT))
        self.assertEqual(merged.end, datetime(2026, 9, 7, 12, 0))

    def test_merge_keeps_existing_end(self):
        """人工/先验截止优先于启发式(复核门禁)。"""
        w = Window("系统", True, None, datetime(2026, 9, 1))
        merged = merge_deadline(w, extract_window_candidates(REAL_TEXT))
        self.assertEqual(merged.end, datetime(2026, 9, 1))


class TestStores(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _snap(self, body: bytes = "<html>内容</html>".encode("utf-8")) -> Snapshot:
        return Snapshot(
            url="https://x.edu.cn/a.htm",
            fetched_at=datetime(2026, 11, 5, 12, 0),
            status_code=200,
            content_type="text/html",
            body=body,
        )

    def test_snapshot_content_addressed_no_overwrite(self):
        store = SnapshotStore(self.root / "snaps")
        p1 = store.save(self._snap(b"AAA"), title="第一版")
        p2 = store.save(self._snap(b"BBB"), title="第二版")  # 同URL新内容
        self.assertNotEqual(p1.name, p2.name)  # 不覆盖:两份快照并存
        again = store.save(self._snap(b"AAA"))
        self.assertEqual(again, p1)  # 同内容幂等

    def test_entry_book_upsert_and_reload(self):
        book = EntryBook(self.root / "entries.json")
        book.upsert(
            EntryRecord(url="https://x.edu.cn/a.htm", name="通知页", last_status="ok")
        )
        book.save()
        reloaded = EntryBook(self.root / "entries.json")
        self.assertEqual(reloaded.get("https://x.edu.cn/a.htm").name, "通知页")

    def test_opportunity_repository_roundtrip(self):
        opp = Opportunity(
            oid="t-1", school="S", department="D", program="P", batch="B",
            entry_year=2027, degree_type="专硕",
            windows=(Window("w", True, datetime(2026, 7, 1), datetime(2026, 9, 7, 12, 0)),),
            conditions=codec.build(
                {"type": "and", "conditions": [
                    {"type": "ranking_at_most", "threshold": 20.0, "label": "前20%"}
                ]}
            ),
            priorities=(),
        )
        repo = OpportunityRepository(self.root / "opps.json")
        repo.save([opp])
        loaded = repo.load()
        self.assertEqual(loaded[0].oid, "t-1")
        self.assertEqual(loaded[0].windows[0].end, datetime(2026, 9, 7, 12, 0))
        # 条件树经codec往返后语义一致
        self.assertEqual(loaded[0].conditions, opp.conditions)
        # 记录↔对象双向可逆
        self.assertEqual(record_to_opportunity(opportunity_to_record(opp)), opp)


if __name__ == "__main__":
    unittest.main()
