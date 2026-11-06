"""可信发布的离线行为测试；不把模拟提取器的结果当作真实模型质量。"""

from __future__ import annotations

import copy
import json
import pathlib
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch

from yantu.acquisition.fetcher import Snapshot
from yantu.acquisition.policy_verification import application_section
from yantu.acquisition.publications import PublicationError, PublicationRepository
from yantu.acquisition.store import SnapshotStore
from yantu.acquisition.structuring_agent import StructuringAgent
from yantu.acquisition.trusted_pipeline import OpportunityScope, TrustedPolicyPipeline
from yantu.domain.eligibility import evaluate
from yantu.domain.models import StudentProfile
from yantu.domain.ranking import rank


SYNTHETIC_HTML = """<html><head><title>合成测试大学计算机学院2027年推免通知</title></head><body>
一、招生学科专业 085405-01 软件工程 专业学位 预报名
二、报名条件 1. CET-6成绩425分及以上。 2. 排名前20%。
三、报名 预报名开始时间为：2026年8月1日09:00；预报名截止时间为：2026年9月7日12:00。
四、综合考核 综合考核总成绩60分及以上为合格。录取后确认待录取。
</body></html>"""

SCOPE = OpportunityScope("synthetic-software-2027", "合成测试大学", "计算机学院", "085405-01 软件工程",
                         "预报名", 2027, "专业学位", "https://fixture.invalid/notice", "fixture.invalid")
TREE = {"type": "and", "conditions": [
    {"type": "cet6_at_least", "threshold": 425, "quote": "CET-6成绩425分及以上"},
    {"type": "ranking_at_most", "threshold": 20, "quote": "排名前20%"},
]}


class DraftExtractor:
    """固定草案的测试替身，仅测试消费边界及发布副作用。"""

    def __init__(self, tree: dict):
        self.tree = tree

    def structure(self, text: str):
        return StructuringAgent.validate_output(json.dumps({"conditions": self.tree}, ensure_ascii=False), text)


def make_snapshot(html: str = SYNTHETIC_HTML) -> Snapshot:
    return Snapshot(SCOPE.source_url, datetime(2026, 8, 2, 10), 200, "text/html; charset=utf-8", html.encode("utf-8"))


class TestTrustedPublication(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.repo = PublicationRepository(self.root / "facts", SnapshotStore(self.root / "snapshots"))

    def tearDown(self):
        self.temp.cleanup()

    def run_pipeline(self, tree=None, html=SYNTHETIC_HTML, scope=SCOPE, expected=None):
        pipeline = TrustedPolicyPipeline(SnapshotStore(self.root / "snapshots"), self.repo, DraftExtractor(tree or TREE))
        return pipeline.run(make_snapshot(html), scope, request_id="test-request", expected_version=expected)

    def test_published_automatic_result_is_reloaded_for_positive_recommendation(self):
        outcome = self.run_pipeline()
        op = PublicationRepository(self.root / "facts", SnapshotStore(self.root / "snapshots")).load(SCOPE.oid)
        profile = StudentProfile(cet6=500, ranking_percent=10)
        item = rank([op], profile, datetime(2026, 8, 15))[0]
        self.assertEqual((outcome.status, item.eligibility.verdict.value, item.included), ("published", "pass", True))

    def test_failing_profile_is_excluded_after_publication_reload(self):
        self.run_pipeline()
        item = rank([self.repo.load(SCOPE.oid)], StudentProfile(cet6=400, ranking_percent=10), datetime(2026, 8, 15))[0]
        self.assertEqual((item.eligibility.verdict.value, item.included), ("fail", False))

    def test_missing_profile_remains_unknown_in_open_candidates(self):
        self.run_pipeline()
        item = rank([self.repo.load(SCOPE.oid)], StudentProfile(ranking_percent=10), datetime(2026, 8, 15))[0]
        self.assertEqual((item.eligibility.verdict.value, item.included), ("unknown", True))

    def test_leaf_evidence_survives_codec_and_storage_roundtrip(self):
        outcome = self.run_pipeline()
        op = self.repo.load(SCOPE.oid)
        detail = evaluate(op, StudentProfile()).details[0]
        evidence = next(e for e in op.evidence if e.eid == detail.evidence_refs[0])
        self.assertEqual(evidence.quote, "CET-6成绩425分及以上")
        self.assertEqual(evidence.snapshot_sha256, make_snapshot().sha256)
        self.assertIn("rawDraft", self.repo.read_record(outcome.version))

    def test_missing_quote_cannot_publish(self):
        tree = copy.deepcopy(TREE)
        tree["conditions"][0].pop("quote")
        outcome = self.run_pipeline(tree)
        self.assertEqual((outcome.status, self.repo.load(SCOPE.oid)), ("quarantined", None))

    def test_real_quote_with_wrong_threshold_cannot_publish(self):
        tree = copy.deepcopy(TREE)
        tree["conditions"][1]["threshold"] = 10
        self.assertEqual(self.run_pipeline(tree).status, "quarantined")

    def test_changed_comparison_direction_cannot_publish(self):
        html = SYNTHETIC_HTML.replace("排名前20%", "排名不高于20%")
        tree = copy.deepcopy(TREE)
        tree["conditions"][1]["quote"] = "排名不高于20%"
        self.assertEqual(self.run_pipeline(tree, html).status, "quarantined")

    def test_omitted_application_clause_is_quarantined(self):
        tree = {"type": "and", "conditions": [copy.deepcopy(TREE["conditions"][0])]}
        self.assertEqual(self.run_pipeline(tree).status, "quarantined")

    def test_group_quote_cannot_hide_an_omitted_leaf(self):
        tree = {"type": "and", "quote": "CET-6成绩425分及以上。 2. 排名前20%",
                "conditions": [copy.deepcopy(TREE["conditions"][0])]}
        self.assertEqual(self.run_pipeline(tree).status, "quarantined")

    def test_or_cannot_regroup_across_numbered_requirements(self):
        html = SYNTHETIC_HTML.replace("CET-6成绩425分及以上", "CET-6成绩425分及以上或雅思6.5分及以上")
        tree = {"type": "or", "quote": "CET-6成绩425分及以上或雅思6.5分及以上。 2. 排名前20%",
                "conditions": [TREE["conditions"][0], {"type": "and", "conditions": [
                    {"type": "ielts_at_least", "threshold": 6.5, "quote": "雅思6.5分及以上"}, TREE["conditions"][1]]}]}
        self.assertEqual(self.run_pipeline(tree, html).status, "quarantined")

    def test_preamble_hard_condition_cannot_be_dropped(self):
        html = SYNTHETIC_HTML.replace("二、报名条件", "二、报名条件 申请人必须已取得推免资格。")
        self.assertEqual(self.run_pipeline(html=html).status, "quarantined")

    def test_additional_required_form_cannot_be_ignored(self):
        html = SYNTHETIC_HTML.replace("四、综合考核", "申请人须同时提交院系表单，表单截止时间为：2026年8月5日12:00。四、综合考核")
        self.assertEqual(self.run_pipeline(html=html).status, "quarantined")

    def test_unrecognized_deadline_cannot_be_ignored(self):
        html = SYNTHETIC_HTML.replace("四、综合考核", "表单截止时间为：2026年8月5日12:00。四、综合考核")
        self.assertEqual(self.run_pipeline(html=html).status, "quarantined")

    def test_model_label_cannot_forge_user_visible_reason(self):
        tree = copy.deepcopy(TREE)
        tree["conditions"][0]["label"] = "排名已达前1%"
        self.run_pipeline(tree)
        result = evaluate(self.repo.load(SCOPE.oid), StudentProfile(cet6=500, ranking_percent=10))
        self.assertEqual(result.details[0].label, "CET-6成绩425分及以上")

    def test_assessment_obligation_does_not_enter_application_tree(self):
        tree = copy.deepcopy(TREE)
        tree["conditions"].append({"type": "unstructured", "quote": "综合考核总成绩60分及以上为合格", "label": "考核要求"})
        self.assertEqual(self.run_pipeline(tree).status, "quarantined")

    def test_application_notes_preserve_future_stage_without_gating(self):
        outcome = self.run_pipeline()
        record = self.repo.read_record(outcome.version)
        self.assertIn("确认待录取", record["nonEligibilityText"])
        self.assertNotIn("确认待录取", str(record["boundTree"]))

    def test_source_scope_year_mismatch_is_quarantined(self):
        self.assertEqual(self.run_pipeline(scope=replace(SCOPE, entry_year=2028)).status, "quarantined")

    def test_source_host_is_not_proved_by_body_text(self):
        self.assertEqual(self.run_pipeline(scope=replace(SCOPE, allowed_host="official.invalid")).status, "quarantined")

    def test_multiple_deadlines_are_not_resolved_by_strictest_wins(self):
        html = SYNTHETIC_HTML.replace("四、综合考核", "预报名截止时间为：2026年9月8日12:00。四、综合考核")
        self.assertEqual(self.run_pipeline(html=html).status, "quarantined")

    def test_missing_opening_boundary_is_not_invented(self):
        html = SYNTHETIC_HTML.replace("预报名开始时间为：2026年8月1日09:00；", "")
        outcome = self.run_pipeline(html=html)
        item = rank([self.repo.load(SCOPE.oid)], StudentProfile(cet6=500, ranking_percent=10), datetime(2026, 8, 15))[0]
        self.assertEqual((outcome.status, item.open_state.value, item.included), ("published_partial", "unknown", False))

    def test_qualitative_clause_preserves_original_quote_and_unknown(self):
        html = SYNTHETIC_HTML.replace("排名前20%", "学习成绩优异")
        tree = copy.deepcopy(TREE)
        tree["conditions"][1] = {"type": "unstructured", "quote": "学习成绩优异", "label": "模型自行概括"}
        outcome = self.run_pipeline(tree, html)
        op = self.repo.load(SCOPE.oid)
        self.assertEqual((outcome.status, op.conditions.conditions[1].quote, evaluate(op, StudentProfile(cet6=500)).verdict.value),
                         ("published_partial", "学习成绩优异", "unknown"))

    def test_repeat_is_idempotent_without_extra_versions(self):
        first = self.run_pipeline()
        second = self.run_pipeline()
        self.assertEqual(first.version, second.version)
        self.assertEqual(len(list((self.repo.root / "versions").glob("*.json"))), 1)

    def test_quarantined_draft_does_not_replace_published_fact(self):
        first = self.run_pipeline()
        tree = copy.deepcopy(TREE)
        tree["conditions"][0].pop("quote")
        result = self.run_pipeline(tree, expected=first.version)
        self.assertEqual((result.status, self.repo.current_version(SCOPE.oid)), ("quarantined", first.version))

    def test_stale_expected_version_cannot_overwrite_fact(self):
        self.run_pipeline()
        html = SYNTHETIC_HTML.replace("9月7日12:00", "9月8日12:00")
        with self.assertRaises(PublicationError):
            self.run_pipeline(html=html)

    def test_prior_version_is_readable_after_valid_update(self):
        first = self.run_pipeline()
        self.run_pipeline(html=SYNTHETIC_HTML.replace("9月7日12:00", "9月8日12:00"), expected=first.version)
        self.assertEqual(self.repo.read_record(first.version)["opportunity"]["windows"][0]["end"], "2026-09-07T12:00:00")

    def test_older_snapshot_cannot_roll_back_a_newer_source(self):
        first = self.run_pipeline()
        older = replace(make_snapshot(), fetched_at=make_snapshot().fetched_at - timedelta(days=1))
        pipeline = TrustedPolicyPipeline(SnapshotStore(self.root / "snapshots"), self.repo, DraftExtractor(TREE))
        with self.assertRaises(PublicationError):
            pipeline.run(older, SCOPE, request_id="old-source", expected_version=first.version)

    def test_corrupt_immutable_record_cannot_be_used_for_recommendation(self):
        first = self.run_pipeline()
        path = self.repo.root / "versions" / f"{first.version}.json"
        path.write_text('{}', encoding="utf-8")
        with self.assertRaises(PublicationError):
            self.repo.load(SCOPE.oid)

    def test_corrupt_existing_snapshot_blocks_publication(self):
        store = SnapshotStore(self.root / "snapshots")
        path = store.save(make_snapshot())
        path.write_bytes(b"corrupted source")
        with self.assertRaises(PublicationError):
            self.run_pipeline()
        self.assertIsNone(self.repo.current_version(SCOPE.oid))

    def test_corrupt_source_after_publication_blocks_candidate_loading(self):
        self.run_pipeline()
        self.repo.snapshots.path_of(make_snapshot().sha256).write_bytes(b"corrupted source")
        with self.assertRaises(PublicationError):
            self.repo.load(SCOPE.oid)

    def test_failed_audit_write_does_not_commit_pointer(self):
        with patch.object(self.repo, "_atomic_write", side_effect=OSError("disk unavailable")):
            with self.assertRaises(PublicationError):
                self.run_pipeline()
        self.assertIsNone(self.repo.current_version(SCOPE.oid))

    def test_unexpected_extractor_bug_is_not_downgraded_to_unknown(self):
        with patch.object(DraftExtractor, "structure", side_effect=RuntimeError("programming error")):
            with self.assertRaises(RuntimeError):
                self.run_pipeline()

    def test_source_section_failure_is_explicit(self):
        with self.assertRaisesRegex(ValueError, "章节"):
            application_section("没有标题的任意网页")


if __name__ == "__main__":
    unittest.main()
