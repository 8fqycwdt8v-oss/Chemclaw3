# Refactor, Hardening & Simplification Plan

Requested 2026-08-02. Planning branch: `claude/codebase-review-refactor-3lbnjs`.

Goal: turn ChemClaw3 from a codebase assembled across many simultaneous agent sessions into one
a human team can deploy, configure, maintain and extend with confidence — robust, maintainable,
performant, bug-free. This document is the master plan; the work packages below are written to be
handed to implementation subagents (model tier named per package). It is a companion to, not a
replacement for, `BACKLOG.md` and `DEFERRED.md` — items already tracked there are cross-referenced,
not copied.

## What the review actually found (the framing that matters)

Nine parallel review agents (one mechanical metrics scan + eight deep dives, three on Opus) swept
every layer against a **green** baseline (`make lint type test`: 2789 passed, 104 sandbox-skipped).
The headline is that **this is not a vibe-coded mess**: 97.5 % docstring coverage, zero bare
`except`, zero `Mock`/`@patch` in 204 test files, CI-enforced config↔`.env.example` parity, 165+
ADRs, an adversarial full-codebase review already behind it, and a 190-probe live run. Most "obvious"
defects are already fixed or already tracked.

The real debt is therefore **structural**, concentrated in ~5 hot files that many sessions edited at
once, plus a small set of genuine correctness/security bugs the breadth of this sweep surfaced:

1. **Two data-loss bugs** (A1, A2) and **two security Mediums** (Sec-1, Sec-2) are the only findings
   that touch a user directly. They lead the plan regardless of the refactor.
2. **`core/config.py`** (143 importers, #1 churn) is the file that serializes all parallel work.
   Splitting it (R6) is the single highest-value unlock.
3. **Four misfiled modules** cause nearly every layering violation (R1/R2/R4) — cheap moves, large
   dependency-graph payoff.
4. A recurring anti-pattern across independent reviews: **guards that are armed but not enforced** —
   a stale hand-maintained module list in the layering test, the "one authz gate" convention, an
   entrypoint-vs-config default duplication, a Temporal error-name match that silently misses, and
   the arm-with-a-destructive-default in A1. The plan lands the *tests* that convert each convention
   into an enforced invariant.

Verdict on shape: **13 packages is right — no merges, no splits, no `agent/` restructure.** The tree
is sound; four files sit one layer too high and one test list is hand-maintained.

## Ground rules for implementation sessions

- **Risk appetite: aggressive.** Internal layouts, signatures and file splits may change freely.
  The invariant is `make lint type test` green **and** documented behavior preserved.
- **One work package = one small PR, auto-merged when green** (per the repo ship rule), so `main`
  stays current for parallel agents. Skip auto-merge only if CI is red or the change is ambiguous.
- **The offline gate is not the whole gate.** The sandbox skips 70 Postgres, 21 Temporal and 13
  xtb/crest tests. CI (pgvector Postgres + Temporal test server + `helm`/`kubeconform`) is the real
  arbiter — do not claim "green" for DB/Temporal/chart work from a local run alone. Once WP-R1.3
  lands, `make ci` mirrors the CI sequence.
- **Model routing.** Haiku = mechanical; Sonnet = standard implementation; Opus =
  architectural/security-critical; **Fable = the highest-judgment packages (subtle correctness where
  a wrong call loses user data, security-boundary design) plus the per-phase orchestration/synthesis
  and the final completeness-critic role.** Each phase has a Fable orchestrator that scopes the
  phase's WPs, dispatches subagents, reconciles their PRs (shared-file conflicts, ordering), and runs
  a completeness critic before closing the phase.
- **Bug fixes ship a mutation-proven test** (remove the fix, watch it fail), per this repo's standing
  bar. Performance changes record a before/after number (lessons.md: "an optimizer change is
  worthless until it is timed").

## Findings register (evidence for each work package)

Severity in brackets; size XS/S/M/L. `file:line` verified by the reviewing agent against current
code. Sec-N are security; A/H/S/P are api+agent; R-N are architecture; F-N are ops/deploy/docs.

### Correctness — the two data-loss bugs (top priority)

- **A1 [High/S] Failed watermark read → full history wipe.** `api/runner.py:236-248,441` +
  `agent/session_store.py:249-262`. `rollback_to(session_id, None)` runs `DELETE … id > 0` — the
  *entire* conversation — when the watermark *read* failed, which is indistinguishable from a
  genuinely empty history. `None` is armed with a maximally destructive default. Fix: track
  `watermark_read: bool`; skip the durable rollback when unread (in-process restore + the existing
  `message_pairing` repair still heal the orphan); make `rollback_to`'s watermark non-optional so the
  ambiguity cannot return.
- **A2 [High/S] An answered turn is rolled back if the verifier or mid-turn resume is slow.**
  `api/runner.py:373-380,414-427`; `agent/verifier.py:225` (the judge LLM call has **no timeout**).
  `answered = True` is set only after `_answer_event()`; a disconnect or the wall-clock deadline
  landing in the verifier/resume window takes the rollback branch and deletes an exchange `agent.run`
  already committed. Fix: a `run_complete` flag set right after the run stack closes gates the
  rollback; keep `answered` for the cost ledger (the two questions have different right answers).

### Security (each traced entry → sink)

- **Sec-1 [Medium/S] Prompt-injection envelope is forgeable and one path skips it.**
  `agent/framing.py:20-27` interpolates content verbatim between `<retrieved-note …>` tags that
  `agent/chemclaw_agent.py:184` tells the model to trust. Three escapes: (a) content is not escaped —
  a `</retrieved-note>` in a note or attachment ends the envelope; (b) `list_attachments`
  (`agent/attachments.py:308-318`) returns the raw excerpt **unframed**; (c) the `note_id` attribute
  is `file.filename` straight off the upload. `agent/verifier.py:160-166` already states the
  escalation rule attachments landed without. Fix: per-turn nonce delimiter + strip/escape the tag
  from content, frame the `list_attachments` excerpt, sanitize the filename to a basename.
- **Sec-2 [Medium/XS] Connector client leaks user identity on redirect (SSRF-shaped).**
  `connectors/registry.py:255-272`: `follow_redirects=True` + the identity-stamp event hook re-adds
  `X-Chemclaw-Actor` (Entra `oid`) and the role set on every redirect hop (verified against httpx
  0.28.1 — only `Authorization` is stripped cross-origin). All seven manifests ship `auth: none`. Fix:
  `follow_redirects=False`, or make the stamp hook a no-op when the host is not the manifest's.
- **Sec-3 [Low/XS] Session ownership fails open on a NULL owner.** `api/app.py:793,804`:
  `owner is not None and owner != oid` makes a NULL-owner row readable/writable by every principal,
  surviving a flip to `entra_required`. Fix: NULL owner → 404 under `entra_required` (mirror the
  `_is_reviewer` split).
- **Sec-4 [Low/XS] `git add` receives note paths as argv with no `--`.** `kg/git_submitter.py:249` —
  a leading-dash path passes the containment guard and reaches git as an option. Add `--`.
- **Sec-5 [Low/S] Connector MCP apps install no request-body cap.** `_BodySizeLimit` is front-door
  only; a connector consumes an unbounded body before auth runs. Lift it to a shared module and
  install in `connector_app`.
- **Sec-6 [Low/S] Redaction inventory is blind to two held credentials.**
  `CHEMCLAW_KNOWLEDGE_REPO_TOKEN` (git push; no `settings` field) and per-connector bearer tokens —
  a traceback logging `os.environ` leaks them. Extend `_secret_values()` with a resolver for both.

### API + agent (concurrency, hardening, decomposition)

- **A3 [Med/S]** turn cleanup lives only in the SSE generator body, which may never start → the
  in-process `active_turns` entry never expires → a permanent 409 for that session (the durable claim
  self-heals; the in-process one has no lease). Fix: make the in-process guard a deadline lease like
  the durable one. **Premise corrected in execution (R3.1): there were two release sites, not one** —
  the SSE generator's `finally` and `post_message`'s, mutually exclusive via a `handed_off` flag —
  and the surviving leak was the narrower window that runs neither: a client gone after the
  streaming response is handed off but before its generator is first advanced, so no `finally` ever
  runs. The lease fix shipped as planned; `api/state.py::_claim_turn_slot`'s docstring records the
  corrected mechanics.
- **A4 [Med/M]** the push-back stream mutates live session state concurrently with a turn
  (`api/app.py:1219-1224` + `agent/harness_todo.py:108-121`); a disconnect's `state_snapshot` restore
  can discard a job completion. Fix: record completions durably, apply at turn start.
- **A5 [Low-Med/XS]** the session LRU can evict and re-mint a handle mid-turn → two diverging handles.
  Pin `active_turns` ids against eviction.
- **A7 [Med/S]** one unparseable historical message bricks a session forever
  (`agent/session_store.py:187`, bare `from_dict` on every turn's read). Fix: a `format_version`
  column + per-row quarantine using the existing repair. *(Carries a retention decision — queue with
  H7 behind the BACKLOG retention row.)*
- **A6 [Low/XS]** a shed turn sends its retry event before releasing the claim → an honest retry hits
  409.
- **H1 [Med/S]** the "one authorization gate" is convention, not a test. Add a `CurrentUser`
  annotated dependency and a test that walks `app.routes` and asserts `require_principal` on every
  non-probe route. **Do this before the app.py decomposition** — it makes the split provably
  gate-preserving.
- **H2 [Med/XS]** bound the verifier LLM call with `asyncio.timeout(settings.verifier_timeout_seconds)`
  (ships with A2).
- **H3 [Med/S]** `service_uvicorn_workers > 1` silently multiplies five per-process guarantees (rate
  limiter, budget, attachment store, session LRU, metrics registry). Refuse it in `Settings`
  validation until they have a shared story (the entrypoint already tells operators to use replicas).
- **H4 [Med/S]** the global `METRICS` gauges close over one `create_app` instance's `app`; bind them
  in the lifespan and clear on shutdown.
- **H5/H6/H8 [Low]** claim-heartbeat failure handling past the lease; a `scope` label splitting
  process vs durable turn conflicts; a `service_serve_static` flag for the unauthenticated static
  mount.
- **S1 [L]** `create_app` decomposition (1068 lines, 38 nested defs) into
  `app.py`/`state.py`/`deps.py`/`schemas.py`/`middleware.py` + `routes/` ×8. The closure idiom
  survives because every closure reads `app.state` and the tests reach `app.state` — **no test
  changes**. Full target layout is in the API+agent agent report.
- **S2 [M]** five hand-rolled bounded-LRU maps (`api/app.py`, `budget.py`, `rate_limit.py`,
  `metrics.py`, `attachments.py`) → one `core/bounded.py::BoundedLru`. **Corrected in execution
  (R3.3): four, not five** — `core/metrics.py`'s label-series cap is refuse-new (past 64 series a
  new key is dropped and logged once), and folding it into an evict-oldest LRU would convert
  cardinality protection into series churn, so it stays its own mechanism. The four callers are
  `api/state.py` (where the `api/app.py` map now lives after the decomposition), `api/budget.py`,
  `api/rate_limit.py` and `agent/attachments.py`. **S3 [S]** three copies of
  "unknown-or-not-yours → the same 404" → one helper. **Corrected in execution: two, not three** —
  `api/deps.py::_visible_proposal` encodes a reviewer-may-see-any allowance (via `_is_reviewer`,
  a role check) that sessions and approval holds have no analogue for; folding it into
  `_refuse_unless_owner` would either grant reviewers session access or lose them proposal access.
  Its docstring argues this at the site. **S6 [XS]** the dry-run ContextVar misfiled in
  `dialogue_tools` (so `connectors/` imports a tool module for a turn flag) → beside the other turn
  ambients. **S7 [XS]** a private `_session_connection` imported across modules → make it a named seam.
- **P1 [S]** past the compaction floor, every turn does `COUNT` + full `SELECT` + a complete re-plan
  (`agent/session_store.py:295-347`); reuse rows in hand and re-plan only on crossing a new floor
  multiple. **P2 [XS]** row-at-a-time rewrites where `executemany` exists two methods over.
- **agent/ package: do NOT restructure** (44 flat single-subject modules + README is the right shape).

### Architecture conformance (the structural backbone)

- **R1 [S]** move `api/metrics.py` → `core/metrics.py` (414 lines, stdlib-only, kernel material).
  Fixes the real layering break (`core/worker_http.py:49` imports `chemclaw.api`), lets the
  `metrics_bridge` lazy hack be deleted, and resolves the `api/metrics` vs `evals/metrics` name clash.
  **Corrected in execution (R2.1): `metrics_bridge` was kept, deliberately.** Its `try` wraps
  `update(METRICS)` — the *update*, not the import — so it guards the 11 `record_metric` call sites
  across six packages from a typo'd counter name (`core/metrics.py` raises `KeyError` on an
  undeclared name by design) propagating into a caller's request path. Only the lazy *import*
  was deletable, and went; the module's own docstring now records what it always actually was.
- **R2 [M]** move the ambient-context primitives `agent/{identity_context,tool_registry,turn_signals}.py`
  + the id-half of `agent/session_context.py` → `core/` (three of four have zero first-party deps;
  imported by up to seven packages). Deletes the `kg→agent` and `core→agent` edges and the two
  lazy-import workarounds; cuts `templates→agent` 5→1 and `connectors→agent` 10→4.
- **R3 [M]** rewrite `tests/test_layering.py` as a **derived allow-list**: AST-walk all of `src/`,
  assert the package adjacency is a subset of a declared allowed-edge set with a one-line reason per
  non-obvious edge. Two hand-maintained lists (`_CORE_MODULES` missing three modules; the false
  "nothing imports cli" premise) are exactly why the `worker_http` break was invisible.
- **R4 [S]** move the library halves of `cli/schedules.py` and `cli/verify_audit_chain.py` →
  `durable/`, leaving `main()` shims in `cli/` (they are durable-layer code, and `api`/`durable`
  already import them).
- **R5 [XS, measured bug]** four error classes bypass `ChemclawError` (`ConnectorError`,
  `DataSourceError`, `TemplateError`, `UnresolvedReference`). Running the Temporal failure converter
  disproves the `template_activities.py:150-157` docstring: `'ConnectorError'` is not in
  `_BAD_DATA_TYPES`, so a bad-data error burns all five retry attempts instead of failing on the
  first. Reparent them, add the names (the completeness test then demands it), correct the docstring.
- **R6 [M-L]** split `core/config.py` → a `core/config/` package, one module per existing mixin,
  `Settings` composed in `__init__.py`, `settings` singleton and the `from chemclaw.core.config import
  settings` import path unchanged. #1 churn file, 143 importers — the highest-value parallel-work
  unlock, and D-156 already blessed the split as "cheap whenever wanted".
- **R7 [S, latent bug]** `evals/retrieval.py:133` calls `asyncio.run` inside a metric helper; it
  raises the moment an eval is scored from a coroutine, and `durable/eval_drift.py` imports it into
  the 66 %-async durable layer. Make the metric contract async or run retrieval on a dedicated loop.
- **R8/R9 [XS/S]** stale docstrings (`src/chemclaw/__init__.py` lists the deleted `mcp` package;
  `core/__init__.py` claims "just the typed configuration") + an Entry-points table in
  ARCHITECTURE.md (today `deploy/entrypoint.sh` is the only place the four process roles are
  discoverable). Do **not** add re-export `__init__`s — each is a new graph edge.

### Science, connectors, ingest, retrieval, tests, ops

- **Science-1 [Med/S]** orphaned child processes on subprocess timeout (`science/calc/xtb_cli.py:342-354`,
  `crest_cli.py:220-234`): `subprocess.run(timeout=)` with no `start_new_session`; CREST forks
  workers that survive the kill. One shared Popen + process-group-kill helper.
- **Science-2 [Low/S]** `_HARTREE_TO_KCAL` copied into six files, `pka.py:68`'s copy drifted
  (627.509 vs 627.5094740631); `_ANGSTROM_TO_BOHR` ×3. One constants module (extend `xtb_engine.py`,
  the documented unit boundary).
- **Science-3 [Low/S]** `pka.py:351-407` acid branch not decomposed like its base branch — extract
  `_predict_acid_pka`, leave `predict_pka` as dispatch.
- **Science-4 [Med/S]** BO propose path has no error translation (`bo/engine.py:184-202`); GP-fit
  failures propagate raw botorch exceptions to the Temporal activity, unlike the module's own
  `_fractional_design` precedent.
- **Science-5 [Low/S]** safety rule tables compile lazily on first request; add a startup eager-load
  or a `safety-validate` make target so a shipped YAML typo fails at deploy, not on the first live
  hazard question.
- **Conn-F1 [Med/S]** `connectors/jobs.py:373-412`: `start_workflow` errors propagate raw to MAF while
  `connect` failures get friendly framing — widen the try/except. **Conn-F2 [Med/S]** BO activities
  have no heartbeat at all (`bo/workflows.py:64-93`) — add `heartbeat_timeout` + in-loop heartbeats
  mirroring calc (also the third instance justifying a shared heartbeat helper). **Conn-F5 [Low/XS]**
  `CHEMCLAW_CONNECTOR_URLS` keys unvalidated against discovered bundles — add a check to
  `cli/validate_connectors.py`. **Conn-F7 [Low/XS]** dead `XtbJobInput.requested_by/.session_id` +
  `xtb_job_key()` — delete. **Conn-F4** MCP exception-sanitizing — needs a spike to confirm FastMCP's
  default first.
- **Ingest-1 [High/XS, execution-verified]** `ingest/eln/ord_adapter.py:236-252,363-365`: `_components`
  and `_amount` crash the whole sync batch on a malformed nested field (list where dict expected →
  uncaught `AttributeError`), violating the module's own "one bad message is rejected, never a crash"
  contract. Add the `isinstance` guards the sibling helpers already use.
- **Retrieval-1 [Med/M]** `retrieval/vector_index.py:267-286` `reindex_notes` re-embeds the entire
  corpus hourly with no changed-since tracking. Add stat-fingerprint incrementality (pattern exists
  in `kg/graph.py`) + an explicit `--full` recovery path.
- **CLI-1 [Low/S]** exit-code convention split (7 files `main() -> int` + `raise SystemExit` vs 5
  files `main() -> None` + `sys.exit`); standardize on the testable form.
- **Test-1 [High/M]** three Postgres persistence classes have zero direct tests
  (`PostgresArtifactStore`, `PostgresCampaignStore`, `PostgresTurnCostSink`) — D-011 territory.
  **Test-2 [Med/S]** `test_layering.py` spawns 121 subprocesses where 11 suffice. **Corrected in
  execution (R1.1): both numbers were wrong.** The real prior count was 132 (a 12-module × 11-sibling
  parametrization), and the shipped fix is neither number but a derived shape: one subprocess per
  core module plus one per retrieval module, each checking every forbidden sibling in that single
  process, with the module lists derived from disk (`tests/test_layering.py`'s docstring records
  the arithmetic).
  **Test-3/4/5 [Low]** `_free_port` ×3 → conftest; parametrize four invalid-SMILES tests;
  `test_deferred_register.py:59` `> 15` false-fails when the backlog is paid down.
- **Ops-F5 [High/XS]** `Makefile:92-95` `helm-validate` pipe has no `pipefail` — a broken
  `helm template` yields zero docs and `kubeconform` exits 0, greening CI on an unrenderable chart.
  Add `SHELL := bash` + `.SHELLFLAGS := -eu -o pipefail -c`. **Ops-F1 [Med/XS]** docker-compose
  Temporal UI vs the README quickstart both bind 8080 — **resolved in R1.3:
  `infra/docker-compose.yml` binds the Temporal UI at `8081:8080`.** **Ops-F7/F8 [Med]** `image.yml` re-implements
  `make deps-audit` by hand (already diverged); `make check` ≠ the CI gate — add a `make ci`
  meta-target. **Ops-F12 [Med/XS]** syft installed from `main` unpinned in the SBOM step. **Ops
  [XS]** F2/F3 onboarding gaps, F4 `.env.example` quickstart banner, F9 CI concurrency groups, F10
  workflow permissions, F11 uv cache, F13 helm pin, F6 `.PHONY`, F16 Containerfile layer order, F14
  expand/contract migration note, F17 extend prose-validate to the Makefile.

Verified clean (do not re-litigate): all 20 routes gated; SQL fully parameterized; subprocess argv /
`shell=False` / `_safe()`; no pickle/eval/exec; `yaml.safe_load`; `extra="forbid"`; path traversal
guarded; DoS clamps present; no hardcoded secrets; bundle scaffolding is the deliberate engine/wrapper
split (≈42 residual lines behind two shared helpers — no extraction warranted); zero non-determinism
in any workflow body; migrations checksummed with an advisory lock and `IF NOT EXISTS` throughout;
the Helm chart is in better shape than the checklist.

## Phasing

Each phase runs behind a Fable orchestrator. Each work package is one auto-merged PR.

### R0 — Correctness & security first (highest priority; no refactor dependency)

| WP | Model | Scope |
|---|---|---|
| R0.1 | **Fable** | A1 + A2 + H2 — rollback protects only what it should; bounded verifier. +test that disconnects during the answer. |
| R0.2 | **Fable** | Sec-1 prompt-injection envelope (nonce delimiter design + frame `list_attachments` + filename sanitize). |
| R0.3 | Opus | Sec-2 connector redirect identity leak. |
| R0.4 | Sonnet | Sec-3/4/5/6 (NULL-owner 404, git `--`, connector body cap, redaction resolver). |
| R0.5 | Sonnet | Science-1 process-group kill helper + Science-4 BO error translation + Conn-F2 BO heartbeat. |
| R0.6 | Sonnet | Ingest-1 isinstance guards (execution-verified crash) + R5 error-class reparenting + R7 evals asyncio.run. |

### R1 — Convert armed-but-unenforced guards into tests + ops one-liners (parallel)

| WP | Model | Scope |
|---|---|---|
| R1.1 | Sonnet | R3 derived allow-list layering test (folds in the 4 core-module gaps + the cli premise). |
| R1.2 | Sonnet | H1 `CurrentUser` alias + route-coverage test (prerequisite for the R3-phase decomposition). |
| R1.3 | Haiku | Ops one-liners: F5 pipefail, F1 port, F6 `.PHONY`, F9 concurrency, F10 permissions, F11 uv cache, F12 syft pin, F13 helm pin, F7 dedupe deps-audit, F8 `make ci`. |
| R1.4 | Haiku | Core-4 entrypoint-default parity test + H3 refuse `workers>1` + H6 conflict scope label + Test-5 threshold relax. |
| R1.5 | Sonnet | Test-1 three Postgres classes + Science-5 safety validator/eager-load. |

### R2 — Structural moves that unblock parallelism (serial; discrete PRs)

| WP | Model | Scope |
|---|---|---|
| R2.1 | Opus | R1 move `api/metrics.py` → `core/metrics.py` (+ delete metrics_bridge hack — **executed as: delete the lazy import only; the defensive swallow stayed**, see R1 above). |
| R2.2 | Opus | R2 move ambient-context primitives → `core/`. |
| R2.3 | Sonnet | R4 move cli library halves → `durable/` (shims in cli); + S6 dry-run ContextVar relocation. |
| R2.4 | **Fable** | R6 split `core/config.py` → `core/config/` package (import path unchanged) — the wide-blast-radius unlock. |

### R3 — Decompose the two hot files (after R1.2 + R2)

| WP | Model | Scope |
|---|---|---|
| R3.1 | **Fable** | `api/app.py` decomposition per the delivered layout (app/state/deps/schemas/middleware + routes/×8); gate-preserving. |
| R3.2 | **Fable** | `runner.py` split (runner/trace/usage/answer) + A3 lease + A5 pin + A4 pending-completions — the concurrency cluster. |
| R3.3 | Sonnet | S2 `core/bounded.py::BoundedLru` (consolidates the 5 maps) + S3 no-leak-404 helper. |

### R4 — Per-package cleanup (fully parallel once R6 lands)

| WP | Model | Scope |
|---|---|---|
| R4.1 | Sonnet | science/: constants module (Science-2) + pka decomposition (Science-3). |
| R4.2 | Sonnet | retrieval/: reindex incrementality (Retrieval-1). |
| R4.3 | Haiku | cli/ exit-code unification (CLI-1) + Conn-F7 dead fields + Conn-F5 URL-key validator. |
| R4.4 | Sonnet | Conn-F1 start_workflow framing + Conn-F4 MCP exception-sanitizing (after the spike). |
| R4.5 | Haiku | Test-2 121→11 subprocesses (**executed in R1.1 as a derived allow-list; real prior count was 132**, see Test-2 above) + Test-3 `_free_port` → conftest + Test-4 parametrize invalid-SMILES + P2 executemany. |

### R5 — Docs & onboarding (parallel, low-risk)

| WP | Model | Scope |
|---|---|---|
| R5.1 | Sonnet | R8/R9 stale docstrings + Entry-points table in ARCHITECTURE.md. |
| R5.2 | Sonnet | Ops F2/F3/F4 onboarding + F14 migration note + F17 prose-validate over the Makefile. |
| R5.3 | **Fable** | Final integration + completeness critic: `make ci`, import-graph diff vs the R0 baseline, re-check every "verified clean / deferred" claim, write the closing ADR(s), update BACKLOG/DEFERRED/lessons.md. |

**Fable's share:** 6 of ~24 WPs (R0.1, R0.2, R2.4, R3.1, R3.2, R5.3) + the per-phase orchestrator
role. Everything mechanical or localized is Haiku/Sonnet; everything architectural-but-bounded is Opus.

## Deferred / not doing (with reasons)

- No package merges or splits (13-package layout verified sound); no `agent/` restructure; no
  re-export `__init__`s (each is a new graph edge).
- Naming drift (`get_`/`load_`/`find_`/`fetch_`) is cosmetic — a convention for new code, not a
  rename churn.
- Conn-F3 (three "not-found" conventions) is documented and locally justified — no code change.
- H7 (GET-mutates-history repair) and A7 (message quarantine) carry a retention decision — queue them
  behind the existing BACKLOG retention row rather than ahead of it.
- The agent-pool duplication (S9) stays DEFERRED — its trigger is an upstream MAF fix.

## Verification per work package

- Every WP: `make lint type test` green locally, and the full CI gate (`make ci` once R1.3 lands),
  including the Postgres/Temporal/chart jobs the offline sandbox skips.
- R0 bug fixes each ship a mutation-proven test: A1 — a disconnect with an unreadable watermark keeps
  the history; A2 — a slow verifier keeps the answer; Sec-1 — a forged delimiter is neutralized;
  Sec-2 — a redirect drops the identity headers; Ingest-1 — a malformed `amount` skips one record,
  not the batch.
- Structural moves (R1/R2/R6) are behavior-preserving: the derived layering test (R3) plus the full
  suite are the proof; diff the import graph before/after.
- Performance work (P1, retrieval reindex, the layering subprocesses) records a before/after wall
  clock.
