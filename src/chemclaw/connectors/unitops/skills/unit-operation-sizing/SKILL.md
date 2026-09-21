---
name: unit-operation-sizing
description: >-
  Use when the question is about equipment rather than chemistry — "we are going from 1 L to 250
  L", agitation, tip speed, will the solids stay suspended, jacket heat transfer, a solvent swap by
  distillation, crystallisation yield, how long the filtration or the drying will take. Sizes each
  unit operation from measurements the chemist supplies, and names the ones no correlation here can
  supply.
tools:
  - agitation_scale_up
  - just_suspended_speed
  - heat_transfer_time_constant
  - shortcut_distillation
  - crystallisation_yield
  - filtration_time
  - drying_time
  - boiling_point_at_pressure
  - enumerate_stereoisomers
  - ich_impurity_limit
  - predict_solubility
  - ask_clarifying_question
  - similar_reactions
  - gather_evidence
---

# Unit-operation sizing

## This bundle holds no data, and that is deliberate

No vessel register, no VLE table, no solubility curve, no cake resistance, no drying curve, no
impeller catalogue. Every tool refuses to default the measured number its answer is made of, so a
question that names a vessel and no measurement is one that cannot be answered here.

That refusal is the feature. A correlation handed a defaulted `U`, a guessed relative volatility or
an assumed specific cake resistance returns a number with the same number of decimal places as a
real one. Ask for the measurement (`ask_clarifying_question`), or look for it in the record
(`gather_evidence`) — and where it genuinely does not exist, say which experiment produces it: a
solubility curve, a filtration leaf test, a drying curve, a heat-transfer trial.

## Agitation: you cannot hold every similarity criterion at once

`agitation_scale_up` carries a duty between vessels on **matched power-per-volume** or **matched
tip speed**, and those give different speeds. Choosing between them is a chemistry decision, not an
arithmetic one, so make the chemist's limitation explicit before you call it:

- **Gas–liquid or liquid–liquid mass transfer** limited → match P/V.
- **Shear-sensitive** crystals or an emulsion you must not make → match tip speed.
- **Solids suspension** → neither; the criterion is `just_suspended_speed` (Zwietering), and the
  answer is a floor the agitator must clear rather than a scaled duty.
- **A fast reaction at the feed point** → blend time, which nothing here computes. Say so. This is
  where the impurity that never appeared on the bench appears, because a dosed reagent sees a local
  excess before it mixes.

Zwietering is a **fitted correlation** with a geometry-dependent constant, not a law. Report it as
a starting point for a trial, not as a setpoint.

## Heat transfer: the number that says why the bench told you nothing

`heat_transfer_time_constant` is τ = M·cp/(U·A) plus the duty at a stated driving force. The reason
it matters at scale is geometric: surface-to-volume falls as volume rises, so cooling that was
instant in a round-bottom is a time constant in a 250 L vessel.

This is a **capacity**, not a load. Comparing it to the heat a reaction actually releases is
`thermalsafety`'s question and needs measured calorimetry — do not close that loop with a computed
enthalpy.

## Distillation and the solvent swap

`shortcut_distillation` is Fenske–Underwood–Gilliland: a **shortcut**, not a stage-by-stage
calculation, and it needs a relative volatility somebody measured or looked up. For a solvent swap
the chain that actually answers the question is:

1. `predict_solubility` or the record — will the product stay in solution through the swap, or oil
   out partway.
2. `boiling_point_at_pressure` (the `props` bundle) — can you distil it at all without cooking the
   product, which is usually the binding constraint rather than the stage count.
3. `shortcut_distillation` — stages and carry-over.
4. `ich_impurity_limit` — is the residual first solvent under its Q3C limit.

Report the carry-over against the limit, not on its own. "Three theoretical stages" is not an
answer to "can we swap out DMF".

## Crystallisation yield is a mass balance, not a crystallisation

`crystallisation_yield` is the **equilibrium maximum** two measured solubilities permit. It says
nothing about whether you will get a solid, which polymorph, what habit, how long it takes, or
whether it oils out. A real batch comes in under it.

So report it as a ceiling and a liquor loss, and say what it does not cover. The form question has
no tool in this system at all — `enumerate_stereoisomers` and the calculators do not answer it —
and a polymorph screen is the experiment.

Mother-liquor loss is yield you are choosing to give up; it belongs in the PMI conversation, not
only in the yield one.

## Filtration and drying are measurements carried forward

`filtration_time` assumes an **incompressible cake** — a compressible one (a fine needle, a gel)
behaves qualitatively differently and the number will be optimistic. `drying_time` reads a drying
curve somebody measured; without one it has nothing.

**Neither computes a wash volume**, and that is usually the actual ask. Displacement washing and
dilution washing give very different volumes for the same target, and there is no tool here for
either — say so rather than dividing the cake volume by something.

These two are also the steps that do *not* scale with the charge. A protocol scaled by multiplying
every quantity has silently turned a 20-minute filtration into an overnight one, which changes the
hold time the intermediate sees.
