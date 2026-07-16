# Model Workload Telemetry

<!-- toolkit-trust-card:start -->
> **Public contract:** Experimental tool · about 10 min · Python 3 · no model · no network
>
> **Operation:** Read-only check; examples may use temporary files
>
> **A pass establishes:** Only task instances attempted by every compared model contribute to each workload report.
>
> **It does not establish:** The report does not prove causal model superiority, cost efficiency, or statistical significance.
>
> **First check:** `python3 -B model_workload_telemetry.py --self-test`
<!-- toolkit-trust-card:end -->

A dependency-free CLI for comparing model runs by shared task class instead of
raw token totals.

It validates synthetic JSONL run records, separates failure buckets, and
reports completion, turns, uncached tokens, cached tokens, wall time, human
score, and revision burden only across task IDs attempted by every compared
model in that workload class.

It deliberately does not calculate a universal model winner.

## Why It Exists

Raw totals are usually workload totals in disguise. One model may have handled
many short maintenance tasks while another handled a few long integration
tasks. Comparing their aggregate tokens says little about capability or value.

A more useful comparison asks:

- Did the models attempt the same task instances?
- What kind of task was each one doing?
- How often did each route finish?
- Were failures caused by infrastructure, schema, source boundaries, answer
  quality, or human revision burden?
- How much time, context, and revision did a successful result require?

## Run

Requires Python 3.10 or newer.

```sh
python3 -B model_workload_telemetry.py validate examples/runs.jsonl
python3 -B model_workload_telemetry.py report examples/runs.jsonl
python3 -B model_workload_telemetry.py report examples/runs.jsonl --json
python3 -B model_workload_telemetry.py --self-test
python3 -B -m unittest discover -s tests -v
```

The bundled dataset contains two fictional models attempting the same synthetic
maintenance, integration, and research tasks. It is designed to show different
workload strengths, not to imitate or rank real products.

## Record Contract

Each JSONL record includes:

- unique run, task, workload-class, and model identifiers;
- turn, input-token, output-token, cached-token, and wall-time measurements;
- `completed` or `failed` status;
- a failure bucket for infrastructure, schema, source boundary, answer quality,
  or human revision;
- a human score from 1 to 5 for completed runs; and
- the number of human revision rounds.

One model may have only one record for a task ID. If repeated trials are needed,
give each trial a distinct task ID shared by every compared model.

## Comparison Rule

Within each task class, the report first finds task IDs attempted by every
model. All reported model metrics for that class use only those shared tasks.
This prevents missing or selectively assigned tasks from silently improving a
model's apparent result.

## What A Report Does Not Prove

The report does not prove model quality, causal superiority, cost efficiency,
or statistical significance. Token accounting differs between providers,
cached tokens may have different meanings, human scores are subjective, and
task IDs are comparable only if the operator designed them that way.

Use this as an inspectable experiment ledger, not a benchmark leaderboard.

## Public Data Notice

The bundled telemetry is synthetic. Do not commit real prompts, responses,
private project names, user identifiers, credentials, provider exports, or raw
production logs.
