# D-2026-09-03-a-guard-that-fails-open-in-its-own-example — the sixth cycle

**Status:** accepted · **Date:** 2026-09-03

## Context

`D-2026-08-30-a-review-by-six-strangers-found-thirty-seven-defects` was the fifth review-fix cycle
over the prescriptive tier and merged as `#300` and `Chemclaw3_ui#60`. This is the sixth, run the
same way and over the *fixes themselves*: six subagents with fresh context windows over disjoint
slices of `git diff 0e92b7dc...2348112f`, each told to demonstrate every finding by execution and to
drop anything that did not reproduce.

The reason to keep paying for this is in the results rather than in the argument. The fifth cycle
was careful — every fix landed with a test, and the author mutation-checked them. The sixth cycle
still found two guards that **fail open in the regime their own docstrings were written about**, and
in both cases the docstring asserted the opposite in the present tense.

## The two that fail open

**`_agreement_tolerance` read the written precision back out of a float, which cannot be done.**
The fifth cycle replaced a flat 2% tolerance — which refused 4 of 18 correctly-rounded catalyst
lines — with `Decimal(repr(stated))`, on the reasoning that the exponent recovers "the precision of
the figure as it was typed". A Python float carries no trailing zero: `repr(0.10)` is `'0.1'`. So
the recovered precision is right for `0.07` and wrong by a factor of ten for every figure whose last
typed digit is `0`, and the function's own worked example inverted. Measured:

```
implied 5 mol% of 1.37 mmol = 0.0685
written 0.07 : tolerance 0.005  → passes  (intended)
written 0.10 : tolerance 0.05   → passes  (46% out, and the docstring says it fails
                                           by "six times the slack")
```

A **blocker** that admits a catalyst line 46% wrong, in exactly the catalyst-loading regime the
change was made for. The replacement derives the tolerance from the *implied* amount and from
nothing the chemist typed — a twentieth of it, floored at half a unit in the third decimal — so
neither term can be moved by how a figure was rounded. The test is a fifteen-row sweep across four
scales and five loadings, because one worked example is not a sweep and one worked example is what
was wrong.

**`_quote_supports` related value to quote only when the quote was one word.** The fifth cycle's
headline example — four limits a chemist never named, stored as their own words — is refused
exactly as claimed. But the rule opens `if len(words) < 2`, and the quote's *length* was never what
made those fabrications fabrications. The same four go straight back in on two-word quotes drawn
from the same message: `scale='5 g'` quoting `'you think'`, `plate_format='96'` quoting
`'deactivated chloride'`. All four accepted. Every quote is related to its value now — the value's
figures have to be the quote's, compared as numbers; a figure written in words satisfies that; a
value carrying no figures needs the quote's own tokens. What that still cannot do is *attribution*,
and `docs/planning/BACKLOG.md` carries the row saying so rather than a docstring implying otherwise.

## The word for a document, and the document

**A revision's `kind` was declared by its caller and one caller had it wrong.**
`structure_experiment_request` carries a drafted procedure forward when a chemist corrects the ask —
deliberately, and the fifth cycle added it — and stamped `kind="request"` regardless, while
`require_movable` reads that column to decide whether a design has a procedure to approve. So
correcting the ask made a fully drafted plate **permanently un-approvable and un-executable**: every
arm, step and charge line present, and the store refusing the sign-off with "this design holds only
the structured ask". The document was right and only the word for it was wrong, so nothing on the
page hinted at the contradiction.

`store.revision_kind` derives it from `has_protocol` and the parameter is gone from all three
callers. This is `has_protocol`'s own lesson arriving a fourth time: the property exists because
three callers deciding it separately is how two of them got it wrong, and `kind` was a fourth
caller deciding the same thing under a different name.

## A page stating conditions nobody runs

`## Conditions` rendered `design.base.setpoints` while the run sheet carries a column only where the
arms *disagree*. A field every arm overrode to the same value therefore fell through both. Measured
on three arms all set to `N2` over a body reading `air`: the page said "Atmosphere: air", there was
no atmosphere column, and the atmosphere the design is run under appeared **nowhere** on a document
a chemist runs from — with a wrong one stated as fact. The section shows what every arm agrees on,
resolved, and says so when a field was dropped; the two halves are complements by construction
rather than by two lists somebody keeps in step. The same defect was live on `Chemclaw3_ui`, which
no reviewer could see from either side alone, and is fixed there in the same shape.

## What else reproduced

- **`_text` guarded three fields and eight went unescaped.** A title reading `T\n\n## Forged` forged
  a section, as did the goal, the objectives, the exclusions, the solvent, the atmosphere, an arm's
  note and a citation's `supports`. Its marker set also omitted the four openers that do the most
  damage — a leading backtick or `~` opens a fence that swallows the rest of the document, `<` opens
  raw HTML, `1.` opens a list — and `_code`'s doubled fence closed on a doubled run inside it.
- **`_reading_order` raised `ValueError` on a superscript.** `'²'.isdigit()` is `True` and `int('²')`
  raises, while `\D` matches it, so a factor named `²` was a 500 out of the diff route. It was also
  not a total order: `A1` and `A01` produced identical keys, so two distinct paths compared equal
  and their order fell to `sorted`'s stability over a **set**, whose iteration varies with the hash
  seed.
- **`limiting_is_limiting` weighed nothing on an unlabelled table and said it had.** `role` defaults
  to `UNKNOWN`, so the check's own worked example passes untouched, reported as "'acid' is the
  smallest stoichiometric charge" — a claim about a comparison that never happened. Kept, because it
  catches that fault the moment the roles are labelled; a passing verdict now says what it looked at.
- **A replicate counted as a second experiment**, so a triplicate came out a screen and drew a
  controls warning about "a screen with nothing to compare against" over three arms the model
  validator guarantees are identical.
- **Every refused design write was invisible.** `deps._refuse` records a metric and a WARNING for
  each session and proposal refusal precisely because the response discloses nothing; the two design
  write routes raised inline on both raise sites.
- **The torn-read test missed its own regression a third of the time.** Measured with the isolation
  level removed and nothing else changed: 9 of 200 rounds tore. At the 25 rounds it ran for, a broken
  store passes with probability `0.955 ** 25` = 32%.
- **`page()` had no test of what it returns** — the torn-read test asserts only that its four halves
  agree — and three mutants survived. It also diverged between backends on `revision=0`, in the one
  method written to remove a divergence.
- **Four call sites invented a status the design did not have**, each unreachable and each wrong if
  reached.
- On `Chemclaw3_ui`: a verdict guarded on `status === 'requested'` where the service picks the stage
  from `has_protocol`, so the green badge the guard removed was still reachable on a `draft`;
  a number field that rewrote `1e5` as `100000` mid-keystroke; and an `aria-label` on a `role=cell`
  that **replaced** name-from-content, buying the well id at the price of the run order and the
  control marking.

## Decision

Fix all of the above, each with a test that fails on the previous source. Take the two fail-open
guards as the finding of the cycle, and record the general form.

## Consequences

**A guard is most likely to be wrong in the regime its docstring argues about.** Both fail-open
defects sit inside the paragraph explaining why the guard is right, and in both the paragraph states
the false half in the present tense with a worked example beside it. That is not carelessness: the
example is chosen while the fix is being written, from the case the author already has in mind, and
it is therefore the case least likely to be re-derived. **A worked example in a docstring is a
claim, and it costs one script to check.** The sweep that replaced `_agreement_tolerance`'s example
is fifteen rows and took one command.

**A count of one's own work is a measurement like any other.** Three documents from the fifth cycle
gave three different figures for the number of tests it added — 23, 21 and four — and a reviewer
collecting the node ids at both ends of the branch measured 41 over 36 functions. `tasks/lessons.md`
now says so and the practice is described without a number.

**A figure that depends on two variables cannot be quoted as one number.** `models.py` said the
largest design the ceilings permit "serialises to 414 KB and diffs in 0.060 s". A reviewer measured
1782 KB and 1.556 s; re-measured here it is 482 KB at 0.329 s with the free text empty, 572 KB at
0.278 s with short notes and 1382 KB at 0.282 s with long ones. All three readings are correct,
because the ceilings bound *counts* and the free text inside them is bounded only by the body cap —
so "the largest design these ceilings permit" is not one object. What the sweep shows is better than
the claim it replaces: the diff time is **flat in the bytes** and set by the path count, which is
precisely the evidence that the ceilings rather than the cap bound the cost. The single figure was
one sample, low on both axes.

**A rate quoted as a constant is a rate nobody can act on.** The fifth cycle published "2/25" for
the torn read; three re-runs gave 1/25, 3/25 and 1/25, and the honest figure over 200 rounds is 9.
That denominator is what sets the test's round count now, and the arithmetic is in its docstring so
the number is derived rather than picked.

**The three-repository shape hides a defect from every reviewer.** The `## Conditions` fault was
live in the service and in the frontend, in the same shape, and neither repository's reviewer could
have found the other half — the service's reviewer sees a fixed frontend it does not read, and the
frontend's reviewer measures against a shipped service. Where a rule is transcribed across the two,
the transcription is a place to look, not a place to trust: `setpointsFor` was checked against 400
generated fixtures and `sharedSetpoints` now exists beside it for the same reason.
