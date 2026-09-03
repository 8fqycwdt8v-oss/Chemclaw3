# D-2026-08-30-a-review-by-six-strangers-found-thirty-seven-defects — nobody owned a design

**Status:** accepted · **Date:** 2026-08-30

## Context

`D-2026-08-29-the-review-of-the-prescriptive-tier-found-fifteen-defects` was the fourth review-fix
cycle over the prescriptive tier and left the suite green at 233 tests. This is the fifth, run
differently: six subagents with **fresh context windows** over disjoint slices of the merged
feature, each told to demonstrate every finding by execution and to drop anything that did not
reproduce. The point of the fresh context is that the previous four cycles were run by the author of
the code, and an author re-reading their own fix reads the intention rather than the text.

They reported **67 findings**, of which 37 were defects in this repository and 8 in `Chemclaw3_ui`.
Every one below reproduced when re-run here before it was fixed.

## The finding that matters most

**Nothing decided who may write to a design.** `design_id_for` hashed the title, goal,
transformation and mode and nothing about who was asking, and neither write surface checked
ownership. Measured:

- A second chemist's *turn* reached the first chemist's design — same title and goal, different
  `oid`, different session — restructured her ask, demoted her `approved` header to `draft`, and
  replaced her two-arm plate with his own. `status_history` still recorded her sign-off at
  revision 2.
- Over HTTP it needed no collision at all: `post_status` and `post_revision` took `CurrentUser` and
  nothing else, so an unrelated principal **with no role** wrote `executed` into somebody else's
  status trail and then landed a revision on it as its author. The module docstring cites
  `POST /proposals/{id}/decision` as its precedent; that route calls `_is_reviewer` and this one
  called nothing.

Two halves, and neither works alone: owner-scoped ids stop the ordinary collision, and an ownership
gate stops an explicit `design_id` from reaching another chemist's design. The gate is
`owner_permits` — the one ownership rule this tree already has, now on its third caller — and the
HTTP answer is 403 for owner-or-reviewer rather than the sessions' 404, because a design is a shared
scientific artifact: its existence is not the secret, the right to change it is.

## What the other slices found

**The document a chemist runs from was the least tested thing in the tier.** No test anywhere
imported `render_markdown`, `run_sheet_rows` or `summarise` — the whole assertion surface across 472
lines was two lines checking that the page starts with a title and contains `## Evidence`. Nine
defects, and the first is a safety one: `## Conditions` renders the shared body whenever there is
more than one arm and the run sheet carried only temperature, time and solvent, so a design running
arm A2 at **50 bar H2** printed a page saying **1 bar N2**, with `H2` and `50` appearing nowhere and
no check firing. Beside it: `1.23457e+06` mg for a kilogram-scale charge (the fixed function's own
docstring example, which the fix it describes never closed), 999999.5 and 1000000.5 mg both printing
`1e+06`, `replicate_of` collected and never rendered, `FactorLevel.unit` dropped while the `Unit`
column showed the *factor's*, and free text able to forge a second `## Waste` section with
conflicting disposal instructions.

**Eight checks were wrong.** `forbidden_absent` read the *ask*, so the commonest request in process
chemistry — get me out of DMF, which names DMF as the incumbent and forbids it in one sentence — was
a permanent blocker on a design running in 2-MeTHF, and `draft_experiment_protocol` raises on any
blocker, so that design could never be stored. The equivalents tolerance was 2% of each line's own
figure and refused 4 of 18 correct tables. `coverage_is_stated` counted arms against a product of
level counts, so a screen covering two of four combinations reported "full grid: 4 of 4".
`quantities_are_plausible` read 2 of the 8 fields it claimed. Three checks keyed on `request.mode`,
so one mis-set enum on the intake switched off the plate-fits blocker, the controls warning and the
coverage note on a 96-arm plate.

**Two store defects were backend divergences of the kind `InMemoryDesignStore` exists not to have.**
An unpaired UTF-16 surrogate — which stdlib `json.loads` produces and pydantic only refuses on a
constrained field — was **500 on Postgres and 200 in memory**. And `GET /protocols/{id}` answered its
four halves from four transactions: measured with one concurrent `append`, **100 of 100** reads were
internally inconsistent, serving revision 1's document under a header saying head revision 2 with
revision 2 in the history beside it. One transaction is not the fix, which is the subtlety worth
recording: READ COMMITTED takes a new snapshot per *statement*, so four statements in one
transaction still tore 2/25. `REPEATABLE READ` is what makes "the history comes back in the same
call" mean anything.

**`basis="stated"` attested a quote and never the value.** Moving the haystack out of the model's
reach (D-2026-08-28) fixed who supplies the text and left untouched what `stated` means: only the
quote was checked, as a substring, and any substring occurs somewhere. Against a chemist who wrote
"We need to get the Suzuki on the deactivated chloride working. Try what you think.", a model stored
`scale='5 g'` quoting `'working'`, `plate_format='96'` quoting `'the'`, `max_runs='96'` quoting
`'Suzuki'` and `deadline='2026-09-01'` quoting `'.'` — four limits the chemist never named, recorded
as their own words.

## The rule this cycle leaves behind

**A test can stop reaching the branch it was written for, and nothing says so.** The audit slice
applied 21 source mutations that left the suite green. The sharpest was
`test_two_writers_racing_on_one_head_lose_as_a_revision_conflict`: it exists to prove the
`(design_id, revision)` primary key decides a real race and that the `UniqueViolation` is
translated, and it asserts, emphatically, that the loser "cannot pass by inheritance". The
`FOR UPDATE` added by a *later* ADR for a *different* defect serialises those two writers, so the
loser is now refused by the `parent_revision` comparison and never reaches the INSERT — replacing
that handler with a raised `AssertionError` leaves the whole suite green. The test still passes, its
docstring is now false, and no signal existed anywhere.

That generalises past this test: **a fix in one place can silently retire the coverage of a test
somewhere else**, and the only thing that finds it is mutating the source and watching what fails to
go red. Four of this cycle's own new tests were checked that way before being kept.

## Decision

Fix all 37, in five commits grouped by the surface they touch, each carrying the measurement that
found it. Two are deliberately *not* fixed and are `docs/planning/BACKLOG.md` rows instead:

- **A second sign-off at the same revision overwrites the first**, 100/100. `expected_revision` is a
  compare-and-set on the *document*, never on the *status*. Closing it means the caller stating the
  status it saw, which is a new field on `StatusIn`, on the store Protocol, on both backends and in
  `Chemclaw3_ui`'s sign-off panel. An optional field nobody sends would be a control that exists
  only in its docstring, which is the `map_to_hpc_identity` shape this tree deletes on sight. The
  half needing no contract change shipped: `require_movable` refuses `approved` and `executed` on a
  design holding only the structured ask, which was a lab record saying an experiment had been run
  against a document with no procedure in it.
- **A truthful `stated` quote from an earlier turn is unrepresentable**, because the ambient carries
  only the message that started this turn while the tool's own docstring says to call it
  iteratively. Widening it to the thread's user turns is a read on the runner's hot path; the
  anti-spoofing argument is unaffected, since prior turns are the chemist's own words too.

Four prose claims are corrected in place, each false in the reassuring direction: `models.py` on
which lists are bounded (it named as unbounded two collections this cycle bounds), a
`# pragma: no cover` over a reachable clause, `skills/protocol-generation/SKILL.md` on how many
blockers there are (seven, not one), and the race test above.

## Consequences

- `design_id_for` takes `owner` as a **required** keyword. An id that silently omits the owner is
  the defect itself, so a caller that forgets it must not compile.
- `find_experiment_protocols` and `read_experiment_protocol` leave the external MCP face. They meet
  all three of that list's own criteria — a named employee's identifier, free text a chemist typed,
  and ids derived from the ask — and a partner token issued to look up melting points read all of it.
- `is_single_experiment` and `is_plate` join `has_protocol` as single definitions on
  `ExperimentDesign`. `summarise` was the fourth caller spelling one of them out by hand.
- `tests/test_protocol_render.py` and `tests/test_protocol_authorization.py` are new; the store
  tests gained a concurrency probe that fails without the isolation level rather than without the
  transaction.
- This does not supersede D-2026-08-29's decisions. It corrects the sentence in that ADR claiming
  the grant matrix "was probed **live**, against a real role applying the real grant file" — no such
  probe is in the repository, and `tests/test_database_privileges.py` says in its own docstring that
  it needs no database. The privileges themselves were separately probed live in this cycle and hold.
