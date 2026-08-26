# D-2026-08-26-a-barrier-is-a-difference-between-two-numbers-measured-the-same-way — what a code review of the rotational profile found

**Status:** accepted · **Date:** 2026-08-26 · Fixes eight defects in the feature
`D-2026-08-26-a-torsion-is-named-not-indexed` shipped, found by reviewing it after it merged. That
ADR's measured table is corrected here rather than left standing.

## Context

The rotational profile was written, tested against a fake, run against the real GFN2 server, and
merged. A review of the merged code then found eight defects. Two are the subject of this ADR
because they carry a rule; the rest are recorded below because a list of what a review caught is
worth more than the fixes taken individually.

**None of the eight was reachable from the test suite as it stood**, and that is the finding that
generalizes. The fake carried an n-butane-shaped surface — three wells, a 2.8 kcal/mol barrier,
a relaxation that lowered nothing, and Hessians that reported no imaginary mode and a constant
electronic energy. Every one of those properties made one of the defects inexpressible.

## The rule

**A barrier is a difference between two numbers measured the same way.** Both defects that carry
this ADR's name are the same mistake at different scales:

- **The pass and the wells had different zeros.** The pass's height came from the profile, relative
  to its lowest *constrained* scan point; each well's energy was relative to the lowest *released*
  minimum. Releasing a constraint lowers a well, so every barrier was understated by that
  difference — measured 0.118 kcal/mol on n-butane and **1.8 kcal/mol on N,N-dimethylacetamide** —
  and could in principle have come out negative, which Eyring turns into a femtosecond half-life
  for a real compound.
- **A free-energy barrier was an electronic barrier plus an absolute correction.** At
  `level="thorough"` the pass's `G − E` — a molecule's entire thermal-plus-entropic term, tens of
  kcal/mol — was added to an electronic barrier whose well had nothing subtracted. Measured on
  n-butane: **69.9 kcal/mol** where the electronic barrier is 1.9, and a half-life of 10³⁸ s. The
  fix is the definition: `G(pass) − G(well)`, and the fallback when either side has no free energy
  is the electronic barrier *labelled* `E`.

Both are now computed from absolute Hartree energies carried on an internal `_Well`, so the
subtraction has one zero by construction rather than by agreement between two call sites.

## The other six

- **`atoms` was never validated.** The handle guards `bond`; the scan drives `atoms`. Measured,
  `atoms=[-1, 1, 2, 3]` returned a full profile with a barrier and no error — Python's negative
  indexing addressed a real atom — `[0, 1, 2, 2]` returned another for a repeated atom, and
  `[0, 1, 2, 99]` escaped as a bare numpy `IndexError`. This is the silent-wrong-answer failure the
  whole feature exists to remove, one field along from where it was being watched for.
- **`highest_barrier_kcal` defaulted to `0.0`** when nothing resolved, and the publish projector
  wrote it as a real `rotational_barrier` fact — indistinguishable from free rotation. It is
  `None` now, and an unresolved pass says so in `warnings` instead of being dropped in silence.
- **A rotamer's geometry and its free energy could describe different structures.** Above `quick`,
  `relax_to_minimum` may displace along an imaginary mode and re-optimize; its result was thrown
  away and the pre-refinement geometry kept. The refinement now happens *before* the merge check,
  so the `structure_id`, the dihedral, the electronic energy and the free energy are all one
  geometry's.
- **No check that the step could resolve the period.** `period_degrees=20` with the default 30°
  step scanned two points and reported wells and barriers over them.
- **The discontinuity warning named the wrong interval**, `angle - step`, while the profile also
  holds refinement points spaced far closer — so it pointed at an angle usually not in the profile.
- **The published barrier was `max(forward_kcal)`** while the comment beside it said "out of the
  most populated well". The comment was right about what the record wants; the code is now that.

## What the fake had to learn to express them

A fake that cannot express the failure is not evidence
(`D-2026-08-26-a-tool-result-is-not-a-model-on-the-wire`), and this one could not express three of
these. It now: lowers a released well below its constrained point by ~0.13 kcal/mol, the order the
live server gives; reports a **first-order saddle** at a torsional maximum, which is what a
geometry there is, so the `thorough` path is reachable at all; and answers `compute_hessian` with
the energy *its own surface* gives that geometry rather than a constant, so a free-energy barrier
computed from it keeps its electronic term.

All eight fixes are pinned by tests verified against the pre-fix code — checked out of `origin/main`
into a worktree with the new tests copied in, where **eight of them fail**.

## Consequences

- `D-2026-08-26-a-torsion-is-named-not-indexed`'s measured table is corrected in place: n-butane's
  syn barrier 5.03 → **5.15**, anti↔gauche 2.53 → **2.65**, biphenyl's perpendicular 1.51 → **2.00**
  and planar 2.27 → **2.76**, DMA's amide 18.10 → **19.91** kcal/mol. Well depths, populations and
  the twist angle are unchanged — they never involved the pass.
- **The conclusion that GFN2 runs low on n-butane's anti↔gauche barrier survives**: 2.65 against a
  literature 3.3–3.6 is still the method rather than the arithmetic. That was checked rather than
  assumed, because the alternative — a code defect reported as a property of the method — is the
  more embarrassing of the two and was live for the length of one PR.
- `RotationProfile.highest_barrier_kcal` is now `float | None`. Additive and defaulted, so
  workflow histories in flight decode unchanged.
