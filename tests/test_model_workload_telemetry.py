from __future__ import annotations

import copy
import json
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

    def _route_receipt_fixture(self, name: str = "route_receipt_enforced.json") -> tuple[dict[str, Any], dict[str, Any]]:
        ground_truth, ground_truth_findings = model_workload_telemetry.load_attempt_ground_truth(
            EXAMPLES / "route_receipt_attempt_ground_truth.json"
        )
        self.assertEqual(ground_truth_findings, [])
        self.assertIsNotNone(ground_truth)
        receipt, receipt_findings = model_workload_telemetry.load_route_receipt(EXAMPLES / name)
        self.assertEqual(receipt_findings, [])
        self.assertIsNotNone(receipt)
        return receipt, ground_truth

    def test_good_records_validate(self) -> None:
        records, findings = model_workload_telemetry.check_path(EXAMPLES / "runs.jsonl")
        self.assertEqual(findings, [])
        self.assertEqual(len(records), 12)

    def test_nonfinite_wall_time_is_rejected_before_metric_calculation(self) -> None:
        records, findings = model_workload_telemetry.check_path(EXAMPLES / "runs.jsonl")
        self.assertEqual(findings, [])
        records[0]["wall_seconds"] = float("inf")
        findings = model_workload_telemetry.validate_records(records)
        self.assertIn("NONNEGATIVE_NUMBER", {finding.code for finding in findings})

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

    def test_human_score_gate_uses_exact_value_at_boundary(self) -> None:
        for fast_score, expected_status, expected_route in (
            (4.0, "pass", "fast_small"),
            (3.999, "fail", "primary_quality"),
        ):
            with self.subTest(fast_score=fast_score):
                records, findings = model_workload_telemetry.check_path(EXAMPLES / "runs.jsonl")
                self.assertEqual(findings, [])
                for record in records:
                    if record["task_class"] == "maintenance" and record["model"] == "compact-a":
                        record["human_score"] = fast_score

                report = model_workload_telemetry.build_shadow_route_report(records, self._good_policy())
                maintenance = next(
                    item for item in report["task_classes"] if item["task_class"] == "maintenance"
                )
                score_gate = next(
                    gate for gate in maintenance["gates"] if gate["name"] == "fast_small.minimum_human_score"
                )
                self.assertEqual(score_gate["status"], expected_status)
                self.assertEqual(score_gate["observed"], fast_score)
                self.assertEqual(maintenance["candidate_route"], expected_route)

                presented_score = maintenance["evidence"]["fast_small"]["avg_human_score_completed"]
                self.assertEqual(presented_score, round(fast_score, 2))

    def test_wall_time_gate_uses_exact_value_before_presentation_rounding(self) -> None:
        records, findings = model_workload_telemetry.check_path(EXAMPLES / "runs.jsonl")
        self.assertEqual(findings, [])
        for record in records:
            if record["task_class"] != "maintenance":
                continue
            record["wall_seconds"] = 60.001 if record["model"] == "compact-a" else 60.0

        report = model_workload_telemetry.build_shadow_route_report(records, self._good_policy())
        maintenance = next(item for item in report["task_classes"] if item["task_class"] == "maintenance")
        wall_gate = next(
            gate for gate in maintenance["gates"] if gate["name"] == "fast_small.maximum_average_wall_seconds"
        )
        self.assertEqual(wall_gate["status"], "fail")
        self.assertEqual(wall_gate["observed"], 60.001)
        self.assertEqual(maintenance["evidence"]["fast_small"]["avg_wall_seconds_per_attempt"], 60.0)

    def test_completion_gate_uses_exact_ratio_before_presentation_rounding(self) -> None:
        records, findings = model_workload_telemetry.check_path(EXAMPLES / "runs.jsonl")
        self.assertEqual(findings, [])
        fast_extra = dict(
            next(
                record
                for record in records
                if record["task_class"] == "maintenance" and record["model"] == "compact-a"
            )
        )
        fast_extra.update(
            run_id="run-m3-compact",
            task_id="maintenance-3",
            status="failed",
            failure_bucket="human_revision",
            human_score=None,
        )
        primary_extra = dict(
            next(
                record
                for record in records
                if record["task_class"] == "maintenance" and record["model"] == "integrator-b"
            )
        )
        primary_extra.update(run_id="run-m3-integrator", task_id="maintenance-3")
        records.extend((fast_extra, primary_extra))
        self.assertEqual(model_workload_telemetry.validate_records(records), [])

        policy = self._good_policy()
        policy["defaults"]["min_shared_tasks"] = 3
        policy["defaults"]["min_completion_rate"] = 0.667
        report = model_workload_telemetry.build_shadow_route_report(records, policy)
        maintenance = next(item for item in report["task_classes"] if item["task_class"] == "maintenance")
        completion_gate = next(
            gate for gate in maintenance["gates"] if gate["name"] == "fast_small.minimum_completion_rate"
        )
        self.assertEqual(completion_gate["status"], "fail")
        self.assertLess(completion_gate["observed"], 0.667)
        self.assertEqual(maintenance["evidence"]["fast_small"]["completion_rate"], 0.667)

    def test_relative_gates_use_exact_values_on_both_sides_of_thresholds(self) -> None:
        for primary_score, expected_status, expected_route in (
            (4.5, "pass", "fast_small"),
            (4.5001, "fail", "primary_quality"),
        ):
            with self.subTest(gate="quality_gap", primary_score=primary_score):
                records, findings = model_workload_telemetry.check_path(EXAMPLES / "runs.jsonl")
                self.assertEqual(findings, [])
                for record in records:
                    if record["task_class"] != "maintenance":
                        continue
                    record["human_score"] = 4.0 if record["model"] == "compact-a" else primary_score

                report = model_workload_telemetry.build_shadow_route_report(records, self._good_policy())
                maintenance = next(
                    item for item in report["task_classes"] if item["task_class"] == "maintenance"
                )
                quality_gate = next(
                    gate for gate in maintenance["gates"] if gate["name"] == "fast_small.maximum_quality_gap"
                )
                self.assertEqual(quality_gate["status"], expected_status)
                self.assertEqual(maintenance["candidate_route"], expected_route)

        for fast_wall, expected_status, expected_route in (
            (13.05, "pass", "fast_small"),
            (13.051, "fail", "primary_quality"),
        ):
            with self.subTest(gate="latency_advantage", fast_wall=fast_wall):
                records, findings = model_workload_telemetry.check_path(EXAMPLES / "runs.jsonl")
                self.assertEqual(findings, [])
                for record in records:
                    if record["task_class"] != "maintenance":
                        continue
                    record["human_score"] = 4.5
                    record["wall_seconds"] = fast_wall if record["model"] == "compact-a" else 14.5

                report = model_workload_telemetry.build_shadow_route_report(records, self._good_policy())
                maintenance = next(
                    item for item in report["task_classes"] if item["task_class"] == "maintenance"
                )
                latency_gate = next(
                    gate
                    for gate in maintenance["gates"]
                    if gate["name"] == "fast_small.minimum_latency_advantage"
                )
                self.assertEqual(latency_gate["status"], expected_status)
                self.assertEqual(maintenance["candidate_route"], expected_route)

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

    def test_route_receipt_json_schema_is_strict_and_matches_examples(self) -> None:
        schema = json.loads((ROOT / "schemas" / "route_receipt_v0.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["schema_version"]["const"], "route_receipt_v0")
        for nested in ("fallback", "quality", "writes", "finalization"):
            self.assertFalse(schema["properties"][nested]["additionalProperties"])
        self.assertFalse(schema["$defs"]["attempt"]["additionalProperties"])
        self.assertEqual(schema["$defs"]["empty_string_list"]["maxItems"], 0)
        for name in model_workload_telemetry.ROUTE_RECEIPT_SELF_TEST_CASES:
            receipt = json.loads((EXAMPLES / name).read_text(encoding="utf-8"))
            self.assertEqual(set(receipt), set(schema["required"]))

    def test_attempt_ground_truth_is_independent_and_non_executing(self) -> None:
        _, ground_truth = self._route_receipt_fixture()
        self.assertEqual(model_workload_telemetry.validate_attempt_ground_truth(ground_truth), [])
        self.assertFalse(ground_truth["model_called"])
        self.assertFalse(ground_truth["network_called"])
        self.assertFalse(ground_truth["state_mutating"])
        self.assertEqual([attempt["ordinal"] for attempt in ground_truth["attempts"]], [1, 2])

    def test_attempt_ground_truth_binds_candidate_and_final_attempts(self) -> None:
        _, ground_truth = self._route_receipt_fixture()
        wrong_candidate = copy.deepcopy(ground_truth)
        wrong_candidate["candidate_route"] = "primary_quality"
        wrong_final = copy.deepcopy(ground_truth)
        wrong_final["final_attempt_id"] = "attempt-fast-1"
        self.assertIn(
            "GROUND_TRUTH_CANDIDATE_ATTRIBUTION",
            {finding.code for finding in model_workload_telemetry.validate_attempt_ground_truth(wrong_candidate)},
        )
        self.assertIn(
            "GROUND_TRUTH_FINAL_ATTEMPT",
            {finding.code for finding in model_workload_telemetry.validate_attempt_ground_truth(wrong_final)},
        )

    def test_passive_and_enforced_route_receipts_validate_with_distinct_authority(self) -> None:
        passive, ground_truth = self._route_receipt_fixture("route_receipt_passive.json")
        enforced, _ = self._route_receipt_fixture("route_receipt_enforced.json")
        self.assertEqual(model_workload_telemetry.validate_route_receipt(passive, ground_truth), [])
        self.assertEqual(model_workload_telemetry.validate_route_receipt(enforced, ground_truth), [])
        self.assertEqual((passive["enforcement_status"], passive["completion_claim"]), ("not_applied", "observed_only"))
        self.assertEqual((enforced["enforcement_status"], enforced["completion_claim"]), ("pass", "auditable_complete"))
        self.assertEqual(passive["attempts"], enforced["attempts"])

    def test_route_receipt_rejects_missing_attempt(self) -> None:
        receipt, ground_truth = self._route_receipt_fixture()
        receipt = copy.deepcopy(receipt)
        receipt["attempts"].pop(0)
        findings = model_workload_telemetry.validate_route_receipt(receipt, ground_truth)
        self.assertIn("RECEIPT_MISSING_ATTEMPT", {finding.code for finding in findings})

    def test_route_receipt_malformed_values_fail_closed(self) -> None:
        receipt, ground_truth = self._route_receipt_fixture()
        receipt = copy.deepcopy(receipt)
        receipt["candidate_route"] = []
        receipt["final_attempt_id"] = []
        findings = model_workload_telemetry.validate_route_receipt(receipt, ground_truth)
        codes = {finding.code for finding in findings}
        self.assertIn("RECEIPT_ROUTE", codes)
        self.assertIn("RECEIPT_FINAL_ATTEMPT_ATTRIBUTION", codes)

    def test_passive_route_receipt_cannot_claim_enforcement(self) -> None:
        receipt, ground_truth = self._route_receipt_fixture("route_receipt_passive.json")
        receipt = copy.deepcopy(receipt)
        receipt["enforcement_status"] = "pass"
        receipt["completion_claim"] = "auditable_complete"
        findings = model_workload_telemetry.validate_route_receipt(receipt, ground_truth)
        self.assertIn("RECEIPT_PASSIVE_AUTHORITY", {finding.code for finding in findings})

    def test_route_receipt_rejects_wrong_final_model_attribution(self) -> None:
        receipt, ground_truth = self._route_receipt_fixture()
        receipt = copy.deepcopy(receipt)
        receipt["final_model"] = "compact-a"
        findings = model_workload_telemetry.validate_route_receipt(receipt, ground_truth)
        self.assertIn("RECEIPT_FINAL_MODEL_ATTRIBUTION", {finding.code for finding in findings})

    def test_route_receipt_rejects_unassessed_quality_contamination(self) -> None:
        receipt, ground_truth = self._route_receipt_fixture()
        receipt = copy.deepcopy(receipt)
        receipt["quality"]["assessed_attempt_ids"].append("attempt-fast-1")
        findings = model_workload_telemetry.validate_route_receipt(receipt, ground_truth)
        self.assertIn("RECEIPT_UNASSESSED_QUALITY_CONTAMINATION", {finding.code for finding in findings})

    def test_route_receipt_rejects_forbidden_fallback(self) -> None:
        receipt, ground_truth = self._route_receipt_fixture()
        receipt = copy.deepcopy(receipt)
        receipt["fallback"]["trigger_outcome"] = "validation_failure"
        findings = model_workload_telemetry.validate_route_receipt(receipt, ground_truth)
        self.assertIn("RECEIPT_FORBIDDEN_FALLBACK", {finding.code for finding in findings})

    def test_route_receipt_rejects_finalization_failure(self) -> None:
        receipt, ground_truth = self._route_receipt_fixture()
        receipt = copy.deepcopy(receipt)
        receipt["finalization"] = {"status": "failed", "integrity_verified": False}
        findings = model_workload_telemetry.validate_route_receipt(receipt, ground_truth)
        self.assertIn("RECEIPT_FINALIZATION_FAILURE", {finding.code for finding in findings})

    def test_route_receipt_rejects_unexpected_writes(self) -> None:
        receipt, ground_truth = self._route_receipt_fixture()
        receipt = copy.deepcopy(receipt)
        receipt["writes"]["observed"] = ["state/unexpected.json"]
        receipt["writes"]["unexpected"] = ["state/unexpected.json"]
        findings = model_workload_telemetry.validate_route_receipt(receipt, ground_truth)
        self.assertIn("RECEIPT_UNEXPECTED_WRITES", {finding.code for finding in findings})


if __name__ == "__main__":
    unittest.main()
