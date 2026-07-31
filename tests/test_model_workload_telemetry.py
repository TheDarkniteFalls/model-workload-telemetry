from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

import model_workload_telemetry


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
PHASE2 = EXAMPLES / "phase2"


class ModelWorkloadTelemetryTests(unittest.TestCase):
    def _phase2_manifest(self) -> tuple[dict[str, Any], Path]:
        path = PHASE2 / "route_receipt_case_manifest_v1.json"
        manifest, findings = model_workload_telemetry.load_route_receipt_case_manifest(path)
        self.assertEqual(findings, [])
        self.assertIsNotNone(manifest)
        assert manifest is not None
        return manifest, path

    def _phase3_manifest(self) -> tuple[dict[str, Any], Path]:
        path = EXAMPLES / "phase3_decision_receipt_provenance_manifest_v1.json"
        manifest, findings = (
            model_workload_telemetry.load_decision_receipt_provenance_manifest(path)
        )
        self.assertEqual(findings, [])
        self.assertIsNotNone(manifest)
        assert manifest is not None
        return manifest, path

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

    def test_v1_case_manifest_schema_is_strict_and_matches_manifest(self) -> None:
        schema = json.loads(
            (ROOT / "schemas" / "route_receipt_case_manifest_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        manifest, _ = self._phase2_manifest()
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            schema["properties"]["schema_version"]["const"],
            "route_receipt_case_manifest_v1",
        )
        for name in ("artifact", "receipt", "mutation", "case"):
            self.assertFalse(schema["$defs"][name]["additionalProperties"])
        self.assertEqual(set(manifest), set(schema["required"]))
        self.assertEqual(len(manifest["cases"]), 10)
        self.assertEqual(
            sum(len(case["receipts"]) for case in manifest["cases"]),
            12,
        )
        self.assertEqual(
            sum(len(case["mutations"]) for case in manifest["cases"]),
            24,
        )

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

    def test_v1_positive_fixture_matrix_validates_against_independent_truth(self) -> None:
        manifest, _ = self._phase2_manifest()
        receipt_schema = json.loads(
            (ROOT / "schemas" / "route_receipt_v1.schema.json").read_text(encoding="utf-8")
        )
        ground_truth_schema = json.loads(
            (ROOT / "schemas" / "route_attempt_ground_truth_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        expected_cases = {
            "P2-D01": ("completed_without_fallback", "deliver", "pass"),
            "P2-M01": ("completed_without_fallback", "deliver", "pass"),
            "P2-M02": ("completed_via_fallback", "deliver", "pass"),
            "P2-I01": ("completed_via_fallback", "deliver", "pass"),
            "P2-I02": ("completed_via_fallback", "deliver", "pass"),
            "P2-I03": ("hold", "hold", "not_assessed"),
            "P2-Q01": ("completed_without_fallback", "hold", "fail"),
            "P2-R01": ("hold", "hold", "not_assessed"),
            "P2-R02": ("hold", "hold", "not_assessed"),
            "P2-S01": ("hold", "hold", "not_assessed"),
        }

        seen_cases: dict[str, tuple[str, str, str]] = {}
        receipt_count = 0
        for case in manifest["cases"]:
            with self.subTest(case=case["case_id"]):
                ground_truth, findings = model_workload_telemetry.load_attempt_ground_truth(
                    PHASE2 / case["ground_truth"]["path"]
                )
                self.assertEqual(findings, [])
                self.assertIsNotNone(ground_truth)
                assert ground_truth is not None
                self.assertEqual(set(ground_truth), set(ground_truth_schema["required"]))
                self.assertFalse(ground_truth["model_called"])
                self.assertFalse(ground_truth["network_called"])
                self.assertFalse(ground_truth["state_mutating"])
                seen_cases[ground_truth["case_id"]] = (
                    ground_truth["completion_status"],
                    ground_truth["decision_disposition"],
                    ground_truth["final_quality_status"],
                )

                for receipt_spec in case["receipts"]:
                    receipt_count += 1
                    receipt, receipt_findings = model_workload_telemetry.load_route_receipt(
                        PHASE2 / receipt_spec["path"]
                    )
                    self.assertEqual(receipt_findings, [])
                    self.assertIsNotNone(receipt)
                    assert receipt is not None
                    self.assertEqual(set(receipt), set(receipt_schema["required"]))
                    self.assertEqual(
                        model_workload_telemetry.validate_route_receipt(receipt, ground_truth),
                        [],
                    )
                    self.assertFalse(receipt["model_called"])
                    self.assertFalse(receipt["network_called"])
                    self.assertFalse(receipt["state_mutating"])
                    self.assertEqual(receipt["actual_route"], "none")
                    self.assertFalse(receipt["automatic_route_change"])
                    self.assertEqual(receipt["promotion_decision"], "not_promoted")

        self.assertEqual(seen_cases, expected_cases)
        self.assertEqual(receipt_count, 12)

    def test_v1_runtime_stage_and_boundary_cases_remain_distinct(self) -> None:
        expected_stages = {
            "p2_m02": "pre_request",
            "p2_i01": "request_open",
            "p2_i02": "response_stream",
        }
        for case, expected_stage in expected_stages.items():
            with self.subTest(case=case):
                truth, findings = model_workload_telemetry.load_attempt_ground_truth(
                    PHASE2 / f"{case}_ground_truth.json"
                )
                self.assertEqual(findings, [])
                assert truth is not None
                self.assertEqual(truth["attempts"][0]["failure_stage"], expected_stage)
                self.assertEqual(
                    truth["attempts"][0]["quality_status"],
                    "not_assessed_runtime_failure",
                )

        i02_truth, _ = model_workload_telemetry.load_attempt_ground_truth(
            PHASE2 / "p2_i02_ground_truth.json"
        )
        i02_receipt, _ = model_workload_telemetry.load_route_receipt(
            PHASE2 / "p2_i02_enforced.json"
        )
        assert i02_truth is not None and i02_receipt is not None
        self.assertEqual(i02_truth["attempts"][0]["responding_model"], "compact-a")
        self.assertGreater(i02_truth["attempts"][0]["output_tokens"], 0)
        self.assertNotIn(
            i02_truth["attempts"][0]["attempt_id"],
            i02_receipt["quality"]["assessed_attempt_ids"],
        )

        r02_truth, _ = model_workload_telemetry.load_attempt_ground_truth(
            PHASE2 / "p2_r02_ground_truth.json"
        )
        assert r02_truth is not None
        self.assertIsNone(r02_truth["attempts"][0]["responding_model"])
        self.assertIsNone(r02_truth["final_model"])
        self.assertEqual(r02_truth["decision_disposition"], "hold")

    def test_v1_passive_authority_is_distinct_for_delivery_and_hold(self) -> None:
        for case in ("p2_d01", "p2_i03"):
            with self.subTest(case=case):
                ground_truth, findings = model_workload_telemetry.load_attempt_ground_truth(
                    PHASE2 / f"{case}_ground_truth.json"
                )
                self.assertEqual(findings, [])
                self.assertIsNotNone(ground_truth)
                enforced, _ = model_workload_telemetry.load_route_receipt(
                    PHASE2 / f"{case}_enforced.json"
                )
                passive, _ = model_workload_telemetry.load_route_receipt(
                    PHASE2 / f"{case}_passive.json"
                )
                self.assertIsNotNone(enforced)
                self.assertIsNotNone(passive)
                assert ground_truth is not None and enforced is not None and passive is not None
                self.assertEqual(model_workload_telemetry.validate_route_receipt(enforced, ground_truth), [])
                self.assertEqual(model_workload_telemetry.validate_route_receipt(passive, ground_truth), [])
                self.assertEqual(passive["attempts"], enforced["attempts"])
                self.assertEqual(passive["decision_disposition"], enforced["decision_disposition"])
                self.assertEqual(
                    (passive["enforcement_status"], passive["completion_claim"]),
                    ("not_applied", "observed_only"),
                )
                expected_claim = (
                    "auditable_complete"
                    if ground_truth["decision_disposition"] == "deliver"
                    else "auditable_hold"
                )
                self.assertEqual(
                    (enforced["enforcement_status"], enforced["completion_claim"]),
                    ("pass", expected_claim),
                )

    def test_route_receipt_dispatch_keeps_v0_and_v1_exact(self) -> None:
        v0_receipt, v0_truth = self._route_receipt_fixture()
        v1_truth, v1_truth_findings = model_workload_telemetry.load_attempt_ground_truth(
            PHASE2 / "p2_d01_ground_truth.json"
        )
        v1_receipt, v1_receipt_findings = model_workload_telemetry.load_route_receipt(
            PHASE2 / "p2_d01_enforced.json"
        )
        self.assertEqual(v1_truth_findings, [])
        self.assertEqual(v1_receipt_findings, [])
        assert v1_truth is not None and v1_receipt is not None
        self.assertEqual(model_workload_telemetry.validate_route_receipt(v0_receipt, v0_truth), [])
        self.assertEqual(model_workload_telemetry.validate_route_receipt(v1_receipt, v1_truth), [])
        self.assertIn(
            "RECEIPT_SCHEMA_VERSION_MISMATCH",
            {
                finding.code
                for finding in model_workload_telemetry.validate_route_receipt(v1_receipt, v0_truth)
            },
        )
        self.assertIn(
            "RECEIPT_SCHEMA_VERSION_MISMATCH",
            {
                finding.code
                for finding in model_workload_telemetry.validate_route_receipt(v0_receipt, v1_truth)
            },
        )

    def test_v1_json_types_do_not_use_python_numeric_equality(self) -> None:
        ground_truth, ground_truth_findings = model_workload_telemetry.load_attempt_ground_truth(
            PHASE2 / "p2_m02_ground_truth.json"
        )
        receipt, receipt_findings = model_workload_telemetry.load_route_receipt(
            PHASE2 / "p2_m02_enforced.json"
        )
        self.assertEqual(ground_truth_findings, [])
        self.assertEqual(receipt_findings, [])
        assert ground_truth is not None and receipt is not None

        bool_ground_truth = copy.deepcopy(ground_truth)
        bool_ground_truth["attempts"][0]["ordinal"] = True
        self.assertIn(
            "GROUND_TRUTH_ATTEMPT_ORDER",
            {
                finding.code
                for finding in model_workload_telemetry.validate_attempt_ground_truth(bool_ground_truth)
            },
        )

        float_receipt = copy.deepcopy(receipt)
        float_receipt["attempts"][0]["ordinal"] = 1.0
        self.assertIn(
            "RECEIPT_ATTEMPT_ORDER",
            {
                finding.code
                for finding in model_workload_telemetry.validate_route_receipt(
                    float_receipt, ground_truth
                )
            },
        )

        bool_transition = copy.deepcopy(receipt)
        bool_transition["fallback_transitions"][0]["ordinal"] = True
        self.assertIn(
            "RECEIPT_FALLBACK",
            {
                finding.code
                for finding in model_workload_telemetry.validate_route_receipt(
                    bool_transition, ground_truth
                )
            },
        )

        numeric_permitted = copy.deepcopy(receipt)
        numeric_permitted["fallback_transitions"][0]["permitted"] = 1
        self.assertIn(
            "RECEIPT_FALLBACK",
            {
                finding.code
                for finding in model_workload_telemetry.validate_route_receipt(
                    numeric_permitted, ground_truth
                )
            },
        )

    def test_v1_manifest_mutations_produce_every_primary_finding_without_mutating_bases(self) -> None:
        manifest, _ = self._phase2_manifest()
        seen_mutations: set[str] = set()
        for case in manifest["cases"]:
            ground_truth, truth_findings = model_workload_telemetry.load_attempt_ground_truth(
                PHASE2 / case["ground_truth"]["path"]
            )
            self.assertEqual(truth_findings, [])
            assert ground_truth is not None
            receipts = {item["receipt_id"]: item for item in case["receipts"]}
            for mutation in case["mutations"]:
                mutation_id = mutation["mutation_id"]
                with self.subTest(mutation=mutation_id):
                    receipt_spec = receipts[mutation["base_receipt_id"]]
                    receipt_path = PHASE2 / receipt_spec["path"]
                    receipt, receipt_findings = model_workload_telemetry.load_route_receipt(
                        receipt_path
                    )
                    self.assertEqual(receipt_findings, [])
                    assert receipt is not None
                    original = copy.deepcopy(receipt)
                    findings = model_workload_telemetry._run_route_receipt_v1_mutation(
                        mutation_id,
                        receipt,
                        ground_truth,
                        receipt_path.read_text(encoding="utf-8"),
                    )
                    self.assertIn(
                        mutation["primary_finding_code"],
                        {finding.code for finding in findings},
                    )
                    self.assertEqual(receipt, original)
                    seen_mutations.add(mutation_id)
        self.assertEqual(seen_mutations, model_workload_telemetry.ROUTE_RECEIPT_V1_MUTATION_IDS)
        self.assertEqual(len(seen_mutations), 24)

    def test_v1_conformance_report_is_complete_and_byte_deterministic(self) -> None:
        _, manifest_path = self._phase2_manifest()
        first, first_findings = model_workload_telemetry.build_route_receipt_conformance_report(
            manifest_path
        )
        second, second_findings = model_workload_telemetry.build_route_receipt_conformance_report(
            manifest_path
        )
        self.assertEqual(first_findings, [])
        self.assertEqual(second_findings, [])
        assert first is not None and second is not None
        first_bytes = json.dumps(first, indent=2, sort_keys=True, allow_nan=False).encode("utf-8")
        second_bytes = json.dumps(second, indent=2, sort_keys=True, allow_nan=False).encode("utf-8")
        self.assertEqual(first_bytes, second_bytes)
        self.assertTrue(first["conformant"])
        self.assertEqual(first["accepted_cases"]["passed"], 10)
        self.assertEqual(first["accepted_cases"]["total"], 10)
        self.assertEqual(first["accepted_receipts"]["passed"], 12)
        self.assertEqual(first["accepted_receipts"]["total"], 12)
        self.assertEqual(first["negative_mutations"]["detected"], 24)
        self.assertEqual(first["negative_mutations"]["total"], 24)
        for field in ("false_accepts", "primary_misses", "validator_crashes"):
            self.assertEqual(first["negative_mutations"][field], 0)
        self.assertEqual(
            first["non_execution"],
            {
                "model_called": False,
                "network_called": False,
                "state_mutating": False,
                "actual_route": "none",
                "automatic_route_change": False,
                "promotion_decision": "not_promoted",
            },
        )
        rendered = model_workload_telemetry.render_route_receipt_conformance_report(first)
        self.assertIn("accepted_cases=10/10", rendered)
        self.assertIn("negative_mutations=24/24", rendered)
        self.assertNotIn("%", rendered)

    def test_v1_manifest_rejects_integrity_and_contract_defects_before_reporting(self) -> None:
        source_manifest, _ = self._phase2_manifest()

        def run_manifest_mutation(mutator: Any, expected_code: str) -> None:
            with tempfile.TemporaryDirectory() as temporary:
                copied = Path(temporary) / "phase2"
                shutil.copytree(PHASE2, copied)
                manifest = copy.deepcopy(source_manifest)
                mutator(manifest, copied)
                manifest_path = copied / "route_receipt_case_manifest_v1.json"
                manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
                report, findings = model_workload_telemetry.build_route_receipt_conformance_report(
                    manifest_path
                )
                self.assertIsNone(report)
                self.assertIn(expected_code, {finding.code for finding in findings})

        defects = (
            (lambda manifest, _: manifest.__setitem__("unknown", True), "MANIFEST_UNKNOWN_FIELD"),
            (
                lambda manifest, _: manifest["cases"][1].__setitem__(
                    "case_id", manifest["cases"][0]["case_id"]
                ),
                "MANIFEST_CASE_ID",
            ),
            (
                lambda manifest, _: manifest["cases"][0]["ground_truth"].__setitem__(
                    "path", "../outside.json"
                ),
                "MANIFEST_ARTIFACT_PATH",
            ),
            (
                lambda manifest, _: manifest["cases"][0]["receipts"][0].__setitem__(
                    "receipt_id", "wrong-receipt-id"
                ),
                "MANIFEST_RECEIPT_IDENTITY",
            ),
            (
                lambda manifest, _: manifest["cases"][0]["mutations"][0].__setitem__(
                    "mutation_id", "unsupported-mutation"
                ),
                "MANIFEST_MUTATION_UNSUPPORTED",
            ),
            (
                lambda _manifest, copied: (copied / "p2_d01_enforced.json").write_text(
                    "{}\n", encoding="utf-8"
                ),
                "MANIFEST_ARTIFACT_DIGEST",
            ),
        )
        for mutator, expected_code in defects:
            with self.subTest(expected_code=expected_code):
                run_manifest_mutation(mutator, expected_code)

        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / "phase2"
            shutil.copytree(PHASE2, copied)
            manifest_path = copied / "route_receipt_case_manifest_v1.json"
            raw = manifest_path.read_text(encoding="utf-8").replace(
                '  "schema_version": "route_receipt_case_manifest_v1",',
                '  "schema_version": "route_receipt_case_manifest_v1",\n'
                '  "schema_version": "route_receipt_case_manifest_v1",',
                1,
            )
            manifest_path.write_text(raw, encoding="utf-8")
            report, findings = model_workload_telemetry.build_route_receipt_conformance_report(
                manifest_path
            )
            self.assertIsNone(report)
            self.assertIn("MANIFEST_JSON", {finding.code for finding in findings})

    def test_v1_manifest_denominator_ignores_undeclared_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / "phase2"
            shutil.copytree(PHASE2, copied)
            (copied / "undeclared.json").write_text('{"ignored": true}\n', encoding="utf-8")
            report, findings = model_workload_telemetry.build_route_receipt_conformance_report(
                copied / "route_receipt_case_manifest_v1.json"
            )
            self.assertEqual(findings, [])
            assert report is not None
            self.assertEqual(report["accepted_cases"]["total"], 10)
            self.assertEqual(report["accepted_receipts"]["total"], 12)
            self.assertEqual(report["negative_mutations"]["total"], 24)
            self.assertTrue(report["conformant"])

    def test_phase3_manifest_schema_is_strict_and_catalog_is_closed(self) -> None:
        manifest, _ = self._phase3_manifest()
        schema = json.loads(
            (
                ROOT
                / "schemas"
                / "decision_receipt_provenance_manifest_v1.schema.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            schema["properties"]["schema_version"]["const"],
            "decision_receipt_provenance_manifest_v1",
        )
        self.assertFalse(schema["properties"]["artifacts"]["additionalProperties"])
        self.assertFalse(schema["properties"]["decision"]["additionalProperties"])
        self.assertFalse(schema["$defs"]["artifact"]["additionalProperties"])
        self.assertFalse(schema["$defs"]["mutation"]["additionalProperties"])
        self.assertEqual(set(manifest), set(schema["required"]))
        self.assertEqual(
            tuple(manifest["artifacts"]),
            model_workload_telemetry.DECISION_RECEIPT_PROVENANCE_ARTIFACTS,
        )
        self.assertEqual(
            tuple(
                (item["mutation_id"], item["primary_finding_code"])
                for item in manifest["mutations"]
            ),
            model_workload_telemetry.DECISION_RECEIPT_PROVENANCE_MUTATIONS,
        )
        self.assertEqual(len(manifest["mutations"]), 16)
        for spec in manifest["artifacts"].values():
            path = Path(spec["path"])
            self.assertFalse(path.is_absolute())
            self.assertNotIn("..", path.parts)
            artifact_path = EXAMPLES / path
            self.assertEqual(
                spec["sha256"],
                hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
            )

    def test_phase3_exact_replay_and_every_positive_link(self) -> None:
        manifest, _ = self._phase3_manifest()
        records, record_findings = model_workload_telemetry.check_path(
            EXAMPLES / manifest["artifacts"]["workload"]["path"]
        )
        policy, policy_findings = model_workload_telemetry.load_shadow_policy(
            EXAMPLES / manifest["artifacts"]["policy"]["path"]
        )
        self.assertEqual(record_findings, [])
        self.assertEqual(policy_findings, [])
        assert policy is not None
        recomputed = model_workload_telemetry.build_shadow_route_report(records, policy)
        self.assertEqual(
            model_workload_telemetry._deterministic_json_bytes(recomputed),
            (EXAMPLES / manifest["artifacts"]["shadow_report"]["path"]).read_bytes(),
        )
        selected = next(
            item
            for item in recomputed["task_classes"]
            if item["task_class"] == manifest["decision"]["task_class"]
        )
        self.assertEqual(
            (
                selected["task_class"],
                selected["candidate_route"],
                selected["candidate_model"],
            ),
            ("maintenance", "fast_small", "compact-a"),
        )

        ground_truth, truth_findings = model_workload_telemetry.load_attempt_ground_truth(
            EXAMPLES / manifest["artifacts"]["ground_truth"]["path"]
        )
        receipt, receipt_findings = model_workload_telemetry.load_route_receipt(
            EXAMPLES / manifest["artifacts"]["receipt"]["path"]
        )
        self.assertEqual(truth_findings, [])
        self.assertEqual(receipt_findings, [])
        assert ground_truth is not None and receipt is not None
        self.assertEqual(
            model_workload_telemetry.validate_route_receipt(receipt, ground_truth),
            [],
        )
        self.assertEqual(
            (
                policy["policy_id"],
                recomputed["policy_id"],
                ground_truth["policy_id"],
                receipt["policy_id"],
            ),
            ("synthetic_example_v0",) * 4,
        )
        self.assertEqual(
            (ground_truth["candidate_route"], receipt["candidate_route"]),
            (selected["candidate_route"],) * 2,
        )
        self.assertEqual(
            (
                ground_truth["attempts"][0]["requested_model"],
                receipt["attempts"][0]["requested_model"],
                ground_truth["final_model"],
                receipt["final_model"],
            ),
            (selected["candidate_model"],) * 4,
        )
        self.assertEqual(
            (ground_truth["case_id"], receipt["case_id"]),
            (manifest["decision"]["case_id"],) * 2,
        )
        self.assertEqual(receipt["receipt_id"], manifest["decision"]["receipt_id"])

    def test_phase3_closed_mutations_detect_primary_without_mutating_bases(self) -> None:
        manifest, manifest_path = self._phase3_manifest()
        artifacts, findings = (
            model_workload_telemetry._load_decision_receipt_provenance_artifacts(
                manifest,
                manifest_path,
            )
        )
        self.assertEqual(findings, [])
        original_manifest = copy.deepcopy(manifest)
        original_artifacts = dict(artifacts)
        manifest_text = manifest_path.read_text(encoding="utf-8")
        seen: set[str] = set()
        for mutation in manifest["mutations"]:
            mutation_id = mutation["mutation_id"]
            with self.subTest(mutation=mutation_id):
                mutation_findings = (
                    model_workload_telemetry._run_decision_receipt_provenance_mutation(
                        mutation_id,
                        manifest,
                        artifacts,
                        manifest_text,
                    )
                )
                self.assertIn(
                    mutation["primary_finding_code"],
                    {finding.code for finding in mutation_findings},
                )
                self.assertEqual(manifest, original_manifest)
                self.assertEqual(artifacts, original_artifacts)
                seen.add(mutation_id)
        self.assertEqual(
            seen,
            {
                mutation_id
                for mutation_id, _ in (
                    model_workload_telemetry.DECISION_RECEIPT_PROVENANCE_MUTATIONS
                )
            },
        )

    def test_phase3_duplicate_manifest_key_fails_before_reporting(self) -> None:
        _, source_path = self._phase3_manifest()
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / source_path.name
            raw = source_path.read_text(encoding="utf-8").replace(
                "{",
                '{"schema_version":"decision_receipt_provenance_manifest_v1",',
                1,
            )
            copied.write_text(raw, encoding="utf-8")
            report, findings = (
                model_workload_telemetry.build_decision_receipt_provenance_report(
                    copied
                )
            )
            self.assertIsNone(report)
            self.assertIn(
                "PROVENANCE_MANIFEST_JSON",
                {finding.code for finding in findings},
            )

    def test_phase3_denominators_ignore_undeclared_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / "examples"
            shutil.copytree(EXAMPLES, copied)
            (copied / "phase3" / "undeclared.json").write_text(
                '{"ignored": true}\n',
                encoding="utf-8",
            )
            report, findings = (
                model_workload_telemetry.build_decision_receipt_provenance_report(
                    copied / "phase3_decision_receipt_provenance_manifest_v1.json"
                )
            )
            self.assertEqual(findings, [])
            assert report is not None
            self.assertEqual(report["artifact_integrity"], {"passed": 5, "total": 5})
            self.assertEqual(
                report["shadow_report_replay"],
                {"passed": 1, "total": 1},
            )
            self.assertEqual(report["decision_receipt_chain"]["total"], 1)
            self.assertEqual(report["negative_mutations"]["total"], 16)
            self.assertTrue(report["conformant"])

    def test_phase3_reports_are_byte_deterministic_and_claim_bounded(self) -> None:
        _, manifest_path = self._phase3_manifest()
        first, first_findings = (
            model_workload_telemetry.build_decision_receipt_provenance_report(
                manifest_path
            )
        )
        second, second_findings = (
            model_workload_telemetry.build_decision_receipt_provenance_report(
                manifest_path
            )
        )
        self.assertEqual(first_findings, [])
        self.assertEqual(second_findings, [])
        assert first is not None and second is not None
        first_bytes = model_workload_telemetry._deterministic_json_bytes(first)
        second_bytes = model_workload_telemetry._deterministic_json_bytes(second)
        self.assertEqual(first_bytes, second_bytes)
        rendered = model_workload_telemetry.render_decision_receipt_provenance_report(
            first
        )
        self.assertEqual(
            rendered,
            model_workload_telemetry.render_decision_receipt_provenance_report(second),
        )
        self.assertTrue(first["conformant"])
        self.assertEqual(first["artifact_integrity"], {"passed": 5, "total": 5})
        self.assertEqual(first["shadow_report_replay"], {"passed": 1, "total": 1})
        self.assertEqual(first["decision_receipt_chain"]["passed"], 1)
        self.assertEqual(first["decision_receipt_chain"]["total"], 1)
        self.assertEqual(first["decision_receipt_chain"]["false_rejects"], 0)
        self.assertEqual(first["negative_mutations"]["detected"], 16)
        self.assertEqual(first["negative_mutations"]["total"], 16)
        for field in ("false_accepts", "primary_misses", "validator_crashes"):
            self.assertEqual(first["negative_mutations"][field], 0)
        self.assertIn("artifacts=5/5 replay=1/1 chain=1/1 mutations=16/16", rendered)
        self.assertIn(
            "false_accepts=0 false_rejects=0 primary_misses=0 validator_crashes=0",
            rendered,
        )
        self.assertIn("conformant=true", rendered)
        combined = rendered + first_bytes.decode("utf-8")
        for forbidden in (
            "%",
            str(ROOT),
            "timestamp",
            "discovered",
            "comparative",
            "runtime_authority",
        ):
            self.assertNotIn(forbidden, combined)

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
