# Backlog features — assessment + implementation plan

Source: `BACKLOG.md` (every open item) + the open rows of `DEFERRED.md` it points at.
Plan with the per-item assessment: **`docs/backlog-plan.md`**.
Branch: `claude/backlog-feature-assessment-dytr10`.

## Assessment (done)

- [x] Enumerate every open backlog item (31) and check each claim against the tree, not memory.
- [x] Assess each against the five questions (trigger held? real defect? offline-verifiable?
      KISS/Rule of Three? cost vs value) → BUILD / DEFER / DROP / BLOCKED.
- [x] Write `docs/backlog-plan.md`: verdict table, specs for the survivors, triggers for the rest.
- [x] Correct the backlog entries whose claims are false today (the DROP verdicts).

Result: **8 BUILD · 14 DEFER · 5 DROP · 12 BLOCKED**. No BUILD item needs live infra.

## Stale entries corrected in `BACKLOG.md`

- [x] F4-T5 "per-request role → skills scoping" — already delivered by D-052
      (`agents/skill_access.py::RoleScopedSkillsSource`, wired at `agents/chemclaw_agent.py:139`).
- [x] 1b.5 Temporal lookup/persist activities — folded into 1c.5 by design; checkbox never cleared.
- [x] `QMJobWorkflow`→`CalculationWorkflow` rename — dropped, not deferred: the workflow type name
      is durable-history state, so the rename is the exact un-versioned change C1's policy forbids.
- [x] OKF per-bundle `log.md` — dropped *as designed* (concurrent note branches all append to one
      file = manufactured merge conflicts); redesign as a generated view recorded with its trigger.
- [x] Design caution "apply skills/tools selectively, measured per task" — satisfied by `evals/ab.py`
      (2b.4) + `AgentProfile` (D-075); nothing left to build.
- [x] F0-T4 stand-in-server variant — dropped (would test the stand-in; the client-wiring half is
      already proven by `tests/test_harness_execution.py`, D-058). Live-endpoint half stays blocked.

## Build queue (specs in `docs/backlog-plan.md` §3)

### Wave A — silent failures made visible [S]
- [x] **A1** ELN late-file detection — aggregated WARNING when a file arrives after the cursor with
      an older payload timestamp (`eln/adapter.py` helper + both adapters). Tests: `test_eln.py`.
- [x] **A2** Deployment docs: `CHEMCLAW_ENTRA_REQUIRED` for exposed deployments, removed
      `CHEMCLAW_ENTRA_CLIENT_ID`; comment pinning the background worker at `replicas: 1`.
- [x] **A3** Eval-drift alert visibility — WARNING at emission + documented read procedure.
      Tests: `test_eval_drift.py`.

### Wave B — real defects [S]–[M]
- [x] **B1** Substructure matching off the event loop (`asyncio.to_thread` + wall-clock bound,
      `substructure_match_timeout_seconds`). Tests: `test_molfp.py` incl. loop-responsiveness.
- [x] **B2** Emit `PlanEvent` + `JobStartedEvent` (both currently dead types; closes D-042).
      Ambient job sink + changed-only plan emission. Tests: `test_runner.py`/`test_service.py`. ADR D-077.
- [x] **B3** Supersede memory notes on cluster merge/shrink (`memory/supersede.py`, bi-temporal
      `valid_to` + plain-text replacement id). Tests: `test_memory.py`. ADR D-078.

### Wave C — needs a decision first
- [x] **C1** Workflow-versioning policy doc + deploy checklist (no CI guard, on purpose). ADR D-079.
- [x] **C2** Chemical safety screening, minimum viable slice — implemented under the plan's stated
      defaults (advisory-only, committed SMARTS table, hard-failing `kg-validate` gate). All three
      are config, so reversing any of them is an env change. ADR D-080.

## Verification
- One commit per item; `make lint type test` green after each.
- CHECKMATE (G1–G7) after wave B, and again after C2 — B3 and C2 both touch the GxP surface.
- `BACKLOG.md`/`DEFERRED.md` updated at the end of each wave.

## Review — implementation (all eight items shipped)

Seven commits, each green under `make lint type test`; the suite went **624 → 684 passing**
(41 offline skips unchanged). One commit per item, revertable on its own.

| Item | Commit | What landed |
|---|---|---|
| A1 | `d23d2fd` | Late-arriving ELN exports warn by name instead of vanishing (`eln/adapter.py` helper, both adapters) |
| A2+A3 | `7fb9fdb` | Boot-blocking settings documented; drift alerts logged at WARNING; three stale runbook statements corrected |
| B1 | `cf13334` | Substructure matching moved to a worker thread under `substructure_match_timeout_seconds` |
| B2 | `f2e083a` | `PlanEvent` + `JobStartedEvent` emitted (D-077) — the two dead event types are now live |
| B3 | `6d96da2` | Memory notes retired on cluster merge/shrink (D-078) |
| C1 | `b2ea2d4` | `docs/workflow-versioning.md` + deploy checklist (D-079) |
| C2 | `744c265` | Advisory hazard screening: rule table, tool, skill, `kg-validate` gate, recall metric (D-080) |

**Decisions taken during implementation, not in the plan:**

- **B3's supersede predicate is `valid_to is None`, not `is_current`.** The plan said "still
  current"; that made the future-`valid_from` clamp unreachable dead code and left the idempotence
  argument implicit. Testing for an unset end date is simpler, makes re-runs provably idempotent,
  and covers not-yet-valid notes.
- **B2 announces only genuine launches.** The idempotent re-submit branch returns a possibly
  already-completed job that will never push back; announcing it would leave a permanently
  "running" row in the UI.
- **C2 shipped under the plan's default answers** to its three open questions rather than blocking,
  since each is config-reversible and the gate has nothing to break today (no procedure notes in
  the corpus). Flagged for confirmation below.
- **Rule-table SMARTS were verified against parsed molecules, not read.** Perchlorate and
  permanganate needed `~` (any-bond) patterns because RDKit sanitizes them to charge-separated
  forms — a double-bond pattern would have silently never fired.

**Guards that fired and were fixed properly, not around:** `SafetyRulesError` had to join the
non-retryable bad-data list (`test_publish.py` walks every `ChemclawError` subclass), and
`screen_hazards` had to be added to the expected in-process tool set (`test_tool_registry.py`).
Both existed to catch exactly this kind of omission.

**Still open for the user** (unchanged from the plan's §5): confirm C2's scope defaults; decide
whether to give the Neo4j tipping point a measurable trigger.

## Review — assessment pass

The three findings worth flagging:

1. **Six backlog lines were claims about the tree that are no longer true** — the largest being
   F4-T5's "per-request role scoping" open item, which D-052 delivered. Assessing before building
   removed more work than it added.
2. **Two "deferred polish" items are actually defects**, and both were sitting under headings that
   read as done: the substructure match blocks the shared event loop (B1), and memory cluster
   merge/shrink leaves stale notes as current knowledge with no supersede link (B3).
3. **The one genuinely large item (C2, safety screening) is the one the user parked for a decision**,
   and its own precondition — "decide before a phase that could propose a hazardous procedure" — is
   already past, since BO recommendations and development reports both publish procedures today.
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
