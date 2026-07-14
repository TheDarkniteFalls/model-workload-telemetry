#!/usr/bin/env python3
"""Compare model runs by shared workload instead of raw token totals."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any, Iterable


MAX_JSONL_BYTES = 5_000_000
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


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_nonnegative_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


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
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    if args.self_test:
        return run_self_test(root)
    if args.command is None:
        parser.error("choose validate, report, or --self-test")

    records, findings = check_path(args.path.resolve())
    if findings:
        for finding in findings:
            print(f"FAIL {finding.code} {finding.message}")
        return 1
    if args.command == "validate":
        print(f"PASS telemetry_records {len(records)}")
        return 0

    report = build_report(records)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
