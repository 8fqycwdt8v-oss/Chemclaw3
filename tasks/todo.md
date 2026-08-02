# Task: grand refactor, hardening & simplification

Planned 2026-08-02. Branch: `claude/codebase-review-refactor-3lbnjs`.

Full plan and evidence: `docs/planning/refactor-hardening-plan.md`. This is the execution
checklist — one line per work package, each a single auto-merged PR. Model tier in brackets.
Mark done as PRs merge. Every bug fix ships a mutation-proven test; every perf change records a
before/after number.

Source: nine parallel review agents (metrics + eight deep dives, three on Opus) against a green
baseline (`make lint type test`: 2789 passed, 104 sandbox-skipped). The tree is sound — 13 packages,
97.5 % docstrings, 0 bare excepts, 0 mocks, CI-enforced config parity. The debt is structural (five
hot files) plus a small set of real correctness/security bugs. No package merges/splits, no `agent/`
restructure.

## R0 — Correctness & security first

- [x] **R0.1 [Fable]** A1 failed-watermark-read → full history wipe (`runner.py`/`session_store.py`);
      A2 answered turn rolled back on slow verifier; H2 verifier timeout. +disconnect-during-answer test.
- [x] **R0.2 [Fable]** Sec-1 forgeable injection envelope: nonce delimiter, frame `list_attachments`,
      sanitize upload filename (`agent/framing.py`, `agent/attachments.py`).
- [x] **R0.3 [Opus]** Sec-2 connector redirect identity leak — `follow_redirects=False` / host-guard
      the stamp hook (`connectors/registry.py`).
- [x] **R0.4 [Sonnet]** Sec-3 NULL-owner → 404 under `entra_required`; Sec-4 `git add … --`;
      Sec-5 connector body cap; Sec-6 redaction resolver for the two held tokens.
- [x] **R0.5 [Sonnet]** Science-1 process-group kill helper (xtb/crest); Science-4 BO error
      translation; Conn-F2 BO activity heartbeats.
- [x] **R0.6 [Sonnet]** Ingest-1 ord_adapter isinstance guards (execution-verified crash);
      R5 reparent 4 error classes to `ChemclawError` + `_BAD_DATA_TYPES`; R7 evals `asyncio.run`.

## R1 — Turn armed guards into tests + ops one-liners

- [ ] **R1.1 [Sonnet]** R3 rewrite `test_layering.py` as a derived allow-list (folds in the 3
      missing core modules + the false "nothing imports cli" premise).
- [ ] **R1.2 [Sonnet]** H1 `CurrentUser` alias + route-coverage test (prereq for the decomposition).
- [ ] **R1.3 [Haiku]** Ops one-liners: F5 pipefail, F1 port, F6 `.PHONY`, F9 concurrency, F10
      permissions, F11 uv cache, F12 syft pin, F13 helm pin, F7 dedupe deps-audit, F8 `make ci`.
- [ ] **R1.4 [Haiku]** Core-4 entrypoint-default parity test; H3 refuse `workers>1`; H6 conflict
      scope label; Test-5 relax `test_deferred_register` threshold.
- [ ] **R1.5 [Sonnet]** Test-1 three untested Postgres classes; Science-5 safety validator/eager-load.

## R2 — Structural moves (serial)

- [ ] **R2.1 [Opus]** R1 move `api/metrics.py` → `core/metrics.py`; delete `metrics_bridge` hack.
- [ ] **R2.2 [Opus]** R2 move ambient-context primitives → `core/` (identity_context, tool_registry,
      turn_signals, session-id half).
- [ ] **R2.3 [Sonnet]** R4 move cli library halves → `durable/` (shims in cli); S6 dry-run ContextVar.
- [ ] **R2.4 [Fable]** R6 split `core/config.py` → `core/config/` package, import path unchanged.

## R3 — Decompose the two hot files (after R1.2 + R2)

- [ ] **R3.1 [Fable]** `api/app.py` decomposition (app/state/deps/schemas/middleware + routes/×8);
      gate-preserving, no test changes.
- [ ] **R3.2 [Fable]** `runner.py` split (runner/trace/usage/answer) + A3 lease + A5 pin + A4
      pending-completions.
- [ ] **R3.3 [Sonnet]** S2 `core/bounded.py::BoundedLru` (5 maps) + S3 no-leak-404 helper.

## R4 — Per-package cleanup (parallel once R6 lands)

- [ ] **R4.1 [Sonnet]** science/ constants module (Science-2 drifted constant) + pka decomposition.
- [ ] **R4.2 [Sonnet]** retrieval/ reindex incrementality + `--full` recovery path.
- [ ] **R4.3 [Haiku]** cli/ exit-code unification; Conn-F7 dead fields; Conn-F5 URL-key validator.
- [ ] **R4.4 [Sonnet]** Conn-F1 start_workflow framing; Conn-F4 MCP exception-sanitizing (after spike).
- [ ] **R4.5 [Haiku]** Test-2 121→11 subprocesses; Test-3 `_free_port` → conftest; Test-4 parametrize;
      P2 executemany.

## R5 — Docs & onboarding

- [ ] **R5.1 [Sonnet]** R8/R9 stale docstrings + Entry-points table in ARCHITECTURE.md.
- [ ] **R5.2 [Sonnet]** Ops F2/F3/F4 onboarding + F14 migration note + F17 prose-validate over Makefile.
- [ ] **R5.3 [Fable]** Final integration + completeness critic: `make ci`, import-graph diff, re-check
      every claim, closing ADR(s), update BACKLOG/DEFERRED/lessons.md.

## Deferred (see plan for reasons)

- No package merges/splits; no `agent/` restructure; no re-export `__init__`s; naming drift is
  new-code convention only; Conn-F3 documented (no change); H7/A7 behind the retention BACKLOG row;
  agent-pool unification stays DEFERRED (upstream trigger).

## Review

_(filled at session end — one line per merged WP, plus anything the completeness critic reopened.)_
