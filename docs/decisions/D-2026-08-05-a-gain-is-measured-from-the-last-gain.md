# D-2026-08-05-a-gain-is-measured-from-the-last-gain — A gain is measured from the last gain, not from the last run

**Status:** accepted · **Date:** 2026-08-05 · **Supersedes:** the plateau arithmetic in
D-2026-08-04-a-plateau-needs-the-noise-you-measured-it-with (the required `assay_noise` and
everything else in that ADR stand)

## Context

W1 shipped `campaign_progress` to stop a lab leader being told that gains inside the assay were
real — probe `op-13` was graded *fabricated* for exactly that. A review of the shipped code found it
making the mirror-image error, and making it silently.

**A campaign that climbed 50.0 → 70.9 over twelve runs reported `plateaued=True`.** That is
**+20.9 against a stated ±2 assay noise** — ten times the noise, and precisely the gain a chemist
measures when they compare the first run with the last. The summary said "the last gain larger than
the stated assay noise (+/-2) was 11 evaluation(s) ago."

The verdict was a step function on the size of a *single step*, blind to how far the campaign had
come. Two campaigns one point apart in total gain got opposite advice:

| per-step | total gain over 12 runs | old verdict |
| --- | --- | --- |
| 1.9 | +20.9 (10× noise) | `plateaued` |
| 2.01 | +22.1 (11× noise) | still moving |

## Why the first version was written that way

`evaluations_since_improvement` compared each result to a **continuously updated running best**. A
sub-noise gain moved the best without resetting the counter, so the bar rose underneath the counter
and a monotone climb never reset it.

That was deliberate, and it was argued for in a test docstring — *"what makes a gain real is the
assay, not the slope"* — with a pair of tests built around the claim. The sentence is true of one
step and false of a series. Twelve steps of 1.9 are not twelve non-gains; they are one gain of 20.9
delivered slowly, and it is measurable on the same assay that cannot resolve any single step of it.

This is the failure mode `CLAUDE.md` names: prose is evidence about what its author believed. The
argument was articulate, the test encoded it, the suite was green, and the behaviour was wrong.

## Decision

**The counter measures distance from the last real gain, not from the last run.** An anchor holds
the value at the last reset; a result beating the anchor by more than `assay_noise` is a real gain
and resets both. The running best still moves on any improvement, because it is what the campaign
has actually reached and reporting a stale one would misstate where it is.

```python
if best is None or _improved_by(direction, value, best) > 0:
    best = value                      # what the campaign reached
if anchor is None or _improved_by(direction, value, anchor) > assay_noise:
    anchor, since = value, 0          # a real gain over the last real gain
else:
    since += 1
```

**Measured against every series already in the suite.** It changes only the two creeping cases and
leaves the campaigns that genuinely stopped moving exactly where they were:

| series | old | new |
| --- | --- | --- |
| `op-13`, the probe W1 exists for | since 6, plateaued | **since 6, plateaued** |
| flat after a real gain | since 5, plateaued | **since 5, plateaued** |
| 5-point steps against ±2 | since 0, moving | **since 0, moving** |
| 1-point steps, +6 total | since 6, **plateaued** | since 0, moving |
| 1.9-point steps, +20.9 total | since 11, **plateaued** | since 1, moving |

`op-13` is the regression guard and it is untouched: best 89.0, six evaluations since a real gain,
`plateaued`, window span 16.0 — identical before and after.

**The error this fixes is the more expensive one.** Being told a plateau is real when it is not
costs a fortnight of lab time. Being told a campaign has plateaued when it is climbing costs the
rest of the campaign — the compound that was two more runs away is never made. Both are failures of
the same tool; only one of them is invisible afterwards, because nobody measures what a stopped
campaign would have found.

## Consequences

- `test_the_same_series_at_one_point_steps_is_plateaued` is **replaced by its own opposite**,
  `test_a_series_creeping_past_the_noise_in_small_steps_is_not_plateaued`, whose docstring records
  that it reversed and why — so the next person to read it sees the argument that lost rather than
  rediscovering it. A second test pins the +20.9 case that found the defect.
- No caller changes. `campaign_progress` is agent-facing only: nothing in the durable loop reads it,
  so no in-flight campaign's control flow moves. `space_exhausted` remains the only early stop.
- `window_span` / `window_indistinguishable` are untouched. They answer a different question — "do
  the last N results differ from each other at all" — and were never part of the defect.
- The module docstring states the anchoring, because the arithmetic is the whole claim this module
  makes and a reader who assumes "since the best improved" will misread every number it returns.
