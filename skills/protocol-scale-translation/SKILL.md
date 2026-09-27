---
name: protocol-scale-translation
description: >-
  Use when a procedure has to run at a different size — "we ran this at 1 g, give me the 2 kg
  version", "scale this to the 250 L reactor", "what changes going from the bench to the kilo
  lab", a tech-transfer question about an existing protocol. Scales the charges deterministically
  and spends its whole effort on the quantities that do *not* scale, which is where a scaled batch
  actually fails.
tools:
  - rescale_experiment_protocol
  - read_experiment_protocol
  - find_experiment_protocols
  - draft_experiment_protocol
  - stoichiometry_table
  - green_metrics
  - screen_hazards
  - heat_removal_capacity
  - semibatch_accumulation_profile
  - similar_reactions
  - gather_evidence
  - recall_observations
  - ask_clarifying_question
---

# Translating a protocol to another scale

## The charges are arithmetic; everything else is the job

`rescale_experiment_protocol` multiplies every charge by one factor off the limiting line and
leaves equivalents alone. That part is deterministic and you do not have to think about it.

What it also returns is a `caveats` list — every quantity it refused to scale, with the reason.
**Those are the answer.** A chemist who gets a correctly scaled charge table and no caveats has
been handed a document that will produce a different reaction from the one the procedure describes,
and will not find out until the batch is running.

So: report every caveat, beside the charges, in the chemist's own terms. Do not compress them into
"note that some times may need adjustment".

## Why each one is on the list

- **Addition time.** On the bench a dose is however long the syringe pump took. At scale it is set
  by what the jacket can remove and by how much unreacted reagent may accumulate — and those two
  pull in opposite directions, which is the whole reason semi-batch addition is a decision.
  `heat_removal_capacity` and `semibatch_accumulation_profile` are what settle it, **from measured
  numbers**. If those bundles are not enabled, say the dose time is unresolved rather than carrying
  the bench number forward as though it were a specification.
- **Filtration and drying.** Limited by area, cake resistance and heat transfer through a deeper
  bed. A twenty-minute filtration becomes a shift, and the intermediate now sits wet on the filter
  for that long — which is a hold time nobody wrote down and a stability question nobody asked.
- **Ramps and cooling.** Surface-to-volume falls as the vessel grows, so the same ramp rate is not
  available. This is also why a bench reaction that "never got warm" is not evidence about 20 kg.
- **Reaction time.** Carried across unchanged, because it is a property of the chemistry — but it
  was *measured* under bench mixing and bench heat transfer. If the reaction is fast relative to
  mixing, the scaled batch is a different experiment at the same nominal time.
- **Concentration.** Held, which is what scaling every charge by one factor means. Check the result
  fits the vessel's working volume, including the work-up, which is usually the larger number and
  is the thing nothing in this system knows.

## Ask for the vessel before you answer

A scale is not a batch. The questions that decide whether the scaled protocol is runnable are the
ones the target scale does not carry: which vessel, what working volume, what the jacket can do,
what the filter area is. `ask_clarifying_question` rather than assuming — and if the chemist does
not have them yet, say which of the caveats stay open until they do.

## What changes that is not a number

- **The hazard picture.** Re-run `screen_hazards` on the scaled protocol, and say the sentence:
  a screen flags and never clears. A quench that was a few millilitres of water is now an exotherm
  of its own, and it is usually absent from the procedure as written.
- **The thermal envelope.** Anything with a meaningful exotherm needs `thermal-safety-assessment`
  before the scaled protocol is run, and that skill's first move is to ask for calorimetry. A
  rescale is not a safety assessment and must never be presented as one.
- **Mass efficiency.** `green_metrics` on the scaled charges is where a solvent volume that was
  invisible at 1 g becomes the dominant cost and the dominant waste.
- **Order of addition.** Unchanged by the arithmetic, and at scale it is frequently the thing that
  matters most: a reagent added as a bolus on the bench sees a local excess at the feed point in a
  250 L vessel, which is the classic source of an impurity that never appeared in development.

## Check the record before you compute

`similar_reactions`, `gather_evidence` and `recall_observations` first. If this organisation has
already run this transformation at scale, what happened is worth more than any arithmetic here —
including, and especially, if it went badly. A scale-up that failed once is the most useful thing
in the corpus and the easiest to skip past.

## Storing it

The tool returns a proposal and stores nothing. Decide the open quantities with the chemist, then
`draft_experiment_protocol` with the head's `parent_revision` so the scaled version is an ordinary
revision they can diff and reject. Do not store a scaled protocol whose caveats are still
unresolved without saying, in the `change_note`, which ones are.
