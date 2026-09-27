---
name: kinetics-and-reactor-choice
description: >-
  Use when the question is about rate or reactor rather than outcome — "how long should I hold
  it", "what is the activation energy", "batch or flow", "how fast can I dose this", a
  concentration-against-time table. Carries a measured rate constant to another temperature,
  answers ideal-batch conversion and time, compares PFR against CSTR, and integrates a semi-batch
  addition into the accumulation profile a dose time is chosen against. Also says plainly what this
  server refuses: it fits nothing, so it cannot turn a time course into a rate law.
tools:
  - rate_constant_at_temperature
  - activation_energy_from_two_rates
  - batch_conversion_after
  - batch_time_to_reach
  - continuous_reactor_conversion
  - semibatch_accumulation_profile
  - agitation_scale_up
  - just_suspended_speed
  - ask_clarifying_question
  - similar_reactions
  - gather_evidence
---

# Kinetics and reactor choice

## What this server will not do, said before you try it

**There is no regression here.** No time-course data goes in, no goodness of fit comes out. The
most common real question — *"here is concentration against time for four runs at three
temperatures, fit me a rate law"* — is one these six tools cannot answer, and discovering that by
calling them one at a time wastes a turn and produces a number from two points that looks like a
fit over twelve.

When that is the ask, say so in the first sentence and offer what is actually available:

- `activation_energy_from_two_rates` is **exact algebra through two points**, not a fit. It has no
  residual, so it cannot tell you the Arrhenius line is bad. Use it, and say which two points you
  used and that a third would either confirm or destroy it.
- If a sandboxed Python connector is enabled, a least-squares fit over the whole table is an
  ordinary analysis and is the better answer.
- Otherwise the honest reply is that the fit needs a tool this deployment does not have.

## Extrapolation is where an Arrhenius answer goes wrong

`rate_constant_at_temperature` will carry a k anywhere. Two points fix a line; nothing fixes the
line's validity outside the window they were measured in. State the measured window every time you
extrapolate beyond it, and treat a mechanism change (a different rate-determining step, a
solubility limit crossed, a catalyst decomposing) as the thing that breaks it — those are exactly
what a wide extrapolation hides.

An Ea from two rates a few degrees apart is dominated by the error in the two rates. If the
temperatures are close, say the number is not worth quoting.

## Ideal means ideal

`batch_conversion_after`, `batch_time_to_reach` and `continuous_reactor_conversion` assume perfect
mixing, no dispersion, no mass-transfer limitation and no energy balance. Real reactors are none of
those, and the gap is *not* a small correction for the cases people ask about:

- A **heterogeneous** reaction (a slurry, a gas, a biphasic system) is frequently mass-transfer
  limited, in which case the rate law is about stirring rather than chemistry and these tools are
  answering a different question. `unitops`' `just_suspended_speed` and `agitation_scale_up` are
  where that lives.
- An **exothermic** reaction is not isothermal unless the jacket makes it so, which is
  `thermalsafety`'s question.

PFR against CSTR at equal residence time is a statement about the **order**. For first order the
PFR wins by a known margin; for zero order they are equal; for an autocatalytic or
product-inhibited system the ordering can invert. So check that the order was *measured* rather
than assumed before presenting that comparison — an assumed first order is the single assumption
that makes this tool confidently wrong.

## The accumulation profile is a safety output before it is a kinetics one

`semibatch_accumulation_profile` integrates a constant-rate dose and returns how much unreacted
reagent is present over time. Its peak is the input `thermalsafety`'s `mtsr` needs, and the two
belong in one answer: the MTSR that matters is the one the *accumulated* fraction supports, not the
one a fully-charged batch would give.

Say the peak and when it occurs. A dose time chosen only to stay inside the jacket's duty can be
the dose time that maximises accumulation, and that trade-off is the reason to show the profile
rather than a single number.

Constant rate is an assumption too — a real addition is a pump that was set once and a line that
blocked. If the chemist describes a portion-wise addition, this tool is the wrong shape and you
should say so.

## Precedent first

`similar_reactions` and `gather_evidence` before any of this: a rate constant somebody here already
measured for this transformation beats an extrapolation, and a hold time the record shows working
at scale beats a computed conversion. These tools are for when the record is silent or when the
question is explicitly "what if we changed the temperature".
