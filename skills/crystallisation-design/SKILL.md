---
name: crystallisation-design
description: >-
  Use for an isolation by crystallisation — yield, liquor loss, cooling profile, seeding, oiling
  out, antisolvent addition, which form. The yield is a mass balance over two measured
  solubilities and says nothing about kinetics, habit or form.
tools:
  - crystallisation_yield
  - heat_transfer_time_constant
  - just_suspended_speed
  - predict_solubility
  - solvent_properties
  - compare_solvent_properties
  - green_metrics
  - ich_impurity_limit
  - similar_reactions
  - gather_evidence
  - recall_observations
  - ask_clarifying_question
---

# Designing a crystallisation

## What the number is, and the four questions it does not answer

`crystallisation_yield` is a **mass balance over two measured solubilities** — the solubility at
the dissolution temperature and at the end temperature. It is an equilibrium maximum. A real batch
comes in under it.

It says nothing about:

1. **Whether you get a solid at all.** Supersaturation without nucleation is a clear solution you
   cool to 0 °C and still have nothing.
2. **Which form.** Polymorph, solvate, hydrate, amorphous. This system has no solid-form
   capability, so if the question is "which form will I get out of ethanol", say plainly that the
   answer is a screen and not a calculation.
3. **Habit and filterability.** Needles from a fast crash-cool filter differently from the plates a
   slow ramp gives, and filtration time is often what decides the cycle.
4. **How long.** Growth and the approach to equilibrium are kinetics.

Report the yield as a ceiling with the liquor loss beside it, and name which of the four are open.

## The solubility numbers are the whole answer, so ask where they came from

Two measured points at the two temperatures, in the actual solvent system including any water
carried over from the work-up. If the chemist has not measured them, say so — `predict_solubility`
is an **aqueous log S** predictor with its own uncertainty and is not a substitute for a solubility
curve in 2-MeTHF. Using it as one is how a crystallisation gets designed against a number nobody
measured, and the error lands entirely in the yield.

`ask_clarifying_question` for the solvent composition if the protocol is ambiguous about it: a
crystallisation from "the reaction solvent" after an aqueous work-up is a crystallisation from an
unknown mixture.

## Cooling profile: linear is the wrong default and it is the usual one

A linear ramp spends most of its supersaturation early, which is where uncontrolled nucleation and
fines come from. A profile that is slow through the metastable zone and faster afterwards gives
fewer, larger crystals. Nothing here computes that curve — say it as chemistry, not as a number.

**Seeding is the control that makes the batch reproducible**: seed inside the metastable zone, at a
temperature where the seed does not dissolve, with a stated load. An unseeded crystallisation is
one whose particle size is decided by whatever happened to nucleate, which is why it is the step
that changes on scale-up.

**Oiling out** is the failure to watch for when the compound has a low melting point or the
antisolvent addition is fast. It is a liquid-liquid phase split, not slow crystallisation, and the
material in the oil is not purified at all. Where the chemist reports "it came out as a gum", that
is this and the answer is a different solvent system rather than a slower ramp.

## Purity is the reason for the step, so say what it rejects

A crystallisation is a purification. What it rejects depends on where the impurity sits — in the
liquor, in the lattice, or on the crystal surface — and only the first is cleaned up by a wash.
An impurity that co-crystallises does not come out however many times you recrystallise, and that
is a structural question about the impurity rather than a process parameter.

Where a solvent is being chosen, `ich_impurity_limit` for its Q3C class: a crystallisation solvent
is the one most likely to stay in the product, and a Class 2 solvent here is a specification and a
drying study rather than a preference.

## What scale changes

- **Cooling rate.** The jacket cannot hold the bench profile in a 250 L vessel; the ramp is limited
  by heat transfer, and `heat_transfer_time_constant` is the number.
- **Mixing.** Too slow and the solids settle; too fast and crystals break, giving fines and a
  slower filtration. `just_suspended_speed` is the floor.
- **Addition time** for an antisolvent, which sets local supersaturation at the feed point — the
  same feed-point problem a fast reaction has, with the same consequence that the bench never saw
  it.

## Before you finish

`similar_reactions`, `gather_evidence` and `recall_observations`: if this compound has been
crystallised here before, the solvent system and what went wrong are worth more than the
arithmetic. `green_metrics` if the solvent volumes are large — a crystallisation is frequently the
largest single contributor to PMI, and the liquor loss is a yield decision and a waste decision at
once.
