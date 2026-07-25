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
- [x] Security-review pass over the whole branch diff → 9 findings (1 S2
      introduced by the campaign's own kg fix, 6 S3, 2 S4), all fixed or
      documented (D-073). Final gate: 625 passed / 17 skips, coverage 89.64%.
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

---

# Task — deep capability-gap analysis (missing features), 2026-07-25

**Ask:** find features that are *missing* and would benefit the infrastructure — agent capability,
knowledge, tool integration, scheduling — across the whole codebase, not only the named areas, plus
free ideation on topics not yet on any plan.

## Plan

- [x] Read the persistent memory (`BACKLOG`/`DEFERRED`/`docs/audit/00`,`08`,`09`) first, to establish
      what is already catalogued — so the analysis adds signal instead of restating AG-*/KM-*.
- [x] Sweep the code by dimension, grounding every claim in a file/symbol: reachability (workflow ↔
      caller), deployment (Helm vs. runtime assumptions), scheduling + data lifecycle, agent/turn
      lifecycle, tool surface, knowledge schema.
- [x] Ideate beyond the mapped areas (topics no plan document mentions).
- [x] Write `docs/audit/12-capability-gap-analysis.md` — 34 findings, severity + effort + proposed
      shape each, sequenced into waves; plus an explicit "deliberately not flagged" section so the
      document cannot be read as contradicting `DEFERRED.md`.
- [x] Point `BACKLOG.md` at it (Proposals section, mirroring the Phase 8/9 convention: proposals,
      not executed work).

## Review

Analysis only — no behavior change, no source touched, so `make lint type test` is unaffected.
Verification was per-finding: every claim is a grep/read against the tree at `d77302e`, and the
document cites the file (and line, where it pins a specific statement) so a reader can falsify it
directly. Line citations were re-checked against the tree after drafting; two were off by a few
lines and corrected.

**The load-bearing result is a reframe.** The prior gap docs concluded the *engine* is sound and the
residue is about operating it at scale. That still holds — but the sharpest gaps are not in the
engine, they are at the seams around it:

1. Three built subsystems have no caller at all (`DevelopmentReportWorkflow`, `BoCampaignWorkflow`,
   the human side of the approval hold). All three read as finished — the backlog marks their phases
   complete — which is why nothing caught it.
2. The Helm chart cannot run the knowledge layer in either direction (no volume, no git-sync, no
   push credential). F6's "offline-verified" gate checked the chart is well-formed, not sufficient.

Those outrank every "add a capability" idea, because they are capability already paid for and
unusable.

**One pattern worth keeping.** Two independent findings turned out to be the same defect class —
prose naming capability the code lacks (`experiment-design/SKILL.md` → `BoCampaignWorkflow`;
`_INSTRUCTIONS` → impurity answers with no schema field). Invisible to mypy, to pytest, and to
`make skill-validate` (frontmatter only). IDEA-7 proposes the ~50-line CI check that catches it, and
notes it is the *deterministic* half of the AG-13 behavior eval — the half that does not need the
live LLM the deferral is waiting on.

**Method note for next time.** The reachability sweep (grep every `@workflow.defn` for a non-worker,
non-test caller) and the deployment sweep (grep every runtime filesystem/credential assumption
against the chart) both found Crit/High items in minutes and neither is in any existing checklist.
Both are cheap enough to run each phase; worth adding to the CHECKMATE G1–G7 routine.

---

# Task — implement the gap-closure plan (2026-07-25)

**Ask:** write an implementation plan for all findings in `docs/audit/12-capability-gap-analysis.md`
and implement it.

## Plan → `docs/gap-closure-plan.md` (phase F11, waves W0–W4)

Sequenced by *dependency* rather than the analysis's *value* ordering: config and schema first
(later waves read them), reachability before the capabilities it exposes.

- [x] **W0 deployment truth** — DEP-1 knowledge sync, DEP-2 push credential, DEP-3 MCP default,
      DEP-5 (found during implementation: image completeness + git + a Schedules Job), SCH-2 note
      reindex, RCH-3 the approval decision surface.
- [x] **W1 reachability** — RCH-1/RCH-2 durable tools, RCH-4/RCH-5 plan + job + proposal events,
      IDEA-7 the prose↔code CI gate. AGT-1 investigated and **withdrawn as false**.
- [x] **W2 chemistry** — KNW-1 date, KNW-2 purity/impurities, TOOL-2 identity resolution,
      TOOL-3 hazard screen + `process-safety` skill, TOOL-4 stoichiometry, TOOL-5 rendering.
- [x] **W3 (partial)** — SCH-3 overlap policy + deterministic jitter, SCH-1 retention.
- [ ] **W3 remainder** — SCH-4 schedule health, SCH-5 scheduled audit-chain verify, DEP-4 metrics,
      AGT-2 mid-turn resume.
- [ ] **W4** — the depth/ideation set (KNW-3…7, TOOL-1/6/7, AGT-3…6, SCH-6, IDEA-1…6).

## Review

Gate green throughout: ruff + `mypy --strict` clean, test count 601 → 700+, no test weakened or
skipped to pass. Four commits, one per wave, each independently green. ADR **D-074**.

**What implementing changed about the analysis.** Two corrections, both recorded rather than
quietly dropped:

1. **AGT-1 was wrong.** "No turn cancellation" rested on a grep for `CancelledError` finding
   nothing. The handling is structural, not by name, and was already correct. I verified by
   measurement before writing a fix, which is the only reason no time went into fixing a
   non-problem. The test that proves it is kept.
2. **DEP-5 was missed entirely.** Reading the Containerfile to implement DEP-1 revealed that
   `skills/`, `scripts/` and `evals/` were never in the image and `git` was never installed — a
   strictly worse finding than the one I went looking for (the agent had *no skills* in-cluster).
   The analysis had checked the chart against the code and never checked the image against either.

**The pattern worth keeping.** Both the reachability sweep (grep every `@workflow.defn` for a
non-worker, non-test caller) and the deployment sweep (grep every runtime filesystem/credential
assumption against the chart *and the image*) found Crit/High items in minutes, and neither is in
any existing checklist. `make prose-validate` is the same idea made permanent: it found a live bug
within a minute of first running. Worth adding all three to the CHECKMATE G1–G7 routine.

**Where I stopped, and why.** W3's remainder and W4 are ~25 items, several of them M/L (graph
analytics, networked MCP transport, file ingress, mid-turn resume). I implemented complete,
tested waves rather than starting a fifth and leaving it half-built — a half-wired metrics endpoint
or a partial compound-note migration would be worse than an honest boundary. The open items are
listed in `BACKLOG.md` with their original severity, unchanged.

## Continuation — W3 remainder + W4 (same session)

- [x] **W3 remainder** — DEP-4 metrics, SCH-4 schedule health, SCH-5 scheduled chain verify,
      AGT-2 mid-turn resume.
- [x] **W4a** — KNW-5 gap queries, KNW-6 type registry, KNW-3 negative results, IDEA-5 source
      tiers, IDEA-3 (tool half).
- [x] **W4b** — TOOL-1 networked MCP, SCH-6 merge webhook, KNW-7 compound notes, KNW-4 vocabulary.
- [x] **W4c** — AGT-5 clarifying questions, IDEA-4 dry-run, AGT-4 user preferences.
- [ ] **Open, with reasons in BACKLOG.md/DEFERRED.md** — TOOL-6 (needs a literature-source
      decision), AGT-3 (needs a first document format), IDEA-2 and IDEA-1 (sizeable, own design
      note), IDEA-6 (depends on AGT-3). TOOL-7 and AGT-6 closed as not-gaps after assessment.

## Review (continuation)

Gate green throughout: 696 → 755 passing, ruff + `mypy --strict` clean, `kg-validate`,
`skill-validate` and `prose-validate` all pass. Five commits. ADR **D-075**.

**The pattern that kept recurring, and is worth naming.** Three separate findings resolved into the
same rule: *a capability that cannot cover something must say so, or its silence reads as a
clearance it has not earned.* It shows up in `screen_hazards` reporting `unresolved` as prominently
as findings, in retention refusing `audit_events` rather than quietly skipping it, and in
`/schedules` reporting a never-applied Schedule rather than omitting it. Each was a place where the
honest-but-quiet implementation would have been actively misleading.

**Two findings closed as not-gaps**, which is a result rather than an omission: TOOL-7 (units are
already carried in field names; a `Quantity` type would be a one-caller abstraction) and AGT-6 (the
W1 tools' typed pydantic arguments already force a validated payload at the exact call site whose
absence justified the original deferral). Both are recorded in `DEFERRED.md` so they are not
re-opened blindly.

**Where I stopped.** Five items remain, and the split matters: three are blocked on a decision or a
prerequisite rather than on effort (TOOL-6 needs a literature-source choice; AGT-3 needs a first
real document format; IDEA-6 depends on AGT-3), and two are genuinely large enough to deserve their
own design note (IDEA-2 calibration, IDEA-1 standing queries). Building TOOL-6 against a guessed API
or IDEA-6 over a parser that does not exist would have produced confident-looking stubs — worse than
an honest boundary.

## Continuation 2 — the five blocked items (same session)

- [x] **IDEA-2** calibration ledger (migration 016, `calc/calibration.py`, two agent tools).
- [x] **IDEA-1** standing queries (migration 017, `agents/subscriptions.py`, `workflows/digest.py`).
- [x] **AGT-3** file ingress (`agents/attachments.py`, upload route, two agent tools).
- [x] **IDEA-6** corpus backfill (`scripts/backfill_corpus.py`, reusing AGT-3's parsers).
- [x] **TOOL-6** external literature (`report/literature.py`, via the F7 registry).

## Review (continuation 2)

Gate green: 755 → 774 passing, ruff + `mypy --strict` clean, all four validators pass. ADR **D-076**.

I had recorded these five as blocked. Asked to implement them anyway, the correct move was the one
D-057 already established here: **make the blocking decision explicitly and record it, rather than
defer a second time.** Each decision now lives in the module that embodies it.

**The decisions, and what made each defensible rather than arbitrary:**

- *Literature source → PubChem.* Not "an API I picked" but the only option clearing every constraint
  this repo actually has: licence-clean, credential-free, and structure-keyed so a hit joins on the
  key the fingerprint index already uses. Reaxys/SciFinder are not excluded — they are one sibling
  class each, which is what the F7 seam was built to buy.
- *Upload formats → a closed allowlist that refuses.* The refusal is the load-bearing half. A PDF
  "read" by scraping text-like bytes produces confident nonsense a chemist cannot tell from a real
  reading — worse than the gap it would close.
- *Backfill → verbatim, never summarized.* An LLM-summarized backfill would put thousands of
  unreviewed paraphrases into the corpus, which is the fastest way to make a graph untrustworthy.
- *Calibration → three figures.* Uncertainty coverage is the one a mean error cannot show, and the
  one distinguishing "imprecise but honest" from "precise-looking and misleading".
- *Digest watermark → advances after delivery.* A crash must re-report, never silently skip.

**A pre-existing test earned its keep.** `test_every_session_scoped_route_is_ownership_gated`
enumerates session-scoped routes rather than hardcoding them, and failed the instant the attachments
route appeared — forcing both an inventory update and a behavioural non-owner sweep over the new
route. That inventory-assertion pattern is worth copying to other route families.

**Phase F11 is complete.** Every finding in the analysis is implemented or explicitly closed, with
three (AGT-1, TOOL-7, AGT-6) withdrawn after assessment and recorded so they are not re-opened
blindly. What genuinely remains is unchanged and outside this environment: the live edges needing a
real tenant/broker/cluster, and the audit-trail archive-then-reseal design, which needs an ADR with
QA sign-off rather than a cleanup job.
