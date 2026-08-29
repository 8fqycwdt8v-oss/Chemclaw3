# D-2026-08-29-a-check-a-reader-never-sees-is-not-a-check — what six fresh-context reviewers found

**Decision.** Six independent reviewers, none of which wrote this code, went over the prescriptive
tier after two of my own review cycles had already been through it. They found **more than both of
those cycles combined**, including defects those cycles introduced. All are fixed, here and in
`Chemclaw3_ui#57`.

**The finding that outranks the rest:** cycle one's headline was *a check that could not fail*, and
cycle two's was *a fix whose stated cost was paid by a record that did not exist*. This one is
sharper than either. **A check can fail and still be invisible.** `render_markdown` and `summarise`
both list failed checks only — so four checks returning `_ok` on a *finding* put "checked and fine"
in front of a reader about a species nobody resolved, a charge table nobody can weigh out, a
reaction nobody could read, and a screen covering a quarter of its grid. `coverage_is_stated` had no
`_fail` in any branch at all: its only substantive sentence, the one naming what a reduced design
confounds, could reach a reader through **no** rendering path. `_unreadable`'s own docstring
describes that exact defect as fixed, in the same file, four branches away.

## What the reviewers could see that I could not

Every one of them was told to prove a finding with a runnable script and to report what it attacked
and could **not** break. The second half is what makes the first trustworthy, and it is also where
the most useful result came from: `layout.py` survived an exhaustive sweep — `row_label` against an
independently written base-26 oracle for rows 0–701, every well of every format, `place()` across
every format × size × seed mode with the run order a true permutation every time — and the grant
matrix was probed **live**, against a real role applying the real grant file, which is the first
evidence those privileges behave rather than merely being written down.

The reviewers also found the three defects my own two cycles left behind or created, which is the
argument for the whole exercise:

- `arms_are_distinct` told a chemist to mark a repeat with `replicate_of` — which **my** cycle-2
  validator refuses when the setpoints differ. Each was right about a different definition of "the
  same conditions".
- **My** cycle-1 `_labelled` fix moved the misattributed-edit failure rather than removing it:
  `#<position>` shifts, so deleting an unrelated line reported "toluene volume 5.0 → 2.0", an edit
  nobody made, 33 paths for a one-line deletion.
- **My** cycle-2 `advanced()` demoted an approval on a byte-identical revision, justified by "the
  document has changed", with my own test appending the same object and asserting `draft`.

## The seven that reach a chemist

1. **`basis="stated"` was self-graded.** It obliges the chemist's verbatim words, checked against
   `source_text` — a tool argument the model fills. `core/turn_text.py` carries it ambiently now, on
   `session_context`'s stated argument for the session id, stamped by the two paths a chemist's words
   arrive on. Absent means refused. The parameter is gone, so the attack cannot be attempted.
2. **A chemist's abandonment was reverted 93 times out of 100** — no row lock between `append`'s
   read of the status and its write of it, under READ COMMITTED, against `advanced()`'s promise
   that `abandoned` is held. Five runs of twenty: 20, 18, 18, 20, 17. `FOR UPDATE`: **0 of 100**,
   five runs of twenty with no loss at all. This first went into the record as "20 out of 20",
   which was one real run reported as a property — see the postscript.
3. **A ban on a solvent could not be enforced**, because `_named_species` never read
   `setpoints.solvent` — the field a solvent lives in, and the commonest hard exclusion there is.
4. **The bench document printed the body's conditions over the arm's.** A single experiment got no
   run sheet, so a design running at 120 °C in toluene rendered as 80 °C in dioxane, every blocker
   passing. `setpoints_for` fell back whole-object, so a partial override blanked the rest.
5. **A plate could hold one arm twice** — a set comparison can only see an arm that is missing.
6. **One request blocked the event loop for 46 s** — `labels.count()` in a comprehension over a
   list with no bound, chosen by the browser. **The `max_length` ceilings are what fix that, not
   the `Counter` that replaced the scan**: the 46 s payload can no longer be posted, the largest
   design the ceilings now admit serialises to 414 KB and diffs in 0.060 s, and at that ceiling
   (n=1536) the quadratic scan costs 22.4 ms against `Counter`'s 0.107 ms.
7. **Fifteen leaf fields never reached the document**, `base.waste` and every unit among them.

## Consequences

- One new module (`core/turn_text.py`), one migration (080, the index the unfiltered listing needs),
  one new error type, and length bounds on **the six lists a diff keys by an identifier** — the
  only ones whose cost was superlinear. The per-item collections below them (`Factor.levels`,
  `ProtocolArm.levels`, `ProtocolStep.components`, `Analytic.measures`, `EvidenceRef.supports`)
  stay unbounded on purpose: they are walked linearly and bounded by the body cap, and a ceiling
  with no cost behind it is a refusal a chemist meets for no reason. "Bounds on every list in a
  design" is what this line said first, and it was not true.
- `structure_experiment_request` loses a parameter. A schema shrank and a docstring grew to correct
  a promise it had backwards; net +7 tokens, recorded.
- Four checks that reported findings as passes now fail, so nine tests asserting the old behaviour
  were updated rather than deleted — each says which belief it used to encode.
- **The structural cause is recorded and not fixed here:** no test anywhere imports
  `render_markdown`, `run_sheet_rows` or `summarise`. The whole assertion surface for the module a
  chemist reads is two lines in a tool test. That is a `docs/planning/BACKLOG.md` row.

## The eighth, found by the fourth review and fixed here

**A sign-off named whatever the head had become.** `set_status` read `head_revision` at the moment
it ran, so a chemist who opened revision 1, thought about it, and clicked Approve after a colleague
saved revision 2 had their name recorded against a document they had never read — in the very table
`D-2026-08-29-a-sign-off-names-a-revision-or-it-names-nothing` added so that the revision signed
could be known. The ADR is right and the write did not honour it.

`set_status` now takes a required, keyword-only `expected_revision` and refuses anything else with
`RevisionConflict`; the route answers the same 409 and the same machine-readable code the revision
route does, and `Chemclaw3_ui` sends the revision on screen. It is `append(parent_revision=…)`'s
twin and was always meant to be.

**It needs no race, which is why it is worth more than the concurrency defects beside it.**
Measured as plain latency — read, wait, click — it is **100 of 100** across five runs of twenty,
and 0 of 100 with the comparison. Driven as a true `asyncio.gather` race it reproduced **0 of 100**,
because the two statements serialise on the pool: had I gone looking for it as a race, as the
review described it, I would have concluded there was nothing there.

## Postscript: a fourth review, over these fixes

A fourth cycle of fresh-context reviewers read the diff above rather than the code it changed, and
**falsified three numbers this ADR had already published.** They are corrected in place above; what
they have in common is worth more than any of them.

- *"20 out of 20"* was one run reported as a property. Five runs of twenty measure 20, 18, 18, 20,
  17 — the race is overwhelmingly likely, not certain, and a reader planning around "always" would
  have been planning around the wrong shape. (The lock still holds 0 of 100.)
- *"2.61 s at the 4 MB cap payload, down from 46 s"* described a payload that the same commit had
  made unpostable. The `Counter` is a constant-factor tidy-up; the ceilings are the control. Two
  fixes shipped together and the credit went to the wrong one.
- *"90.6 ms → 15.7 ms"* for a header-only `history()` reproduces at 384 arms and not at 24, and
  that method was reverted in this same cycle for a correctness reason — so the line survived the
  change it described.

**The pattern is one instrument used once.** Each number was real when it was taken, and each was
then written down as a property of the system rather than as one observation of it. A measurement
taken once, in one configuration, immediately after the change it is meant to justify, is the most
flattering measurement available — and this repository's own rule ("measure it, don't argue it")
does not say how many times. It does now, for the class of claim that ends up in an ADR: **a
number that is going into the record is run more than once, and reported as what varied.**

The corresponding lesson is in `tasks/lessons.md`; the reason it belongs in an ADR too is that
three of these had already passed a review cycle. Prose about a measurement is evidence about what
its author believed, exactly as `CLAUDE.md` says of prose about code — including when the author
is the one who took the measurement.
