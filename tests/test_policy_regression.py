"""校验离线报告的样本分母、来源隔离与真实落盘证据。"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from functools import reduce

from yantu.acquisition.structuring_agent import StructuringAgent
from yantu.evaluation.policy_regression import load_source, run_regression

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "data/pilot/trusted_policy"


class TestPolicyRegression(unittest.TestCase):
    def test_report_preserves_full_case_denominator_and_source_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "run"
            report = run_regression(FIXTURES / "manifest.json", output)
            self.assertEqual(report["counts"], {"regression_pass": report["planned"]})
            self.assertEqual(len(report["results"]), report["planned"])
            self.assertEqual(report["modelQuality"], "N/A")
            self.assertEqual(report["realModelCalls"], 0)
            self.assertTrue((output / "report.md").exists())
            actual = next(row for row in report["results"] if row["id"] == "actual_ecnu_source")
            self.assertEqual((actual["observed"]["eligibility"], actual["observed"]["opening"]), ("unknown", "closed"))
            self.assertEqual(actual["evidenceCount"], 6)

    def test_official_snapshot_cannot_be_rewritten_as_synthetic(self):
        manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "官方fixture"):
            load_source(FIXTURES, manifest["sources"]["ecnu"], html_replace=["2027", "2028"])

    def test_corrupt_official_fixture_is_rejected_before_pipeline_execution(self):
        manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
        definition = manifest["sources"]["ecnu"]
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / definition["html"]).write_bytes(b"tampered content")
            (root / definition["metadata"]).write_bytes((FIXTURES / definition["metadata"]).read_bytes())
            with self.assertRaisesRegex(ValueError, "摘要"):
                load_source(root, definition)

    def test_excessive_model_tree_is_rejected_without_recursive_publication(self):
        tree = reduce(lambda node, _: {"type": "and", "conditions": [node]}, range(20),
                      {"type": "unstructured", "quote": "原文", "label": "原文"})
        result = StructuringAgent.validate_output(json.dumps({"conditions": tree}), "原文")
        self.assertEqual(result.status, "degraded")
        self.assertIsNone(result.raw_tree)

    def test_model_condition_type_must_be_a_string(self):
        result = StructuringAgent.validate_output('{"conditions":{"type":[],"quote":"原文"}}', "原文")
        self.assertEqual(result.status, "degraded")

    def test_nonstandard_json_numbers_cannot_enter_audit_record(self):
        result = StructuringAgent.validate_output('{"conditions":{"type":"unstructured","quote":"原文","reason":NaN}}', "原文")
        self.assertEqual(result.status, "degraded")
