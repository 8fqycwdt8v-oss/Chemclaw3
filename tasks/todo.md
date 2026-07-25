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
- [ ] **A1** ELN late-file detection — aggregated WARNING when a file arrives after the cursor with
      an older payload timestamp (`eln/adapter.py` helper + both adapters). Tests: `test_eln.py`.
- [ ] **A2** Deployment docs: `CHEMCLAW_ENTRA_REQUIRED` for exposed deployments, removed
      `CHEMCLAW_ENTRA_CLIENT_ID`; comment pinning the background worker at `replicas: 1`.
- [ ] **A3** Eval-drift alert visibility — WARNING at emission + documented read procedure.
      Tests: `test_eval_drift.py`.

### Wave B — real defects [S]–[M]
- [ ] **B1** Substructure matching off the event loop (`asyncio.to_thread` + wall-clock bound,
      `substructure_match_timeout_seconds`). Tests: `test_molfp.py` incl. loop-responsiveness.
- [ ] **B2** Emit `PlanEvent` + `JobStartedEvent` (both currently dead types; closes D-042).
      Ambient job sink + changed-only plan emission. Tests: `test_runner.py`/`test_service.py`. ADR D-077.
- [ ] **B3** Supersede memory notes on cluster merge/shrink (`memory/supersede.py`, bi-temporal
      `valid_to` + plain-text replacement id). Tests: `test_memory.py`. ADR D-078.

### Wave C — needs a decision first
- [ ] **C1** Workflow-versioning policy doc + deploy checklist (no CI guard, on purpose). ADR D-079.
- [ ] **C2** Chemical safety screening, minimum viable slice — **blocked on the §5 scope questions**
      (advisory-only? committed SMARTS table vs external DB? hard-fail `kg-validate`?). ADR D-080.

## Verification
- One commit per item; `make lint type test` green after each.
- CHECKMATE (G1–G7) after wave B, and again after C2 — B3 and C2 both touch the GxP surface.
- `BACKLOG.md`/`DEFERRED.md` updated at the end of each wave.

## Review

Assessment pass only; no feature code written yet. The three findings worth flagging:

1. **Six backlog lines were claims about the tree that are no longer true** — the largest being
   F4-T5's "per-request role scoping" open item, which D-052 delivered. Assessing before building
   removed more work than it added.
2. **Two "deferred polish" items are actually defects**, and both were sitting under headings that
   read as done: the substructure match blocks the shared event loop (B1), and memory cluster
   merge/shrink leaves stale notes as current knowledge with no supersede link (B3).
3. **The one genuinely large item (C2, safety screening) is the one the user parked for a decision**,
   and its own precondition — "decide before a phase that could propose a hazardous procedure" — is
   already past, since BO recommendations and development reports both publish procedures today.
