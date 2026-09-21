---
name: thermal-safety-assessment
description: >-
  Use when a scale-up question touches heat — an exotherm, a cooling failure, a dose rate, a
  quench, a jacket, "is this safe at 20 kg", "what is the MTSR", "what happens if the agitator
  stops". Turns calorimetry a person measured into the adiabatic rise, MTSR, TMR_ad and Stoessel
  class a cooling-failure argument is made of, and — far more importantly — refuses to invent any
  of the numbers those rest on. Load it before quoting any thermal quantity at scale, and before
  letting a computed reaction enthalpy anywhere near a heat load.
tools:
  - adiabatic_temperature_rise
  - mtsr
  - tmr_ad
  - stoessel_criticality_class
  - heat_removal_capacity
  - semenov_critical_ambient
  - oxygen_balance_screen
  - semibatch_accumulation_profile
  - heat_transfer_time_constant
  - compute_reaction_energy
  - screen_hazards
  - ich_impurity_limit
  - ask_clarifying_question
  - find_notes
  - gather_evidence
  - record_knowledge_note
---

# Thermal safety assessment

Seven tools that do arithmetic, and one rule that matters more than all of them.

## The rule: this server measures nothing, and neither do you

**Every input is a DSC, ARC or RC1 number a person measured.** There is no calorimetry model here,
no fit, no correlation and no default — the fleet's own manifest says a defaulted number "returns a
plausible number nobody measured", and for a safety argument that is the whole failure mode.

So the first move on any thermal question is to find out which numbers exist. Ask
(`ask_clarifying_question`) for what is missing rather than proceeding with a placeholder, and say
which measurement would close it. A chemist who has not run the DSC needs to hear that they have
not run the DSC, not a number.

**A computed reaction enthalpy is not a process heat load.** This is the one that will tempt you,
because `compute_reaction_energy` is cheap and sitting right there. A GFN2-xTB ΔG over a balanced
equation is a *ranking of alternatives*. It is not ΔH_rxn for a charge: it omits mixing and
dilution heats, heat of crystallisation, the quench, and every species the equation left out, and
it carries several kcal/mol of error besides. Feeding it to `adiabatic_temperature_rise` produces a
ΔT_ad with a decimal point and no measurement behind it, which is worse than no answer because it
looks like one. The agent's own system prompt denies this unconditionally; this skill is why.

If the only enthalpy available is computed, say that the assessment cannot be made and name the
experiment: a reaction calorimetry run.

## The frame, in the order the numbers depend on each other

1. **ΔT_ad** (`adiabatic_temperature_rise`) — every joule of reaction heat staying in the batch.
   Needs the measured heat of reaction and the batch's mass and heat capacity.
2. **MTSR** (`mtsr`) — the temperature the synthesis actually reaches on cooling failure, which is
   the process temperature plus the rise the *accumulated* fraction supports. **Accumulation is the
   input people get wrong**, see below.
3. **MTSR against MTT and T_D24** — the boiling point or pressure limit, and the temperature at
   which `tmr_ad` reaches 24 hours. These three orderings are the whole assessment.
4. **`stoessel_criticality_class`** — classes 1–5 from that ordering.
5. **`heat_removal_capacity`** — what the jacket can take out, as a *capacity*. Comparing it to a
   load is your job; the tool does not know the load.

## Accumulation is a kinetics question with a thermal answer

`mtsr` takes the accumulated fraction and will happily take 1.0. For a semi-batch dose that is the
worst case and often not the real one — and the real one is `kinetics`'
`semibatch_accumulation_profile`, which integrates the dose against the reaction and says how much
unreacted reagent is sitting there at each moment.

**The composition is the actual question and no single tool answers it**: accumulation profile ×
measured ΔH → the MTSR the accumulated reagent supports. Do it explicitly, state the peak
accumulation and when it occurs, and note that a dose time chosen purely for heat removal can be
the dose time that maximises accumulation. That trade-off is the reason semi-batch addition exists
and the reason it goes wrong.

If the `kinetics` bundle is not enabled, say that the accumulation is unknown and that assuming
1.0 is the conservative choice you are making — do not present a worst case as a result.

## A class 3, 4 or 5 is not an answer

It is a finding to hand to process safety, and it belongs at the *top* of the reply, not in a
closing caveat. Say the class, say the ordering that produced it, and say what would move it — a
lower process temperature, a longer dose, a more dilute charge, a different solvent with a higher
MTT.

Never write "safe", "safe to scale" or "no thermal hazard". The tools compute from what they were
given; they know nothing about what was not measured.

## What no tool here sees

Say these out loud when they are in play, because their absence is invisible:

- **Gas evolution and pressure rise.** Nothing in this bundle models it. A reaction that evolves
  CO2, N2 or H2 has a hazard whose magnitude is not in any number above.
- **Secondary decomposition** with an onset the DSC scan rate flatters. A dynamic onset is not an
  isothermal onset.
- **Scale-dependent heat loss.** Surface-to-volume falls as volume rises, so a bench reaction that
  "never got warm" is not evidence about 20 kg. `heat_transfer_time_constant` in `unitops` is where
  that becomes a number.
- **The quench and the work-up**, which are frequently the larger exotherm and are usually left out
  of the question as asked.

`semenov_critical_ambient` is about a *stored package* self-heating, not about a stirred batch —
do not quote it as an SADT and do not use it for the reactor.

## The oxygen-balance screen is a triage, not a classification

`oxygen_balance_screen` is stoichiometry from a molecular formula. It says a compound is worth
sending for calorimetry; it never says a compound is explosive, and it never clears one. Report it
beside `screen_hazards` and keep the two separate — one is a structural motif table, the other is
arithmetic on a formula, and neither is a test.

## Recording

A measured calorimetry value a chemist gives you is worth keeping (`record_knowledge_note`), with
the method and the scan rate. The computed quantities are not: they are a function of inputs that
are already in the record, and a stored MTSR outlives the accumulation assumption it was made
under.
