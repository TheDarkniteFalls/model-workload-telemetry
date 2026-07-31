# Route Receipt Validation Phase 3 Proposal

- Status: implemented synthetic provenance conformance
- Implementation authority: local artifacts, validation, tests, and reporting only
- Runtime authority: none

## Decision Summary

Phase 3 adds one deterministic provenance layer between the existing shadow
routing report and the existing v1 route-receipt validator.

The implementation answers one bounded question:

> Can the exact bytes used to produce one deterministic shadow decision be
> replayed, linked to one independently validated v1 ground-truth/receipt pair,
> and checked against a closed mutation catalog without granting execution or
> promotion authority?

The answer is evaluated only for the declared synthetic chain. Phase 3 does
not change routing behavior, receipt validity, or the meaning of any existing
v0 or v1 contract.

## Boundary From Phase 2

Phase 2 validates whether a receipt is internally complete and agrees with
independent attempt ground truth. It intentionally does not prove which
workload evidence and policy produced the receipt's candidate route.

That missing cross-artifact link is a new provenance-layer boundary. It is not
a defect in the receipt validator.

Phase 3 therefore leaves the v0 and v1 schemas, dispatch, fixtures, findings,
and conformance report unchanged. It first applies the existing validators,
then performs separate `PROVENANCE_*` checks across the declared artifacts.

## Positive Chain

The positive chain uses the unchanged
[`examples/runs.jsonl`](../examples/runs.jsonl) workload and unchanged
[`examples/shadow_route_policy.json`](../examples/shadow_route_policy.json)
policy.

Exact deterministic replay selects this report entry:

| Task class | Candidate route | Candidate model |
| --- | --- | --- |
| `maintenance` | `fast_small` | `compact-a` |

The expected route and model are not stored in the provenance manifest. The
validator obtains them only from the recomputed shadow report and then checks
the ground truth and receipt against that result.

The Phase 3-specific evidence is:

- [`examples/phase3/shadow_route_report_v0.json`](../examples/phase3/shadow_route_report_v0.json):
  exact deterministic JSON bytes produced from the unchanged workload and
  policy;
- [`examples/phase3/p3_m01_ground_truth.json`](../examples/phase3/p3_m01_ground_truth.json):
  independent `route_attempt_ground_truth_v1` evidence for `P3-M01`; and
- [`examples/phase3/p3_m01_enforced.json`](../examples/phase3/p3_m01_enforced.json):
  one enforced `route_receipt_v1` using policy ID `synthetic_example_v0`.

The receipt remains a synthetic replay. It keeps model, network, state
mutation, actual route, automatic route change, and promotion indicators
disabled.

## Strict Provenance Manifest

[`examples/phase3_decision_receipt_provenance_manifest_v1.json`](../examples/phase3_decision_receipt_provenance_manifest_v1.json)
is the only Phase 3 denominator. Its strict schema is
[`schemas/decision_receipt_provenance_manifest_v1.schema.json`](../schemas/decision_receipt_provenance_manifest_v1.schema.json).

The manifest binds the exact bytes of five artifacts with lowercase SHA-256
digests:

1. workload JSONL;
2. shadow policy;
3. frozen shadow report;
4. v1 attempt ground truth; and
5. v1 enforced receipt.

Every artifact path resolves relative to `examples/`. Absolute paths, parent
traversal, missing files, duplicate artifact paths, digest mismatches, unknown
fields, duplicate JSON keys, and catalog drift fail closed. Runtime file
discovery does not add cases, artifacts, or mutations.

## Replay And Chain Checks

Validation proceeds in this order:

1. parse the strict manifest with duplicate-key rejection;
2. resolve only its five safe relative artifact paths;
3. verify every exact-byte SHA-256 binding;
4. validate the unchanged workload and policy;
5. recompute the shadow report and compare its deterministic JSON bytes with
   the frozen report;
6. select the declared task class from the recomputed report;
7. derive the expected candidate route and model from that selected decision;
8. apply the unchanged v1 ground-truth and receipt validators; and
9. check policy, task class, route, model, case, receipt, and non-execution
   links across the chain.

A replay mismatch is reported separately from a chain mismatch. Receipt
findings remain owned by the receipt validator; Phase 3 only wraps invalid
receipt evidence at the provenance boundary.

## Closed Mutation Catalog

Phase 3 generates exactly these 16 mutations in memory. It does not commit
negative fixtures and does not introduce a general mutation framework.

| Mutation | Declared primary finding |
| --- | --- |
| workload digest mismatch | `PROVENANCE_ARTIFACT_DIGEST` |
| policy digest mismatch | `PROVENANCE_ARTIFACT_DIGEST` |
| shadow-report digest mismatch | `PROVENANCE_ARTIFACT_DIGEST` |
| ground-truth digest mismatch | `PROVENANCE_ARTIFACT_DIGEST` |
| receipt digest mismatch | `PROVENANCE_ARTIFACT_DIGEST` |
| shadow-report replay mismatch | `PROVENANCE_SHADOW_REPORT_REPLAY` |
| policy linkage mismatch | `PROVENANCE_POLICY_LINK` |
| task-class linkage mismatch | `PROVENANCE_TASK_CLASS_LINK` |
| route linkage mismatch | `PROVENANCE_ROUTE_LINK` |
| model linkage mismatch | `PROVENANCE_MODEL_LINK` |
| case linkage mismatch | `PROVENANCE_CASE_LINK` |
| receipt linkage mismatch | `PROVENANCE_RECEIPT_LINK` |
| authority overclaim | `PROVENANCE_AUTHORITY_OVERCLAIM` |
| unsafe path | `PROVENANCE_ARTIFACT_PATH` |
| unknown manifest field | `PROVENANCE_MANIFEST_UNKNOWN_FIELD` |
| duplicate manifest key | `PROVENANCE_MANIFEST_JSON` |

Each mutation preserves the base manifest and artifact bytes. Mutations that
change an artifact for a replay or linkage test also update that artifact's
in-memory digest so the declared primary finding remains specific to the
intended boundary.

## Deterministic Reporting

The human and JSON report modes are:

```sh
python3 -B model_workload_telemetry.py decision-receipt-provenance examples/phase3_decision_receipt_provenance_manifest_v1.json
python3 -B model_workload_telemetry.py decision-receipt-provenance examples/phase3_decision_receipt_provenance_manifest_v1.json --json
```

For the declared manifest, the expected result is:

- artifacts: `5/5`;
- exact replay: `1/1`;
- decision-to-receipt chain: `1/1`;
- declared mutations: `16/16`;
- false accepts: `0`;
- false rejects: `0`;
- primary misses: `0`;
- validator crashes: `0`; and
- `conformant=true`.

Machine-readable output is byte-identical across repeated runs. Neither report
contains timestamps, absolute paths, percentages, discovered-file counts,
comparative claims, or runtime-authority claims.

## Acceptance Gates

Phase 3 remains complete only while:

1. all existing v0 and v1 tests and fixtures pass unchanged;
2. Phase 2 retains its exact `10/10` cases, `12/12` receipts, and `24/24`
   mutation results;
3. all five Phase 3 artifact digests match exact bytes;
4. deterministic shadow replay is byte-exact;
5. the selected route and model come only from recomputation;
6. every policy, task-class, route, model, case, and receipt link passes;
7. all 16 in-memory mutations produce their declared primary finding;
8. mutation bases remain unchanged;
9. duplicate keys and unsafe paths fail closed;
10. report denominators come only from the manifest;
11. repeated JSON reports are byte-identical; and
12. the full local test and publication-safety gates pass.

## Public Claim Boundary

The strongest supported claim is:

> In the declared synthetic Phase 3 chain, five digest-bound artifacts replayed
> one deterministic shadow decision exactly, linked that decision to one valid
> v1 ground-truth/receipt pair, and detected all 16 declared in-memory
> provenance mutations.

This result is not a production reliability estimate and does not establish a
better model, a better router, live-route safety, statistical significance, or
authority to execute or promote a route.

## Explicitly Deferred

Phase 3 does not add:

- model or network calls;
- live routing or persistence;
- automatic fallback;
- route promotion or default changes;
- workload expansion;
- a hold-receipt redesign;
- production claims; or
- comparative reliability claims.
