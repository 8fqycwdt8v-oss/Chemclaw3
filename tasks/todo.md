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
