# Consolidate the memory landscape, then close what the audit found

**Session:** 2026-08-28 · branch `claude/kemplaw-memory-investigation-lj1fzg`

A deep investigation of every memory in this system (model context, turn state, transcript,
scratchpad, durable memories, preferences, observations, knowledge) produced four measured
defects and one consolidation. Four parallel audits verified each against HEAD and against a live
Postgres; every claim below was re-checked by hand before being queued.

## A — the context policy: the budget is a trigger, not a control

- [x] A1 `agent_keep_last_conversation_groups` -> `ge=0`, default `0` (no group floor).
      Measured at the shipped defaults, 2,000 prose groups: retained goes 1,944 -> 99,954 tokens
      against a 100,000 budget. The regression the `max()` arm exists to prevent stays closed:
      20 x 60kB groups still cut to 90,366, not 180,180.
- [x] A2 Correct the four measurably false sentences (compaction heading, `trigger` field,
      `apply` docstring, and `core/config/agent.py`'s "raising N no longer raises what a request
      can cost" — off by 50x).
- [x] A3 `.env.example` mirrors the new default.
- [x] A4 Test: the budget binds at the shipped defaults; a floor still bites when a deployment
      sets one.

## B — erasure returns a false green

- [x] B1 A digest mailbox row is keyed `digest-<owner>` and has no `session_owners` row, so
      `_ERASE`'s session_events arm cannot match it: a departing person's unread digests survive
      while the report prints `session_events: 0`. Erase them, both actor spellings.
- [x] B2 The completeness test's actor vocabulary is a hardcoded six names; `audit_anchors.reseal_by`
      is a seventh and escapes it. Teach the vocabulary the name and classify the table.

## C — one rule per table, in one place

- [x] C1 `_RETAINED` implies "refused": three of the seven retained tables say *nothing bounds it,
      no decision on record* while the same argument governs all seven. Fix the reasons and derive
      the test from `_RETAINED` instead of hardcoding four names.
- [x] C2 `store_vectors`' disposal reason states a deployment fact, not a decision; `scratchpad.py`
      logs two tables where `setup()` made one.
- [x] C3 `STORE_TABLES` has no upstream-derived backstop (the checkpointer's does). A LangGraph
      minor adding a store table would escape both registers with every test green.
- [x] C4 `session_owners` disposal silently requires `retention_tool_results_days`: with only a
      messages window set, no session that ever called a tool is disposable. Make it visible.

## D — the landscape itself

- [x] D1 `infra/sql/README.md`: `bo_campaigns` reads "nothing bounds it" where the code refuses it
      and a test pins the refusal; the Disposal legend delegates to a BACKLOG row that no longer
      exists.
- [x] D2 The chart's retention example omits one of the five windows.
- [x] D3 Planning files: a row with its own trigger filed in BACKLOG; two pairs of rows describing
      one subject from two files; four stale anchors (one names a class that does not exist, and a
      docstring in `src/` repeats it).
- [x] D4 ADR + ledger row.

## Verification plan

`make lint type test` green with Postgres up (baseline captured before any edit), plus the
compaction measurement re-run against the new default and the erasure test proving the digest row
goes.

## Review

**All fourteen items are done.** Nothing was descoped; two things were deliberately *not* built and
each says so below rather than being dropped quietly.

**What changed behaviourally.** Three of these are user-visible and the rest are the registers and
the prose that describe them:

1. A long conversation now sends the model up to the token budget instead of twelve turns. Measured
   at the shipped defaults, 2,000 prose groups: 1,944 -> 99,954 tokens retained. The arm that
   *bounds* the thread is untouched — 20 groups of 60 kB still cut to 90,366 against a 100,000
   budget, not to the 180,180 the count-only version left.
2. A departing person's unread digests, and their id inside a publication payload, no longer
   survive an erasure that reports success.
3. Erasing one person no longer deletes a tool result another person's session still links.

**How the erasure fixes were proven.** The three new tests were run against the *pre-fix*
statements (restored at runtime, no file reverted) and all three fail; against the fix, all three
pass. A test that passes both ways proves nothing, and this file's own history says so.

**Two things deliberately not built.**

- *An orphaned `tool_result_links` row is beyond erasure permanently.* It names a session id no
  ownership row resolves, so what it keeps alive is unattributable rather than somebody's, and the
  age sweep collects the link with its blob. Recorded in the ADR as an accepted consequence rather
  than filed as work.
- *`delete_session` and the retention prune take their two rows in opposite orders.* The BACKLOG row
  asked for them to be ordered consistently; examined, that is not available — each order is
  required by its own invariant, and reversing either trades a one-statement deadlock window for a
  correctness bug. The row now records that the obvious fix was tried and rejected, which is the
  contribution the row was worth.

**One audit finding was wrong and the baseline is what showed it.** A subagent reported
`tests/test_deploy_chart.py::test_the_fleet_ceiling_...` as failing on a clean tree. The baseline
run taken before any edit was **5,444 passed, 11 skipped, exit 0**. Prose about a test is evidence
about what its author believed; the run is the evidence.


---

## Follow-up: the review of this change (2026-08-28)

Four adversarial passes over the merged diff. **Three defects in my own change, all now closed**,
two false claims in its ADRs retracted in a new one, and one gap filed with its measurement.

- [x] `jsonb_array_elements` on a non-array aborted the whole erasure — guarded, parametrized over
      the five shapes Postgres refuses.
- [x] The leaver's own orphaned link spared the leaver's own blob, under a report reading `0` —
      the anti-join goes through `session_owners` now, paired with the test proving another
      *person* still spares it.
- [x] A `BACKLOG.md` section heading was deleted with a moved row, silently re-filing three rows.
- [x] A test fixture that outlived its own test and was measured by the next one.
- [x] `unwindowed_ownership_dependencies` claimed to be derived and was not — a test now joins the
      two maps; and its `session_events` entry named a window that cannot unblock it.
- [x] Corrected: the "three tables said no decision is on record" quotation (two did), the 90,366
      figure (90,090 on the fixture the suite builds), the ordering row's overgeneralisation from
      erasure to `delete_session`, and the counts in the unbounded-tables row.
- [x] Hardened: the register assertion tested an English substring; the shipped compaction default
      was pinned only by fixtures it could outgrow; a declared column nothing read now has to exist.
- [ ] **Filed, not fixed**: no deployment declares `llm_context_window_tokens`, so the ~30k prefix
      is uncharged and a request now measures ~135,700 against a configured 100,000 — and
      `_record_overrun` cannot see it. Three candidate fixes, one of which changes what
      `agent_context_token_budget` means. That is a decision with an owner.
