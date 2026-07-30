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

## Lessons Demonstrated

This repository turns a few practical routing lessons into inspectable,
synthetic examples:

- Compare models only on task IDs they both attempted within the same workload
  class. Raw totals mostly measure workload mix.
- Apply route gates to exact, unrounded measurements. Round values only when
  presenting the report.
- Treat `hold` as an explicit outcome when evidence is missing, runtime work is
  incomplete, or a declared boundary blocks delivery.
- Keep runtime, schema, source-boundary, safety, and answer-quality outcomes
  separate. A failed request is not evidence of a poor answer.
- Attribute fallback to the complete ordered attempt chain and to the model
  that actually produced the final response.
- Check receipts against independently authored attempt ground truth so the
  receipt does not supply its own expected result.
- Separate observation from authority: passive receipts describe what was
  observed, while enforced receipts need complete evidence before claiming an
  auditable delivery or hold.

These examples demonstrate reviewable evidence patterns. They do not establish
that this harness is more reliable than a simple task-class router or that a
route improves answer quality.

## Run

Requires Python 3.10 or newer.

```sh
python3 -B model_workload_telemetry.py validate examples/runs.jsonl
python3 -B model_workload_telemetry.py report examples/runs.jsonl
python3 -B model_workload_telemetry.py report examples/runs.jsonl --json
python3 -B model_workload_telemetry.py shadow-route examples/runs.jsonl examples/shadow_route_policy.json --json
python3 -B model_workload_telemetry.py validate-route-receipt examples/route_receipt_passive.json examples/route_receipt_attempt_ground_truth.json
python3 -B model_workload_telemetry.py validate-route-receipt examples/route_receipt_enforced.json examples/route_receipt_attempt_ground_truth.json
python3 -B model_workload_telemetry.py validate-route-receipt examples/phase2/p2_d01_enforced.json examples/phase2/p2_d01_ground_truth.json
python3 -B model_workload_telemetry.py validate-route-receipt examples/phase2/p2_i03_enforced.json examples/phase2/p2_i03_ground_truth.json
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

## Evidence-Gated Shadow Routing

The `shadow-route` report turns paired workload measurements into an
inspectable candidate route without calling a model, using the network,
changing defaults, or executing the route.

It accepts the existing JSONL run records unchanged plus a separate policy
file that binds two model roles and declares thresholds per task class:

- `deterministic`: a declared non-model path whose synthetic fixture summary
  has enough cases and zero failures;
- `fast_small`: the smaller model passes the hard gates, stays within the
  allowed human-score gap, and provides the declared latency advantage;
- `primary_quality`: the primary model passes while the fast model is blocked
  by quality, boundary, completion, revision, or relative-comparison evidence;
- `hold`: evidence is missing, insufficient, runtime-incomplete, or unsafe.

The bundled policy uses two shared tasks so the example stays small. That is a
demonstration threshold, not statistical or production guidance.

Every successful report states:

```json
{
  "report_mode": "shadow_only",
  "model_called": false,
  "network_called": false,
  "state_mutating": false,
  "actual_route": "none",
  "automatic_route_change": false,
  "promotion_decision": "not_promoted"
}
```

The report gates shared-task coverage, completion, schema and source-boundary
failures, answer quality, human revision burden, average latency, quality gap,
and fast-route latency advantage. Thresholds use unrounded values; metric
summaries are rounded only after route decisions are made. Infrastructure
failures produce `hold_runtime_incomplete`; they are not relabeled as
answer-quality failures.

Action authority, protected-path proof, semantic truth, live-model quality,
statistical significance, and real monetary cost remain explicitly
`not_assessed`. A shadow candidate is evidence for human review, not authority
to change a route.

This complements the [Local Model Reliability Example](https://github.com/TheDarkniteFalls/local-model-reliability-example),
which validates one proposed model output before an application trusts it.
Here, the unit of evidence is a set of paired run records used to assess a
candidate workload route. The [Local Assistant Reliability Lab](https://github.com/TheDarkniteFalls/local-assistant-reliability-lab)
remains the navigator and integrated overview; this repo supplies the focused
measurement and shadow-decision example rather than duplicating its catalog or
workflow.

## Synthetic Route Receipts

Phase 1 adds a strict, dependency-free `route_receipt_v0` validation contract.
It reconciles a synthetic receipt against a separate attempt-ground-truth
fixture so a receipt cannot verify itself. The formal schema is
[`schemas/route_receipt_v0.schema.json`](schemas/route_receipt_v0.schema.json).

The examples describe the same synthetic fallback sequence in two modes:

- `passive` records what happened and may claim only `observed_only`;
- `enforced` may claim `auditable_complete` only when attempt attribution,
  fallback policy, quality separation, expected writes, and receipt
  finalization all pass.

The contract rejects missing attempts, wrong final-model attribution,
unassessed runtime failures entering quality evidence, forbidden fallback,
receipt-finalization failure, and unexpected writes. Both examples keep
`execution_mode=synthetic_replay`, `model_called=false`,
`network_called=false`, `state_mutating=false`, `actual_route=none`, and
`promotion_decision=not_promoted`.

This is a proposal-compatible validation surface, not a router. It does not
expand the workload set, call or select a live model, retry a request, change a
default, persist a receipt, or promote a route.

The published v1 contracts are the
[`route_receipt_v1` schema](schemas/route_receipt_v1.schema.json) and the
independent
[`route_attempt_ground_truth_v1` schema](schemas/route_attempt_ground_truth_v1.schema.json).
They preserve exact version identity and formalize deliver-versus-hold,
per-attempt boundary evidence, and ordered fallback attribution without
changing the frozen v0 contract.

The smallest Phase 2 positive conformance slice is now runnable. Exact version
dispatch keeps v0 inputs on the unchanged v0 validator and reconciles v1
receipts only against v1 attempt ground truth. The synthetic matrix contains:

| Case | Positive path | Enforced claim | Passive companion |
| --- | --- | --- | --- |
| `P2-D01` | direct deterministic delivery | `auditable_complete` | yes |
| `P2-M02` | permitted runtime fallback delivery | `auditable_complete` | no |
| `P2-I03` | runtime fallback exhaustion hold | `auditable_hold` | yes |
| `P2-Q01` | completed response with assessed-fail quality | `auditable_hold` | no |
| `P2-R01` | source-boundary validation hold | `auditable_hold` | no |
| `P2-S01` | safety-policy hold before a response | `auditable_hold` | no |

The passive companions use the same attempt evidence while remaining limited
to `observed_only`; changing receipt mode does not upgrade authority. Every
case keeps model, network, mutation, actual-route, automatic-route-change, and
promotion indicators disabled.

This slice does not include negative mutation coverage, aggregate reporting,
workload expansion, comparative reliability evidence, or live routing. The
docs-only
[Phase 2 proposal](docs/route_receipt_phase2_proposal.md) describes that
larger possible expansion. Nothing in the current repository represents
production traffic or establishes production readiness.

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
