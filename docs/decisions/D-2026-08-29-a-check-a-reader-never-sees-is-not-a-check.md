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
2. **A chemist's abandonment was reverted 20 times out of 20** — no row lock between `append`'s read
   of the status and its write of it, under READ COMMITTED, against `advanced()`'s promise that
   `abandoned` is held. `FOR UPDATE`: 20/20 held.
3. **A ban on a solvent could not be enforced**, because `_named_species` never read
   `setpoints.solvent` — the field a solvent lives in, and the commonest hard exclusion there is.
4. **The bench document printed the body's conditions over the arm's.** A single experiment got no
   run sheet, so a design running at 120 °C in toluene rendered as 80 °C in dioxane, every blocker
   passing. `setpoints_for` fell back whole-object, so a partial override blanked the rest.
5. **A plate could hold one arm twice** — a set comparison can only see an arm that is missing.
6. **One request blocked the event loop for 46 s** — `labels.count()` in a comprehension over a list
   with no bound, chosen by the browser. 2.61 s at the same payload, and the payload is now refused.
7. **Fifteen leaf fields never reached the document**, `base.waste` and every unit among them.

## Consequences

- One new module (`core/turn_text.py`), one migration (078, the index the unfiltered listing needs),
  one new error type, and length bounds on every list in a design.
- `structure_experiment_request` loses a parameter. A schema shrank and a docstring grew to correct
  a promise it had backwards; net +7 tokens, recorded.
- `history()` no longer reads documents: 90.6 ms → 15.7 ms over 40 revisions.
- Four checks that reported findings as passes now fail, so nine tests asserting the old behaviour
  were updated rather than deleted — each says which belief it used to encode.
- **The structural cause is recorded and not fixed here:** no test anywhere imports
  `render_markdown`, `run_sheet_rows` or `summarise`. The whole assertion surface for the module a
  chemist reads is two lines in a tool test. That is a `docs/planning/BACKLOG.md` row.
