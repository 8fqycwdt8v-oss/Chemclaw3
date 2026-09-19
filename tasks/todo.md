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
