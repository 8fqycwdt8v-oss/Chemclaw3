# Finish the four points the ten-wave review left open — plan

The ten-wave review (`tasks/review-2026-09-19-ten-waves.md`) closed with four things it had
**not** done, stated rather than implied. This is the plan that closes them. Each is its own
PR, merged when CI is green, in the order they became ready.

The previous occupant of this file was the peer-handoff plan. Its twelve items were all
**unchecked and all shipped** — `agent/handoff.py`, `agent/turn_graph.py`, `active_agent`,
`agent_peer_roster`/`agent_max_handoffs`, `HandoffEvent`, the ADR and the multi-hop test in
`tests/test_turn_graph.py` all verified present before it was moved. A plan file whose boxes
are empty over finished work reads as live state, which is the defect `D-154` records about
`DEFERRED.md` rows describing shipped work, one register over. It is now
`docs/archive/plans/peer-handoff-plan.md`.

## OP4 — `helm-validate` end to end

- [x] 1. Install `kubeconform` and `promtool` (the runbook says how, and says so correctly) and
      run the target. **It passes**: Valid 31 and 35 over the two arms, the external-connector
      render OK, promtool parsing 219/220/220 rules over the three monitoring arms.
- [x] 2. What the first real run found: `_EXPECTED_SKIPPED_RESOURCES = 1` was a count of
      **resources** compared against `len(_UNVALIDATED_KINDS)`, a count of **kinds**, and the
      union arm reports `Skipped: 3`. The second skipped kind sat in the set whose stated reason
      was that it could not be skipped.
- [x] 3. Derive the count from the render per arm; measure it against kubeconform's own summary
      line; read the arms out of the `Makefile` rather than restating them.
- [x] 4. Three mutations watched failing (one deletion, two rewords). `make lint`, `make type`
      (919 files) green; 210 passed in `tests/test_deploy_chart.py` with the binaries present.
- [x] 5. PR #420.

## OP3 — the 49 remaining `model_copy(update=…)` fixtures

Wave 10 proposed a *mechanism* rather than a verdict: flag the call in any test whose subject
model declares `extra="ignore"` or `extra="forbid"`, because `model_copy` does not validate and
so a fixture built that way can supply its own subject.

- [ ] 1. Measure whether that heuristic actually separates the dangerous sites from the
      harmless ones over all 140 call sites, and if it does not, find the property that does.
- [ ] 2. Triage the flagged set; rebuild through the real constructor every fixture that
      supplies its own subject, and re-prove each guard by mutating what it guards.
- [ ] 3. Exemptions as an explicit allowlist with a reason at each entry, held in both
      directions.

## OP2 — `tasks/lessons.md` has regrown into the shape it was created to escape

Its own header: *"it was 1,937 lines across 80 sections, which is not readable at session
start, so it was not read, so the rules did not fire"* — and *"do not append a dated section,
that is how the old file grew"*. It is **3,212 lines**, and ~2,890 of them are dated sections.

- [ ] 1. Fold every dated entry's rule into the thematic sections; lossless in rules.
- [ ] 2. Archive the narrative to `docs/archive/lessons-2026-09.md`, as the first eighty are
      archived.
- [ ] 3. Build the mechanism the header itself prescribes for a rule broken repeatedly — here,
      the file's own prohibition on appending. Note that several existing headings carry their
      date in trailing parentheses, so a prefix match would have caught only some of them.
- [ ] 4. Check the citations: tests and modules quote this file by phrase and by rule number.

## OP1 — the delegation experiment has a comparator and no runner

`CLAUDE.md`: *"What is still open is whether delegation pays: `evals/delegation.py` has never
run against a model."* The comparator, the corpus and the ADR all exist; nothing constructs an
`ArmRun`, nothing records `delegated`, and there is no `no-helper` profile.

- [ ] 1. A runner in the shape of `cli/live_probes.py`'s `--suite ab`, not a second harness.
- [ ] 2. `delegated` observed off the turn's own record, never inferred from the arm's name —
      the baseline arm is behavioural because `task` cannot be removed.
- [ ] 3. Four arms: `no-helper`, `helper`, `helper-routed`, and the `peer` arm the BACKLOG row
      added after `D-2026-09-19-a-handoff-redistributes-the-turns-authority-it-cannot-extend-it`.
- [ ] 4. Prove it end to end against `cli/mock_llm` on loopback, driving all four compliance
      buckets — none of which any real run has ever exercised.
- [ ] 5. **No number from the mock may be reported as evidence about delegation.** A mock
      answer is evidence about the runner. This environment has no credential: `API-KEY` is
      empty and no gateway is configured, so the question stays open and the runner is what
      closes the gap to it.

## Review

(Filled in when the four are merged.)
