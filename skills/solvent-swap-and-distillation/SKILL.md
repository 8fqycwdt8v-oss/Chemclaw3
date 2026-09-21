---
name: solvent-swap-and-distillation
description: >-
  Use when a solvent is exchanged or removed — a swap, a strip, carry-over, a residual-solvent
  limit. Chains properties, vacuum boiling point, stage count and the ICH limit, because carry-
  over only means something against its limit.
tools:
  - solvent_swap_candidates
  - compare_solvent_properties
  - solvent_properties
  - boiling_point_at_pressure
  - vapour_pressure
  - shortcut_distillation
  - ich_impurity_limit
  - predict_solubility
  - screen_hazards
  - green_metrics
  - gather_evidence
  - ask_clarifying_question
---

# Swapping a solvent

## The chain, in the order the constraints bind

A solvent swap fails on the second or third of these far more often than on the first, so do not
stop at the shortlist.

1. **`solvent_swap_candidates`** — the Hansen neighbourhood, filtered. A starting list, not an
   answer: solubility similarity is one axis of many.
2. **`compare_solvent_properties`** on the shortlist — boiling point, water miscibility, peroxide
   formation, ICH class, flash point. **The process constraints usually bind before the solubility
   one does**, which is the same point `skills/solvent-selection` makes about the computed
   comparison.
3. **`boiling_point_at_pressure`** — can you actually distil the *old* solvent out without cooking
   the product? A high-boiling solvent under vacuum is often the binding constraint, and it is the
   step that decides whether the swap is feasible at all. A product that degrades at the pot
   temperature required is a different route, not a longer distillation.
4. **`shortcut_distillation`** — stages and carry-over. Fenske-Underwood-Gilliland is a *shortcut*,
   not a stage-by-stage calculation, and it needs a relative volatility somebody measured or looked
   up. Say which you used.
5. **`ich_impurity_limit`** — the Q3C class and limit for the solvent being removed.

**Report the carry-over against the limit, never on its own.** "Three theoretical stages" answers
nothing a chemist asked. "Three stages takes DMF to roughly X ppm against a Q3C Class 2 limit of
880 ppm" is the answer, and if it does not clear the limit the swap needs more stages, a different
technique, or a different solvent.

## Does the product stay in solution

The thing that turns a distillation into an incident: as the good solvent goes, solubility falls,
and the product crashes out in the still. Check with the record or `predict_solubility` (knowing it
is an aqueous predictor and a poor proxy here), and ask whether a constant-volume swap — feeding
the new solvent as the old distils — is what is actually wanted. That is usually the answer at
scale and it is a different operation from "strip and redissolve".

## Two properties that decide more than polarity

- **Water miscibility**, because it decides the work-up as much as the swap. A water-miscible
  solvent that cannot be washed is one you must distil out entirely.
- **Peroxide formation** in ethers, especially where the process concentrates them. A strip that
  concentrates a peroxide-former to near dryness is a hazard that has nothing to do with the
  chemistry; `screen_hazards` will not see it, so say it.

Also check flash point against the process temperature, and whether the new solvent's boiling point
leaves any headroom above the reaction temperature at all.

## The swap is a change to the whole step, not to one field

Changing the solvent changes rate, selectivity, solubility of every species, the work-up, the
crystallisation and the residual-solvent specification. `gather_evidence` for whether this
transformation has been run in the candidate before — a precedent in 2-MeTHF is worth more than
any property table — and say plainly which downstream steps the swap re-opens.

`green_metrics` where the volumes are large: a swap that doubles the process mass intensity to save
a Class 2 solvent may still be the right call, but it should be a stated trade rather than an
invisible one.
