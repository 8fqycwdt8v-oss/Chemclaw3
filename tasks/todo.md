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

## Wave 4 — Execution truth: run it, do not read it
- [ ] W4.1 Postgres-backed suite driven against the live DB (the ~200 skips), every gate reached
- [ ] W4.2 Temporal worker + durable jobs end to end against the running broker
- [ ] W4.3 Front door: OIDC-off and OIDC-on, SSE stream, session push-back, budget metering
- [ ] W4.4 The live lane (`make live-infra`/`live-up`/`live-probes`) against `chemclaw.cli.mock_llm`
- [ ] W4.5 Every CLI entry point and every `make` target actually invoked
- [ ] W4.6 Chart render + kubeconform + promtool on shipped defaults and on the refusal paths
- [ ] W4 fix stage, gate, PR, merge on green

## Wave 5 — Adversarial input and hostile state
- [ ] W5.1 Untrusted text: envelope framing/defanging, nonce, Cf category, helper reports
- [ ] W5.2 Authorization chain: every middleware order, refusal paths, subagent attenuation
- [ ] W5.3 Manifest/declaration fuzz: connector.yaml, datasource.yaml, sink, SKILL.md, templates
- [ ] W5.4 Egress guard + secrets: what escapes, what is logged, what reaches a metric label
- [ ] W5.5 Malformed persisted state: half-written rows, unknown shapes, migration gaps
- [ ] W5 fix stage, gate, PR, merge on green

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
