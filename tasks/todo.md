# Multi-wave codebase review (2026-09-06)

Full re-review of the tree as if new. Prior review waves ignored by construction: every
agent gets the code, not the history, and every finding must be proved by running something.

## Wave 0 — baseline (done before any review)
- [x] Start the daemon: `sudo -n dockerd`, `make up`, `make db-migrate` (Postgres + Temporal live)
- [x] `make lint` green
- [x] `make type` green (804 files, mypy --strict)
- [ ] `make test` green with Postgres up — record the skip count the epilogue reports

## Wave 1 — per-package correctness sweep (fan-out, find-only)
Ten agents, one package cluster each, reading for defects that change behaviour.
- [ ] agent/ graph + middleware chain + budgets
- [ ] agent/ sessions, authz, tools, skills, subagents
- [ ] core/ + protocols/
- [ ] ingest/
- [ ] science/ + evals/
- [ ] connectors/ + templates/
- [ ] durable/ + operations/
- [ ] api/ + deliver/
- [ ] cli/ + publish/
- [ ] kg/ + retrieval/ + memory/
- [ ] Verify each finding adversarially, fix, `make lint type test`, PR, merge on green

## Wave 2 — cross-cutting invariants (fan-out, find-only)
Cuts that no per-package reader can see.
- [ ] Concurrency and async correctness (event loops, pools, locks, cancellation)
- [ ] SQL, migrations and data integrity (schema vs. reader, index vs. query, transactions)
- [ ] Security: authorization chain, redaction, injection, credential handling
- [ ] Resource lifecycle: leaks, unclosed clients, unbounded growth
- [ ] Config/settings: readerless settings, magic numbers, chart vs. code drift
- [ ] Tests that prove nothing (mocks asserting themselves, dead fixtures, absent arms)
- [ ] Prose vs. code: CLAUDE.md / ADR / docstring claims falsified by the current commit
- [ ] Verify, fix, `make lint type test`, PR, merge on green

## Wave 3 — deep semantic pass on what waves 1–2 disturbed
- [ ] Re-review the fixed regions plus anything two waves both flagged
- [ ] Verify, fix, `make lint type test`, PR, merge on green

## Review
(filled in at the end)
