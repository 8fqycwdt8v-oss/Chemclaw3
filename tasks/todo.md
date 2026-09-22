# Wave 8 — the mutation backstop's own coverage, the prose surface, and the organic line

## Items

- [x] **Pair the mutation selection with the source paths it claims to cover** (`826ba28c`).
- [x] **Copy the schema tree, and guard the list that says what is copied** (`c35ff891`).
- [x] **Widen the model-facing prose guards to the surface** (`0e60f65c`). ADR + ledger + row.
- [x] **Widen `_is_organic`, bump to std10** (`1051dc2e`). ADR + ledger + row.
- [x] **Set the gate's numbers from the first completed run** (`ee3d691f`).
- [x] **Three fresh-context subagent reviews**, one per change. All three found blockers.
- [x] **Act on the chem review** (`a013679b`): the nitrogen count alone split the cyanamides.
- [x] **Act on the mutation review** (`b1d3e21a`): the job could not finish and failed silently.
- [x] **Act on the prose review** (`f2a3e4f6`): exemptions could swallow real prose.
- [x] **Full serial `make cov`** on the reviewed tree: **10,578 passed, 8 skipped, 1 failed**,
      coverage **89.93%** against a floor of 84.0, 34m31s. The one failure was
      `test_dead_vocabulary.py::test_no_adr_written_after_a_word_died_uses_it_unargued` — the new
      prose ADR names `PR-gate`, which is retired vocabulary, and the word is the guard's own
      pattern. Argued in `_ARGUED` rather than reworded.
- [ ] PR, and merge when CI is green.

## What the reviews caught

**Mutation gate.** `timeout-minutes: 60` against an 87-minute run, and a job-level timeout
*cancels* — so every step was skipped by `!cancelled()` and the notification fired only on
`failure()`: no stats, no artifact, no gate, no issue. The floor's derivation named two methods and
wrote a third number. Both gates exited on the first failure reached, which is the uninformative
one in the canonical case. The `also_copy` guard was blind to files and dotfiles. The pin held
`source_paths` and not the test selection — the lever that actually moved the rate.

**`_is_organic`.** Counting nitrogens alone put the cyanamides on the organic side, where
`[Ca+2].[N-]=C=[N-]` neutralised to the carbodiimide tautomer and `[Na+].[NH-]C#N` to the nitrile
one: calcium cyanamide took free HN=C=NH's `compound_id`. "Driven over twenty species" reproduced
nowhere and the real class is 29 wide. The ADR's thesis — a latent defect is the cheapest moment to
bump — is false on the mechanism: a bump retires *every* row under the old definition.

**Prose guards.** An exemption could form across a line break and exempt whatever followed.
`_A_LIVE_GATE` had become an exemption over nothing. `_SAFETY_BLOCKS` was outside the universe,
which for a profile turn is the entire system prompt. The poison test was blind to a loader
returning *less*. Six of this wave's own measurement claims were wrong.

## Measurements this wave rests on

- Mutation, first completed run: 2,260 killed of 3,640 (**62.1%**), 446 no-test (12.3%), 68
  timeouts, 0 suspicious/segfault, 87 min at 0.70/s. Before pairing the selection: 1,612 killed
  (44.3%) with 1,009 no-test, same 3,640 mutants, no code change.
- Prose, today's universe (187 texts, six classes): the pre-existing patterns produce 10
  occurrences over 8 texts, all correct. Widening the promise vocabulary back: 14 occurrences over
  9 texts, at least 12 correct — declined.
- `_is_organic`, 125 species: 29 change class, every new answer chemically right; every defended
  inorganic case holds; the shipped reagent table is unchanged in all 68 structures.

## Review

Three reviews, three sets of blockers, all fixed in their own commits with the measurement that
found them. The pattern worth keeping: **every one of the nine prose findings and four of the chem
findings were wrong *numbers in my own prose*, not wrong code.** The code was defensible each time;
the claims about it were not. `_THE_ORGANIC_LINE` and the per-class counts in the prose universe
are the structural answer — a drive that lives in a test cannot be overstated in a commit message.
