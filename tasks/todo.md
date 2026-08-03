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

- [x] **R1.0 [self]** `test_repo_map.py` judged files by absolute path, so any checkout under a
      dot-directory found 0 directories and every assertion degenerated. Blocked every worktree agent.
- [x] **R1.6 [Sonnet]** Register `ProfileError` + `AuthorizationError` (and two subclasses the new
      completeness walk found) in `_BAD_DATA_TYPES`; delete the false retry claim.

- [x] **R1.1 [Sonnet]** R3 rewrite `test_layering.py` as a derived allow-list (folds in the 3
      missing core modules + the false "nothing imports cli" premise).
- [x] **R1.2 [Sonnet]** H1 `CurrentUser` alias + route-coverage test (prereq for the decomposition).
- [x] **R1.3 [Haiku]** Ops one-liners: F5 pipefail, F1 port, F6 `.PHONY`, F9 concurrency, F10
      permissions, F11 uv cache, F12 syft pin, F13 helm pin, F7 dedupe deps-audit, F8 `make ci`.
- [x] **R1.4 [Haiku]** Core-4 entrypoint-default parity test; H3 refuse `workers>1`; H6 conflict
      scope label; Test-5 relax `test_deferred_register` threshold.
- [x] **R1.5 [Sonnet]** Test-1 three untested Postgres classes; Science-5 safety validator/eager-load.

## R2 — Structural moves (serial)

- [x] **R2.1 [Opus]** R1 move `api/metrics.py` → `core/metrics.py`. The plan said "delete the
      `metrics_bridge` hack"; that was **half wrong** and was not done — `record_metric`'s `try`
      wraps `update(METRICS)`, not just the import, so deleting it would have let a typo'd counter
      name propagate into ~10 callers' request paths. The lazy *import* went; the defensive
      *swallow* stayed.
- [x] **R2.2 [Opus]** R2 move ambient-context primitives → `core/` (identity_context, tool_registry,
      turn_signals, session-id half).
- [x] **R2.3 [Sonnet]** R4 move cli library halves → `durable/` (shims in cli); S6 dry-run ContextVar.
- [x] **R2.4 [Fable]** R6 split `core/config.py` → `core/config/` package, import path unchanged.

## R3 — Decompose the two hot files (after R1.2 + R2)

- [x] **R3.1 [Fable]** `api/app.py` decomposition (app/state/deps/schemas/middleware + routes/×8);
      gate-preserving, no test changes.
- [x] **R3.2 [Fable]** `runner.py` split (runner/trace/usage/answer) + A3 lease + A5 pin + A4
      pending-completions.
- [x] **R3.3 [Sonnet]** S2 `core/bounded.py::BoundedLru` (5 maps) + S3 no-leak-404 helper.

## R4 — Per-package cleanup (parallel once R6 lands)

- [x] **R4.1 [Sonnet]** science/ constants module (Science-2 drifted constant) + pka decomposition.
- [x] **R4.2 [Sonnet]** retrieval/ reindex incrementality + `--full` recovery path.
- [x] **R4.3 [Haiku]** cli/ exit-code unification; Conn-F7 dead fields; Conn-F5 URL-key validator.
- [x] **R4.4 [Sonnet]** Conn-F1 start_workflow framing; Conn-F4 MCP exception-sanitizing (after spike).
- [x] **R4.5 [Haiku]** Test-2 121→11 subprocesses; Test-3 `_free_port` → conftest; Test-4 parametrize;
      P2 executemany.

## R5 — Docs & onboarding

- [x] **R5.1 [Sonnet]** R8/R9 stale docstrings + Entry-points table in ARCHITECTURE.md.
- [x] **R5.2 [Sonnet]** Ops F2/F3/F4 onboarding + F14 migration note + F17 prose-validate over Makefile.
- [x] **R5.3 [Fable]** Final integration + completeness critic: `make ci`, import-graph diff, re-check
      every claim, closing ADR(s), update BACKLOG/DEFERRED/lessons.md.

## Deferred (see plan for reasons)

- No package merges/splits; no `agent/` restructure; no re-export `__init__`s; naming drift is
  new-code convention only; Conn-F3 documented (no change); H7/A7 behind the retention BACKLOG row;
  agent-pool unification stays DEFERRED (upstream trigger).

## Review (R5.3, 2026-08-03 — the completeness pass; full record in
D-2026-08-03-the-refactor-closes-what-it-measured)

One line per phase, then what re-verification changed:

- **R0** — both data-loss bugs, both security Mediums and the four Lows closed, each with a
  mutation-proven test; the redirect fix needed *both* proposed remedies, not either.
- **R1** — the derived layering allow-list, the route-auth walk, the ops one-liners; R1.6 closed
  the two `_BAD_DATA_TYPES` gaps R0.6 had left (registered by name, deliberately not reparented).
- **R2** — the four misfiled-module moves and the config split; `metrics_bridge` kept (the plan's
  "delete" was half wrong — the swallow guards 11 call sites, only the lazy import went).
- **R3** — `app.py` → routes/state/deps/schemas/middleware with **zero** test-file changes;
  `runner.py` → runner/trace/usage/answer with import re-points in four test files, no assertion
  changed; the three turn-lifecycle races closed; `BoundedLru` consolidated **four** maps (not the
  planned five — metrics' cap is refuse-new and stays) and the 404 helper **two** gates (not
  three — `_visible_proposal` carries a reviewer allowance).
- **R4** — science constants + pKa split, incremental reindex, CLI unification,
  `start_workflow` framing + MCP exception sanitization (probed over the real transport),
  subprocess-count and fixture cleanups.
- **R5** — stale-docstring/ARCHITECTURE corrections, onboarding + prose-gate widening, and this
  closing pass.

Closing gate (quiet box): 2852 passed / 127 skipped / 0 failed in 311 s; lint and
`mypy --strict` clean; every validator green **except** `helm-validate`, which has no `helm`/
`kubeconform` here — `make ci` therefore cannot pass locally and is not claimed. Import-graph
diff vs `39f9135`: 8 edges removed, 3 added, `core` at zero module-scope sibling edges plus the
one declared lazy edge. Docstrings 98.3 %, 208 ADRs, largest source file 808 lines (was 2157).

Reopened/corrected by the critic: the plan's six disproven statements (metrics_bridge, five→four
LRUs, three→two 404 gates, 121→132 subprocesses, Ops-F1 resolved, A3's two release sites) are
corrected in the plan in place; one new BACKLOG row (pKa tests fail under box load, cause
uncaptured); the two R1.6-closed BACKLOG rows and the accepted lazy-edge row are marked Done.
