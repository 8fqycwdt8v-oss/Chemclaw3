# D-2026-09-13-a-digit-inside-a-word-is-not-a-figure-somebody-stated — the count a queue row asked for, and the third of the exposure that was not about attribution

A `basis="stated"` slot claims the chemist wrote this value, and `_quote_supports` is what stops a
model inventing one. A queue row recorded its honest limit — it cannot tell whether the figure a
quote carries is *about* this slot, so `max_runs='24'` quoting "24 wells" passes when the chemist
said 24 wells about the plate — and said what was owed first: **a count.** Over real turns, how
often does a `stated` slot's quote carry a figure that belongs to a different slot.

The count is below, and it found that a third of the exposure is not an attribution problem at all.

## What was measured

The corpus is `data/evals/probes/` — **295 chemist asks**, written to grade answers rather than this
rule, which is what makes them usable here. They are not a deployment's turns, and that is stated
rather than glossed: they are realistic asks authored for another purpose.

- **154 of 295** questions carry at least one figure; **94** carry two or more, so most of them
  already contain a figure that is about something other than any one slot.
- Distinct quotable figures: **537** over the corpus, median 2 per question carrying any, max 28.
- Every one of those is a figure a fabricated `stated` slot can be attributed to today: the
  three-word window around it is verbatim, and `_quote_supports` compares digits.

Then the part the row did not anticipate. Of those 537, **166 — 31% — were never quantities at
all**:

| what a chemist wrote | digits the rule credited it with |
|---|---|
| `COc1ccc(-c2ccccc2C(=O)O)cc1` | 1, 2 |
| `a C18 column` | 18 |
| `RRT 3.87` | 3, 87 |

So a chemist who pasted a **structure** was recorded as having capped the run count, and a retention
time of 3.87 minutes stated `max_runs='87'`. That is not "the figure belongs to another slot" — it
is a figure nobody wrote.

## The decision

**`_DIGITS` matches a figure, not a run of digits**: `(?<![A-Za-z0-9.])\d+(?:\.\d+)?(?![A-Za-z])`.
A digit run welded to a letter is not a quantity, and a decimal is one number rather than the two
either side of its point. Both sides of the comparison use it, and the comparison is by value, so
`'09'` in an ISO date still meets `'9'` in prose.

Every ordinary spelling survives, which is the half that decides the shape — narrowing what counts
as a figure is exactly where a check quietly starts refusing honest work. Measured on the same
corpus, `96-well`, `2 g`, `24 wells`, `48 runs` and `2026-09-01` all still state their figures,
because a digit beside **punctuation** is a figure and only a digit beside a **letter** is not.

**The attribution half stays open and moves to `DEFERRED.md` with the count above.** Closing it
needs the slot's identity in the judgment — a per-slot unit vocabulary, or asking the model to point
at a span and checking the span's neighbourhood — and the row's own argument against both stands:
the first is a table that will be wrong for the first ask nobody anticipated, the second is a model
call inside a check that costs a regex. What the measurement adds is that the remaining exposure is
**371 figures rather than 537**, and that the trigger is a count over a deployment's own turns
rather than over asks written for another purpose.

## What keeps it true

- `tests/test_protocol_design_tools.py::test_a_ring_closure_digit_in_a_structure_is_not_a_figure_the_chemist_stated`
  — both directions on one source string: the structure, the `C18` and the decimal fragment stop
  supporting a limit, and `96-well` and `2 g` still state theirs.
- `tests/test_protocol_design_tools.py::test_a_decimal_is_one_figure_and_not_the_two_either_side_of_its_point`
  — the other mechanism, separately, and the whole number still quotable.
