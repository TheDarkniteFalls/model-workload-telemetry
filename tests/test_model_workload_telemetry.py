from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

import model_workload_telemetry


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


class ModelWorkloadTelemetryTests(unittest.TestCase):
    def _good_policy(self) -> dict[str, Any]:
        policy, findings = model_workload_telemetry.load_shadow_policy(EXAMPLES / "shadow_route_policy.json")
        self.assertEqual(findings, [])
        self.assertIsNotNone(policy)
        return policy

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

    def test_shadow_route_report_covers_all_four_routes(self) -> None:
        records, findings = model_workload_telemetry.check_path(EXAMPLES / "runs.jsonl")
        self.assertEqual(findings, [])
        report = model_workload_telemetry.build_shadow_route_report(records, self._good_policy())
        routes = {item["task_class"]: item["candidate_route"] for item in report["task_classes"]}
        self.assertEqual(
            routes,
            {
                "integration": "primary_quality",
                "lookup": "deterministic",
                "maintenance": "fast_small",
                "research": "hold",
            },
        )

    def test_invalid_shadow_policy_rejects_same_model_binding(self) -> None:
        _, findings = model_workload_telemetry.load_shadow_policy(EXAMPLES / "bad_shadow_route_policy.json")
        self.assertIn("POLICY_ROUTE_BINDING", {finding.code for finding in findings})

    def test_shadow_route_report_preserves_non_execution_invariants(self) -> None:
        records, findings = model_workload_telemetry.check_path(EXAMPLES / "runs.jsonl")
        self.assertEqual(findings, [])
        report = model_workload_telemetry.build_shadow_route_report(records, self._good_policy())
        self.assertFalse(report["model_called"])
        self.assertFalse(report["network_called"])
        self.assertFalse(report["state_mutating"])
        self.assertEqual(report["actual_route"], "none")
        self.assertFalse(report["automatic_route_change"])
        self.assertEqual(report["promotion_decision"], "not_promoted")
        self.assertTrue(all(item["promotion_decision"] == "not_promoted" for item in report["task_classes"]))

    def test_unbound_model_does_not_change_route_comparison(self) -> None:
        records, findings = model_workload_telemetry.check_path(EXAMPLES / "runs.jsonl")
        self.assertEqual(findings, [])
        observer = dict(
            next(
                record
                for record in records
                if record["model"] == "compact-a" and record["task_id"] == "maintenance-1"
            )
        )
        observer.update(run_id="observer-maintenance-1", model="observer-c")
        records.append(observer)
        self.assertEqual(model_workload_telemetry.validate_records(records), [])

        report = model_workload_telemetry.build_shadow_route_report(records, self._good_policy())
        maintenance = next(item for item in report["task_classes"] if item["task_class"] == "maintenance")
        self.assertEqual(maintenance["candidate_route"], "fast_small")
        self.assertEqual(maintenance["evidence"]["shared_task_count"], 2)
        self.assertEqual(report["input_record_count"], 13)

    def test_infrastructure_failure_holds_without_becoming_quality_failure(self) -> None:
        records, findings = model_workload_telemetry.check_path(EXAMPLES / "runs.jsonl")
        self.assertEqual(findings, [])
        runtime_records = [dict(record) for record in records]
        compact_maintenance = next(
            record
            for record in runtime_records
            if record["model"] == "compact-a" and record["task_class"] == "maintenance"
        )
        compact_maintenance["status"] = "failed"
        compact_maintenance["failure_bucket"] = "infrastructure"
        compact_maintenance["human_score"] = None
        self.assertEqual(model_workload_telemetry.validate_records(runtime_records), [])

        report = model_workload_telemetry.build_shadow_route_report(runtime_records, self._good_policy())
        maintenance = next(item for item in report["task_classes"] if item["task_class"] == "maintenance")
        self.assertEqual(maintenance["candidate_route"], "hold")
        self.assertEqual(maintenance["decision_status"], "hold_runtime_incomplete")
        self.assertEqual(maintenance["rejection_reasons"], ["NOT_ASSESSED_RUNTIME_FAILURE"])
        self.assertEqual(
            [gate["name"] for gate in maintenance["gates"]],
            ["comparison.zero_infrastructure_failures"],
        )

    def test_infrastructure_failure_precedes_missing_model_evidence(self) -> None:
        records, findings = model_workload_telemetry.check_path(EXAMPLES / "runs.jsonl")
        self.assertEqual(findings, [])
        runtime_records = [
            dict(record)
            for record in records
            if not (record["model"] == "integrator-b" and record["task_class"] == "maintenance")
        ]
        compact_maintenance = next(
            record
            for record in runtime_records
            if record["model"] == "compact-a" and record["task_class"] == "maintenance"
        )
        compact_maintenance["status"] = "failed"
        compact_maintenance["failure_bucket"] = "infrastructure"
        compact_maintenance["human_score"] = None
        self.assertEqual(model_workload_telemetry.validate_records(runtime_records), [])

        report = model_workload_telemetry.build_shadow_route_report(runtime_records, self._good_policy())
        maintenance = next(item for item in report["task_classes"] if item["task_class"] == "maintenance")
        self.assertEqual(maintenance["candidate_route"], "hold")
        self.assertEqual(maintenance["decision_status"], "hold_runtime_incomplete")
        self.assertEqual(maintenance["rejection_reasons"], ["NOT_ASSESSED_RUNTIME_FAILURE"])
        self.assertEqual(
            [gate["name"] for gate in maintenance["gates"]],
            ["comparison.zero_infrastructure_failures"],
        )

    def test_policy_rejects_nonfinite_threshold(self) -> None:
        policy = self._good_policy()
        policy["defaults"]["max_avg_wall_seconds"] = float("inf")
        findings = model_workload_telemetry.validate_shadow_policy(policy)
        self.assertIn("POLICY_THRESHOLD", {finding.code for finding in findings})

    def test_human_report_states_all_non_execution_invariants(self) -> None:
        records, findings = model_workload_telemetry.check_path(EXAMPLES / "runs.jsonl")
        self.assertEqual(findings, [])
        report = model_workload_telemetry.build_shadow_route_report(records, self._good_policy())
        rendered = model_workload_telemetry.render_shadow_route_report(report)
        self.assertIn(
            "model_called=false network_called=false state_mutating=false "
            "actual_route=none automatic_route_change=false promotion_decision=not_promoted",
            rendered,
        )


if __name__ == "__main__":
    unittest.main()
