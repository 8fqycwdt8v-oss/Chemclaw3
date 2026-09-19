# Making a stale constraint fail a test

Investigation found: the constraint corpus blocks feature work not because the rules are wrong but
because nothing can say when a *no* stopped applying. 71 ADRs decline a class of work; 4 state a
condition for revisiting. All 377 statuses read `accepted`. 327/680 ADRs are in no index.

## Chemclaw3

- [x] `tests/test_dead_vocabulary.py` — a dead term must have a marker section, and no ADR written
      after a term's death may use it unargued. Replaces the hand-listed GxP roster.
- [x] `docs/decisions/README.md` — marker sections for the PR-gate and the provider collapse;
      GxP section reduced to the rule plus the substance list (the 51-file roster is a `grep`).
- [x] `docs/decisions/README.md` — two stale rows: "BaseStore is not adopted", "spend cap ships at 0".
- [x] `tests/test_declines_carry_a_trigger.py` — an ADR newer than the cursor that declines must
      carry `Revisit when:`.
- [x] `tests/test_claude_md_figures.py` — a figure in CLAUDE.md must resolve to a symbol.
- [x] `CLAUDE.md` — cut the changelog (69–400) to present-tense state; fix the specialist
      contradiction, the Bash/`pyexec` sentence, the dead Schedule/PR prohibition, LangSmith,
      `science/labels`; delete every transcribed figure.
- [x] `CLAUDE.md` — the ADR procedure gains: a defect fix is a commit and a test, not an ADR;
      a decline carries `Revisit when:`.
- [x] `src/chemclaw/agent/authz.py` — two comments claiming `synthesize_memory` opens pull requests.
- [x] `src/chemclaw/durable/schedules.py` — the same dead constraint.
- [x] `docs/planning/DEFERRED.md` — delete the answer-revision row (shipped); fix the literature
      reopen condition ("Nothing." against D-135).
- [x] `docs/planning/BACKLOG.md` — delete the stale deadline-ratio row; checkbox the five invisible
      sections; fix five rotted anchors; move the trigger-gated rows to `DEFERRED.md`.
- [x] `docs/archive/findings-2026-08.md` — it is a record, so its 221 rows stop being checkboxes.
- [x] ADR + ledger row.

## Chemclaw3-mcp

- [ ] `CLAUDE.md` — the PR-gate row, the 7-file tree (the test requires 13), the port census,
      "two rows" → three, the guard's 6 → 9 entry points, "seven servers".
- [ ] `docs/adding-a-server.md` — the last line names a table a test forbids.
- [ ] `docs/BACKLOG.md` — the empty §7, the Rxn-INSIGHT row carrying its own refutation.
- [x] ADR + ledger row.

## Verification

- [ ] `make lint type test` green in both repos; new tests fail before the fix and pass after.

- [x] `tests/test_decision_log.py` — the unfiled-arrears ratchet (231 may only shrink) and the
      topic cursor moved back to the earliest date it covers for free.

## Review

**The finding that changed the shape of the fix.** The registers were healthy and the sibling repo
was clean, so the diagnosis is not "too many rules" — it is that nothing in the machinery can say a
decision stopped applying. 71 declines, 4 with a revisit condition; 680 ADRs, all `accepted`, none
superseded; 19 tests over the record that enforce referential integrity and never validity. The cure
already existed for one dead vocabulary and had itself gone stale, by hand-listing files. So three of
the four new rules are ratchets and the file roster is gone.

**What a written trigger is worth, measured.** `D-092` stated a precise reopening condition, `D-135`
met it, and nothing happened. Every claim this change makes about `Revisit when:` is therefore
scoped to the trigger being *written* — CLAUDE.md, the ADR and the test all say so rather than
implying a watcher exists.

**Two things the work found about itself.** The dead-vocabulary and decline ratchets both caught the
ADR written to introduce them, on their first run after it landed — the cleanest available proof they
discriminate. And the full gate caught a documentation edit: `test_the_after_model_call_cap_...`
greps CLAUDE.md for a literal lowercase phrase, and the rewrite started that sentence, capitalising
it. A deliberate prose coupling, doing its job within one run.

**Left undone, deliberately.** `test_a_warm_parse_forkserver_...` is red at 117.65 MiB against a
ceiling of 112, and red on `origin/main` at 117.59 — a closure that grew under the ceiling, not a
regression here. Re-deriving `FORKSERVER_POD_COST_MIB` is a pod-sizing measurement against a real
front door, so it is a queue row rather than a commit on a documentation branch.

**Not attempted.** Extending `_TOPIC_CURSOR` backwards over the whole corpus: the arrears are 231
rows of judgment, and a ratchet that can only shrink is the honest intermediate. `BACKLOG.md`'s
section ordering — `## Everything else` sits above five topic sections — was noticed and left.
