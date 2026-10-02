# Model Workload Telemetry

Compare model runs on tasks they both attempted, instead of comparing their
raw token totals. This Python command-line tool reads saved run records and
reports completion, turns, tokens, time, human scores and revision rounds for
shared tasks in the same workload class—a group such as maintenance or research.

Start with the fictional dataset below. You can inspect the comparison, try a
policy that suggests a route, and check the records behind that suggestion.
The tool does not call models or use the network, and it does not calculate a
universal model winner.

## Why It Exists

One model may have handled many short maintenance tasks while another handled
a few long integration tasks. Their total token use reflects those different
assignments. Start by checking which tasks they have in common before drawing
a comparison.

A more useful comparison asks:

- Did the models attempt the same task instances?
- What kind of task was each one doing?
- How often did each route finish?
- Were failures caused by infrastructure, schema, source boundaries, answer
  quality, or human revision burden?
- How much time, context, and revision did a successful result require?

## Run

From this repository’s folder, use Python 3.10 or newer. Start with `validate`
and `report` in the block below: validation checks the records, and the report
shows results for shared tasks. The later commands explore routing proposals
and receipt checks; `--self-test` and the unit tests check the bundled examples.

```sh
python3 -B model_workload_telemetry.py validate examples/runs.jsonl
python3 -B model_workload_telemetry.py report examples/runs.jsonl
python3 -B model_workload_telemetry.py report examples/runs.jsonl --json
python3 -B model_workload_telemetry.py shadow-route examples/runs.jsonl examples/shadow_route_policy.json --json
python3 -B model_workload_telemetry.py validate-route-receipt examples/route_receipt_passive.json examples/route_receipt_attempt_ground_truth.json
python3 -B model_workload_telemetry.py validate-route-receipt examples/route_receipt_enforced.json examples/route_receipt_attempt_ground_truth.json
python3 -B model_workload_telemetry.py validate-route-receipt examples/phase2/p2_d01_enforced.json examples/phase2/p2_d01_ground_truth.json
python3 -B model_workload_telemetry.py validate-route-receipt examples/phase2/p2_i03_enforced.json examples/phase2/p2_i03_ground_truth.json
python3 -B model_workload_telemetry.py route-receipt-conformance examples/phase2/route_receipt_case_manifest_v1.json
python3 -B model_workload_telemetry.py route-receipt-conformance examples/phase2/route_receipt_case_manifest_v1.json --json
python3 -B model_workload_telemetry.py decision-receipt-provenance examples/phase3_decision_receipt_provenance_manifest_v1.json
python3 -B model_workload_telemetry.py decision-receipt-provenance examples/phase3_decision_receipt_provenance_manifest_v1.json --json
python3 -B model_workload_telemetry.py --self-test
python3 -B -m unittest discover -s tests -v
```

The bundled dataset contains two fictional models attempting the same synthetic
maintenance, integration, and research tasks. It is designed to show different
workload strengths, not to imitate or rank real products. A successful check
means these records or synthetic cases met the declared rules; it does not
show that a live route is safe or better.

<!-- toolkit-trust-card:placement -->

<!-- toolkit-trust-card:start -->
> **Public contract:** Experimental tool · about 10 min · Python 3 · no model · no network
>
> **Operation:** Read-only check; examples may use temporary files
>
> **A pass establishes:** The declared synthetic shared-task, receipt, and Phase 3 provenance checks pass.
>
> **It does not establish:** The checks do not prove live-routing safety, production reliability, causal model superiority, cost efficiency, or statistical significance.
>
> **First check:** `python3 -B model_workload_telemetry.py --self-test`
<!-- toolkit-trust-card:end -->

## Lessons Demonstrated

Use the synthetic examples to practise these checks:

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

## Record Contract

JSONL is a text file with one JSON record per line. Each record includes:

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

A shadow route is a suggestion you can inspect without running it. The
`shadow-route` report applies a policy to paired workload measurements. It
calls no model, uses no network, changes no defaults and executes no route.

It accepts the existing JSONL run records unchanged plus a separate policy
file that names two model roles and sets thresholds for each task class:

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
Use the paired records here to inspect a candidate workload route. Visit the
[Local Assistant Reliability Lab](https://github.com/TheDarkniteFalls/local-assistant-reliability-lab)
for an overview of the related tools and examples.

## Synthetic Route Receipts

A route receipt records the attempts and outcome of a proposed route. Phase 1
provides the strict, dependency-free `route_receipt_v0` format. Its validator
compares the synthetic receipt with a separate record of expected attempts
(the attempt-ground-truth fixture), so the receipt cannot supply its own
expected answer. The formal schema is
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

Phase 2 synthetic conformance is now runnable. Exact version dispatch keeps v0
inputs on the unchanged v0 validator and reconciles v1 receipts only against
v1 attempt ground truth. The accepted matrix contains:

| Case | Positive path | Enforced claim | Passive companion |
| --- | --- | --- | --- |
| `P2-D01` | direct deterministic delivery | `auditable_complete` | yes |
| `P2-M01` | direct fast-route delivery | `auditable_complete` | no |
| `P2-M02` | pre-request failure, then permitted fallback delivery | `auditable_complete` | no |
| `P2-I01` | request-open failure, then permitted fallback delivery | `auditable_complete` | no |
| `P2-I02` | response-stream failure, then permitted fallback delivery | `auditable_complete` | no |
| `P2-I03` | runtime fallback exhaustion hold | `auditable_hold` | yes |
| `P2-Q01` | completed response with assessed-fail quality | `auditable_hold` | no |
| `P2-R01` | source-boundary validation hold | `auditable_hold` | no |
| `P2-R02` | required source unavailable before acceptance | `auditable_hold` | no |
| `P2-S01` | safety-policy hold before a response | `auditable_hold` | no |

The passive companions use the same attempt evidence while remaining limited
to `observed_only`; changing receipt mode does not upgrade authority. Every
case keeps model, network, state-mutation, actual-route,
automatic-route-change, and promotion indicators disabled.

The strict
[`route_receipt_case_manifest_v1` schema](schemas/route_receipt_case_manifest_v1.schema.json)
defines the denominator, and the
[`Phase 2 manifest`](examples/phase2/route_receipt_case_manifest_v1.json)
binds each truth and receipt to the SHA-256 digest of its exact bytes. The
report does not discover extra files at runtime.

For the published manifest, the deterministic report validates `10/10`
accepted cases and `12/12` accepted receipts and detects `24/24` declared
in-memory mutations with their primary finding codes. `P2-S02` and `P2-F01`
are represented as unexpected-write and finalization mutations rather than
negative fixture files. `P2-A01` is covered across the paired passive and
enforced delivery and hold receipts. The
[Phase 2 design and acceptance record](docs/route_receipt_phase2_proposal.md)
documents the full boundary.

These exact counts describe only the declared synthetic corpus. They are not a
reliability estimate, do not generalize statistically, and do not establish
production readiness, better routing, or better model quality.

Phase 3 checks provenance: which exact inputs produced a decision and how
that decision links to its receipt. This separate check does not change the
receipt validator. The strict
[`decision_receipt_provenance_manifest_v1`](examples/phase3_decision_receipt_provenance_manifest_v1.json)
binds the exact workload, policy, frozen shadow report, v1 ground truth, and v1
enforced receipt bytes. It recomputes the unchanged shadow report, selects the
`maintenance` decision, and derives `fast_small` and `compact-a` only from that
recomputed result before checking policy, task-class, route, model, case, and
receipt links.

The declared synthetic result is `5/5` artifacts, `1/1` exact replay, `1/1`
decision-to-receipt chain, and `16/16` closed in-memory provenance mutations,
with no false accepts, false rejects, primary misses, or validator crashes.
The [Phase 3 proposal and acceptance record](docs/route_receipt_phase3_proposal.md)
defines this provenance boundary. It adds no model or network call, live route,
persistence, fallback execution, promotion, workload expansion, production
claim, or comparative reliability claim.

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
