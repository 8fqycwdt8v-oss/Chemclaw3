# Code Review, Hardening & Refactoring Campaign — Plan

Approved plan for the full campaign: code review → bug fixing → hardening →
simplification/refactoring across all 15 packages (~13.6k prod lines, ~9.6k test
lines). Orchestration constraint: all deep reading happens in subagent contexts
(find → adversarial-verify pipelines returning structured findings only), keeping
the main context lean while coverage stays exhaustive. Branch:
`claude/code-review-refactor-plan-wm34wc`.

## Exploration findings that shape the campaign

- **Architecture**: `chemclaw/` is the shared kernel (imported by every package,
  imports none) → reviewed first, highest blast radius. `workflows/` is the top
  integration layer. One import cycle: `agents ↔ report` via
  `agents/embedding_provider.py`.
- **Quality baseline is high** (mypy --strict, mandatory docstrings, coverage gate
  80%/baseline ~86%, zero inline TODOs, config fully centralized) → the campaign
  targets deep correctness/security verification and targeted refactoring, not
  style cleanup.
- **Risk map** (no shell/SQL injection, no eval/pickle, Temporal determinism clean
  on first pass; residual risk is config-default posture):
  1. `entra_required=False` + `service_host="0.0.0.0"` defaults = unauthenticated
     service on all interfaces; startup only warns (`config.py:372/304`, `app.py:333`).
  2. `_resolve_session` (`service/app.py:180`) is the sole IDOR/ownership boundary.
  3. `tool_authz_default="allow"` (`config.py:395`) — RBAC opt-in; write tools
     (`index_molecule`, job launchers) ungated by default.
  4. `kg/git_submitter.py` lock is per-process only — shared `note_repo_dir` across
     processes would corrupt branches (documented, unenforced).
  5. `datetime` imports in `workflows/eln_sync.py:16` / `memory_jobs.py:11` need an
     activity-only confirmation.
- **Catalogued debt worth acting on**: 736-line `chemclaw/config.py` (split),
  mock-heavy boundary tests (`test_authz.py`, `test_service.py`, `test_runner.py`).
  O(n²) playbook clustering and the 5000-row substructure scan cap stay in
  DEFERRED.md (triggers haven't fired).

## Severity rubric

S1 exploitable/corruption · S2 wrong result/latent bug · S3 hardening gap · S4 refactor.

## Wave 0 — Baseline

- [x] Run `make lint type test` + `make cov`; record green baseline + coverage number
      (if red, fix first).
      **Baseline (2026-07-23)**: lint clean · mypy strict clean · 508 passed /
      16 skipped (Temporal test server unreachable in sandbox; Postgres 16 +
      pgvector 0.8.0 brought up locally, so all 18 DB tests now run) ·
      coverage **88.43%** (gate 80%). Wave 6 must be ≥ this.

## Wave 1 — Kernel review (`chemclaw/`, before dependents)

- [x] Reviewer A: `config.py` — validator correctness, default posture, dead settings,
      cohesion (input to Wave-5 split).
- [x] Reviewer B: `db.py`, `http.py`, `temporal_client.py` — lifecycle, timeouts,
      error paths.
- [x] Reviewer C: `errors.py`, `ids.py`, `chem.py`, `logging.py` — contracts the ~60
      importers rely on.
- [x] Adversarially verify all kernel findings.

## Wave 2 — Domain review fan-out (parallel; find → skeptic-verify per unit)

Each unit = one reviewer with four lenses (correctness; hardening/failure-modes;
simplification/dead-code; extensibility/config gaps); each finding independently
refuted-or-confirmed by a skeptic agent before it counts.

- [x] U1 `calc/` — numeric edge cases, cache-once invariant (D-011)
- [x] U2 `kg/` — pr_gate, git_submitter arg/path safety, cross-process lock
- [x] U3 `mcp_servers/` — validation of LLM-controlled args, fpstore SQL
- [x] U4 `bo/` + calc interface — objective/constraint correctness
- [x] U5 `memory/` + `eln/` + kg interface — ingest validation, cursor/idempotency
- [x] U6 `agents/` + `report/` (embedding cycle) — authz gates, audit chain, retrievers
- [x] U7 `service/` + agents boundary — every route through `_resolve_session`,
      SSE lifecycle, budget
- [x] U8 `workflows/` + `workers/` — determinism, retry/idempotency, heartbeats
- [x] U9 `evals/`, `sources/`, `scripts/` — light pass (thin-test areas)
- [x] Dedicated security reviewer re-walks risk-map targets 1–5.
      **Review outcome (2026-07-23)**: 73 raw findings → 23 refuted by skeptics →
      **50 confirmed** (13 S2, 30 S3, 7 S4) + 11 S3/S4 whose verifiers hit the usage
      limit (fix agents re-verify those before acting). No S1. Determinism re-walk of
      workflow datetime usage produced no finding. Findings archive:
      scratchpad/confirmed_findings.json + unverified_findings.json.

## Wave 3 — Bug fixes (S1/S2, batched per package with their S3/S4 siblings)

- [x] Batch A (kernel, calc, kg, bo, mcp_servers): 28 findings fixed, committed as
      five scoped commits (2e7148c kg, 4a47a07 calc, b23df5c mcp, 2e317b2 kernel,
      ef9bce9 bo). Combined tree gated green: lint + mypy strict clean,
      551 passed / 16 Temporal-only skips (43 new behavior tests over baseline).
- [x] Batch B (eln/memory, report/agents, service, workflows, evals/scripts,
      Wave-4 residuals): completed as commits 9eade98 (wave-4 residuals),
      dcae2d1 (evals/scripts), 79625a1 (eln/memory), 82b8723 (report/agents),
      50fc856 (workflows), 4af678b (service). Combined tree gated green:
      lint + mypy strict clean, 610 passed / 17 Temporal-only skips
      (102 new behavior tests over baseline). All 60 findings resolved
      (59 fixed, 1 refuted).
- [x] Orchestrator verification of the 11 skeptic-orphaned findings: 10 confirmed,
      1 refuted (evals/baseline.py:70 — documented deliberate behavior).

## Wave 4 — Hardening (S3 + risk-map targets)

- [x] Fail-closed startup: refuse boot when `entra_required=False` AND bind address
      non-loopback, unless explicit `service_allow_insecure=true`; ADR in DECISIONS.md.
- [x] Ownership-boundary test enumerating session-scoped routes → each must funnel
      through `_resolve_session`.
- [x] `tool_authz_default`: deny-by-default for write tools or default gate set for
      `index_molecule`/job launchers; ADR either way.
- [x] `git_submitter`: enforce single-process ownership (advisory lock file or
      fail-fast on concurrent use).
- [x] Confirm/fix workflow-body `datetime` usage in `eln_sync.py`/`memory_jobs.py`.
- [x] Behavioral-test reinforcement for `test_authz.py`/`test_service.py` where
      feasible offline.
- [x] Apply remaining confirmed S3 findings.

## Wave 5 — Simplification / refactoring (S4, only on green)

- [x] Split `chemclaw/config.py` into cohesive sub-models, keeping the single
      `settings` import surface (no caller churn). → 18 mixin sections, 160 fields
      byte-identical, zero call-site edits (4afbada).
- [x] Break `agents ↔ report` cycle: move the embedding-provider seam to a neutral
      home so dependencies point one way. → `chemclaw/embeddings.py`, layering
      regression test in `tests/test_layering.py` (cca7b65 + a0009fc).
- [x] Apply confirmed S4 simplifications (dead params, single-caller abstractions
      inlined, DRY extractions) — landed inside the per-package fix commits.

## Wave 6 — Close-out

- [x] Full `make lint type test` + `make cov`; coverage ≥ Wave-0 baseline.
      → lint + mypy strict clean; 616 passed / 17 Temporal-only skips;
      coverage **89.60%** vs 88.43% baseline.
- [ ] Security-review pass over the whole branch diff — in flight.
- [x] Update `BACKLOG.md`, `DECISIONS.md` (ADRs D-067…D-072), `DEFERRED.md`; write the
      review section below.
- [x] Commit in logical chunks (kernel / fixes-per-unit / hardening / refactor) and
      push to `claude/code-review-refactor-plan-wm34wc`.

## Token-efficiency rules (bind all agents)

- Reviewers/verifiers return structured findings only (file:line, claim, concrete
  failure scenario, severity) — never file contents or diffs into the main context.
- Skeptics verify findings, not files; read only what's needed to confirm/refute.
- Style is out of scope (ruff owns it); dedupe by file:line before verification.
- Main context carries only plan state, confirmed-finding queue, and gate results.

## Review (close-out, 2026-07-24)

**Method.** 13 reviewer agents (3 kernel, 9 domain units, 1 security re-walk), every finding
independently attacked by a refute-by-default skeptic; only survivors were fixed. 73 raw →
23 refuted → 60 actioned (59 fixed, 1 refuted late). Fixes ran as 12 scoped agents in two
parallel batches plus two Wave-5 refactor agents, each gated by ruff + mypy strict + targeted
behavior tests before its commit; the combined tree was full-gated after every batch.

**Outcome.** No S1 existed. 13 S2 correctness bugs fixed (chemistry: wrong-charge xTB energies
cached forever, pKa charge inversion, cache-key/compute spelling mismatch; infra: git staged-residue
leak, sync cursor poisoning, DSN password leak, Entra deny-all half-config, retrieval eligibility
drift, Nextflow transient/terminal conflation). 4 risk-map hardening items landed (fail-closed
startup D-067, write-tool gates D-068, submitter flock D-069, ownership-boundary sweep test).
Structure: config split into 18 sections, agents↔report cycle broken with a layering guard.
Coverage 88.43% → 89.60%, tests 508 → 616, all green. ADRs D-067…D-072; new follow-ups and
deferrals recorded in BACKLOG.md / DEFERRED.md.

**Lesson captured.** Long-running parallel fix batches survive session usage limits cleanly when
each agent's scope is disjoint and committed independently — resumed agents (SendMessage) and the
workflow journal cache made both interruptions lossless.
