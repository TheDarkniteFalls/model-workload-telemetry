#!/usr/bin/env python3
"""Compare model runs by shared workload instead of raw token totals."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any, Iterable


MAX_JSONL_BYTES = 5_000_000
MAX_POLICY_BYTES = 100_000
SHADOW_POLICY_SCHEMA = "shadow_route_policy_v0"
SHADOW_REPORT_SCHEMA = "evidence_gated_shadow_route_report_v0"
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
        if not _is_nonnegative_number(record.get("wall_seconds")):
            findings.append(Finding("NONNEGATIVE_NUMBER", f"line {line}: wall_seconds must be non-negative"))

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


def _rounded_average(values: Iterable[float]) -> float | None:
    materialized = list(values)
    return round(mean(materialized), 2) if materialized else None


def _metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [record for record in records if record["status"] == "completed"]
    failures = Counter(record["failure_bucket"] for record in records if record["status"] == "failed")
    return {
        "attempts": len(records),
        "completed": len(completed),
        "completion_rate": round(len(completed) / len(records), 3) if records else None,
        "avg_turns_per_attempt": _rounded_average(record["turns"] for record in records),
        "avg_uncached_tokens_per_attempt": _rounded_average(
            record["input_tokens"] + record["output_tokens"] for record in records
        ),
        "avg_cached_tokens_per_attempt": _rounded_average(record["cached_tokens"] for record in records),
        "avg_wall_seconds_per_attempt": _rounded_average(record["wall_seconds"] for record in records),
        "avg_human_score_completed": _rounded_average(record["human_score"] for record in completed),
        "avg_revisions_completed": _rounded_average(record["revision_count"] for record in completed),
        "failure_buckets": dict(sorted(failures.items())),
    }


def build_report(records: list[dict[str, Any]]) -> dict[str, Any]:
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
            results.append({"model": model, **_metrics(model_records)})

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
        "observed": observed,
        "required": required,
        "reason": reason,
    }


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
            result["completion_rate"] is not None
            and result["completion_rate"] >= thresholds["min_completion_rate"],
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
                score is not None and score >= thresholds["min_avg_human_score"],
                score,
                thresholds["min_avg_human_score"],
                "Completed paired runs must meet the declared average human score.",
            ),
            _gate(
                f"{prefix}.maximum_average_revisions",
                result["avg_revisions_completed"] is not None
                and result["avg_revisions_completed"] <= thresholds["max_avg_revisions"],
                result["avg_revisions_completed"],
                thresholds["max_avg_revisions"],
                "Completed paired runs must stay within the declared revision burden.",
            ),
            _gate(
                f"{prefix}.maximum_average_wall_seconds",
                result["avg_wall_seconds_per_attempt"] is not None
                and result["avg_wall_seconds_per_attempt"] <= thresholds["max_avg_wall_seconds"],
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
        round((primary_wall - fast_wall) / primary_wall, 3)
        if fast_wall is not None and primary_wall is not None and primary_wall > 0
        else None
    )
    relative_gates = [
        _gate(
            "fast_small.maximum_quality_gap",
            quality_gap is not None and quality_gap <= thresholds["max_quality_gap"],
            quality_gap,
            thresholds["max_quality_gap"],
            "The fast route must stay within the declared human-score gap to the primary route.",
        ),
        _gate(
            "fast_small.minimum_latency_advantage",
            latency_advantage is not None
            and latency_advantage >= thresholds["min_latency_advantage_fraction"],
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
    comparison_report = build_report([record for record in records if record["model"] in route_models])
    comparisons = {item["task_class"]: item for item in comparison_report["task_classes"]}
    task_classes = sorted(set(comparisons) | set(policy["task_classes"]))
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
        "task_classes": [
            _build_task_class_route(task_class, comparisons.get(task_class), policy) for task_class in task_classes
        ],
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
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    if args.self_test:
        return run_self_test(root)
    if args.command is None:
        parser.error("choose validate, report, shadow-route, or --self-test")

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
