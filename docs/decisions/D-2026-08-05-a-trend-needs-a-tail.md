# D-2026-08-05-a-trend-needs-a-tail — A trend needs a tail, not just a slope

**Status:** accepted · **Date:** 2026-08-05

## Context

The soak exists to answer one question no single run can: does anything grow that should not. The
first attempt answered it the only way that is always available and never sound — it subtracted the
first sample from the last. api RSS went 643,304 → 650,756 KB across five rounds, which reads as a
7 MB leak and is equally consistent with a warm-up curve, with allocator jitter, and with a leak ten
times larger hiding under one noisy sample.

`D-2026-08-04-a-plateau-needs-the-noise-you-measured-it-with` had already established the first half
of the rule for the storm's knee finder: a threshold chosen before the noise is measured produces a
confident answer at random. Applying it here produced the fit, the standard error, and the refusal
to name a slope inside its own error.

**That was not enough, and the run proved it on itself.** At 29 rounds the api RSS series fitted
`+4,690 KB/round (± 764)`, with a *tail* slope of `+6,896` — a resolved slope, accelerating, and one
step from being filed as a memory leak. At 43 rounds the same series, same process, nothing changed,
fitted `rises then settles — flat within its noise over the last 22 rounds`. Round 40 had returned
134 MB in one step, which is what allocator arenas do and what a leak never does.

The 29-round reading was not noisy. It was **resolved**, with a small error bar, and wrong about the
thing anyone would use it for.

## Decision

**A trend claim needs two fits, not one: the whole series, and its tail. A verdict that names growth
without a tail long enough to fit is reported as "too short to say whether it settles" — never as
growth.**

Concretely, in `chemclaw.cli.soak_report`:

- `fit()` returns a slope **with its standard error**; `Trend.resolved` is `|slope| > 2·stderr` and
  requires at least four points.
- `describe()` fits both halves as well, and distinguishes four outcomes the first draft collapsed
  into two:
  - whole unresolved → *flat within its own noise*,
  - whole resolved, **tail shorter than four points** → *grows N/round; too few tail points to say
    whether it settles*,
  - whole resolved, tail long enough and unresolved → *rises then settles*,
  - both halves resolved → *grows and steady / slowing / steepening — first half N, second half M*.

**And the comparison is between the two halves, never between the tail and the whole**, which is the
correction the run itself forced. The whole *contains* the tail: on a series that rises in steps, an
early flat stretch drags the whole-run slope below the tail's, so "tail below whole" reads as
deceleration when nothing decelerated. This soak did exactly that — at 104 rounds `whole +2,317 /
tail +1,345` was written down as "decelerating", and at 138 rounds the two halves came out **+3,166
and +3,177**, identical, with the last quarter at **+5,138**. Two equal-length independent windows
can be compared; a window and its own superset cannot.

The middle case is the decision. "The tail is flat" and "we did not look at the tail" both fail
`resolved`, and they are opposite statements: one is evidence of a plateau, the other is absence of
evidence. Collapsing them is exactly how a five-round record gets read as a settled system, and how a
29-round record gets read as a leak.

## Consequences

- A soak must run long enough that its **second half** is a fittable series, not merely long enough
  that its total is. Twice the minimum, not the minimum.
- The readings this rule produced on its first real subject went *accelerating* (29 rounds) →
  *plateau* (43) → *decelerating* (104) → *steady, ~3,200 KB/round* (138), and every one of them was
  resolved with a tight error bar. Three of the four were wrong, and only the last was checked
  against an equal-length comparison window. A resolved fit is a statement about the data in front
  of it; treating it as a statement about the system is a separate step that has to be earned.
- The verdict is allowed to say it cannot answer, and the run is not a failure when it does. That is
  the same property `live_storm._knee` gained, and the same reason: a harness that returns a number
  while unable to measure is worse than one that returns nothing, because the number gets quoted.
- Three of the tests in `tests/test_soak_report.py` exist for this rule specifically, and one of them
  corrected the claim it was written to defend: the plan called five points "a lead, not a leak" on
  the grounds that five is few, and the fit says the residuals are small enough that +1,825 KB/round
  clears four times its own standard error. Five points **can** resolve a slope. What five points
  cannot do is fit a tail — which is the thing that separates a warm-up from a leak, and therefore
  the thing the claim actually needed.

## Alternatives considered

**Report the slope and let the reader judge.** This is what the endpoint subtraction did with two
numbers instead of one, and the failure mode is identical: whoever quotes it drops the error bar.
The module exists so that a caller who can see `stderr` beside `slope` cannot accidentally report the
slope alone.

**Require a fixed minimum run length (say 50 rounds) instead of a tail check.** A length chosen in
advance is the same mistake as a threshold chosen in advance, one level up — a noisier machine needs
more rounds and a quieter one fewer, and only the data knows which this is. The tail check adapts;
a constant does not.
