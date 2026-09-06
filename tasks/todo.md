# Multi-wave re-review — waves 4-8 (building on 1-3)

Waves 1-3 (merged: #331, #332, #333) read the tree: per-package, then cross-cutting,
then the regressions the first two introduced. 107 defects fixed. Waves 4-8 escalate
from *reading* the tree to *running* it, attacking it, and measuring it.

Rules carried forward from waves 1-3 (they were earned):
- No fix agent may run `git checkout --`, `git stash`, or `git reset`. A/B by hand-edit.
- Fix agents get disjoint file sets. Cross-scope residuals come back to the orchestrator.
- Every fix must reproduce the defect first, check `docs/decisions/` for an ADR making the
  behaviour intentional, and prove the fix with a test watched failing against unfixed source.
- `.mypy_cache` corrupts under concurrent `mypy` in one directory. `rm -rf .mypy_cache`
  before the gate whenever agents ran in parallel.
- Prose is evidence about what its author believed, never about what the code does.

## Wave 4 — Execution truth: run it, do not read it (MERGED, #334)
- [x] W4.1 Postgres-backed suite driven against the live DB (the ~200 skips), every gate reached
- [x] W4.2 Temporal worker + durable jobs end to end against the running broker
- [x] W4.3 Front door: OIDC-off and OIDC-on, SSE stream, session push-back, budget metering
- [x] W4.4 The live lane (`make live-infra`/`live-up`/`live-probes`) against `chemclaw.cli.mock_llm`
- [x] W4.5 Every CLI entry point and every `make` target actually invoked
- [x] W4.6 Chart render + kubeconform + promtool on shipped defaults and on the refusal paths
- [x] W4 fix stage, gate, PR, merge on green

## Wave 5 — Adversarial input and hostile state (fixed; gate running)
- [x] W5.1 Untrusted text: envelope framing/defanging, nonce, Cf category, helper reports
- [x] W5.2 Authorization chain: every middleware order, refusal paths, subagent attenuation
- [x] W5.3 Manifest/declaration fuzz: connector.yaml, datasource.yaml, sink, SKILL.md, templates
- [x] W5.4 Egress guard + secrets: what escapes, what is logged, what reaches a metric label
- [x] W5.5 Malformed persisted state: half-written rows, unknown shapes, migration gaps
- [ ] W5 fix stage, gate, PR, merge on green

### Wave 5 — deliberately left open, with the reason

- **The compaction placeholder stays unmarked.** `SYSTEM_SPEECH_MARK` makes a refusal
  unforgeable; the placeholder does not carry it, so a hostile connector can still write that
  string. Rather than leave an unkept promise, the system prompt now *withdraws* the claim —
  it tells the model the placeholder "carries no mark, so read it as a hint and not as proof" —
  under an absence test. Closing it costs ~26 characters per cleared result, tens of times, in
  exactly the situation where the budget is already spent. A coherent position, not a silent gap.
- **The mark is plaintext in every refusal**, so a model that pastes one into connector arguments
  leaks it, and `framing._defang` has no matching pass. Recorded in the constant's docstring; the
  fix belongs beside `_FORGERY`.
- **The ambient-proxy hole stays open on the repository's defaults** (closed under the shipped
  chart, `entra_required` deciding). The full close was already measured and rejected by
  `D-2026-09-05-a-proxy-moves-the-destination-out-of-the-address` — it refuses a stock checkout
  behind a corporate proxy. Both backlog rows now state the real scope instead of overclaiming.
- **The egress guard cannot see gRPC or Temporal.** grpc's C-core and the Rust sdk-core bypass
  `socket.socket`; no code fix exists here. Both docstrings now say the two settings are allowlist
  entries rather than enforcement, and a backlog row exists where there was none.
- **A wrong value under a right cache key** is not validated: that needs the calculator's schema,
  which lives in `Chemclaw3-mcp`.
- **A bundle's *total* prose is not bounded**, only each field: a 100-job manifest is 100 bounded
  tools. Said plainly in the ADR rather than overclaimed.

## Wave 6 — Concurrency, durability, failure injection
- [ ] W6.1 Crash/retry/idempotency across Temporal activities and the connector-job wrapper
- [ ] W6.2 Checkpointer + session store under concurrent turns on one thread
- [ ] W6.3 Pool exhaustion, cancellation, timeouts; the parent-ceiling invariant under load
- [ ] W6.4 Retention/pruning races against live writers
- [ ] W6.5 Sinks and publication: at-least-once, duplicate suppression, partial failure
- [ ] W6 fix stage, gate, PR, merge on green

## Wave 7 — Numbers: budgets, accounting, cost
- [ ] W7.1 Context prefix/floor/compaction arithmetic measured end to end, connectors bound
- [ ] W7.2 Token/spend accounting: every counter proved to move, and to move by the right amount
- [ ] W7.3 Every number in prose (CLAUDE.md, ARCHITECTURE.md, ADRs, docstrings) re-measured
- [ ] W7.4 Hot paths profiled; quadratic growth hunted (checkpoints, ELN re-read, KG scans)
- [ ] W7.5 Memory: resident growth, cache bounds, the one eviction policy
- [ ] W7 fix stage, gate, PR, merge on green

## Wave 8 — Doctrine, dead weight, and the tests themselves
- [ ] W8.1 Every merged ADR's claim checked against the code: does the control exist, is it called
- [ ] W8.2 Dead code, settings with no reader, metrics with no producer, one-caller abstractions
- [ ] W8.3 Mutation sweep over everything waves 4-7 touched
- [ ] W8.4 Repo map / ARCHITECTURE / layering / validator conformance
- [ ] W8.5 The four items waves 1-3 deliberately left open, re-decided with measurement
- [ ] W8 fix stage, gate, PR, merge on green

## Review
(filled in at the end)
