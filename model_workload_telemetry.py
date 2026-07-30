#!/usr/bin/env python3
"""Compare model runs by shared workload instead of raw token totals."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable


MAX_JSONL_BYTES = 5_000_000
MAX_POLICY_BYTES = 100_000
MAX_ROUTE_RECEIPT_BYTES = 100_000
MAX_ROUTE_RECEIPT_CASE_MANIFEST_BYTES = 500_000
SHADOW_POLICY_SCHEMA = "shadow_route_policy_v0"
SHADOW_REPORT_SCHEMA = "evidence_gated_shadow_route_report_v0"
ROUTE_RECEIPT_SCHEMA = "route_receipt_v0"
ATTEMPT_GROUND_TRUTH_SCHEMA = "route_attempt_ground_truth_v0"
ROUTE_RECEIPT_SCHEMA_V1 = "route_receipt_v1"
ATTEMPT_GROUND_TRUTH_SCHEMA_V1 = "route_attempt_ground_truth_v1"
ROUTE_RECEIPT_CASE_MANIFEST_SCHEMA_V1 = "route_receipt_case_manifest_v1"
ROUTE_RECEIPT_CONFORMANCE_REPORT_SCHEMA_V1 = "route_receipt_conformance_report_v1"
ROUTE_RECEIPT_ROUTES = {
    "deterministic",
    "fast_small",
    "primary_quality",
    "deep_comparison",
    "hold",
}
ROUTE_ATTEMPT_OUTCOMES = {
    "completed",
    "infrastructure_failure",
    "validation_failure",
    "policy_rejected",
}
ROUTE_ATTEMPT_FAILURE_STAGES = {
    "none",
    "pre_request",
    "request_open",
    "response_stream",
    "validation",
    "policy",
}
ROUTE_ATTEMPT_QUALITY_STATUSES = {
    "assessed_pass",
    "assessed_fail",
    "not_assessed_runtime_failure",
    "not_assessed_validation_failure",
    "not_assessed_policy_rejection",
}
ROUTE_RECEIPT_COMPLETION_STATUSES = {
    "completed_without_fallback",
    "completed_via_fallback",
    "hold",
}
ROUTE_RECEIPT_MUTATION_CATEGORIES = {
    "attempts",
    "final_models",
    "fallback_transitions",
    "quality_status",
    "source_boundaries",
    "safety_boundaries",
    "writes",
    "passive_authority",
    "enforced_false_pass",
    "finalization",
    "contract_robustness",
}
ROUTE_RECEIPT_ATTRIBUTION_CATEGORIES = (
    "attempts",
    "final_models",
    "fallback_transitions",
    "quality_status",
    "source_boundaries",
    "safety_boundaries",
    "writes",
)
ROUTE_RECEIPT_AUTHORITY_CATEGORIES = (
    "passive_authority",
    "enforced_false_pass",
)
ROUTE_RECEIPT_OTHER_CATEGORIES = (
    "finalization",
    "contract_robustness",
)
ROUTE_RECEIPT_V1_MUTATION_IDS = {
    "missing_attempt",
    "duplicated_attempt",
    "reordered_attempts",
    "invented_attempt",
    "candidate_route_mismatch",
    "final_attempt_not_last",
    "final_model_mismatch",
    "fallback_wrong_trigger",
    "fallback_absent_from_policy",
    "fallback_marked_unused",
    "runtime_quality_contamination",
    "source_relabelled_as_quality",
    "safety_relabelled_as_runtime",
    "invented_expected_write",
    "invented_observed_write",
    "omitted_unexpected_write",
    "exhausted_hold_claimed_complete",
    "passive_promoted_to_enforcement",
    "failed_finalization",
    "unverified_finalization",
    "unknown_field",
    "duplicate_json_key",
    "invalid_field_type",
    "nonfinite_attempt_metric",
}
FIELDS = {
    "schema_version",
    "run_id",
    "task_id",
    "task_class",
    "model",
    "turns",
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "wall_seconds",
    "status",
    "failure_bucket",
    "human_score",
    "revision_count",
}
FAILURE_BUCKETS = {
    "none",
    "infrastructure",
    "schema",
    "source_boundary",
    "answer_quality",
    "human_revision",
}
CRITICAL_ROUTE_FAILURE_BUCKETS = {
    "schema",
    "source_boundary",
    "answer_quality",
    "human_revision",
}
THRESHOLD_FIELDS = {
    "min_shared_tasks",
    "min_deterministic_cases",
    "min_completion_rate",
    "min_avg_human_score",
    "max_avg_revisions",
    "max_avg_wall_seconds",
    "max_quality_gap",
    "min_latency_advantage_fraction",
}
METRIC_PRESENTATION_PRECISION = {
    "completion_rate": 3,
    "avg_turns_per_attempt": 2,
    "avg_uncached_tokens_per_attempt": 2,
    "avg_cached_tokens_per_attempt": 2,
    "avg_wall_seconds_per_attempt": 2,
    "avg_human_score_completed": 2,
    "avg_revisions_completed": 2,
}


@dataclass(frozen=True)
class Finding:
    code: str
    message: str


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_records(path: Path) -> tuple[list[dict[str, Any]], list[Finding]]:
    if not path.is_file():
        return [], [Finding("FILE_MISSING", f"input file does not exist: {path}")]
    if path.stat().st_size > MAX_JSONL_BYTES:
        return [], [Finding("FILE_SIZE", "input file exceeds five megabytes")]

    records: list[dict[str, Any]] = []
    findings: list[Finding] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            value = json.loads(raw_line, object_pairs_hook=_reject_duplicate_keys)
        except (ValueError, json.JSONDecodeError) as exc:
            findings.append(Finding("JSON_LINE", f"line {line_number}: {exc}"))
            continue
        if not isinstance(value, dict):
            findings.append(Finding("RECORD_SHAPE", f"line {line_number}: record must be an object"))
            continue
        value["_line"] = line_number
        records.append(value)
    if not records and not findings:
        findings.append(Finding("EMPTY_INPUT", "input has no records"))
    return records, findings


def load_shadow_policy(path: Path) -> tuple[dict[str, Any] | None, list[Finding]]:
    if not path.is_file():
        return None, [Finding("POLICY_FILE_MISSING", f"policy file does not exist: {path}")]
    if path.stat().st_size > MAX_POLICY_BYTES:
        return None, [Finding("POLICY_FILE_SIZE", "policy file exceeds one hundred kilobytes")]
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (ValueError, json.JSONDecodeError) as exc:
        return None, [Finding("POLICY_JSON", str(exc))]
    if not isinstance(value, dict):
        return None, [Finding("POLICY_SHAPE", "policy must be a JSON object")]
    findings = validate_shadow_policy(value)
    return value, findings


def _load_contract_object(
    path: Path,
    *,
    label: str,
    missing_code: str,
    size_code: str,
    json_code: str,
    shape_code: str,
) -> tuple[dict[str, Any] | None, list[Finding]]:
    if not path.is_file():
        return None, [Finding(missing_code, f"{label} file does not exist: {path}")]
    if path.stat().st_size > MAX_ROUTE_RECEIPT_BYTES:
        return None, [Finding(size_code, f"{label} file exceeds one hundred kilobytes")]
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (ValueError, json.JSONDecodeError) as exc:
        return None, [Finding(json_code, str(exc))]
    if not isinstance(value, dict):
        return None, [Finding(shape_code, f"{label} must be a JSON object")]
    return value, []


def load_attempt_ground_truth(path: Path) -> tuple[dict[str, Any] | None, list[Finding]]:
    value, findings = _load_contract_object(
        path,
        label="attempt ground truth",
        missing_code="GROUND_TRUTH_FILE_MISSING",
        size_code="GROUND_TRUTH_FILE_SIZE",
        json_code="GROUND_TRUTH_JSON",
        shape_code="GROUND_TRUTH_SHAPE",
    )
    if value is None:
        return None, findings
    return value, findings + validate_attempt_ground_truth(value)


def load_route_receipt(path: Path) -> tuple[dict[str, Any] | None, list[Finding]]:
    return _load_contract_object(
        path,
        label="route receipt",
        missing_code="RECEIPT_FILE_MISSING",
        size_code="RECEIPT_FILE_SIZE",
        json_code="RECEIPT_JSON",
        shape_code="RECEIPT_SHAPE",
    )


def _is_sha256_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _resolve_manifest_artifact(
    manifest_path: Path,
    artifact: object,
    context: str,
    findings: list[Finding],
    *,
    expected_fields: set[str] | None = None,
) -> Path | None:
    if not isinstance(artifact, dict):
        findings.append(Finding("MANIFEST_ARTIFACT", f"{context} must be an object"))
        return None
    _check_contract_fields(
        artifact,
        expected_fields or {"path", "sha256"},
        context,
        findings,
        prefix="MANIFEST",
    )
    relative_value = artifact.get("path")
    digest = artifact.get("sha256")
    if not _is_nonempty_text(relative_value):
        findings.append(Finding("MANIFEST_ARTIFACT_PATH", f"{context}.path must be non-empty text"))
        return None
    if not _is_sha256_digest(digest):
        findings.append(
            Finding("MANIFEST_ARTIFACT_DIGEST", f"{context}.sha256 must be a lowercase SHA-256 digest")
        )
        return None

    try:
        relative_path = Path(relative_value)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            findings.append(
                Finding("MANIFEST_ARTIFACT_PATH", f"{context}.path must stay within the manifest folder")
            )
            return None
        base = manifest_path.resolve().parent
        resolved = (base / relative_path).resolve()
        resolved.relative_to(base)
    except (OSError, RuntimeError, ValueError):
        findings.append(Finding("MANIFEST_ARTIFACT_PATH", f"{context}.path resolves outside the manifest folder"))
        return None
    if not resolved.is_file():
        findings.append(Finding("MANIFEST_ARTIFACT_MISSING", f"{context}.path does not name a file"))
        return None
    if resolved.stat().st_size > MAX_ROUTE_RECEIPT_BYTES:
        findings.append(Finding("MANIFEST_ARTIFACT_SIZE", f"{context}.path exceeds one hundred kilobytes"))
        return None
    actual_digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    if actual_digest != digest:
        findings.append(Finding("MANIFEST_ARTIFACT_DIGEST", f"{context}.sha256 disagrees with exact fixture bytes"))
        return None
    return resolved


def validate_route_receipt_case_manifest(
    manifest: dict[str, Any], manifest_path: Path
) -> list[Finding]:
    findings: list[Finding] = []
    _check_contract_fields(
        manifest,
        {"schema_version", "manifest_id", "cases"},
        "manifest",
        findings,
        prefix="MANIFEST",
    )
    if manifest.get("schema_version") != ROUTE_RECEIPT_CASE_MANIFEST_SCHEMA_V1:
        findings.append(
            Finding(
                "MANIFEST_SCHEMA_VERSION",
                f"schema_version must be {ROUTE_RECEIPT_CASE_MANIFEST_SCHEMA_V1}",
            )
        )
    if not _is_nonempty_text(manifest.get("manifest_id")):
        findings.append(Finding("MANIFEST_ID", "manifest_id must be non-empty text"))

    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        findings.append(Finding("MANIFEST_CASES", "cases must be a non-empty list"))
        return findings

    seen_case_ids: set[str] = set()
    seen_receipt_ids: set[str] = set()
    seen_mutation_ids: set[str] = set()
    seen_paths: set[str] = set()
    for case_index, case in enumerate(cases):
        case_context = f"manifest.cases[{case_index}]"
        if not isinstance(case, dict):
            findings.append(Finding("MANIFEST_CASE", f"{case_context} must be an object"))
            continue
        _check_contract_fields(
            case,
            {"case_id", "ground_truth", "receipts", "mutations"},
            case_context,
            findings,
            prefix="MANIFEST",
        )
        case_id = case.get("case_id")
        if not _is_nonempty_text(case_id):
            findings.append(Finding("MANIFEST_CASE_ID", f"{case_context}.case_id must be non-empty text"))
        elif case_id in seen_case_ids:
            findings.append(Finding("MANIFEST_CASE_ID", f"duplicate case_id {case_id}"))
        else:
            seen_case_ids.add(case_id)

        truth_spec = case.get("ground_truth")
        truth_path = _resolve_manifest_artifact(
            manifest_path,
            truth_spec,
            f"{case_context}.ground_truth",
            findings,
        )
        if isinstance(truth_spec, dict) and _is_nonempty_text(truth_spec.get("path")):
            if truth_spec["path"] in seen_paths:
                findings.append(Finding("MANIFEST_ARTIFACT_PATH", f"duplicate artifact path {truth_spec['path']}"))
            seen_paths.add(truth_spec["path"])
        if truth_path is not None:
            ground_truth, truth_findings = load_attempt_ground_truth(truth_path)
            if truth_findings:
                for finding in truth_findings:
                    findings.append(
                        Finding(
                            "MANIFEST_GROUND_TRUTH_INVALID",
                            f"{case_context}: {finding.code} {finding.message}",
                        )
                    )
            elif ground_truth is not None and (
                ground_truth.get("schema_version") != ATTEMPT_GROUND_TRUTH_SCHEMA_V1
                or ground_truth.get("case_id") != case_id
            ):
                findings.append(
                    Finding(
                        "MANIFEST_GROUND_TRUTH_IDENTITY",
                        f"{case_context}.ground_truth must be v1 truth for {case_id}",
                    )
                )

        receipts = case.get("receipts")
        local_receipt_ids: set[str] = set()
        if not isinstance(receipts, list) or not receipts:
            findings.append(Finding("MANIFEST_RECEIPTS", f"{case_context}.receipts must be a non-empty list"))
            receipts = []
        for receipt_index, receipt_spec in enumerate(receipts):
            receipt_context = f"{case_context}.receipts[{receipt_index}]"
            if not isinstance(receipt_spec, dict):
                findings.append(Finding("MANIFEST_RECEIPT", f"{receipt_context} must be an object"))
                continue
            receipt_id = receipt_spec.get("receipt_id")
            mode = receipt_spec.get("mode")
            if not _is_nonempty_text(receipt_id):
                findings.append(Finding("MANIFEST_RECEIPT_ID", f"{receipt_context}.receipt_id must be non-empty text"))
            elif receipt_id in seen_receipt_ids:
                findings.append(Finding("MANIFEST_RECEIPT_ID", f"duplicate receipt_id {receipt_id}"))
            else:
                seen_receipt_ids.add(receipt_id)
                local_receipt_ids.add(receipt_id)
            if not _is_enum_value(mode, {"passive", "enforced"}):
                findings.append(Finding("MANIFEST_RECEIPT_MODE", f"{receipt_context}.mode is invalid"))
            receipt_path = _resolve_manifest_artifact(
                manifest_path,
                receipt_spec,
                receipt_context,
                findings,
                expected_fields={"receipt_id", "mode", "path", "sha256"},
            )
            if _is_nonempty_text(receipt_spec.get("path")):
                if receipt_spec["path"] in seen_paths:
                    findings.append(Finding("MANIFEST_ARTIFACT_PATH", f"duplicate artifact path {receipt_spec['path']}"))
                seen_paths.add(receipt_spec["path"])
            if receipt_path is not None:
                receipt, receipt_findings = load_route_receipt(receipt_path)
                if receipt_findings:
                    for finding in receipt_findings:
                        findings.append(
                            Finding(
                                "MANIFEST_RECEIPT_INVALID",
                                f"{receipt_context}: {finding.code} {finding.message}",
                            )
                        )
                elif receipt is not None and (
                    receipt.get("schema_version") != ROUTE_RECEIPT_SCHEMA_V1
                    or receipt.get("receipt_id") != receipt_id
                    or receipt.get("case_id") != case_id
                    or receipt.get("receipt_mode") != mode
                ):
                    findings.append(
                        Finding(
                            "MANIFEST_RECEIPT_IDENTITY",
                            f"{receipt_context} identity disagrees with the referenced receipt",
                        )
                    )

        mutations = case.get("mutations")
        if not isinstance(mutations, list):
            findings.append(Finding("MANIFEST_MUTATIONS", f"{case_context}.mutations must be a list"))
            mutations = []
        for mutation_index, mutation in enumerate(mutations):
            mutation_context = f"{case_context}.mutations[{mutation_index}]"
            if not isinstance(mutation, dict):
                findings.append(Finding("MANIFEST_MUTATION", f"{mutation_context} must be an object"))
                continue
            _check_contract_fields(
                mutation,
                {"mutation_id", "base_receipt_id", "category", "primary_finding_code"},
                mutation_context,
                findings,
                prefix="MANIFEST",
            )
            mutation_id = mutation.get("mutation_id")
            base_receipt_id = mutation.get("base_receipt_id")
            category = mutation.get("category")
            primary_code = mutation.get("primary_finding_code")
            if not _is_nonempty_text(mutation_id):
                findings.append(Finding("MANIFEST_MUTATION_ID", f"{mutation_context}.mutation_id must be non-empty text"))
            elif mutation_id in seen_mutation_ids:
                findings.append(Finding("MANIFEST_MUTATION_ID", f"duplicate mutation_id {mutation_id}"))
            else:
                seen_mutation_ids.add(mutation_id)
                if mutation_id not in ROUTE_RECEIPT_V1_MUTATION_IDS:
                    findings.append(Finding("MANIFEST_MUTATION_UNSUPPORTED", f"unsupported mutation_id {mutation_id}"))
            if base_receipt_id not in local_receipt_ids:
                findings.append(
                    Finding(
                        "MANIFEST_MUTATION_BASE",
                        f"{mutation_context}.base_receipt_id must name a receipt in the same case",
                    )
                )
            if not _is_enum_value(category, ROUTE_RECEIPT_MUTATION_CATEGORIES):
                findings.append(Finding("MANIFEST_MUTATION_CATEGORY", f"{mutation_context}.category is invalid"))
            if not _is_nonempty_text(primary_code):
                findings.append(
                    Finding(
                        "MANIFEST_MUTATION_FINDING",
                        f"{mutation_context}.primary_finding_code must be non-empty text",
                    )
                )
    return findings


def load_route_receipt_case_manifest(path: Path) -> tuple[dict[str, Any] | None, list[Finding]]:
    if not path.is_file():
        return None, [Finding("MANIFEST_FILE_MISSING", f"case manifest does not exist: {path}")]
    if path.stat().st_size > MAX_ROUTE_RECEIPT_CASE_MANIFEST_BYTES:
        return None, [Finding("MANIFEST_FILE_SIZE", "case manifest exceeds five hundred kilobytes")]
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (ValueError, json.JSONDecodeError) as exc:
        return None, [Finding("MANIFEST_JSON", str(exc))]
    if not isinstance(value, dict):
        return None, [Finding("MANIFEST_SHAPE", "case manifest must be a JSON object")]
    findings = validate_route_receipt_case_manifest(value, path)
    return value, findings


def _check_contract_fields(
    value: dict[str, Any],
    expected: set[str],
    context: str,
    findings: list[Finding],
    *,
    prefix: str,
) -> None:
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown:
        findings.append(Finding(f"{prefix}_UNKNOWN_FIELD", f"{context}: {', '.join(unknown)}"))
    if missing:
        findings.append(Finding(f"{prefix}_MISSING_FIELD", f"{context}: {', '.join(missing)}"))


def _is_nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _is_enum_value(value: object, allowed: set[str]) -> bool:
    return isinstance(value, str) and value in allowed


def _validate_string_list(
    value: object,
    context: str,
    findings: list[Finding],
    *,
    code: str,
) -> list[str] | None:
    if not isinstance(value, list) or any(not _is_nonempty_text(item) for item in value):
        findings.append(Finding(code, f"{context} must be a list of non-empty strings"))
        return None
    if len(value) != len(set(value)):
        findings.append(Finding(code, f"{context} must not contain duplicates"))
    return value


def _validate_synthetic_invariants(
    value: dict[str, Any], context: str, findings: list[Finding], *, prefix: str
) -> None:
    if value.get("execution_mode") != "synthetic_replay":
        findings.append(Finding(f"{prefix}_EXECUTION_MODE", f"{context}.execution_mode must be synthetic_replay"))
    for field in ("model_called", "network_called", "state_mutating"):
        if value.get(field) is not False:
            findings.append(Finding(f"{prefix}_NON_EXECUTION", f"{context}.{field} must be false"))


def _validate_route_attempts(
    value: object,
    findings: list[Finding],
    *,
    prefix: str,
    context: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        findings.append(Finding(f"{prefix}_ATTEMPTS", f"{context} must be a non-empty list"))
        return []

    attempt_fields = {
        "attempt_id",
        "ordinal",
        "route",
        "requested_model",
        "responding_model",
        "outcome",
        "failure_stage",
        "quality_status",
        "fallback_from_attempt_id",
        "input_tokens",
        "output_tokens",
        "wall_seconds",
    }
    attempts: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, attempt in enumerate(value, start=1):
        attempt_context = f"{context}[{index - 1}]"
        if not isinstance(attempt, dict):
            findings.append(Finding(f"{prefix}_ATTEMPT_SHAPE", f"{attempt_context} must be an object"))
            continue
        _check_contract_fields(attempt, attempt_fields, attempt_context, findings, prefix=prefix)
        attempts.append(attempt)

        attempt_id = attempt.get("attempt_id")
        if not _is_nonempty_text(attempt_id):
            findings.append(Finding(f"{prefix}_ATTEMPT_ID", f"{attempt_context}.attempt_id must be non-empty text"))
        elif attempt_id in seen_ids:
            findings.append(Finding(f"{prefix}_ATTEMPT_ID", f"duplicate attempt_id {attempt_id}"))
        else:
            seen_ids.add(attempt_id)

        if attempt.get("ordinal") != index:
            findings.append(Finding(f"{prefix}_ATTEMPT_ORDER", f"{attempt_context}.ordinal must be {index}"))
        if not _is_enum_value(attempt.get("route"), ROUTE_RECEIPT_ROUTES - {"hold"}):
            findings.append(Finding(f"{prefix}_ATTEMPT_ROUTE", f"{attempt_context}.route is invalid"))
        if not _is_nonempty_text(attempt.get("requested_model")):
            findings.append(
                Finding(f"{prefix}_ATTEMPT_MODEL", f"{attempt_context}.requested_model must be non-empty text")
            )
        responding_model = attempt.get("responding_model")
        if responding_model is not None and not _is_nonempty_text(responding_model):
            findings.append(
                Finding(f"{prefix}_ATTEMPT_MODEL", f"{attempt_context}.responding_model must be text or null")
            )

        outcome = attempt.get("outcome")
        stage = attempt.get("failure_stage")
        quality_status = attempt.get("quality_status")
        if not _is_enum_value(outcome, ROUTE_ATTEMPT_OUTCOMES):
            findings.append(Finding(f"{prefix}_ATTEMPT_OUTCOME", f"{attempt_context}.outcome is invalid"))
        if not _is_enum_value(stage, ROUTE_ATTEMPT_FAILURE_STAGES):
            findings.append(Finding(f"{prefix}_ATTEMPT_STAGE", f"{attempt_context}.failure_stage is invalid"))
        if not _is_enum_value(quality_status, ROUTE_ATTEMPT_QUALITY_STATUSES):
            findings.append(Finding(f"{prefix}_ATTEMPT_QUALITY", f"{attempt_context}.quality_status is invalid"))

        if outcome == "completed":
            if stage != "none" or responding_model is None:
                findings.append(
                    Finding(
                        f"{prefix}_ATTEMPT_SEMANTICS",
                        f"{attempt_context}: completed attempts need a responding model and failure_stage none",
                    )
                )
            if not _is_enum_value(quality_status, {"assessed_pass", "assessed_fail"}):
                findings.append(
                    Finding(
                        f"{prefix}_ATTEMPT_SEMANTICS",
                        f"{attempt_context}: completed attempts need assessed quality",
                    )
                )
        elif outcome == "infrastructure_failure":
            if stage not in {"pre_request", "request_open", "response_stream"}:
                findings.append(
                    Finding(
                        f"{prefix}_ATTEMPT_SEMANTICS",
                        f"{attempt_context}: infrastructure failures need a runtime failure stage",
                    )
                )
            if quality_status != "not_assessed_runtime_failure":
                findings.append(
                    Finding(
                        f"{prefix}_ATTEMPT_SEMANTICS",
                        f"{attempt_context}: infrastructure failures must remain not assessed",
                    )
                )
        elif outcome == "validation_failure":
            if stage != "validation" or quality_status != "not_assessed_validation_failure":
                findings.append(
                    Finding(
                        f"{prefix}_ATTEMPT_SEMANTICS",
                        f"{attempt_context}: validation failures need validation-stage unassessed quality",
                    )
                )
        elif outcome == "policy_rejected":
            if stage != "policy" or quality_status != "not_assessed_policy_rejection":
                findings.append(
                    Finding(
                        f"{prefix}_ATTEMPT_SEMANTICS",
                        f"{attempt_context}: policy rejections need policy-stage unassessed quality",
                    )
                )

        fallback_from = attempt.get("fallback_from_attempt_id")
        if index == 1:
            if fallback_from is not None:
                findings.append(
                    Finding(f"{prefix}_FALLBACK_CHAIN", f"{attempt_context}: first attempt cannot be a fallback")
                )
        else:
            previous_id = value[index - 2].get("attempt_id") if isinstance(value[index - 2], dict) else None
            if fallback_from != previous_id:
                findings.append(
                    Finding(
                        f"{prefix}_FALLBACK_CHAIN",
                        f"{attempt_context}.fallback_from_attempt_id must reference the previous attempt",
                    )
                )

        for field in ("input_tokens", "output_tokens"):
            if not _is_nonnegative_int(attempt.get(field)):
                findings.append(
                    Finding(f"{prefix}_ATTEMPT_METRIC", f"{attempt_context}.{field} must be non-negative")
                )
        if not _is_finite_nonnegative_number(attempt.get("wall_seconds")):
            findings.append(
                Finding(f"{prefix}_ATTEMPT_METRIC", f"{attempt_context}.wall_seconds must be finite and non-negative")
            )
    return attempts


def _validate_attempt_ground_truth_v0(ground_truth: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    root_fields = {
        "schema_version",
        "case_id",
        "execution_mode",
        "model_called",
        "network_called",
        "state_mutating",
        "policy_id",
        "candidate_route",
        "fallback_policy",
        "attempts",
        "final_attempt_id",
        "final_model",
        "completion_status",
        "expected_writes",
        "observed_writes",
    }
    _check_contract_fields(ground_truth, root_fields, "ground_truth", findings, prefix="GROUND_TRUTH")
    if ground_truth.get("schema_version") != ATTEMPT_GROUND_TRUTH_SCHEMA:
        findings.append(
            Finding(
                "GROUND_TRUTH_SCHEMA_VERSION",
                f"schema_version must be {ATTEMPT_GROUND_TRUTH_SCHEMA}",
            )
        )
    for field in ("case_id", "policy_id"):
        if not _is_nonempty_text(ground_truth.get(field)):
            findings.append(Finding("GROUND_TRUTH_STRING", f"ground_truth.{field} must be non-empty text"))
    if not _is_enum_value(ground_truth.get("candidate_route"), ROUTE_RECEIPT_ROUTES - {"hold"}):
        findings.append(Finding("GROUND_TRUTH_ROUTE", "ground_truth.candidate_route is invalid"))
    _validate_synthetic_invariants(ground_truth, "ground_truth", findings, prefix="GROUND_TRUTH")

    fallback_policy = ground_truth.get("fallback_policy")
    allowed_transitions: list[dict[str, Any]] = []
    max_attempts: int | None = None
    if not isinstance(fallback_policy, dict):
        findings.append(Finding("GROUND_TRUTH_FALLBACK_POLICY", "fallback_policy must be an object"))
    else:
        _check_contract_fields(
            fallback_policy,
            {"max_attempts", "allowed_transitions"},
            "ground_truth.fallback_policy",
            findings,
            prefix="GROUND_TRUTH",
        )
        if not _is_nonnegative_int(fallback_policy.get("max_attempts")) or fallback_policy.get("max_attempts", 0) < 1:
            findings.append(Finding("GROUND_TRUTH_FALLBACK_POLICY", "max_attempts must be at least 1"))
        else:
            max_attempts = fallback_policy["max_attempts"]
        transitions = fallback_policy.get("allowed_transitions")
        if not isinstance(transitions, list):
            findings.append(Finding("GROUND_TRUTH_FALLBACK_POLICY", "allowed_transitions must be a list"))
        else:
            for index, transition in enumerate(transitions):
                context = f"ground_truth.fallback_policy.allowed_transitions[{index}]"
                if not isinstance(transition, dict):
                    findings.append(Finding("GROUND_TRUTH_FALLBACK_POLICY", f"{context} must be an object"))
                    continue
                _check_contract_fields(
                    transition,
                    {"from_route", "to_route", "on_outcomes"},
                    context,
                    findings,
                    prefix="GROUND_TRUTH",
                )
                if not _is_enum_value(
                    transition.get("from_route"), ROUTE_RECEIPT_ROUTES - {"hold"}
                ):
                    findings.append(Finding("GROUND_TRUTH_FALLBACK_POLICY", f"{context}.from_route is invalid"))
                if not _is_enum_value(
                    transition.get("to_route"), ROUTE_RECEIPT_ROUTES - {"hold"}
                ):
                    findings.append(Finding("GROUND_TRUTH_FALLBACK_POLICY", f"{context}.to_route is invalid"))
                outcomes = transition.get("on_outcomes")
                if (
                    not isinstance(outcomes, list)
                    or not outcomes
                    or any(
                        not _is_enum_value(outcome, ROUTE_ATTEMPT_OUTCOMES - {"completed"})
                        for outcome in outcomes
                    )
                ):
                    findings.append(Finding("GROUND_TRUTH_FALLBACK_POLICY", f"{context}.on_outcomes is invalid"))
                else:
                    allowed_transitions.append(transition)

    attempts = _validate_route_attempts(
        ground_truth.get("attempts"), findings, prefix="GROUND_TRUTH", context="ground_truth.attempts"
    )
    if attempts and ground_truth.get("candidate_route") != attempts[0].get("route"):
        findings.append(
            Finding("GROUND_TRUTH_CANDIDATE_ATTRIBUTION", "candidate_route must match the first attempt")
        )
    if max_attempts is not None and len(attempts) > max_attempts:
        findings.append(Finding("GROUND_TRUTH_FALLBACK_POLICY", "attempt count exceeds max_attempts"))
    for previous, current in zip(attempts, attempts[1:]):
        allowed = any(
            transition.get("from_route") == previous.get("route")
            and transition.get("to_route") == current.get("route")
            and previous.get("outcome") in transition.get("on_outcomes", [])
            for transition in allowed_transitions
        )
        if not allowed:
            findings.append(
                Finding(
                    "GROUND_TRUTH_FORBIDDEN_FALLBACK",
                    f"fallback from {previous.get('route')} to {current.get('route')} is not permitted",
                )
            )

    attempt_by_id = {
        attempt["attempt_id"]: attempt for attempt in attempts if _is_nonempty_text(attempt.get("attempt_id"))
    }
    final_attempt_id = ground_truth.get("final_attempt_id")
    final_model = ground_truth.get("final_model")
    final_attempt = attempt_by_id.get(final_attempt_id) if isinstance(final_attempt_id, str) else None
    if final_attempt is None:
        findings.append(Finding("GROUND_TRUTH_FINAL_ATTEMPT", "final_attempt_id must reference an attempt"))
    else:
        if attempts and final_attempt_id != attempts[-1].get("attempt_id"):
            findings.append(
                Finding("GROUND_TRUTH_FINAL_ATTEMPT", "final_attempt_id must reference the last attempt")
            )
        if final_model != final_attempt.get("responding_model"):
            findings.append(Finding("GROUND_TRUTH_FINAL_MODEL", "final_model must match the final responding model"))
        expected_completion = (
            "completed_via_fallback"
            if final_attempt.get("outcome") == "completed" and len(attempts) > 1
            else "completed_without_fallback"
            if final_attempt.get("outcome") == "completed"
            else "hold"
        )
        if ground_truth.get("completion_status") != expected_completion:
            findings.append(
                Finding("GROUND_TRUTH_COMPLETION", f"completion_status must be {expected_completion}")
            )
    if final_model is not None and not _is_nonempty_text(final_model):
        findings.append(Finding("GROUND_TRUTH_FINAL_MODEL", "final_model must be text or null"))
    if not _is_enum_value(ground_truth.get("completion_status"), ROUTE_RECEIPT_COMPLETION_STATUSES):
        findings.append(Finding("GROUND_TRUTH_COMPLETION", "completion_status is invalid"))
    expected_writes = _validate_string_list(
        ground_truth.get("expected_writes"),
        "ground_truth.expected_writes",
        findings,
        code="GROUND_TRUTH_WRITES",
    )
    observed_writes = _validate_string_list(
        ground_truth.get("observed_writes"),
        "ground_truth.observed_writes",
        findings,
        code="GROUND_TRUTH_WRITES",
    )
    if expected_writes:
        findings.append(
            Finding("GROUND_TRUTH_NON_MUTATION", "synthetic Phase 1 cases cannot expect writes")
        )
    if observed_writes:
        findings.append(
            Finding("GROUND_TRUTH_NON_MUTATION", "synthetic Phase 1 cases cannot observe writes")
        )
    return findings


def _validate_route_receipt_v0(receipt: dict[str, Any], ground_truth: dict[str, Any]) -> list[Finding]:
    if _validate_attempt_ground_truth_v0(ground_truth):
        return [Finding("RECEIPT_GROUND_TRUTH_INVALID", "attempt ground truth must validate first")]

    findings: list[Finding] = []
    root_fields = {
        "schema_version",
        "receipt_id",
        "case_id",
        "receipt_mode",
        "execution_mode",
        "model_called",
        "network_called",
        "state_mutating",
        "policy_id",
        "candidate_route",
        "actual_route",
        "attempts",
        "final_attempt_id",
        "final_model",
        "completion_status",
        "fallback",
        "quality",
        "writes",
        "finalization",
        "enforcement_status",
        "completion_claim",
        "automatic_route_change",
        "promotion_decision",
    }
    _check_contract_fields(receipt, root_fields, "receipt", findings, prefix="RECEIPT")
    if receipt.get("schema_version") != ROUTE_RECEIPT_SCHEMA:
        findings.append(Finding("RECEIPT_SCHEMA_VERSION", f"schema_version must be {ROUTE_RECEIPT_SCHEMA}"))
    for field in ("receipt_id", "case_id", "policy_id"):
        if not _is_nonempty_text(receipt.get(field)):
            findings.append(Finding("RECEIPT_STRING", f"receipt.{field} must be non-empty text"))
    if not _is_enum_value(receipt.get("receipt_mode"), {"passive", "enforced"}):
        findings.append(Finding("RECEIPT_MODE", "receipt_mode must be passive or enforced"))
    if not _is_enum_value(receipt.get("candidate_route"), ROUTE_RECEIPT_ROUTES - {"hold"}):
        findings.append(Finding("RECEIPT_ROUTE", "candidate_route is invalid"))
    if receipt.get("actual_route") != "none":
        findings.append(Finding("RECEIPT_NON_EXECUTION", "actual_route must remain none"))
    _validate_synthetic_invariants(receipt, "receipt", findings, prefix="RECEIPT")
    if receipt.get("automatic_route_change") is not False:
        findings.append(Finding("RECEIPT_PROMOTION", "automatic_route_change must be false"))
    if receipt.get("promotion_decision") != "not_promoted":
        findings.append(Finding("RECEIPT_PROMOTION", "promotion_decision must be not_promoted"))

    for field in (
        "case_id",
        "execution_mode",
        "model_called",
        "network_called",
        "state_mutating",
        "policy_id",
        "candidate_route",
        "completion_status",
    ):
        if receipt.get(field) != ground_truth.get(field):
            findings.append(Finding("RECEIPT_GROUND_TRUTH_MISMATCH", f"receipt.{field} disagrees with ground truth"))

    attempts = _validate_route_attempts(
        receipt.get("attempts"), findings, prefix="RECEIPT", context="receipt.attempts"
    )
    truth_attempts = ground_truth["attempts"]
    attempts_by_id = {
        attempt["attempt_id"]: attempt for attempt in attempts if _is_nonempty_text(attempt.get("attempt_id"))
    }
    truth_by_id = {attempt["attempt_id"]: attempt for attempt in truth_attempts}
    for attempt_id in sorted(set(truth_by_id) - set(attempts_by_id)):
        findings.append(Finding("RECEIPT_MISSING_ATTEMPT", f"receipt omits ground-truth attempt {attempt_id}"))
    for attempt_id in sorted(set(attempts_by_id) - set(truth_by_id)):
        findings.append(Finding("RECEIPT_EXTRA_ATTEMPT", f"receipt invents attempt {attempt_id}"))
    for attempt_id in sorted(set(attempts_by_id) & set(truth_by_id)):
        if attempts_by_id[attempt_id] != truth_by_id[attempt_id]:
            findings.append(Finding("RECEIPT_ATTEMPT_MISMATCH", f"attempt {attempt_id} disagrees with ground truth"))

    if receipt.get("final_attempt_id") != ground_truth.get("final_attempt_id"):
        findings.append(Finding("RECEIPT_FINAL_ATTEMPT_ATTRIBUTION", "final_attempt_id disagrees with ground truth"))
    if receipt.get("final_model") != ground_truth.get("final_model"):
        findings.append(Finding("RECEIPT_FINAL_MODEL_ATTRIBUTION", "final_model disagrees with ground truth"))

    fallback = receipt.get("fallback")
    if not isinstance(fallback, dict):
        findings.append(Finding("RECEIPT_FALLBACK", "fallback must be an object"))
    else:
        _check_contract_fields(
            fallback,
            {"permitted", "used", "trigger_attempt_id", "trigger_outcome"},
            "receipt.fallback",
            findings,
            prefix="RECEIPT",
        )
        if not isinstance(fallback.get("permitted"), bool) or not isinstance(fallback.get("used"), bool):
            findings.append(Finding("RECEIPT_FALLBACK", "fallback permitted and used must be booleans"))
        fallback_used = len(attempts) > 1
        if fallback.get("used") != fallback_used:
            findings.append(Finding("RECEIPT_FALLBACK_ATTRIBUTION", "fallback.used disagrees with attempts"))
        if fallback_used:
            trigger = attempts[-2]
            final_attempt = attempts[-1]
            if fallback.get("trigger_attempt_id") != trigger.get("attempt_id"):
                findings.append(
                    Finding("RECEIPT_FALLBACK_ATTRIBUTION", "fallback trigger_attempt_id is incorrect")
                )
            if fallback.get("trigger_outcome") != trigger.get("outcome"):
                findings.append(Finding("RECEIPT_FALLBACK_ATTRIBUTION", "fallback trigger_outcome is incorrect"))
            allowed_transitions = ground_truth["fallback_policy"]["allowed_transitions"]
            actual_permitted = any(
                transition["from_route"] == trigger.get("route")
                and transition["to_route"] == final_attempt.get("route")
                and trigger.get("outcome") in transition["on_outcomes"]
                for transition in allowed_transitions
            )
            claimed_permitted = any(
                transition["from_route"] == trigger.get("route")
                and transition["to_route"] == final_attempt.get("route")
                and fallback.get("trigger_outcome") in transition["on_outcomes"]
                for transition in allowed_transitions
            )
            if not actual_permitted or not claimed_permitted:
                findings.append(Finding("RECEIPT_FORBIDDEN_FALLBACK", "fallback transition is not permitted"))
            if fallback.get("permitted") != actual_permitted:
                findings.append(Finding("RECEIPT_FALLBACK_ATTRIBUTION", "fallback.permitted is incorrect"))
        elif fallback.get("trigger_attempt_id") is not None or fallback.get("trigger_outcome") is not None:
            findings.append(Finding("RECEIPT_FALLBACK_ATTRIBUTION", "unused fallback cannot have a trigger"))

    quality = receipt.get("quality")
    final_quality_status: str | None = None
    if not isinstance(quality, dict):
        findings.append(Finding("RECEIPT_QUALITY", "quality must be an object"))
    else:
        _check_contract_fields(
            quality,
            {"assessed_attempt_ids", "unassessed_attempts", "final_quality_status"},
            "receipt.quality",
            findings,
            prefix="RECEIPT",
        )
        assessed = _validate_string_list(
            quality.get("assessed_attempt_ids"),
            "receipt.quality.assessed_attempt_ids",
            findings,
            code="RECEIPT_QUALITY",
        )
        unassessed_value = quality.get("unassessed_attempts")
        unassessed: dict[str, str] = {}
        if not isinstance(unassessed_value, list):
            findings.append(Finding("RECEIPT_QUALITY", "unassessed_attempts must be a list"))
        else:
            for index, item in enumerate(unassessed_value):
                context = f"receipt.quality.unassessed_attempts[{index}]"
                if not isinstance(item, dict):
                    findings.append(Finding("RECEIPT_QUALITY", f"{context} must be an object"))
                    continue
                _check_contract_fields(
                    item,
                    {"attempt_id", "reason"},
                    context,
                    findings,
                    prefix="RECEIPT",
                )
                if not _is_nonempty_text(item.get("attempt_id")) or not _is_enum_value(
                    item.get("reason"),
                    {"runtime_failure", "validation_failure", "policy_rejection"},
                ):
                    findings.append(Finding("RECEIPT_QUALITY", f"{context} is invalid"))
                else:
                    unassessed[item["attempt_id"]] = item["reason"]

        expected_assessed = {
            attempt.get("attempt_id")
            for attempt in attempts
            if _is_nonempty_text(attempt.get("attempt_id"))
            and _is_enum_value(attempt.get("quality_status"), {"assessed_pass", "assessed_fail"})
        }
        reason_by_status = {
            "not_assessed_runtime_failure": "runtime_failure",
            "not_assessed_validation_failure": "validation_failure",
            "not_assessed_policy_rejection": "policy_rejection",
        }
        expected_unassessed = {
            attempt.get("attempt_id"): reason_by_status[attempt["quality_status"]]
            for attempt in attempts
            if _is_nonempty_text(attempt.get("attempt_id"))
            and isinstance(attempt.get("quality_status"), str)
            and attempt.get("quality_status") in reason_by_status
        }
        assessed_set = set(assessed or [])
        if assessed_set & set(expected_unassessed):
            findings.append(
                Finding(
                    "RECEIPT_UNASSESSED_QUALITY_CONTAMINATION",
                    "an unassessed attempt appears in assessed quality evidence",
                )
            )
        if assessed_set != expected_assessed or unassessed != expected_unassessed:
            findings.append(Finding("RECEIPT_QUALITY_ATTRIBUTION", "quality attempt attribution is incomplete"))
        receipt_final_attempt_id = receipt.get("final_attempt_id")
        final_attempt = (
            attempts_by_id.get(receipt_final_attempt_id)
            if isinstance(receipt_final_attempt_id, str)
            else None
        )
        expected_final_quality = (
            "pass"
            if final_attempt and final_attempt.get("quality_status") == "assessed_pass"
            else "fail"
            if final_attempt and final_attempt.get("quality_status") == "assessed_fail"
            else "not_assessed"
        )
        final_quality_status = quality.get("final_quality_status")
        if not _is_enum_value(final_quality_status, {"pass", "fail", "not_assessed"}):
            findings.append(Finding("RECEIPT_QUALITY", "final_quality_status is invalid"))
        elif final_quality_status != expected_final_quality:
            findings.append(Finding("RECEIPT_QUALITY_ATTRIBUTION", "final_quality_status is incorrect"))

    writes = receipt.get("writes")
    computed_unexpected: set[str] = set()
    if not isinstance(writes, dict):
        findings.append(Finding("RECEIPT_WRITES", "writes must be an object"))
    else:
        _check_contract_fields(
            writes,
            {"expected", "observed", "unexpected"},
            "receipt.writes",
            findings,
            prefix="RECEIPT",
        )
        expected = _validate_string_list(
            writes.get("expected"), "receipt.writes.expected", findings, code="RECEIPT_WRITES"
        )
        observed = _validate_string_list(
            writes.get("observed"), "receipt.writes.observed", findings, code="RECEIPT_WRITES"
        )
        unexpected = _validate_string_list(
            writes.get("unexpected"), "receipt.writes.unexpected", findings, code="RECEIPT_WRITES"
        )
        if expected is not None and expected != ground_truth["expected_writes"]:
            findings.append(Finding("RECEIPT_WRITE_ATTRIBUTION", "expected writes disagree with ground truth"))
        if observed is not None and observed != ground_truth["observed_writes"]:
            findings.append(Finding("RECEIPT_WRITE_ATTRIBUTION", "observed writes disagree with ground truth"))
        if expected is not None and observed is not None:
            computed_unexpected = set(observed) - set(expected)
            if unexpected is not None and set(unexpected) != computed_unexpected:
                findings.append(Finding("RECEIPT_WRITE_ATTRIBUTION", "unexpected writes are incomplete"))
            if computed_unexpected:
                findings.append(
                    Finding(
                        "RECEIPT_UNEXPECTED_WRITES",
                        f"unexpected writes observed: {', '.join(sorted(computed_unexpected))}",
                    )
                )

    finalization = receipt.get("finalization")
    finalization_ok = False
    if not isinstance(finalization, dict):
        findings.append(Finding("RECEIPT_FINALIZATION", "finalization must be an object"))
    else:
        _check_contract_fields(
            finalization,
            {"status", "integrity_verified"},
            "receipt.finalization",
            findings,
            prefix="RECEIPT",
        )
        if not _is_enum_value(finalization.get("status"), {"finalized", "failed"}):
            findings.append(Finding("RECEIPT_FINALIZATION", "finalization.status is invalid"))
        if not isinstance(finalization.get("integrity_verified"), bool):
            findings.append(Finding("RECEIPT_FINALIZATION", "integrity_verified must be boolean"))
        finalization_ok = (
            finalization.get("status") == "finalized" and finalization.get("integrity_verified") is True
        )

    if not finalization_ok:
        findings.append(
            Finding(
                "RECEIPT_FINALIZATION_FAILURE",
                "a receipt cannot validate after finalization or integrity failure",
            )
        )

    mode = receipt.get("receipt_mode")
    if mode == "passive":
        if receipt.get("enforcement_status") != "not_applied" or receipt.get("completion_claim") != "observed_only":
            findings.append(
                Finding("RECEIPT_PASSIVE_AUTHORITY", "passive receipts may claim observed_only, not enforcement")
            )
    elif mode == "enforced":
        if receipt.get("enforcement_status") != "pass" or receipt.get("completion_claim") != "auditable_complete":
            findings.append(
                Finding("RECEIPT_ENFORCEMENT", "a successful enforced receipt must claim auditable_complete")
            )
        if computed_unexpected or final_quality_status != "pass":
            findings.append(
                Finding(
                    "RECEIPT_ENFORCEMENT_FALSE_PASS",
                    "enforcement cannot pass with unexpected writes or non-passing final quality",
                )
            )
    return findings


def _validate_route_attempts_v1(
    value: object,
    findings: list[Finding],
    *,
    prefix: str,
    context: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        findings.append(Finding(f"{prefix}_ATTEMPTS", f"{context} must be a non-empty list"))
        return []
    if len(value) > 2:
        findings.append(Finding(f"{prefix}_ATTEMPTS", f"{context} may contain at most two attempts"))

    attempt_fields = {
        "attempt_id",
        "ordinal",
        "route",
        "requested_model",
        "responding_model",
        "outcome",
        "failure_stage",
        "failure_category",
        "quality_status",
        "source_boundary_status",
        "safety_boundary_status",
        "fallback_from_attempt_id",
        "input_tokens",
        "output_tokens",
        "wall_seconds",
    }
    failure_categories = {
        "none",
        "infrastructure",
        "schema",
        "source_boundary",
        "safety_policy",
    }
    quality_statuses = {
        "assessed_pass",
        "assessed_fail",
        "not_assessed_runtime_failure",
        "not_assessed_schema_failure",
        "not_assessed_source_boundary",
        "not_assessed_safety_policy",
    }
    source_statuses = {"not_applicable", "pass", "fail", "not_assessed"}
    safety_statuses = {"not_applicable", "pass", "blocked", "not_assessed"}
    attempts: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for index, attempt in enumerate(value, start=1):
        attempt_context = f"{context}[{index - 1}]"
        if not isinstance(attempt, dict):
            findings.append(Finding(f"{prefix}_ATTEMPT_SHAPE", f"{attempt_context} must be an object"))
            continue
        _check_contract_fields(attempt, attempt_fields, attempt_context, findings, prefix=prefix)
        attempts.append(attempt)

        attempt_id = attempt.get("attempt_id")
        if not _is_nonempty_text(attempt_id):
            findings.append(Finding(f"{prefix}_ATTEMPT_ID", f"{attempt_context}.attempt_id must be non-empty text"))
        elif attempt_id in seen_ids:
            findings.append(Finding(f"{prefix}_ATTEMPT_ID", f"duplicate attempt_id {attempt_id}"))
        else:
            seen_ids.add(attempt_id)

        ordinal = attempt.get("ordinal")
        if not _is_nonnegative_int(ordinal) or ordinal != index:
            findings.append(
                Finding(
                    f"{prefix}_ATTEMPT_ORDER",
                    f"{attempt_context}.ordinal must be integer {index}",
                )
            )
        if not _is_enum_value(attempt.get("route"), ROUTE_RECEIPT_ROUTES - {"hold"}):
            findings.append(Finding(f"{prefix}_ATTEMPT_ROUTE", f"{attempt_context}.route is invalid"))
        if not _is_nonempty_text(attempt.get("requested_model")):
            findings.append(
                Finding(f"{prefix}_ATTEMPT_MODEL", f"{attempt_context}.requested_model must be non-empty text")
            )
        responding_model = attempt.get("responding_model")
        if responding_model is not None and not _is_nonempty_text(responding_model):
            findings.append(
                Finding(f"{prefix}_ATTEMPT_MODEL", f"{attempt_context}.responding_model must be text or null")
            )

        outcome = attempt.get("outcome")
        stage = attempt.get("failure_stage")
        failure_category = attempt.get("failure_category")
        quality_status = attempt.get("quality_status")
        source_status = attempt.get("source_boundary_status")
        safety_status = attempt.get("safety_boundary_status")
        if not _is_enum_value(outcome, ROUTE_ATTEMPT_OUTCOMES):
            findings.append(Finding(f"{prefix}_ATTEMPT_OUTCOME", f"{attempt_context}.outcome is invalid"))
        if not _is_enum_value(stage, ROUTE_ATTEMPT_FAILURE_STAGES):
            findings.append(Finding(f"{prefix}_ATTEMPT_STAGE", f"{attempt_context}.failure_stage is invalid"))
        if not _is_enum_value(failure_category, failure_categories):
            findings.append(
                Finding(f"{prefix}_ATTEMPT_FAILURE_CATEGORY", f"{attempt_context}.failure_category is invalid")
            )
        if not _is_enum_value(quality_status, quality_statuses):
            findings.append(Finding(f"{prefix}_ATTEMPT_QUALITY", f"{attempt_context}.quality_status is invalid"))
        if not _is_enum_value(source_status, source_statuses):
            findings.append(
                Finding(f"{prefix}_ATTEMPT_SOURCE_BOUNDARY", f"{attempt_context}.source_boundary_status is invalid")
            )
        if not _is_enum_value(safety_status, safety_statuses):
            findings.append(
                Finding(f"{prefix}_ATTEMPT_SAFETY_BOUNDARY", f"{attempt_context}.safety_boundary_status is invalid")
            )

        semantics_ok = True
        if outcome == "completed":
            semantics_ok = (
                stage == "none"
                and failure_category == "none"
                and responding_model is not None
                and quality_status in {"assessed_pass", "assessed_fail"}
                and source_status in {"not_applicable", "pass"}
                and safety_status in {"not_applicable", "pass"}
            )
        elif outcome == "infrastructure_failure":
            semantics_ok = (
                stage in {"pre_request", "request_open", "response_stream"}
                and failure_category == "infrastructure"
                and quality_status == "not_assessed_runtime_failure"
                and source_status in {"not_applicable", "not_assessed"}
                and safety_status in {"not_applicable", "not_assessed"}
            )
        elif outcome == "validation_failure" and failure_category == "schema":
            semantics_ok = (
                stage == "validation"
                and quality_status == "not_assessed_schema_failure"
                and source_status in {"not_applicable", "not_assessed"}
                and safety_status in {"not_applicable", "pass", "not_assessed"}
            )
        elif outcome == "validation_failure" and failure_category == "source_boundary":
            semantics_ok = (
                stage == "validation"
                and quality_status == "not_assessed_source_boundary"
                and source_status == "fail"
                and safety_status in {"not_applicable", "pass", "not_assessed"}
            )
        elif outcome == "policy_rejected":
            semantics_ok = (
                stage == "policy"
                and failure_category == "safety_policy"
                and quality_status == "not_assessed_safety_policy"
                and source_status in {"not_applicable", "pass", "not_assessed"}
                and safety_status == "blocked"
            )
        if outcome in ROUTE_ATTEMPT_OUTCOMES and not semantics_ok:
            findings.append(
                Finding(
                    f"{prefix}_ATTEMPT_SEMANTICS",
                    f"{attempt_context}: outcome, failure, quality, and boundary evidence disagree",
                )
            )

        fallback_from = attempt.get("fallback_from_attempt_id")
        if index == 1:
            if fallback_from is not None:
                findings.append(
                    Finding(f"{prefix}_FALLBACK_CHAIN", f"{attempt_context}: first attempt cannot be a fallback")
                )
        else:
            previous_id = value[index - 2].get("attempt_id") if isinstance(value[index - 2], dict) else None
            if fallback_from != previous_id:
                findings.append(
                    Finding(
                        f"{prefix}_FALLBACK_CHAIN",
                        f"{attempt_context}.fallback_from_attempt_id must reference the previous attempt",
                    )
                )

        for field in ("input_tokens", "output_tokens"):
            if not _is_nonnegative_int(attempt.get(field)):
                findings.append(
                    Finding(f"{prefix}_ATTEMPT_METRIC", f"{attempt_context}.{field} must be non-negative")
                )
        if not _is_finite_nonnegative_number(attempt.get("wall_seconds")):
            findings.append(
                Finding(f"{prefix}_ATTEMPT_METRIC", f"{attempt_context}.wall_seconds must be finite and non-negative")
            )
    return attempts


def _validate_attempt_ground_truth_v1(ground_truth: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    root_fields = {
        "schema_version",
        "case_id",
        "execution_mode",
        "model_called",
        "network_called",
        "state_mutating",
        "policy_id",
        "candidate_route",
        "fallback_policy",
        "attempts",
        "final_attempt_id",
        "final_model",
        "completion_status",
        "decision_disposition",
        "final_quality_status",
        "expected_writes",
        "observed_writes",
    }
    _check_contract_fields(ground_truth, root_fields, "ground_truth", findings, prefix="GROUND_TRUTH")
    if ground_truth.get("schema_version") != ATTEMPT_GROUND_TRUTH_SCHEMA_V1:
        findings.append(
            Finding(
                "GROUND_TRUTH_SCHEMA_VERSION",
                f"schema_version must be {ATTEMPT_GROUND_TRUTH_SCHEMA_V1}",
            )
        )
    for field in ("case_id", "policy_id"):
        if not _is_nonempty_text(ground_truth.get(field)):
            findings.append(Finding("GROUND_TRUTH_STRING", f"ground_truth.{field} must be non-empty text"))
    if not _is_enum_value(ground_truth.get("candidate_route"), ROUTE_RECEIPT_ROUTES - {"hold"}):
        findings.append(Finding("GROUND_TRUTH_ROUTE", "ground_truth.candidate_route is invalid"))
    _validate_synthetic_invariants(ground_truth, "ground_truth", findings, prefix="GROUND_TRUTH")

    fallback_policy = ground_truth.get("fallback_policy")
    allowed_transitions: list[dict[str, Any]] = []
    max_attempts: int | None = None
    if not isinstance(fallback_policy, dict):
        findings.append(Finding("GROUND_TRUTH_FALLBACK_POLICY", "fallback_policy must be an object"))
    else:
        _check_contract_fields(
            fallback_policy,
            {"max_attempts", "allowed_transitions"},
            "ground_truth.fallback_policy",
            findings,
            prefix="GROUND_TRUTH",
        )
        if (
            not _is_nonnegative_int(fallback_policy.get("max_attempts"))
            or fallback_policy.get("max_attempts", 0) < 1
            or fallback_policy.get("max_attempts", 0) > 2
        ):
            findings.append(Finding("GROUND_TRUTH_FALLBACK_POLICY", "max_attempts must be 1 or 2"))
        else:
            max_attempts = fallback_policy["max_attempts"]
        transitions = fallback_policy.get("allowed_transitions")
        if not isinstance(transitions, list) or len(transitions) > 1:
            findings.append(
                Finding("GROUND_TRUTH_FALLBACK_POLICY", "allowed_transitions must contain at most one transition")
            )
        else:
            if max_attempts == 1 and transitions:
                findings.append(
                    Finding("GROUND_TRUTH_FALLBACK_POLICY", "max_attempts 1 cannot allow a transition")
                )
            for index, transition in enumerate(transitions):
                context = f"ground_truth.fallback_policy.allowed_transitions[{index}]"
                if not isinstance(transition, dict):
                    findings.append(Finding("GROUND_TRUTH_FALLBACK_POLICY", f"{context} must be an object"))
                    continue
                _check_contract_fields(
                    transition,
                    {
                        "transition_id",
                        "from_route",
                        "to_route",
                        "on_outcomes",
                        "on_failure_categories",
                    },
                    context,
                    findings,
                    prefix="GROUND_TRUTH",
                )
                transition_ok = True
                if not _is_nonempty_text(transition.get("transition_id")):
                    transition_ok = False
                if not _is_enum_value(transition.get("from_route"), ROUTE_RECEIPT_ROUTES - {"hold"}):
                    transition_ok = False
                if not _is_enum_value(transition.get("to_route"), ROUTE_RECEIPT_ROUTES - {"hold"}):
                    transition_ok = False
                outcomes = transition.get("on_outcomes")
                if (
                    not isinstance(outcomes, list)
                    or not outcomes
                    or any(not _is_enum_value(item, ROUTE_ATTEMPT_OUTCOMES - {"completed"}) for item in outcomes)
                    or len(outcomes) != len(set(outcomes))
                ):
                    transition_ok = False
                categories = transition.get("on_failure_categories")
                if (
                    not isinstance(categories, list)
                    or not categories
                    or any(
                        not _is_enum_value(
                            item,
                            {"infrastructure", "schema", "source_boundary", "safety_policy"},
                        )
                        for item in categories
                    )
                    or len(categories) != len(set(categories))
                ):
                    transition_ok = False
                if transition_ok:
                    allowed_transitions.append(transition)
                else:
                    findings.append(Finding("GROUND_TRUTH_FALLBACK_POLICY", f"{context} is invalid"))

    attempts = _validate_route_attempts_v1(
        ground_truth.get("attempts"), findings, prefix="GROUND_TRUTH", context="ground_truth.attempts"
    )
    if attempts and ground_truth.get("candidate_route") != attempts[0].get("route"):
        findings.append(
            Finding("GROUND_TRUTH_CANDIDATE_ATTRIBUTION", "candidate_route must match the first attempt")
        )
    if max_attempts is not None and len(attempts) > max_attempts:
        findings.append(Finding("GROUND_TRUTH_FALLBACK_POLICY", "attempt count exceeds max_attempts"))
    for previous, current in zip(attempts, attempts[1:]):
        allowed = any(
            transition.get("from_route") == previous.get("route")
            and transition.get("to_route") == current.get("route")
            and previous.get("outcome") in transition.get("on_outcomes", [])
            and previous.get("failure_category") in transition.get("on_failure_categories", [])
            for transition in allowed_transitions
        )
        if not allowed:
            findings.append(
                Finding(
                    "GROUND_TRUTH_FORBIDDEN_FALLBACK",
                    f"fallback from {previous.get('route')} to {current.get('route')} is not permitted",
                )
            )

    final_attempt: dict[str, Any] | None = None
    final_attempt_id = ground_truth.get("final_attempt_id")
    if not _is_nonempty_text(final_attempt_id):
        findings.append(Finding("GROUND_TRUTH_FINAL_ATTEMPT", "final_attempt_id must be non-empty text"))
    else:
        final_attempt = next(
            (attempt for attempt in attempts if attempt.get("attempt_id") == final_attempt_id),
            None,
        )
        if final_attempt is None:
            findings.append(Finding("GROUND_TRUTH_FINAL_ATTEMPT", "final_attempt_id must reference an attempt"))
        elif final_attempt is not attempts[-1]:
            findings.append(Finding("GROUND_TRUTH_FINAL_ATTEMPT", "final_attempt_id must reference the last attempt"))

    final_model = ground_truth.get("final_model")
    if final_model is not None and not _is_nonempty_text(final_model):
        findings.append(Finding("GROUND_TRUTH_FINAL_MODEL", "final_model must be text or null"))
    if final_attempt is not None and final_model != final_attempt.get("responding_model"):
        findings.append(Finding("GROUND_TRUTH_FINAL_MODEL", "final_model must match the final responding model"))

    completion_status = ground_truth.get("completion_status")
    decision_disposition = ground_truth.get("decision_disposition")
    final_quality_status = ground_truth.get("final_quality_status")
    if not _is_enum_value(completion_status, ROUTE_RECEIPT_COMPLETION_STATUSES):
        findings.append(Finding("GROUND_TRUTH_COMPLETION", "completion_status is invalid"))
    if not _is_enum_value(decision_disposition, {"deliver", "hold"}):
        findings.append(Finding("GROUND_TRUTH_DISPOSITION", "decision_disposition is invalid"))
    if not _is_enum_value(final_quality_status, {"pass", "fail", "not_assessed"}):
        findings.append(Finding("GROUND_TRUTH_QUALITY", "final_quality_status is invalid"))
    if final_attempt is not None:
        expected_completion = (
            "completed_via_fallback"
            if final_attempt.get("outcome") == "completed" and len(attempts) > 1
            else "completed_without_fallback"
            if final_attempt.get("outcome") == "completed"
            else "hold"
        )
        expected_quality = (
            "pass"
            if final_attempt.get("quality_status") == "assessed_pass"
            else "fail"
            if final_attempt.get("quality_status") == "assessed_fail"
            else "not_assessed"
        )
        expected_disposition = "deliver" if expected_quality == "pass" else "hold"
        if completion_status != expected_completion:
            findings.append(
                Finding("GROUND_TRUTH_COMPLETION", f"completion_status must be {expected_completion}")
            )
        if final_quality_status != expected_quality:
            findings.append(
                Finding("GROUND_TRUTH_QUALITY", f"final_quality_status must be {expected_quality}")
            )
        if decision_disposition != expected_disposition:
            findings.append(
                Finding("GROUND_TRUTH_DISPOSITION", f"decision_disposition must be {expected_disposition}")
            )

    expected_writes = _validate_string_list(
        ground_truth.get("expected_writes"),
        "ground_truth.expected_writes",
        findings,
        code="GROUND_TRUTH_WRITES",
    )
    observed_writes = _validate_string_list(
        ground_truth.get("observed_writes"),
        "ground_truth.observed_writes",
        findings,
        code="GROUND_TRUTH_WRITES",
    )
    if expected_writes:
        findings.append(Finding("GROUND_TRUTH_NON_MUTATION", "synthetic v1 cases cannot expect writes"))
    if observed_writes:
        findings.append(Finding("GROUND_TRUTH_NON_MUTATION", "synthetic v1 cases cannot observe writes"))
    return findings


def _expected_v1_fallback_transitions(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(attempts) < 2:
        return []
    previous, current = attempts[0], attempts[1]
    return [
        {
            "ordinal": 1,
            "from_attempt_id": previous.get("attempt_id"),
            "to_attempt_id": current.get("attempt_id"),
            "from_route": previous.get("route"),
            "to_route": current.get("route"),
            "trigger_outcome": previous.get("outcome"),
            "trigger_failure_category": previous.get("failure_category"),
            "permitted": True,
        }
    ]


def _validate_route_receipt_v1(receipt: dict[str, Any], ground_truth: dict[str, Any]) -> list[Finding]:
    if _validate_attempt_ground_truth_v1(ground_truth):
        return [Finding("RECEIPT_GROUND_TRUTH_INVALID", "attempt ground truth must validate first")]

    findings: list[Finding] = []
    root_fields = {
        "schema_version",
        "receipt_id",
        "case_id",
        "receipt_mode",
        "execution_mode",
        "model_called",
        "network_called",
        "state_mutating",
        "policy_id",
        "candidate_route",
        "actual_route",
        "attempts",
        "final_attempt_id",
        "final_model",
        "completion_status",
        "decision_disposition",
        "fallback_transitions",
        "quality",
        "writes",
        "finalization",
        "enforcement_status",
        "completion_claim",
        "automatic_route_change",
        "promotion_decision",
    }
    _check_contract_fields(receipt, root_fields, "receipt", findings, prefix="RECEIPT")
    if receipt.get("schema_version") != ROUTE_RECEIPT_SCHEMA_V1:
        findings.append(Finding("RECEIPT_SCHEMA_VERSION", f"schema_version must be {ROUTE_RECEIPT_SCHEMA_V1}"))
    for field in ("receipt_id", "case_id", "policy_id"):
        if not _is_nonempty_text(receipt.get(field)):
            findings.append(Finding("RECEIPT_STRING", f"receipt.{field} must be non-empty text"))
    mode = receipt.get("receipt_mode")
    if not _is_enum_value(mode, {"passive", "enforced"}):
        findings.append(Finding("RECEIPT_MODE", "receipt_mode must be passive or enforced"))
    if not _is_enum_value(receipt.get("candidate_route"), ROUTE_RECEIPT_ROUTES - {"hold"}):
        findings.append(Finding("RECEIPT_ROUTE", "candidate_route is invalid"))
    if receipt.get("actual_route") != "none":
        findings.append(Finding("RECEIPT_NON_EXECUTION", "actual_route must remain none"))
    _validate_synthetic_invariants(receipt, "receipt", findings, prefix="RECEIPT")
    if receipt.get("automatic_route_change") is not False:
        findings.append(Finding("RECEIPT_PROMOTION", "automatic_route_change must be false"))
    if receipt.get("promotion_decision") != "not_promoted":
        findings.append(Finding("RECEIPT_PROMOTION", "promotion_decision must be not_promoted"))

    for field in (
        "case_id",
        "execution_mode",
        "model_called",
        "network_called",
        "state_mutating",
        "policy_id",
        "candidate_route",
        "completion_status",
        "decision_disposition",
    ):
        if receipt.get(field) != ground_truth.get(field):
            findings.append(Finding("RECEIPT_GROUND_TRUTH_MISMATCH", f"receipt.{field} disagrees with ground truth"))

    attempts = _validate_route_attempts_v1(
        receipt.get("attempts"), findings, prefix="RECEIPT", context="receipt.attempts"
    )
    truth_attempts = ground_truth["attempts"]
    attempts_by_id = {
        attempt["attempt_id"]: attempt for attempt in attempts if _is_nonempty_text(attempt.get("attempt_id"))
    }
    truth_by_id = {attempt["attempt_id"]: attempt for attempt in truth_attempts}
    for attempt_id in sorted(set(truth_by_id) - set(attempts_by_id)):
        findings.append(Finding("RECEIPT_MISSING_ATTEMPT", f"receipt omits ground-truth attempt {attempt_id}"))
    for attempt_id in sorted(set(attempts_by_id) - set(truth_by_id)):
        findings.append(Finding("RECEIPT_EXTRA_ATTEMPT", f"receipt invents attempt {attempt_id}"))
    for attempt_id in sorted(set(attempts_by_id) & set(truth_by_id)):
        if attempts_by_id[attempt_id] != truth_by_id[attempt_id]:
            findings.append(Finding("RECEIPT_ATTEMPT_MISMATCH", f"attempt {attempt_id} disagrees with ground truth"))

    if receipt.get("final_attempt_id") != ground_truth.get("final_attempt_id"):
        findings.append(Finding("RECEIPT_FINAL_ATTEMPT_ATTRIBUTION", "final_attempt_id disagrees with ground truth"))
    if receipt.get("final_model") != ground_truth.get("final_model"):
        findings.append(Finding("RECEIPT_FINAL_MODEL_ATTRIBUTION", "final_model disagrees with ground truth"))

    transitions = receipt.get("fallback_transitions")
    expected_transitions = _expected_v1_fallback_transitions(attempts)
    if not isinstance(transitions, list) or len(transitions) > 1:
        findings.append(Finding("RECEIPT_FALLBACK", "fallback_transitions must contain at most one transition"))
    else:
        transition_fields = {
            "ordinal",
            "from_attempt_id",
            "to_attempt_id",
            "from_route",
            "to_route",
            "trigger_outcome",
            "trigger_failure_category",
            "permitted",
        }
        for index, transition in enumerate(transitions):
            context = f"receipt.fallback_transitions[{index}]"
            if not isinstance(transition, dict):
                findings.append(Finding("RECEIPT_FALLBACK", f"{context} must be an object"))
                continue
            _check_contract_fields(transition, transition_fields, context, findings, prefix="RECEIPT")
            ordinal = transition.get("ordinal")
            if not _is_nonnegative_int(ordinal) or ordinal != 1:
                findings.append(Finding("RECEIPT_FALLBACK", f"{context}.ordinal must be integer 1"))
            if transition.get("permitted") is not True:
                findings.append(Finding("RECEIPT_FALLBACK", f"{context}.permitted must be true"))
        if transitions != expected_transitions:
            findings.append(
                Finding("RECEIPT_FALLBACK_ATTRIBUTION", "fallback transitions disagree with the ordered attempts")
            )
        if len(attempts) > 1:
            previous, current = attempts[0], attempts[1]
            permitted = any(
                transition.get("from_route") == previous.get("route")
                and transition.get("to_route") == current.get("route")
                and previous.get("outcome") in transition.get("on_outcomes", [])
                and previous.get("failure_category") in transition.get("on_failure_categories", [])
                for transition in ground_truth["fallback_policy"]["allowed_transitions"]
            )
            if not permitted:
                findings.append(Finding("RECEIPT_FORBIDDEN_FALLBACK", "fallback transition is not permitted"))

    quality = receipt.get("quality")
    final_quality_status: str | None = None
    if not isinstance(quality, dict):
        findings.append(Finding("RECEIPT_QUALITY", "quality must be an object"))
    else:
        _check_contract_fields(
            quality,
            {"assessed_attempt_ids", "unassessed_attempts", "final_quality_status"},
            "receipt.quality",
            findings,
            prefix="RECEIPT",
        )
        assessed = _validate_string_list(
            quality.get("assessed_attempt_ids"),
            "receipt.quality.assessed_attempt_ids",
            findings,
            code="RECEIPT_QUALITY",
        )
        unassessed_value = quality.get("unassessed_attempts")
        unassessed: dict[str, str] = {}
        if not isinstance(unassessed_value, list):
            findings.append(Finding("RECEIPT_QUALITY", "unassessed_attempts must be a list"))
        else:
            for index, item in enumerate(unassessed_value):
                context = f"receipt.quality.unassessed_attempts[{index}]"
                if not isinstance(item, dict):
                    findings.append(Finding("RECEIPT_QUALITY", f"{context} must be an object"))
                    continue
                _check_contract_fields(item, {"attempt_id", "reason"}, context, findings, prefix="RECEIPT")
                attempt_id = item.get("attempt_id")
                reason = item.get("reason")
                if not _is_nonempty_text(attempt_id) or not _is_enum_value(
                    reason,
                    {"infrastructure", "schema", "source_boundary", "safety_policy"},
                ):
                    findings.append(Finding("RECEIPT_QUALITY", f"{context} is invalid"))
                elif attempt_id in unassessed:
                    findings.append(Finding("RECEIPT_QUALITY", f"{context}.attempt_id is duplicated"))
                else:
                    unassessed[attempt_id] = reason

        expected_assessed = {
            attempt.get("attempt_id")
            for attempt in attempts
            if _is_nonempty_text(attempt.get("attempt_id"))
            and attempt.get("quality_status") in {"assessed_pass", "assessed_fail"}
        }
        expected_unassessed = {
            attempt["attempt_id"]: attempt.get("failure_category")
            for attempt in attempts
            if _is_nonempty_text(attempt.get("attempt_id"))
            and attempt.get("quality_status")
            in {
                "not_assessed_runtime_failure",
                "not_assessed_schema_failure",
                "not_assessed_source_boundary",
                "not_assessed_safety_policy",
            }
        }
        assessed_set = set(assessed or [])
        if assessed_set & set(expected_unassessed):
            findings.append(
                Finding(
                    "RECEIPT_UNASSESSED_QUALITY_CONTAMINATION",
                    "an unassessed attempt appears in assessed quality evidence",
                )
            )
        if assessed_set != expected_assessed or unassessed != expected_unassessed:
            findings.append(Finding("RECEIPT_QUALITY_ATTRIBUTION", "quality attempt attribution is incomplete"))
        final_quality_status = quality.get("final_quality_status")
        if not _is_enum_value(final_quality_status, {"pass", "fail", "not_assessed"}):
            findings.append(Finding("RECEIPT_QUALITY", "final_quality_status is invalid"))
        elif final_quality_status != ground_truth.get("final_quality_status"):
            findings.append(
                Finding("RECEIPT_QUALITY_ATTRIBUTION", "final_quality_status disagrees with ground truth")
            )

    writes = receipt.get("writes")
    computed_unexpected: set[str] = set()
    if not isinstance(writes, dict):
        findings.append(Finding("RECEIPT_WRITES", "writes must be an object"))
    else:
        _check_contract_fields(
            writes,
            {"expected", "observed", "unexpected"},
            "receipt.writes",
            findings,
            prefix="RECEIPT",
        )
        expected = _validate_string_list(
            writes.get("expected"), "receipt.writes.expected", findings, code="RECEIPT_WRITES"
        )
        observed = _validate_string_list(
            writes.get("observed"), "receipt.writes.observed", findings, code="RECEIPT_WRITES"
        )
        unexpected = _validate_string_list(
            writes.get("unexpected"), "receipt.writes.unexpected", findings, code="RECEIPT_WRITES"
        )
        if expected is not None and expected != ground_truth["expected_writes"]:
            findings.append(Finding("RECEIPT_WRITE_ATTRIBUTION", "expected writes disagree with ground truth"))
        if observed is not None and observed != ground_truth["observed_writes"]:
            findings.append(Finding("RECEIPT_WRITE_ATTRIBUTION", "observed writes disagree with ground truth"))
        if expected is not None and observed is not None:
            computed_unexpected = set(observed) - set(expected)
            if unexpected is not None and set(unexpected) != computed_unexpected:
                findings.append(Finding("RECEIPT_WRITE_ATTRIBUTION", "unexpected writes are incomplete"))
        if any((expected, observed, unexpected)):
            findings.append(Finding("RECEIPT_NON_MUTATION", "synthetic v1 receipts cannot contain writes"))
        if computed_unexpected:
            findings.append(
                Finding(
                    "RECEIPT_UNEXPECTED_WRITES",
                    f"unexpected writes observed: {', '.join(sorted(computed_unexpected))}",
                )
            )

    finalization = receipt.get("finalization")
    finalization_ok = False
    if not isinstance(finalization, dict):
        findings.append(Finding("RECEIPT_FINALIZATION", "finalization must be an object"))
    else:
        _check_contract_fields(
            finalization,
            {"status", "integrity_verified"},
            "receipt.finalization",
            findings,
            prefix="RECEIPT",
        )
        finalization_ok = (
            finalization.get("status") == "finalized" and finalization.get("integrity_verified") is True
        )
    if not finalization_ok:
        findings.append(
            Finding(
                "RECEIPT_FINALIZATION_FAILURE",
                "a receipt cannot validate after finalization or integrity failure",
            )
        )

    disposition = receipt.get("decision_disposition")
    if not _is_enum_value(disposition, {"deliver", "hold"}):
        findings.append(Finding("RECEIPT_DISPOSITION", "decision_disposition is invalid"))
    if mode == "passive":
        if receipt.get("enforcement_status") != "not_applied" or receipt.get("completion_claim") != "observed_only":
            findings.append(
                Finding("RECEIPT_PASSIVE_AUTHORITY", "passive receipts may claim observed_only, not enforcement")
            )
    elif mode == "enforced":
        expected_claim = "auditable_complete" if disposition == "deliver" else "auditable_hold"
        if receipt.get("enforcement_status") != "pass" or receipt.get("completion_claim") != expected_claim:
            findings.append(
                Finding("RECEIPT_ENFORCEMENT", f"enforced {disposition} receipts must claim {expected_claim}")
            )
        if not finalization_ok or computed_unexpected:
            findings.append(
                Finding(
                    "RECEIPT_ENFORCEMENT_FALSE_PASS",
                    "enforcement cannot pass without finalization and write containment",
                )
            )
        if disposition == "deliver" and final_quality_status != "pass":
            findings.append(
                Finding("RECEIPT_ENFORCEMENT_FALSE_PASS", "delivery requires passing final quality")
            )
        if disposition == "hold" and final_quality_status not in {"fail", "not_assessed"}:
            findings.append(
                Finding("RECEIPT_ENFORCEMENT_FALSE_PASS", "a hold cannot claim passing final quality")
            )
    return findings


def validate_attempt_ground_truth(ground_truth: dict[str, Any]) -> list[Finding]:
    version = ground_truth.get("schema_version")
    if version == ATTEMPT_GROUND_TRUTH_SCHEMA:
        return _validate_attempt_ground_truth_v0(ground_truth)
    if version == ATTEMPT_GROUND_TRUTH_SCHEMA_V1:
        return _validate_attempt_ground_truth_v1(ground_truth)
    return [
        Finding(
            "GROUND_TRUTH_SCHEMA_VERSION",
            "schema_version must be route_attempt_ground_truth_v0 or route_attempt_ground_truth_v1",
        )
    ]


def validate_route_receipt(receipt: dict[str, Any], ground_truth: dict[str, Any]) -> list[Finding]:
    if validate_attempt_ground_truth(ground_truth):
        return [Finding("RECEIPT_GROUND_TRUTH_INVALID", "attempt ground truth must validate first")]

    receipt_version = receipt.get("schema_version")
    ground_truth_version = ground_truth.get("schema_version")
    if receipt_version == ROUTE_RECEIPT_SCHEMA and ground_truth_version == ATTEMPT_GROUND_TRUTH_SCHEMA:
        return _validate_route_receipt_v0(receipt, ground_truth)
    if receipt_version == ROUTE_RECEIPT_SCHEMA_V1 and ground_truth_version == ATTEMPT_GROUND_TRUTH_SCHEMA_V1:
        return _validate_route_receipt_v1(receipt, ground_truth)
    if receipt_version not in {ROUTE_RECEIPT_SCHEMA, ROUTE_RECEIPT_SCHEMA_V1}:
        return [
            Finding(
                "RECEIPT_SCHEMA_VERSION",
                "schema_version must be route_receipt_v0 or route_receipt_v1",
            )
        ]
    return [
        Finding(
            "RECEIPT_SCHEMA_VERSION_MISMATCH",
            "receipt and ground truth schema versions must use the same exact contract generation",
        )
    ]


def _run_route_receipt_v1_mutation(
    mutation_id: str,
    receipt: dict[str, Any],
    ground_truth: dict[str, Any],
    receipt_text: str,
) -> list[Finding]:
    if mutation_id == "duplicate_json_key":
        mutated_text = receipt_text.replace(
            "{",
            '{"schema_version":"route_receipt_v1",',
            1,
        )
        try:
            value = json.loads(mutated_text, object_pairs_hook=_reject_duplicate_keys)
        except (ValueError, json.JSONDecodeError) as exc:
            return [Finding("RECEIPT_JSON", str(exc))]
        if not isinstance(value, dict):
            return [Finding("RECEIPT_SHAPE", "route receipt must be a JSON object")]
        return validate_route_receipt(value, ground_truth)

    mutated = copy.deepcopy(receipt)
    if mutation_id == "missing_attempt":
        mutated["attempts"].pop(0)
    elif mutation_id == "duplicated_attempt":
        mutated["attempts"][1] = copy.deepcopy(mutated["attempts"][0])
    elif mutation_id == "reordered_attempts":
        mutated["attempts"].reverse()
    elif mutation_id == "invented_attempt":
        invented_id = "p2-m02-invented-attempt"
        mutated["attempts"][1]["attempt_id"] = invented_id
        mutated["final_attempt_id"] = invented_id
        mutated["fallback_transitions"][0]["to_attempt_id"] = invented_id
        mutated["quality"]["assessed_attempt_ids"] = [invented_id]
    elif mutation_id == "candidate_route_mismatch":
        mutated["candidate_route"] = "deep_comparison"
    elif mutation_id == "final_attempt_not_last":
        mutated["final_attempt_id"] = mutated["attempts"][0]["attempt_id"]
    elif mutation_id == "final_model_mismatch":
        mutated["final_model"] = mutated["attempts"][0]["requested_model"]
    elif mutation_id == "fallback_wrong_trigger":
        mutated["fallback_transitions"][0]["trigger_outcome"] = "validation_failure"
    elif mutation_id == "fallback_absent_from_policy":
        mutated["attempts"][1]["route"] = "deep_comparison"
        mutated["fallback_transitions"][0]["to_route"] = "deep_comparison"
    elif mutation_id == "fallback_marked_unused":
        mutated["fallback_transitions"] = []
    elif mutation_id == "runtime_quality_contamination":
        mutated["quality"]["assessed_attempt_ids"].insert(
            0,
            mutated["attempts"][0]["attempt_id"],
        )
    elif mutation_id == "source_relabelled_as_quality":
        attempt = mutated["attempts"][0]
        attempt.update(
            {
                "outcome": "completed",
                "failure_stage": "none",
                "failure_category": "none",
                "quality_status": "assessed_fail",
                "source_boundary_status": "pass",
            }
        )
        mutated["quality"] = {
            "assessed_attempt_ids": [attempt["attempt_id"]],
            "unassessed_attempts": [],
            "final_quality_status": "fail",
        }
    elif mutation_id == "safety_relabelled_as_runtime":
        attempt = mutated["attempts"][0]
        attempt.update(
            {
                "outcome": "infrastructure_failure",
                "failure_stage": "pre_request",
                "failure_category": "infrastructure",
                "quality_status": "not_assessed_runtime_failure",
                "source_boundary_status": "not_assessed",
                "safety_boundary_status": "not_assessed",
            }
        )
        mutated["quality"]["unassessed_attempts"][0]["reason"] = "infrastructure"
    elif mutation_id == "invented_expected_write":
        mutated["writes"]["expected"] = ["state/expected.json"]
    elif mutation_id == "invented_observed_write":
        mutated["writes"]["observed"] = ["state/observed.json"]
    elif mutation_id == "omitted_unexpected_write":
        mutated["writes"]["observed"] = ["state/unexpected.json"]
        mutated["writes"]["unexpected"] = []
    elif mutation_id == "exhausted_hold_claimed_complete":
        mutated["completion_claim"] = "auditable_complete"
    elif mutation_id == "passive_promoted_to_enforcement":
        mutated["enforcement_status"] = "pass"
        mutated["completion_claim"] = "auditable_hold"
    elif mutation_id == "failed_finalization":
        mutated["finalization"] = {"status": "failed", "integrity_verified": False}
    elif mutation_id == "unverified_finalization":
        mutated["finalization"]["integrity_verified"] = False
    elif mutation_id == "unknown_field":
        mutated["invented_field"] = True
    elif mutation_id == "invalid_field_type":
        mutated["candidate_route"] = []
    elif mutation_id == "nonfinite_attempt_metric":
        mutated["attempts"][0]["wall_seconds"] = float("nan")
    else:
        raise ValueError(f"unsupported route-receipt mutation: {mutation_id}")
    return validate_route_receipt(mutated, ground_truth)


def _conformance_fraction(passed: int, total: int) -> dict[str, int]:
    return {"passed": passed, "total": total}


def build_route_receipt_conformance_report(
    manifest_path: Path,
) -> tuple[dict[str, Any] | None, list[Finding]]:
    manifest, findings = load_route_receipt_case_manifest(manifest_path)
    if manifest is None or findings:
        return None, findings

    base = manifest_path.resolve().parent
    false_reject_case_ids: list[str] = []
    false_reject_receipt_ids: list[str] = []
    evidence_objects: list[dict[str, Any]] = []
    receipt_objects: dict[str, dict[str, Any]] = {}
    receipt_texts: dict[str, str] = {}
    ground_truth_objects: dict[str, dict[str, Any]] = {}
    accepted_receipt_total = 0

    for case in manifest["cases"]:
        case_id = case["case_id"]
        truth_path = base / case["ground_truth"]["path"]
        ground_truth, truth_findings = load_attempt_ground_truth(truth_path)
        case_passed = ground_truth is not None and not truth_findings
        if ground_truth is not None:
            ground_truth_objects[case_id] = ground_truth
            evidence_objects.append(ground_truth)
        for receipt_spec in case["receipts"]:
            accepted_receipt_total += 1
            receipt_id = receipt_spec["receipt_id"]
            receipt_path = base / receipt_spec["path"]
            receipt, receipt_findings = load_route_receipt(receipt_path)
            if receipt is not None:
                receipt_objects[receipt_id] = receipt
                receipt_texts[receipt_id] = receipt_path.read_text(encoding="utf-8")
                evidence_objects.append(receipt)
            validation_findings = list(receipt_findings)
            if receipt is not None and ground_truth is not None:
                validation_findings.extend(validate_route_receipt(receipt, ground_truth))
            if validation_findings or receipt is None or ground_truth is None:
                false_reject_receipt_ids.append(receipt_id)
                case_passed = False
        if not case_passed:
            false_reject_case_ids.append(case_id)

    mutation_total = 0
    mutation_detected = 0
    false_accept_ids: list[str] = []
    primary_miss_ids: list[str] = []
    validator_crash_ids: list[str] = []
    category_totals: Counter[str] = Counter()
    category_passed: Counter[str] = Counter()
    for case in manifest["cases"]:
        ground_truth = ground_truth_objects[case["case_id"]]
        for mutation in case["mutations"]:
            mutation_total += 1
            mutation_id = mutation["mutation_id"]
            category = mutation["category"]
            primary_code = mutation["primary_finding_code"]
            base_receipt_id = mutation["base_receipt_id"]
            category_totals[category] += 1
            try:
                mutation_findings = _run_route_receipt_v1_mutation(
                    mutation_id,
                    receipt_objects[base_receipt_id],
                    ground_truth,
                    receipt_texts[base_receipt_id],
                )
            except Exception:
                validator_crash_ids.append(mutation_id)
                continue
            codes = {finding.code for finding in mutation_findings}
            if primary_code in codes:
                mutation_detected += 1
                category_passed[category] += 1
            elif not mutation_findings:
                false_accept_ids.append(mutation_id)
            else:
                primary_miss_ids.append(mutation_id)

    actual_route = (
        "none"
        if all(receipt.get("actual_route") == "none" for receipt in receipt_objects.values())
        else "invalid"
    )
    promotion_decision = (
        "not_promoted"
        if all(receipt.get("promotion_decision") == "not_promoted" for receipt in receipt_objects.values())
        else "invalid"
    )
    non_execution = {
        "model_called": any(item.get("model_called") is not False for item in evidence_objects),
        "network_called": any(item.get("network_called") is not False for item in evidence_objects),
        "state_mutating": any(item.get("state_mutating") is not False for item in evidence_objects),
        "actual_route": actual_route,
        "automatic_route_change": any(
            receipt.get("automatic_route_change") is not False for receipt in receipt_objects.values()
        ),
        "promotion_decision": promotion_decision,
    }
    non_execution_ok = non_execution == {
        "model_called": False,
        "network_called": False,
        "state_mutating": False,
        "actual_route": "none",
        "automatic_route_change": False,
        "promotion_decision": "not_promoted",
    }
    accepted_case_total = len(manifest["cases"])
    accepted_case_passed = accepted_case_total - len(false_reject_case_ids)
    accepted_receipt_passed = accepted_receipt_total - len(false_reject_receipt_ids)
    conformant = (
        accepted_case_passed == accepted_case_total
        and accepted_receipt_passed == accepted_receipt_total
        and mutation_detected == mutation_total
        and not false_accept_ids
        and not primary_miss_ids
        and not validator_crash_ids
        and non_execution_ok
    )

    def category_report(names: Iterable[str]) -> dict[str, dict[str, int]]:
        return {
            name: _conformance_fraction(category_passed[name], category_totals[name])
            for name in names
        }

    report = {
        "schema_version": ROUTE_RECEIPT_CONFORMANCE_REPORT_SCHEMA_V1,
        "case_manifest_version": manifest["schema_version"],
        "case_manifest_id": manifest["manifest_id"],
        "case_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "accepted_cases": {
            "passed": accepted_case_passed,
            "total": accepted_case_total,
            "false_rejects": len(false_reject_case_ids),
            "false_reject_case_ids": sorted(false_reject_case_ids),
        },
        "accepted_receipts": {
            "passed": accepted_receipt_passed,
            "total": accepted_receipt_total,
            "false_rejects": len(false_reject_receipt_ids),
            "false_reject_receipt_ids": sorted(false_reject_receipt_ids),
        },
        "negative_mutations": {
            "detected": mutation_detected,
            "total": mutation_total,
            "false_accepts": len(false_accept_ids),
            "false_accept_mutation_ids": sorted(false_accept_ids),
            "primary_misses": len(primary_miss_ids),
            "primary_miss_mutation_ids": sorted(primary_miss_ids),
            "validator_crashes": len(validator_crash_ids),
            "validator_crash_mutation_ids": sorted(validator_crash_ids),
        },
        "attribution": category_report(ROUTE_RECEIPT_ATTRIBUTION_CATEGORIES),
        "authority": category_report(ROUTE_RECEIPT_AUTHORITY_CATEGORIES),
        "other_checks": category_report(ROUTE_RECEIPT_OTHER_CATEGORIES),
        "non_execution": non_execution,
        "conformant": conformant,
    }
    return report, []


def render_route_receipt_conformance_report(report: dict[str, Any]) -> str:
    status = "PASS" if report["conformant"] else "FAIL"
    cases = report["accepted_cases"]
    receipts = report["accepted_receipts"]
    mutations = report["negative_mutations"]
    lines = [
        f"{status} route_receipt_conformance manifest={report['case_manifest_id']}",
        f"accepted_cases={cases['passed']}/{cases['total']} false_rejects={cases['false_rejects']}",
        f"accepted_receipts={receipts['passed']}/{receipts['total']} false_rejects={receipts['false_rejects']}",
        (
            f"negative_mutations={mutations['detected']}/{mutations['total']} "
            f"false_accepts={mutations['false_accepts']} primary_misses={mutations['primary_misses']} "
            f"validator_crashes={mutations['validator_crashes']}"
        ),
    ]
    for group_name in ("attribution", "authority", "other_checks"):
        values = report[group_name]
        rendered = " ".join(
            f"{name}={counts['passed']}/{counts['total']}" for name, counts in values.items()
        )
        lines.append(f"{group_name} {rendered}")
    non_execution = report["non_execution"]
    lines.append(
        "non_execution "
        f"model_called={str(non_execution['model_called']).lower()} "
        f"network_called={str(non_execution['network_called']).lower()} "
        f"state_mutating={str(non_execution['state_mutating']).lower()} "
        f"actual_route={non_execution['actual_route']} "
        f"automatic_route_change={str(non_execution['automatic_route_change']).lower()} "
        f"promotion_decision={non_execution['promotion_decision']}"
    )
    return "\n".join(lines)


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_nonnegative_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def _is_finite_nonnegative_number(value: object) -> bool:
    return _is_nonnegative_number(value) and (
        not isinstance(value, float) or math.isfinite(value)
    )


def _check_exact_fields(
    value: dict[str, Any], expected: set[str], context: str, findings: list[Finding]
) -> None:
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown:
        findings.append(Finding("POLICY_UNKNOWN_FIELD", f"{context}: {', '.join(unknown)}"))
    if missing:
        findings.append(Finding("POLICY_MISSING_FIELD", f"{context}: {', '.join(missing)}"))


def _validate_threshold_values(
    values: dict[str, Any], context: str, findings: list[Finding], *, require_all: bool
) -> None:
    if require_all:
        _check_exact_fields(values, THRESHOLD_FIELDS, context, findings)
    else:
        unknown = sorted(set(values) - THRESHOLD_FIELDS)
        if unknown:
            findings.append(Finding("POLICY_UNKNOWN_FIELD", f"{context}: {', '.join(unknown)}"))

    for field, value in values.items():
        if field not in THRESHOLD_FIELDS:
            continue
        if field in {"min_shared_tasks", "min_deterministic_cases"}:
            if not _is_nonnegative_int(value) or value < 1:
                findings.append(Finding("POLICY_THRESHOLD", f"{context}.{field} must be an integer of at least 1"))
        elif field in {"min_completion_rate", "min_latency_advantage_fraction"}:
            if not _is_finite_nonnegative_number(value) or value > 1:
                findings.append(Finding("POLICY_THRESHOLD", f"{context}.{field} must be between 0 and 1"))
        elif field == "min_avg_human_score":
            if not _is_finite_nonnegative_number(value) or not 1 <= value <= 5:
                findings.append(Finding("POLICY_THRESHOLD", f"{context}.{field} must be between 1 and 5"))
        elif field == "max_quality_gap":
            if not _is_finite_nonnegative_number(value) or value > 4:
                findings.append(Finding("POLICY_THRESHOLD", f"{context}.{field} must be between 0 and 4"))
        elif not _is_finite_nonnegative_number(value):
            findings.append(Finding("POLICY_THRESHOLD", f"{context}.{field} must be non-negative"))


def validate_shadow_policy(policy: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    root_fields = {"schema_version", "policy_id", "routes", "defaults", "task_classes"}
    _check_exact_fields(policy, root_fields, "policy", findings)
    if policy.get("schema_version") != SHADOW_POLICY_SCHEMA:
        findings.append(Finding("POLICY_SCHEMA_VERSION", f"schema_version must be {SHADOW_POLICY_SCHEMA}"))
    if not isinstance(policy.get("policy_id"), str) or not policy.get("policy_id"):
        findings.append(Finding("POLICY_ID", "policy_id must be non-empty text"))

    routes = policy.get("routes")
    if not isinstance(routes, dict):
        findings.append(Finding("POLICY_ROUTES", "routes must be an object"))
    else:
        _check_exact_fields(routes, {"fast_small", "primary_quality"}, "routes", findings)
        bound_models: list[str] = []
        for route_name in ("fast_small", "primary_quality"):
            route = routes.get(route_name)
            if not isinstance(route, dict):
                findings.append(Finding("POLICY_ROUTE_BINDING", f"routes.{route_name} must be an object"))
                continue
            _check_exact_fields(route, {"model"}, f"routes.{route_name}", findings)
            model = route.get("model")
            if not isinstance(model, str) or not model:
                findings.append(
                    Finding("POLICY_ROUTE_BINDING", f"routes.{route_name}.model must be non-empty text")
                )
            else:
                bound_models.append(model)
        if len(bound_models) == 2 and bound_models[0] == bound_models[1]:
            findings.append(Finding("POLICY_ROUTE_BINDING", "fast_small and primary_quality must bind different models"))

    defaults = policy.get("defaults")
    if not isinstance(defaults, dict):
        findings.append(Finding("POLICY_DEFAULTS", "defaults must be an object"))
    else:
        _validate_threshold_values(defaults, "defaults", findings, require_all=True)

    task_classes = policy.get("task_classes")
    if not isinstance(task_classes, dict) or not task_classes:
        findings.append(Finding("POLICY_TASK_CLASSES", "task_classes must be a non-empty object"))
    else:
        for task_class, settings in task_classes.items():
            context = f"task_classes.{task_class}"
            if not isinstance(task_class, str) or not task_class:
                findings.append(Finding("POLICY_TASK_CLASS", "task class names must be non-empty text"))
                continue
            if not isinstance(settings, dict):
                findings.append(Finding("POLICY_TASK_CLASS", f"{context} must be an object"))
                continue
            unknown = sorted(set(settings) - THRESHOLD_FIELDS - {"deterministic_evidence"})
            if unknown:
                findings.append(Finding("POLICY_UNKNOWN_FIELD", f"{context}: {', '.join(unknown)}"))
            threshold_overrides = {key: value for key, value in settings.items() if key in THRESHOLD_FIELDS}
            _validate_threshold_values(threshold_overrides, context, findings, require_all=False)

            evidence = settings.get("deterministic_evidence")
            if evidence is None:
                continue
            if not isinstance(evidence, dict):
                findings.append(
                    Finding("POLICY_DETERMINISTIC_EVIDENCE", f"{context}.deterministic_evidence must be an object")
                )
                continue
            evidence_context = f"{context}.deterministic_evidence"
            _check_exact_fields(
                evidence,
                {"route_id", "case_count", "pass_count", "failure_count"},
                evidence_context,
                findings,
            )
            if not isinstance(evidence.get("route_id"), str) or not evidence.get("route_id"):
                findings.append(
                    Finding("POLICY_DETERMINISTIC_EVIDENCE", f"{evidence_context}.route_id must be non-empty text")
                )
            counts_valid = True
            for field in ("case_count", "pass_count", "failure_count"):
                if not _is_nonnegative_int(evidence.get(field)):
                    counts_valid = False
                    findings.append(
                        Finding("POLICY_DETERMINISTIC_EVIDENCE", f"{evidence_context}.{field} must be non-negative")
                    )
            if counts_valid:
                if evidence["case_count"] < 1:
                    findings.append(
                        Finding("POLICY_DETERMINISTIC_EVIDENCE", f"{evidence_context}.case_count must be at least 1")
                    )
                if evidence["pass_count"] + evidence["failure_count"] != evidence["case_count"]:
                    findings.append(
                        Finding(
                            "POLICY_DETERMINISTIC_EVIDENCE",
                            f"{evidence_context} pass_count plus failure_count must equal case_count",
                        )
                    )
    return findings


def validate_records(records: list[dict[str, Any]]) -> list[Finding]:
    findings: list[Finding] = []
    run_ids: set[str] = set()
    model_tasks: set[tuple[str, str]] = set()

    for record in records:
        line = record.get("_line", "?")
        public_fields = set(record) - {"_line"}
        unknown = sorted(public_fields - FIELDS)
        missing = sorted(FIELDS - public_fields)
        if unknown:
            findings.append(Finding("UNKNOWN_FIELD", f"line {line}: {', '.join(unknown)}"))
        if missing:
            findings.append(Finding("MISSING_FIELD", f"line {line}: {', '.join(missing)}"))
            continue
        if record.get("schema_version") != 1:
            findings.append(Finding("SCHEMA_VERSION", f"line {line}: schema_version must be 1"))

        for field in ("run_id", "task_id", "task_class", "model"):
            if not isinstance(record.get(field), str) or not record[field]:
                findings.append(Finding("STRING_FIELD", f"line {line}: {field} must be non-empty"))

        run_id = record.get("run_id")
        if isinstance(run_id, str):
            if run_id in run_ids:
                findings.append(Finding("DUPLICATE_RUN", f"line {line}: duplicate run_id {run_id}"))
            run_ids.add(run_id)

        model = record.get("model")
        task_id = record.get("task_id")
        if isinstance(model, str) and isinstance(task_id, str):
            pair = (model, task_id)
            if pair in model_tasks:
                findings.append(
                    Finding("DUPLICATE_MODEL_TASK", f"line {line}: duplicate model/task pair {model}/{task_id}")
                )
            model_tasks.add(pair)

        for field in ("turns", "input_tokens", "output_tokens", "cached_tokens", "revision_count"):
            if not _is_nonnegative_int(record.get(field)):
                findings.append(Finding("NONNEGATIVE_INTEGER", f"line {line}: {field} must be non-negative"))
        if record.get("turns") == 0:
            findings.append(Finding("ZERO_TURNS", f"line {line}: turns must be at least 1"))
        if not _is_finite_nonnegative_number(record.get("wall_seconds")):
            findings.append(Finding("NONNEGATIVE_NUMBER", f"line {line}: wall_seconds must be finite and non-negative"))

        status = record.get("status")
        bucket = record.get("failure_bucket")
        score = record.get("human_score")
        if status not in {"completed", "failed"}:
            findings.append(Finding("STATUS", f"line {line}: invalid status {status}"))
        if bucket not in FAILURE_BUCKETS:
            findings.append(Finding("FAILURE_BUCKET", f"line {line}: invalid failure_bucket {bucket}"))
        if status == "completed":
            if bucket != "none":
                findings.append(Finding("STATUS_BUCKET", f"line {line}: completed run must use failure_bucket none"))
            if not isinstance(score, (int, float)) or isinstance(score, bool) or not 1 <= score <= 5:
                findings.append(Finding("HUMAN_SCORE", f"line {line}: completed run needs score from 1 to 5"))
        elif status == "failed":
            if bucket == "none":
                findings.append(Finding("STATUS_BUCKET", f"line {line}: failed run needs a failure bucket"))
            if score is not None:
                findings.append(Finding("FAILED_SCORE", f"line {line}: failed run human_score must be null"))

    return findings


def _decimal(value: int | float | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _average(values: Iterable[int | float]) -> Decimal | None:
    materialized = [_decimal(value) for value in values]
    return sum(materialized, Decimal(0)) / Decimal(len(materialized)) if materialized else None


def _metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [record for record in records if record["status"] == "completed"]
    failures = Counter(record["failure_bucket"] for record in records if record["status"] == "failed")
    return {
        "attempts": len(records),
        "completed": len(completed),
        "completion_rate": Decimal(len(completed)) / Decimal(len(records)) if records else None,
        "avg_turns_per_attempt": _average(record["turns"] for record in records),
        "avg_uncached_tokens_per_attempt": _average(
            record["input_tokens"] + record["output_tokens"] for record in records
        ),
        "avg_cached_tokens_per_attempt": _average(record["cached_tokens"] for record in records),
        "avg_wall_seconds_per_attempt": _average(record["wall_seconds"] for record in records),
        "avg_human_score_completed": _average(record["human_score"] for record in completed),
        "avg_revisions_completed": _average(record["revision_count"] for record in completed),
        "failure_buckets": dict(sorted(failures.items())),
    }


def _present_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    presented = dict(metrics)
    for field, precision in METRIC_PRESENTATION_PRECISION.items():
        value = presented[field]
        if value is not None:
            presented[field] = float(round(value, precision))
    return presented


def _build_task_classes(records: list[dict[str, Any]], *, present_metrics: bool) -> list[dict[str, Any]]:
    by_class: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_class[record["task_class"]].append(record)

    classes: list[dict[str, Any]] = []
    for task_class in sorted(by_class):
        class_records = by_class[task_class]
        models = sorted({record["model"] for record in class_records})
        task_models: dict[str, set[str]] = defaultdict(set)
        for record in class_records:
            task_models[record["task_id"]].add(record["model"])
        shared_task_ids = sorted(task_id for task_id, seen in task_models.items() if seen == set(models))
        paired = [record for record in class_records if record["task_id"] in shared_task_ids]

        results = []
        for model in models:
            model_records = [record for record in paired if record["model"] == model]
            metrics = _metrics(model_records)
            if present_metrics:
                metrics = _present_metrics(metrics)
            results.append({"model": model, **metrics})

        classes.append(
            {
                "task_class": task_class,
                "models": models,
                "shared_task_count": len(shared_task_ids),
                "total_task_count": len(task_models),
                "shared_task_ids": shared_task_ids,
                "results": results,
            }
        )

    return classes


def build_report(records: list[dict[str, Any]]) -> dict[str, Any]:
    classes = _build_task_classes(records, present_metrics=True)

    return {
        "schema_version": 1,
        "comparison_rule": "Metrics use only task IDs attempted by every model within each task class.",
        "warning": "The report does not calculate a universal model winner.",
        "task_classes": classes,
    }


def _gate(name: str, passed: bool, observed: object, required: object, reason: str) -> dict[str, Any]:
    return {
        "name": name,
        "status": "pass" if passed else "fail",
        "observed": float(observed) if isinstance(observed, Decimal) else observed,
        "required": float(required) if isinstance(required, Decimal) else required,
        "reason": reason,
    }


def _meets_minimum(observed: int | float | Decimal | None, required: int | float) -> bool:
    return observed is not None and _decimal(observed) >= _decimal(required)


def _meets_maximum(observed: int | float | Decimal | None, required: int | float) -> bool:
    return observed is not None and _decimal(observed) <= _decimal(required)


def _gates_pass(gates: list[dict[str, Any]]) -> bool:
    return all(gate["status"] == "pass" for gate in gates)


def _failed_gate_codes(gates: list[dict[str, Any]]) -> list[str]:
    return [gate["name"].upper().replace(".", "_") for gate in gates if gate["status"] == "fail"]


def _thresholds_for_task(policy: dict[str, Any], task_class: str) -> dict[str, Any]:
    thresholds = dict(policy["defaults"])
    settings = policy["task_classes"].get(task_class, {})
    thresholds.update({key: value for key, value in settings.items() if key in THRESHOLD_FIELDS})
    return thresholds


def _model_route_gates(
    route_name: str,
    result: dict[str, Any] | None,
    shared_task_count: int,
    thresholds: dict[str, Any],
) -> list[dict[str, Any]]:
    prefix = route_name
    if result is None:
        return [
            _gate(
                f"{prefix}.model_evidence_present",
                False,
                False,
                True,
                "The bound model has no paired evidence for this task class.",
            )
        ]

    gates = [
        _gate(
            f"{prefix}.minimum_shared_tasks",
            shared_task_count >= thresholds["min_shared_tasks"],
            shared_task_count,
            thresholds["min_shared_tasks"],
            "Shared-task coverage must meet the declared minimum.",
        ),
        _gate(
            f"{prefix}.minimum_completion_rate",
            _meets_minimum(result["completion_rate"], thresholds["min_completion_rate"]),
            result["completion_rate"],
            thresholds["min_completion_rate"],
            "Completion rate is calculated only from paired task instances.",
        ),
    ]
    for bucket in sorted(CRITICAL_ROUTE_FAILURE_BUCKETS):
        count = result["failure_buckets"].get(bucket, 0)
        gates.append(
            _gate(
                f"{prefix}.zero_{bucket}_failures",
                count == 0,
                count,
                0,
                f"The {bucket} failure bucket must remain empty.",
            )
        )
    score = result["avg_human_score_completed"]
    gates.extend(
        [
            _gate(
                f"{prefix}.minimum_human_score",
                _meets_minimum(score, thresholds["min_avg_human_score"]),
                score,
                thresholds["min_avg_human_score"],
                "Completed paired runs must meet the declared average human score.",
            ),
            _gate(
                f"{prefix}.maximum_average_revisions",
                _meets_maximum(result["avg_revisions_completed"], thresholds["max_avg_revisions"]),
                result["avg_revisions_completed"],
                thresholds["max_avg_revisions"],
                "Completed paired runs must stay within the declared revision burden.",
            ),
            _gate(
                f"{prefix}.maximum_average_wall_seconds",
                _meets_maximum(result["avg_wall_seconds_per_attempt"], thresholds["max_avg_wall_seconds"]),
                result["avg_wall_seconds_per_attempt"],
                thresholds["max_avg_wall_seconds"],
                "Paired attempts must stay within the declared average latency ceiling.",
            ),
        ]
    )
    return gates


def _route_decision(
    *,
    task_class: str,
    candidate_route: str,
    candidate_model: str | None,
    decision_status: str,
    gates: list[dict[str, Any]],
    rejection_reasons: list[str],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    return {
        "task_class": task_class,
        "candidate_route": candidate_route,
        "candidate_model": candidate_model,
        "shadow_gate_status": "fail" if candidate_route == "hold" else "pass",
        "decision_status": decision_status,
        "promotion_decision": "not_promoted",
        "rejection_reasons": sorted(set(rejection_reasons)),
        "gates": gates,
        "evidence": evidence,
    }


def _present_route_decision(decision: dict[str, Any]) -> dict[str, Any]:
    presented = dict(decision)
    evidence = dict(decision["evidence"])
    for route_name in ("fast_small", "primary_quality"):
        metrics = evidence[route_name]
        if metrics is not None:
            evidence[route_name] = _present_metrics(metrics)
    presented["evidence"] = evidence
    return presented


def _build_task_class_route(
    task_class: str,
    comparison: dict[str, Any] | None,
    policy: dict[str, Any],
) -> dict[str, Any]:
    thresholds = _thresholds_for_task(policy, task_class)
    settings = policy["task_classes"].get(task_class, {})
    deterministic = settings.get("deterministic_evidence")
    shared_task_count = comparison["shared_task_count"] if comparison else 0
    by_model = {result["model"]: result for result in comparison["results"]} if comparison else {}
    fast_model = policy["routes"]["fast_small"]["model"]
    primary_model = policy["routes"]["primary_quality"]["model"]
    fast_result = by_model.get(fast_model)
    primary_result = by_model.get(primary_model)
    gates: list[dict[str, Any]] = []
    rejection_reasons: list[str] = []
    evidence = {
        "shared_task_count": shared_task_count,
        "deterministic": deterministic,
        "fast_small": fast_result,
        "primary_quality": primary_result,
    }

    if deterministic is not None:
        deterministic_gates = [
            _gate(
                "deterministic.minimum_case_count",
                deterministic["case_count"] >= thresholds["min_deterministic_cases"],
                deterministic["case_count"],
                thresholds["min_deterministic_cases"],
                "Deterministic evidence must meet the declared case minimum.",
            ),
            _gate(
                "deterministic.zero_failures",
                deterministic["failure_count"] == 0,
                deterministic["failure_count"],
                0,
                "Every declared deterministic fixture must pass.",
            ),
        ]
        gates.extend(deterministic_gates)
        if _gates_pass(deterministic_gates):
            return _route_decision(
                task_class=task_class,
                candidate_route="deterministic",
                candidate_model=None,
                decision_status="candidate_meets_declared_shadow_gates",
                gates=gates,
                rejection_reasons=[],
                evidence=evidence,
            )
        rejection_reasons.extend(_failed_gate_codes(deterministic_gates))

    # Runtime health is a prerequisite to quality evaluation. Stop here so an
    # infrastructure failure cannot be reported as a failed answer-quality gate.
    infrastructure_failures = sum(
        result["failure_buckets"].get("infrastructure", 0)
        for result in (fast_result, primary_result)
        if result is not None
    )
    infrastructure_gate = _gate(
        "comparison.zero_infrastructure_failures",
        infrastructure_failures == 0,
        infrastructure_failures,
        0,
        "Infrastructure failures make route quality not assessed rather than failed.",
    )
    gates.append(infrastructure_gate)
    if infrastructure_failures:
        rejection_reasons.append("NOT_ASSESSED_RUNTIME_FAILURE")
        return _route_decision(
            task_class=task_class,
            candidate_route="hold",
            candidate_model=None,
            decision_status="hold_runtime_incomplete",
            gates=gates,
            rejection_reasons=rejection_reasons,
            evidence=evidence,
        )

    if fast_result is None or primary_result is None:
        fast_gates = _model_route_gates("fast_small", fast_result, shared_task_count, thresholds)
        primary_gates = _model_route_gates("primary_quality", primary_result, shared_task_count, thresholds)
        gates.extend(fast_gates)
        gates.extend(primary_gates)
        rejection_reasons.extend(_failed_gate_codes(fast_gates + primary_gates))
        return _route_decision(
            task_class=task_class,
            candidate_route="hold",
            candidate_model=None,
            decision_status="hold_insufficient_evidence",
            gates=gates,
            rejection_reasons=rejection_reasons,
            evidence=evidence,
        )

    fast_gates = _model_route_gates("fast_small", fast_result, shared_task_count, thresholds)
    primary_gates = _model_route_gates("primary_quality", primary_result, shared_task_count, thresholds)
    gates.extend(fast_gates)
    gates.extend(primary_gates)

    fast_score = fast_result["avg_human_score_completed"]
    primary_score = primary_result["avg_human_score_completed"]
    quality_gap = primary_score - fast_score if fast_score is not None and primary_score is not None else None
    fast_wall = fast_result["avg_wall_seconds_per_attempt"]
    primary_wall = primary_result["avg_wall_seconds_per_attempt"]
    latency_advantage = (
        (primary_wall - fast_wall) / primary_wall
        if fast_wall is not None and primary_wall is not None and primary_wall > 0
        else None
    )
    relative_gates = [
        _gate(
            "fast_small.maximum_quality_gap",
            _meets_maximum(quality_gap, thresholds["max_quality_gap"]),
            quality_gap,
            thresholds["max_quality_gap"],
            "The fast route must stay within the declared human-score gap to the primary route.",
        ),
        _gate(
            "fast_small.minimum_latency_advantage",
            _meets_minimum(latency_advantage, thresholds["min_latency_advantage_fraction"]),
            latency_advantage,
            thresholds["min_latency_advantage_fraction"],
            "The fast route must provide the declared average latency advantage.",
        ),
    ]
    gates.extend(relative_gates)
    fast_eligible = _gates_pass(fast_gates + relative_gates)
    primary_eligible = _gates_pass(primary_gates)

    if fast_eligible:
        return _route_decision(
            task_class=task_class,
            candidate_route="fast_small",
            candidate_model=fast_model,
            decision_status="candidate_meets_declared_shadow_gates",
            gates=gates,
            rejection_reasons=rejection_reasons,
            evidence=evidence,
        )
    rejection_reasons.extend(_failed_gate_codes(fast_gates + relative_gates))
    if primary_eligible:
        return _route_decision(
            task_class=task_class,
            candidate_route="primary_quality",
            candidate_model=primary_model,
            decision_status="candidate_meets_declared_shadow_gates",
            gates=gates,
            rejection_reasons=rejection_reasons,
            evidence=evidence,
        )
    rejection_reasons.extend(_failed_gate_codes(primary_gates))
    return _route_decision(
        task_class=task_class,
        candidate_route="hold",
        candidate_model=None,
        decision_status="hold_gate_failure",
        gates=gates,
        rejection_reasons=rejection_reasons,
        evidence=evidence,
    )


def build_shadow_route_report(records: list[dict[str, Any]], policy: dict[str, Any]) -> dict[str, Any]:
    route_models = {
        policy["routes"]["fast_small"]["model"],
        policy["routes"]["primary_quality"]["model"],
    }
    comparison_classes = _build_task_classes(
        [record for record in records if record["model"] in route_models],
        present_metrics=False,
    )
    comparisons = {item["task_class"]: item for item in comparison_classes}
    task_classes = sorted(set(comparisons) | set(policy["task_classes"]))
    route_decisions = [
        _build_task_class_route(task_class, comparisons.get(task_class), policy) for task_class in task_classes
    ]
    return {
        "schema_version": SHADOW_REPORT_SCHEMA,
        "status": "pass",
        "report_mode": "shadow_only",
        "model_called": False,
        "network_called": False,
        "state_mutating": False,
        "actual_route": "none",
        "automatic_route_change": False,
        "promotion_decision": "not_promoted",
        "policy_id": policy["policy_id"],
        "input_record_count": len(records),
        "comparison_rule": "Only shared task IDs contribute to model-route decisions.",
        "warning": "A shadow candidate is not an executed route or a promotion decision.",
        "task_classes": [_present_route_decision(decision) for decision in route_decisions],
        "external_requirements": [
            {"name": "live_model_quality", "status": "not_assessed"},
            {"name": "semantic_truth", "status": "not_assessed"},
            {"name": "action_authority", "status": "not_assessed"},
            {"name": "protected_path_proof", "status": "not_assessed"},
            {"name": "statistical_significance", "status": "not_assessed"},
            {"name": "real_monetary_cost", "status": "not_assessed"},
        ],
    }


def render_shadow_route_report(report: dict[str, Any]) -> str:
    lines = [
        report["comparison_rule"],
        report["warning"],
        "model_called=false network_called=false state_mutating=false "
        "actual_route=none automatic_route_change=false promotion_decision=not_promoted",
    ]
    for item in report["task_classes"]:
        reasons = ",".join(item["rejection_reasons"]) or "none"
        lines.append(
            f"TASK_CLASS {item['task_class']} candidate={item['candidate_route']} "
            f"model={_display(item['candidate_model'])} status={item['decision_status']} "
            f"rejections={reasons}"
        )
    return "\n".join(lines)


def _display(value: object) -> str:
    return "-" if value is None else str(value)


def render_report(report: dict[str, Any]) -> str:
    lines = [report["comparison_rule"], report["warning"]]
    for task_class in report["task_classes"]:
        lines.append("")
        lines.append(
            f"TASK_CLASS {task_class['task_class']} shared={task_class['shared_task_count']}/{task_class['total_task_count']}"
        )
        for result in task_class["results"]:
            failures = ",".join(f"{key}:{value}" for key, value in result["failure_buckets"].items()) or "none"
            lines.append(
                "MODEL "
                f"{result['model']} attempts={result['attempts']} "
                f"completion={_display(result['completion_rate'])} "
                f"turns={_display(result['avg_turns_per_attempt'])} "
                f"uncached_tokens={_display(result['avg_uncached_tokens_per_attempt'])} "
                f"cached_tokens={_display(result['avg_cached_tokens_per_attempt'])} "
                f"wall_seconds={_display(result['avg_wall_seconds_per_attempt'])} "
                f"human_score={_display(result['avg_human_score_completed'])} "
                f"revisions={_display(result['avg_revisions_completed'])} "
                f"failures={failures}"
            )
    return "\n".join(lines)


SELF_TEST_CASES = {
    "runs.jsonl": (True, None),
    "bad_status_bucket.jsonl": (False, "STATUS_BUCKET"),
    "bad_duplicate_run.jsonl": (False, "DUPLICATE_RUN"),
    "bad_negative_count.jsonl": (False, "NONNEGATIVE_INTEGER"),
}
SHADOW_POLICY_SELF_TEST_CASES = {
    "shadow_route_policy.json": (True, None),
    "bad_shadow_route_policy.json": (False, "POLICY_ROUTE_BINDING"),
}
ROUTE_RECEIPT_SELF_TEST_CASES = (
    "route_receipt_passive.json",
    "route_receipt_enforced.json",
)
def check_path(path: Path) -> tuple[list[dict[str, Any]], list[Finding]]:
    records, load_findings = load_records(path)
    return records, load_findings + validate_records(records)


def run_self_test(root: Path) -> int:
    failures = 0
    for name, (should_pass, expected_code) in SELF_TEST_CASES.items():
        _, findings = check_path(root / "examples" / name)
        codes = {finding.code for finding in findings}
        passed = not findings
        correct = passed == should_pass and (expected_code is None or expected_code in codes)
        print(f"{'PASS' if correct else 'FAIL'} fixture {name}")
        failures += not correct

    records, findings = check_path(root / "examples" / "runs.jsonl")
    report = build_report(records) if not findings else {"task_classes": []}
    integration = next(
        (item for item in report["task_classes"] if item["task_class"] == "integration"),
        None,
    )
    report_ok = integration is not None and integration["shared_task_count"] == 2
    print(f"{'PASS' if report_ok else 'FAIL'} paired_workload_report")
    failures += not report_ok

    good_policy: dict[str, Any] | None = None
    for name, (should_pass, expected_code) in SHADOW_POLICY_SELF_TEST_CASES.items():
        policy, policy_findings = load_shadow_policy(root / "examples" / name)
        codes = {finding.code for finding in policy_findings}
        passed = not policy_findings
        correct = passed == should_pass and (expected_code is None or expected_code in codes)
        print(f"{'PASS' if correct else 'FAIL'} fixture {name}")
        failures += not correct
        if name == "shadow_route_policy.json" and not policy_findings:
            good_policy = policy

    shadow_report = build_shadow_route_report(records, good_policy) if report_ok and good_policy else None
    routes = (
        {item["task_class"]: item["candidate_route"] for item in shadow_report["task_classes"]}
        if shadow_report
        else {}
    )
    expected_routes = {
        "lookup": "deterministic",
        "maintenance": "fast_small",
        "integration": "primary_quality",
        "research": "hold",
    }
    routes_ok = routes == expected_routes
    print(f"{'PASS' if routes_ok else 'FAIL'} shadow_route_candidates")
    failures += not routes_ok

    invariants_ok = bool(shadow_report) and all(
        (
            shadow_report["model_called"] is False,
            shadow_report["network_called"] is False,
            shadow_report["state_mutating"] is False,
            shadow_report["actual_route"] == "none",
            shadow_report["automatic_route_change"] is False,
            shadow_report["promotion_decision"] == "not_promoted",
        )
    )
    print(f"{'PASS' if invariants_ok else 'FAIL'} shadow_route_non_execution")
    failures += not invariants_ok

    ground_truth, ground_truth_findings = load_attempt_ground_truth(
        root / "examples" / "route_receipt_attempt_ground_truth.json"
    )
    ground_truth_ok = ground_truth is not None and not ground_truth_findings
    print(f"{'PASS' if ground_truth_ok else 'FAIL'} fixture route_receipt_attempt_ground_truth.json")
    failures += not ground_truth_ok
    for name in ROUTE_RECEIPT_SELF_TEST_CASES:
        receipt, receipt_findings = load_route_receipt(root / "examples" / name)
        if receipt is not None and ground_truth is not None:
            receipt_findings.extend(validate_route_receipt(receipt, ground_truth))
        receipt_ok = receipt is not None and not receipt_findings
        print(f"{'PASS' if receipt_ok else 'FAIL'} fixture {name}")
        failures += not receipt_ok

    phase2_manifest_path = root / "examples" / "phase2" / "route_receipt_case_manifest_v1.json"
    phase2_report, phase2_findings = build_route_receipt_conformance_report(phase2_manifest_path)
    phase2_ok = (
        phase2_report is not None
        and not phase2_findings
        and phase2_report["conformant"] is True
        and phase2_report["accepted_cases"]["passed"] == 10
        and phase2_report["accepted_receipts"]["passed"] == 12
        and phase2_report["negative_mutations"]["detected"] == 24
    )
    print(f"{'PASS' if phase2_ok else 'FAIL'} phase2_route_receipt_conformance")
    failures += not phase2_ok

    if failures:
        print(f"FAIL self_test {failures} expectations failed")
        return 1
    print("PASS self_test")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", help="run bundled validation and report fixtures")
    subparsers = parser.add_subparsers(dest="command")
    validate_parser = subparsers.add_parser("validate", help="validate JSONL telemetry records")
    validate_parser.add_argument("path", type=Path)
    report_parser = subparsers.add_parser("report", help="render a paired workload report")
    report_parser.add_argument("path", type=Path)
    report_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    shadow_parser = subparsers.add_parser(
        "shadow-route", help="apply declared evidence gates without executing or promoting a route"
    )
    shadow_parser.add_argument("path", type=Path, help="validated JSONL telemetry records")
    shadow_parser.add_argument("policy", type=Path, help="shadow routing policy JSON")
    shadow_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    receipt_parser = subparsers.add_parser(
        "validate-route-receipt",
        help="validate a synthetic route receipt against independent attempt ground truth",
    )
    receipt_parser.add_argument("receipt", type=Path, help="synthetic route_receipt_v0 or route_receipt_v1 JSON")
    receipt_parser.add_argument("ground_truth", type=Path, help="independent synthetic attempt ground truth JSON")
    conformance_parser = subparsers.add_parser(
        "route-receipt-conformance",
        help="run a declared synthetic route-receipt conformance manifest",
    )
    conformance_parser.add_argument("manifest", type=Path, help="digest-bound route-receipt case manifest")
    conformance_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    if args.self_test:
        return run_self_test(root)
    if args.command is None:
        parser.error(
            "choose validate, report, shadow-route, validate-route-receipt, "
            "route-receipt-conformance, or --self-test"
        )

    if args.command == "validate-route-receipt":
        ground_truth, findings = load_attempt_ground_truth(args.ground_truth.resolve())
        receipt, receipt_findings = load_route_receipt(args.receipt.resolve())
        findings.extend(receipt_findings)
        if receipt is not None and ground_truth is not None:
            findings.extend(validate_route_receipt(receipt, ground_truth))
        if findings:
            for finding in findings:
                print(f"FAIL {finding.code} {finding.message}")
            return 1
        assert receipt is not None
        print(
            f"PASS route_receipt {receipt['receipt_id']} "
            f"mode={receipt['receipt_mode']} claim={receipt['completion_claim']}"
        )
        return 0

    if args.command == "route-receipt-conformance":
        conformance_report, conformance_findings = build_route_receipt_conformance_report(
            args.manifest.resolve()
        )
        if conformance_findings:
            for finding in conformance_findings:
                print(f"FAIL {finding.code} {finding.message}")
            return 1
        assert conformance_report is not None
        if args.json:
            print(json.dumps(conformance_report, indent=2, sort_keys=True, allow_nan=False))
        else:
            print(render_route_receipt_conformance_report(conformance_report))
        return 0 if conformance_report["conformant"] else 1

    records, findings = check_path(args.path.resolve())
    if findings:
        for finding in findings:
            print(f"FAIL {finding.code} {finding.message}")
        return 1
    if args.command == "validate":
        print(f"PASS telemetry_records {len(records)}")
        return 0

    if args.command == "shadow-route":
        policy, policy_findings = load_shadow_policy(args.policy.resolve())
        if policy_findings:
            for finding in policy_findings:
                print(f"FAIL {finding.code} {finding.message}")
            return 1
        assert policy is not None
        shadow_report = build_shadow_route_report(records, policy)
        if args.json:
            print(json.dumps(shadow_report, indent=2, sort_keys=True))
        else:
            print(render_shadow_route_report(shadow_report))
        return 0

    report = build_report(records)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
