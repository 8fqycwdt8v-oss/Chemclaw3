# Hardening session — the non-agentic job seam, the quick path, the slow path

**Goal:** maximum robustness of everything between an agent tool call and a durable result,
with the *lowest possible* configuration and maintenance burden. Both halves are the task; a
fix that buys robustness by adding a setting nobody will get right is not a fix.

## Scope (the seams under review)

1. The generic durable-job seam — `connectors/jobs.py`, `durable/connector_job.py`, the
   registry/queue derivation, the workers, retry classification, `job_records`, status read-back.
2. The quick path — MCP connector tools, and `connectors/calc/remote.py`'s
   key -> lookup -> compute-on-miss across the wire after the physics moved out.
3. The slow path — `CalcJobWorkflow` (minutes) and `QMJobWorkflow` + the Nextflow/Tower
   launcher (hours to days), including timeout nesting and cache-key integrity.
4. The periodic/declarative machinery — Temporal Schedules, `TemplateWorkflow`, `fan_out`,
   and the push-back path back into a conversation.
5. The configuration surface and the real cost of adding a new capability.

## Plan

- [x] Start the local stack (dockerd, `make up`, `make db-migrate`) so Postgres-backed tests
      actually run — a local suite without it silently skips ~157 of them.
- [x] Baseline `make lint type` (green) and record a full `make test` baseline.
- [x] Fan out five read-only reviewers, one per seam, each told to measure rather than argue.
- [ ] Triage the findings: confirm each one myself before acting on it; drop the ones that do
      not reproduce.
- [ ] Implement the confirmed fixes, smallest sufficient change each, with a test that fails
      without the fix.
- [ ] Re-run `make lint type test` with infra up; report what was skipped, if anything.
- [ ] ADR(s) for anything that changes a rule rather than fixing a bug; update
      `docs/planning/BACKLOG.md` / `DEFERRED.md` in the same commit.
- [ ] Review section below.

## Findings

(filled in from the review team + my own verification)

## Review

(filled in at the end)
