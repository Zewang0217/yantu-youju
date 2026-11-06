"""LLM 结构化 Agent 的离线测试(注入假客户端,不触真实网络/模型)。"""

import pathlib
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from yantu.acquisition.structuring_agent import StructuringAgent
from yantu.llm import ChatClient, load_client

REAL_TEXT = (
    "（二）报名截止时间 学院推免预报名截止时间为：2026年9月7日中午12:00。"
    "申请条件：全国大学英语六级考试成绩425分及以上，或雅思6.5分及以上。"
    "本科前六学期成绩排名原则上为前30%（需注明参加排名的总人数并盖章）。"
    "有科研成果者优先。身心健康。"
)


class FakeClient:
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []

    def chat(self, messages, temperature=0.1):
        self.calls.append(messages)
        r = self._replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


VALID_JSON = (
    '{"conditions": {"type": "and", "conditions": ['
    '{"type": "or", "label": "英语", "quote":"全国大学英语六级考试成绩425分及以上，或雅思6.5分及以上", "conditions": ['
    '{"type": "cet6_at_least", "threshold": 425, "quote": "全国大学英语六级考试成绩425分及以上"},'
    '{"type": "ielts_at_least", "threshold": 6.5, "quote": "雅思6.5分及以上"}]},'
    '{"type": "unstructured", "label": "排名口径", "quote": "前六学期成绩排名原则上为前30%",'
    '"reason": "原则上=裁量放宽,口径需人工确认"}]}, "notes": []}'
)


class TestStructuringAgent(unittest.TestCase):
    def test_valid_spec_passes_gates(self):
        agent = StructuringAgent(client=FakeClient([VALID_JSON]))
        r = agent.structure(REAL_TEXT)
        self.assertEqual(r.status, "structured")
        self.assertEqual(r.attempts, 1)
        self.assertTrue(all(c.passed for c in r.gate_report))
        self.assertIn("quote", r.conditions_spec["conditions"][1])
        self.assertTrue(r.claims)
        # 确定性窗口并行保留
        self.assertTrue(any(c.date == datetime(2026, 9, 7, 12, 0) for c in r.windows))

    def test_hallucinated_quote_degrades_after_retry(self):
        """quote 不在原文→门禁拒;重试仍错→整体降级。"""
        bad = VALID_JSON.replace("全国大学英语六级考试成绩425分及以上", "六级成绩500分以上(编造)")
        agent = StructuringAgent(client=FakeClient([bad, bad]))
        r = agent.structure(REAL_TEXT)
        self.assertEqual(r.status, "degraded")
        self.assertIsNone(r.conditions_spec)
        self.assertFalse(any(c.name == "quote_grounding" and c.passed for c in r.gate_report))
        self.assertEqual(r.attempts, 2)
        self.assertTrue(r.windows)  # 确定性部分不受影响

    def test_retry_can_recover(self):
        bad = VALID_JSON.replace("全国大学英语六级考试成绩425分及以上", "不存在的摘录")
        agent = StructuringAgent(client=FakeClient([bad, VALID_JSON]))
        r = agent.structure(REAL_TEXT)
        self.assertEqual(r.status, "structured")
        self.assertEqual(r.attempts, 2)

    def test_unknown_condition_type_rejected(self):
        bad = '{"conditions": {"type": "and", "conditions": [{"type": "六百分制", "quote": "六级"}]}}'
        agent = StructuringAgent(client=FakeClient([bad, bad]))
        r = agent.structure(REAL_TEXT)
        self.assertEqual(r.status, "degraded")
        self.assertTrue(any(not c.passed for c in r.gate_report))

    def test_non_json_output_degrades(self):
        agent = StructuringAgent(client=FakeClient(["我认为这个政策很友好", "仍然没有JSON"]))
        r = agent.structure(REAL_TEXT)
        self.assertEqual(r.status, "degraded")
        self.assertTrue(any(c.name == "json_parse" and not c.passed for c in r.gate_report))

    def test_llm_error_degrades_without_retry(self):
        from yantu.llm import LLMError

        agent = StructuringAgent(client=FakeClient([LLMError("HTTP 401")]))
        r = agent.structure(REAL_TEXT)
        self.assertEqual(r.status, "degraded")
        self.assertEqual(r.attempts, 1)
        self.assertTrue(any(c.name == "llm_call" and not c.passed for c in r.gate_report))

    def test_no_client_skipped_with_deterministic_windows(self):
        r = StructuringAgent(client=None).structure(REAL_TEXT)
        self.assertEqual(r.status, "skipped")
        self.assertIsNone(r.conditions_spec)
        self.assertTrue(any(c.date == datetime(2026, 9, 7, 12, 0) for c in r.windows))


class TestChatClientConfig(unittest.TestCase):
    def test_load_client_missing_file(self):
        self.assertIsNone(load_client(pathlib.Path("no/such/file.json")))

    def test_load_client_placeholder_key(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "llm.json"
            p.write_text('{"base_url": "https://x", "api_key": "在这里填入你的API Key", "model": "m"}', encoding="utf-8")
            self.assertIsNone(load_client(p))

    def test_load_client_valid(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "llm.json"
            p.write_text('{"base_url": "https://x/v4", "api_key": "sk-real", "model": "glm-4.6"}', encoding="utf-8")
            c = load_client(p)
            self.assertIsInstance(c, ChatClient)
            self.assertTrue(c.is_configured)
            self.assertEqual(c.model, "glm-4.6")


if __name__ == "__main__":
    unittest.main()
