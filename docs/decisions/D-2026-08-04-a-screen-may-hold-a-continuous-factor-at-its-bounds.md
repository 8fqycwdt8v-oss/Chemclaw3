# D-2026-08-04-a-screen-may-hold-a-continuous-factor-at-its-bounds — A screen may hold a continuous factor at its bounds

**Status:** accepted · **Date:** 2026-08-04 · **Implements:** W2 of
D-2026-08-04-what-bofire-does-when-you-actually-run-it ·
**Supersedes:** D-092's continuous refusal in `factorial_design` ·
**Extends:** D-2026-08-02-the-fraction-lives-where-bofire-will-fractionate

## Context

`generate_screening_design` refused a continuous parameter outright. The reason was sound when it
was written (D-092): BoFire's `FractionalFactorialStrategy` silently fractionates a continuous input
to its two bounds, and a design that looks complete while quietly reshaping a factor is worse than a
clear refusal. The skill therefore told the model to reformulate temperature as `"low"`/`"high"` by
hand — the same transformation, performed worse, with no record that it happened.

Meanwhile three of the class's four unused knobs looked like a straightforward thread-through.
`n_generators` had already taught the opposite lesson: documented, imported, present on the class,
and **inert** on the only domain shape that reached it.

## The measurement, before the code

**M-5 — the four knobs, on both domain shapes.** On the all-categorical domain `factorial_design`
accepted, three of the four are no-ops:

| knob | all-categorical (3 two-level factors) | mixed / continuous |
|---|---|---|
| `n_generators` | **inert** — 8 runs at 0 and 1 | 4 continuous + 1 categorical: 32 → **16** at 1 |
| `n_repetitions` | **inert** — 8 runs at 1, 2, 3 | 2 cont. + 1 cat.: 10 → **18** |
| `n_center` | **inert** — 8 runs at 0, 1, 3 | **defaults to 1**; adds rows *per categorical combination* — measured 4/5/6, 8/10/12, 16/20/24 |
| `randomize_runorder` | **works** | works; seed-reproducible and seed-sensitive |

So admitting continuous factors is not a companion to the other knobs, it is their **precondition**.

**M-8 — does the reduced path fractionate a mixed factor set as one?** The question the reduced half
turned on, because `_fractional_design` re-encodes categoricals onto [0, 1] and a continuous factor
would join them on real bounds. Two real continuous factors beside three re-encoded categoricals:
**32 runs at `n_generators=0`, 16 at 1, 8 at 2**, `n_generators=3` refused as confounded, and every
factor at exactly two levels with the real ones at their declared bounds. The union fractionates as
one factor set, so `n_generators` counts against the total and the generator — hence the resolution
derived from it — describes the whole design rather than part of it. That is the property that makes
returning a resolution honest at all.

## Decision

**A continuous factor is admitted and held at its two bounds, and the design says so.**
`ScreeningDesign` gains `two_level_continuous`, naming every factor collapsed that way. This is the
same move `resolution` is: a fractional design looks like a smaller full grid, and a temperature
column reading 20/120 looks like a considered pair of levels rather than a collapsed range. The
refusal was waiting for a return that could state what had been done; there now is one.

**`n_center`, `n_repetitions` and `randomize` ship; `block_feature_key` does not.** Centre points are
the only rows in a two-level screen that can see curvature; replication is what gives it a pure-error
estimate; randomized order stops a session drift reading as a factor effect. Blocking needs a block
*factor* — a day, a plate, an operator — and none of those exists anywhere in `src/`.

**Both `n_center` and `n_repetitions` are refused on an all-categorical problem.** Measured inert
there, and there is no partial behaviour to fall back on, so accepting the argument would be exactly
the `n_generators` failure repeated with the same shape. `n_center` is also refused on a *reduced*
design that still has categorical factors: a re-encoded categorical at 0.5 decodes to neither of its
levels, which is why `n_center=0` was forced on that path from the start.

**`n_center` is passed explicitly on every construction path, including the default 0.** BoFire's own
default is **1**. Leaving it unset would have the tool silently start emitting midpoint rows the
moment a continuous factor was admitted — a default that is not ours, quietly changing what a chemist
is handed.

**Randomization happens at our boundary, not through `randomize_runorder`.** The argument exists and
works, but the two design paths construct their strategies differently; shuffling once in
`_randomized` under `_resolve_seed` is what makes both randomize identically under one `bo_seed`
default, and it keeps the guarantee ours if a future release changes what that argument seeds from.

## Consequences

- Stories 2.3 and 4.4 are served for a continuous factor as well as a categorical one, and the skill
  no longer instructs the model to discretize by hand.
- The run count a chemist is handed is now stated by `summary` rather than inferred, which matters
  most for `n_center`: 8 corners plus `n_center=2` over one two-level categorical is **12** runs, not
  10. A plate planned from the wrong number is a wasted session.
- The reduced path's re-encoding now carries real continuous factors too, which deepens the coupling
  to *where* BoFire applies a generator that D-2026-08-02 already accepted. M-8's numbers are the
  evidence, and `tests/test_bo_doe.py` pins them.
- `_two_level_names` is derived from the problem rather than from the frame, so a factor that BoFire
  silently dropped would show up as a design missing a column rather than as a quietly complete-
  looking one.
