# Route Receipt Validation Phase 2 Proposal

- Status: implemented synthetic conformance
- Implementation authority: completed and closed
- Runtime authority: none

## Decision Summary

Phase 2 should expand the synthetic conformance surface without changing the
meaning of `route_receipt_v0` and without routing real traffic.

The recommended design is to:

1. freeze the Phase 1 `route_receipt_v0` contract;
2. introduce a versioned `route_receipt_v1` only after this proposal is
   approved;
3. add a strict, independently validated ground-truth schema alongside it;
4. cover direct completion, expected holds, runtime failures, fallback
   attribution, source boundaries, safety boundaries, finalization, and write
   containment with a small synthetic case matrix; and
5. report deterministic conformance counts without claiming production
   reliability or superiority over a simple task-class router.

The completed implementation remains limited to reviewed synthetic schemas,
fixtures, validation, mutations, and reporting. It grants no authority for
network access, model calls, state changes, or live routing.

## Implementation Outcome

The implemented matrix contains 10 accepted logical cases and 12 accepted
receipts bound to independent ground truth by the strict
`route_receipt_case_manifest_v1` contract and SHA-256 digests of exact fixture
bytes. Its deterministic `route_receipt_conformance_report_v1` output accepts
`10/10` cases and `12/12` receipts and detects `24/24` declared in-memory
mutations with their primary finding codes.

`P2-S02` and `P2-F01` are implemented as unexpected-write and finalization
mutations rather than committed negative fixtures. `P2-A01` is covered by the
paired passive and enforced delivery and hold receipts. The report remains
offline, non-executing, non-mutating, and bounded to its declared manifest.

## Why Phase 2 Exists

Phase 1 proves that one two-attempt synthetic fallback receipt can be checked
against independent attempt truth. It also proves that six important receipt
defects are rejected.

That is a credible foundation, but it is deliberately narrow. It does not yet
show that the contract handles:

- a successful route with no fallback;
- failures at different runtime stages;
- a correct, auditable decision to hold rather than return an answer;
- schema and source-boundary failures as distinct from model quality;
- safety-policy rejection before a model response exists;
- retry exhaustion;
- consistent attribution across different workload classes; or
- aggregate conformance reporting across a declared case set.

Phase 2 should answer one bounded question:

> Across a declared synthetic case matrix, does the validator accept complete
> and correctly attributed receipts while rejecting specific omissions,
> misattributions, boundary violations, false quality claims, and authority
> overclaims?

It should not answer whether the routing policy chose the best model.

## Phase 1 Baseline That Must Remain Stable

Phase 2 must preserve all current Phase 1 behavior:

- `route_receipt_v0` remains strict and unchanged;
- receipt evidence remains separate from attempt ground truth;
- passive receipts may claim observation, not enforcement;
- enforced success requires complete attribution and passing final quality;
- runtime failures remain unassessed for answer quality;
- forbidden fallback, finalization failure, and unexpected writes fail closed;
- validation uses no model and no network;
- validation does not persist receipts or modify application state; and
- no validation result changes a route, default, or promotion decision.

Existing Phase 1 examples and tests become permanent regression cases.

## Why A Versioned Contract Is Recommended

The v0 contract intentionally describes a successful enforced receipt. A
correct safety block, source-boundary hold, or exhausted fallback chain is not
the same thing as a successfully completed answer, but it can still be a
correctly enforced routing outcome.

Changing v0 in place would make the already-published contract ambiguous.
Phase 2 should therefore propose, review, and only then implement v1 semantics.
Version dispatch must be exact: v0 inputs remain subject to the v0 validator,
v1 inputs remain subject to the v1 validator, and neither version is silently
coerced into the other.

The minimum v1 additions should be:

- a machine-readable `failure_category` on every attempt, using `none` for a
  completed attempt without a failure;
- explicit per-attempt `source_boundary_status` and
  `safety_boundary_status` fields;
- a `decision_disposition` that separates delivery from hold;
- a valid `auditable_hold` claim that is distinct from successful delivery;
- an explicit ordered fallback-transition list rather than only a summary of
  the last fallback; and
- a strict schema for the independent attempt ground truth.

Suggested values are intentionally small:

| Field | Proposed values |
| --- | --- |
| `failure_category` | `none`, `infrastructure`, `schema`, `source_boundary`, `safety_policy` |
| `source_boundary_status` | `not_applicable`, `pass`, `fail`, `not_assessed` |
| `safety_boundary_status` | `not_applicable`, `pass`, `blocked`, `not_assessed` |
| `decision_disposition` | `deliver`, `hold` |
| `enforcement_status` | `not_applied`, `pass` |
| `completion_claim` | `observed_only`, `auditable_complete`, `auditable_hold` |

An enforced hold means enforcement passed and the decision disposition was
`hold`; it does not mean enforcement was incomplete. It may validate only when
the attempts, policy decision, boundary evidence, write containment, and
finalization are all complete. A failed or unverified receipt finalization
remains invalid; it is not converted into a valid hold.

Mechanical completion and delivery authority must also remain separate. A
model may complete a response that receives `assessed_fail`; the attempt is
mechanically complete, but the enforced disposition must still be `hold` with
an `auditable_hold` claim.

## Proposed Synthetic Coverage Matrix

The workload labels below are lenses for testing attribution. They are not new
benchmarks and do not contain real prompts or responses.

| ID | Workload lens | Synthetic event path | Expected enforced result | Primary evidence |
| --- | --- | --- | --- | --- |
| P2-D01 | deterministic | direct completion, no fallback | `auditable_complete` | one complete attempt and correct final attribution |
| P2-M01 | maintenance | fast route completes | `auditable_complete` | no fallback and assessed quality |
| P2-M02 | maintenance | pre-request infrastructure failure, permitted fallback completes | `auditable_complete` | first attempt unassessed; fallback attributed |
| P2-I01 | integration | request-open failure, permitted fallback completes | `auditable_complete` | runtime stage and final model are correct |
| P2-I02 | integration | response-stream failure, permitted fallback completes | `auditable_complete` | partial runtime evidence does not become quality evidence |
| P2-I03 | integration | infrastructure failure and fallback exhaustion | `auditable_hold` | all attempts present; no answer-quality claim |
| P2-Q01 | any | response completes but receives assessed-fail quality | `auditable_hold` | mechanical completion does not authorize delivery |
| P2-R01 | research | response fails source validation | `auditable_hold` | `source_boundary` is distinct from answer quality |
| P2-R02 | research | required source is unavailable before an answer is accepted | `auditable_hold` | source status is explicit and final quality is unassessed |
| P2-S01 | safety | policy rejects the attempted route | `auditable_hold` | safety block is attributed without a model-quality claim |
| P2-S02 | safety | otherwise valid receipt reports an unexpected write | invalid receipt | write containment fails closed |
| P2-F01 | any | receipt finalization fails | invalid receipt | no completion or hold claim is trusted |
| P2-A01 | any | passive and enforced receipts share the same attempt truth | mode-dependent | passive authority never becomes enforcement authority |

Each accepted case should have one independent truth object and two receipts
where meaningful: passive and enforced. Invalid cases should be generated as
small mutations of a valid synthetic case so the intended defect is obvious.

The source and safety cases validate whether a receipt accurately records the
declared synthetic boundary outcome. They do not prove that a source was true,
that a source policy was complete, or that the safety policy itself was wise.

Phase 2 should remain limited to one fallback transition per case. Multi-hop
and nested fallback are valuable, but they should wait until the v1 transition
shape has proved stable in this smaller matrix.

## Required Negative Mutations

Every accepted case should declare applicable negative mutations. The Phase 2
suite should include at least:

- missing, duplicated, reordered, or invented attempts;
- candidate route not matching the first attempt;
- final attempt not matching the last attempt;
- final model not matching the responding model;
- runtime failure incorrectly included in assessed quality;
- source-boundary failure relabeled as answer-quality failure;
- safety-policy rejection relabeled as model failure;
- fallback triggered by the wrong attempt or outcome;
- fallback transition absent from the allowed policy;
- fallback marked unused when another route was attempted;
- enforced success claimed for an exhausted or blocked case;
- passive observation promoted to an enforcement claim;
- incomplete expected, observed, or unexpected-write attribution;
- failed or unverified finalization; and
- unknown fields, duplicate JSON keys, invalid types, and non-finite metrics.

Each negative mutation must name one primary expected finding code. Secondary
findings are acceptable when one mutation necessarily violates more than one
invariant, but the validator must never crash or silently accept the object.

## Independent Ground Truth

Receipt correctness must continue to be judged from evidence that the receipt
did not generate for itself.

The proposed ground-truth contract should:

- live in a separate file from every receipt;
- have its own strict schema and schema version;
- declare the case ID, policy ID, ordered attempts, permitted transitions,
  final attempt, final responding model, boundary outcomes, and observed
  writes;
- bind the candidate route to the first attempt and the final ID to the last
  attempt;
- reject unknown fields, duplicate keys, missing attempts, and invalid types;
  and
- contain synthetic identifiers and measurements only.

Separate files are necessary but not sufficient for independence. Ground
truth must be authored, reviewed, and locked before the receipts, and the
receipt path must not calculate or revise its own oracle. A case manifest
should bind each receipt to the reviewed truth artifact and the SHA-256 digest
of its exact fixture bytes. Shared helpers may parse common shapes, but they
must not generate both the claimed result and the expected result from the
same mutable object.

The validator must validate ground truth first. Invalid ground truth should
produce one clear `GROUND_TRUTH_INVALID` receipt result rather than allowing a
receipt to be judged against ambiguous evidence.

## Passive And Enforced Semantics

Passive and enforced receipts may share event evidence, but not authority.

| Situation | Passive maximum claim | Enforced maximum claim |
| --- | --- | --- |
| completed with passing quality | `observed_only` | `auditable_complete` |
| completed with assessed-fail quality | `observed_only` | `auditable_hold` |
| correctly blocked or exhausted | `observed_only` | `auditable_hold` |
| unexpected writes | invalid | invalid |
| failed finalization or integrity | invalid | invalid |
| missing or inconsistent attempt evidence | invalid | invalid |

This separation is a core test target. Merely changing a mode field must never
upgrade a passive receipt into an enforced result.

## Non-Execution And Containment Proof

Any later Phase 2 implementation must remain safe to run offline. Its tests
should prove:

- `model_called=false` for every case and report;
- `network_called=false` for every case and report;
- `state_mutating=false` for every case and report;
- no filesystem changes outside test-created temporary directories;
- no persisted receipt, route, default, cache, or promotion state;
- `actual_route=none` and `automatic_route_change=false`; and
- identical findings for identical inputs across repeated runs.

Network and model adapters should not be introduced merely to prove they were
not used. If a future implementation creates an execution seam, sentinel tests
must fail immediately if that seam is called.

## Proposed Reporting

Phase 2 reporting should remain a deterministic conformance summary, not a
model leaderboard.

For each run, report:

- schema and case-manifest versions;
- total accepted cases and accepted-case pass count;
- total negative mutations and detected-mutation count;
- false accepts, false rejects, and validator crashes;
- attribution accuracy for attempts, final models, fallback transitions,
  quality status, source boundaries, safety boundaries, and writes;
- passive-authority overclaim count;
- enforced false-pass count; and
- the non-execution invariants.

A false accept is a declared negative mutation that validates. A false reject
is a declared positive receipt that fails validation. These counts must use
the case manifest as the denominator rather than whatever files happened to be
discovered at runtime.

Rates must always include their integer numerator and denominator. A report
such as `24/24 mutations detected` is acceptable. A bare `100% reliable` claim
is not.

Confidence intervals should not be attached to a hand-selected deterministic
case matrix because they would suggest a population the suite does not sample.
If a later seeded mutation campaign is added, it may report a 95% Wilson
interval for that declared mutation generator only, alongside the seed list,
generator version, and exact sample size.

## Acceptance Gates

Phase 2 implementation is complete only while all of these gates pass:

1. All existing Phase 1 tests still pass unchanged.
2. Every declared positive receipt validates against independent truth.
3. Every declared negative mutation produces its primary expected finding.
4. No malformed input causes an exception or partial success.
5. Attempt, final-model, and fallback-transition attribution are exact in every
   case.
6. Runtime, source-boundary, safety, and answer-quality evidence never
   contaminate one another.
7. Passive receipts never claim enforcement.
8. Enforced delivery or hold is accepted only with complete evidence, verified
   finalization, no unexpected writes, and the matching
   `decision_disposition`.
9. Repeated runs produce byte-identical machine-readable reports.
10. Tests, self-test, compilation, JSON checks, diff checks, and publication
    safety scans all pass.
11. The repository contains only synthetic public-safe data.
12. No model, network, state mutation, route execution, default change, or
    promotion occurs.

Any future failure reopens Phase 2 implementation status. It must not be
reframed as partial production readiness.

## Public Claim Boundary

With all Phase 2 gates passing, the strongest defensible public claim is:

> In the published synthetic Phase 2 conformance matrix, the validator
> accepted every declared valid delivery or hold receipt and detected every
> declared mutation while preserving attempt, fallback, quality, boundary,
> write, and authority attribution.

The following claims would still be unsupported:

- the harness is more reliable than a simple task-class router;
- the harness selects the best model;
- the harness improves answer quality, latency, or cost;
- the synthetic cases represent production traffic;
- the validator prevents every routing or safety failure;
- the system is ready for live traffic; or
- the observed detection rate generalizes statistically beyond the declared
  corpus.

A comparative reliability claim requires a later, separately authorized phase
with a frozen task-class-router baseline, representative workloads, repeated
trials, controlled runtime failures, blinded or fixed quality assessment,
predeclared metrics, and uncertainty reporting.

## Implementation Record

1. **Contract:** strict v1 receipt, ground-truth, and case-manifest schemas.
2. **Positive cases:** 10 accepted synthetic cases and 12 receipts, with v0
   behavior preserved.
3. **Mutations:** 24 declared in-memory mutations with one primary finding
   code each and no committed negative fixtures.
4. **Reporting:** deterministic human and JSON conformance summaries whose
   denominator comes only from the digest-bound manifest.
5. **Publication:** full tests and public-safety gates are required before any
   published change.

The implementation contains no model call, network-dependent validation,
persistent state, live routing, or private data.

## Deferred Beyond Phase 2

Phase 2 should not include:

- real prompts, responses, users, providers, or production logs;
- live model or endpoint health checks;
- concurrent, nested, or mid-stream multi-hop routing;
- writes to application state, caches, defaults, or route configuration;
- automatic fallback execution;
- model-quality judging by another model;
- cost or provider-comparison claims;
- automatic promotion or rollback; or
- a claim that the route-receipt harness beats a simple router.

Those require separate designs and explicit authority.
