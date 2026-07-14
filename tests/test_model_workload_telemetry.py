from __future__ import annotations

import unittest
from pathlib import Path

import model_workload_telemetry


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


class ModelWorkloadTelemetryTests(unittest.TestCase):
    def test_good_records_validate(self) -> None:
        records, findings = model_workload_telemetry.check_path(EXAMPLES / "runs.jsonl")
        self.assertEqual(findings, [])
        self.assertEqual(len(records), 12)

    def test_expected_bad_records_fail_with_named_code(self) -> None:
        for name, (_, expected_code) in model_workload_telemetry.SELF_TEST_CASES.items():
            if expected_code is None:
                continue
            with self.subTest(name=name):
                _, findings = model_workload_telemetry.check_path(EXAMPLES / name)
                self.assertIn(expected_code, {finding.code for finding in findings})

    def test_integration_comparison_uses_shared_tasks(self) -> None:
        records, findings = model_workload_telemetry.check_path(EXAMPLES / "runs.jsonl")
        self.assertEqual(findings, [])
        report = model_workload_telemetry.build_report(records)
        integration = next(item for item in report["task_classes"] if item["task_class"] == "integration")
        self.assertEqual(integration["shared_task_count"], 2)
        by_model = {item["model"]: item for item in integration["results"]}
        self.assertEqual(by_model["compact-a"]["completion_rate"], 0.5)
        self.assertEqual(by_model["integrator-b"]["completion_rate"], 1.0)

    def test_report_refuses_an_overall_winner(self) -> None:
        records, findings = model_workload_telemetry.check_path(EXAMPLES / "runs.jsonl")
        self.assertEqual(findings, [])
        report = model_workload_telemetry.build_report(records)
        self.assertIn("does not calculate a universal model winner", report["warning"])


if __name__ == "__main__":
    unittest.main()
