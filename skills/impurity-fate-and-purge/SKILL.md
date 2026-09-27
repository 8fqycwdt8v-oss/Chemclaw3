---
name: impurity-fate-and-purge
description: >-
  Use when an impurity must be tracked rather than found — where it forms, whether it purges, a
  mutagenic alert, a peak that grew on scale-up. Maps formation to control, and never invents a
  purge factor.
tools:
  - enumerate_degradants
  - screen_genotoxic_alerts
  - ich_impurity_limit
  - screen_hazards
  - enumerate_bond_cleavages
  - predict_site_reactivity
  - similar_reactions
  - reactions_making_substructure
  - gather_evidence
  - recall_observations
  - check_against_specification
  - record_knowledge_note
  - ask_clarifying_question
---

# Where an impurity comes from and where it goes

## Two questions, and only one of them has tools

**Formation** — what could produce this structure — is something the record and the reactivity
tools can speak to. **Fate** — how much of it survives each unit operation — is measured, and this
system holds no model for it. Keep them apart in the answer; the second is where overclaiming does
real damage, because a control strategy rests on it.

## Purge factors are measured, never predicted

This is the load-bearing rule and it has already been broken once in this system's history: before
the ICH tables shipped, an invented purge factor and invented acceptable-intake limits were what
the model produced. `skills/safety-screening` records that, and this skill inherits the refusal
without softening it.

A purge factor is a ratio somebody measured across a specific step — a wash, a crystallisation, a
carbon treatment, a distillation — on this material at this scale. You may **cite** one from the
record (`gather_evidence`, `recall_observations`). You may **ask** for one. You may reason about
the direction: a highly water-soluble impurity is likelier to purge in an aqueous wash than one
that co-crystallises. You may not produce a number.

Where the question is "will it purge", the honest answer names the step where it would be measured
and, if the record has a comparable measurement, cites it as a comparable rather than as an answer.

## Building the map

Work the route step by step, and for each step ask three things:

1. **Could it form here?** Over-reaction, under-reaction, the reagent's own impurities, a
   regiochemical or stereochemical sibling, a degradation product of the product.
   `enumerate_degradants` for the hydrolysis and oxidation families, `predict_site_reactivity` and
   `enumerate_bond_cleavages` for where the molecule is vulnerable, `reactions_making_substructure`
   and `similar_reactions` for what this corpus has actually seen.
2. **Is it carried in?** An impurity in a starting material or a reagent is the one nobody looks
   for, and it is frequently the answer when a batch behaves differently with no process change.
   Ask about the supplier and the lot before theorising.
3. **Where is it controlled?** Name the step and say whether the control is measured or assumed.
   An impurity with no named control point is an open item, and saying so is more useful than a
   plausible story about the wash.

## The mutagenic question is a different question

`screen_genotoxic_alerts` answers "will this need a mutagenic-impurity control strategy", and
`ich_impurity_limit` gives the Q3C/Q3D limits. Neither is a toxicology assessment and neither
clears anything. Report them as their own finding, separate from the process-safety screen
(`screen_hazards`), because mixing the two is how a table of energetic motifs gets read as an ICH
M7 assessment.

For a nitrosamine question, pass the **whole route** rather than one step: the formation alert is
about an amine meeting a nitrosating agent, which is a property of the sequence.

## When the impurity appeared on scale-up

This is the common live case and it has a short list of usual causes, none of which is chemistry
that changed:

- **Feed-point local excess** — a reagent dosed into a large vessel sees a huge local excess before
  it mixes. The classic source of an over-reaction impurity that the bench never showed.
- **Longer hold times** — a filtration that takes a shift is a hold the procedure does not mention,
  and a wet cake is where hydrolysis happens.
- **Higher temperature for longer** — a jacket that cannot hold the ramp, or an exotherm that took
  the batch above the bench setpoint.
- **A different lot** of a starting material or reagent.

Where several are in play and the evidence is genuinely split, that is
`skills/competing-hypotheses` rather than an argued single answer.

## Closing it out

`check_against_specification` puts a measured level against its limit and returns four verdicts —
`not_measured` and `indeterminate` are answers, not failures, and both are worth reporting as what
they are. Where the investigation reaches a finding, `record_knowledge_note` it with its evidence:
an impurity's origin, once established, is the thing the next campaign most needs and the thing
least likely to be written down.
