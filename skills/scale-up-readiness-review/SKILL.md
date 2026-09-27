---
name: scale-up-readiness-review
description: >-
  Use for the whole package before a scale decision — readiness for 200 L, what we know before the
  meeting, which programme carries the most chemistry risk. The deliverable is the unknowns.
tools:
  - gather_evidence
  - assemble_evidence_pack
  - request_development_report
  - similar_reactions
  - conditions_for_similar_reaction
  - condense_protocols
  - read_experiment_protocol
  - rescale_experiment_protocol
  - screen_hazards
  - screen_genotoxic_alerts
  - ich_impurity_limit
  - green_metrics
  - stoichiometry_table
  - recall_observations
  - record_failure
  - ask_clarifying_question
---

# The scale-up readiness review

## The unknowns are the deliverable

Everybody can write the section headings. What a scale-up meeting needs, and what nobody brings, is
**the list of what is not known and the experiment that would close each item**. Put it at the top,
not at the end, and make each row concrete: the measurement, who runs it, and what decision it
unblocks.

A review that reads as complete when three of its sections rest on no evidence is worse than a
short one, because the gaps are what the meeting exists to allocate.

## What this system cannot supply, said first and plainly

Before anything else, know the boundary so the answer does not pretend past it:

- **No critical process parameter, proven acceptable range, design space, tech-transfer package or
  master batch record.** These are denied deliberately. You can report what the runs showed the
  process tolerated; you may not produce a PAR or a design space, and you must not present a
  readiness review as either.
- **No calorimetry, no heat- or mass-transfer model, no mixing or addition-rate model.** The
  thermal numbers come from measurements a person made, via `thermal-safety-assessment`.
- **No project, programme, capacity, headcount or timeline data.** A "which programme has the most
  risk" question can be answered about the *chemistry* from the record, and not about schedule.
- **Never devise a parameter** — no column, part number, gradient, flow rate, retention time, form
  designation or regulatory limit that did not come from a cited source. Quoting a recorded one is
  not that.

Say which of these the question needs, before offering what you can actually support.

## The sections, and where each one's evidence lives

| Section | Where it comes from |
| --- | --- |
| Route and step context | `gather_evidence`, `similar_reactions`, `condense_protocols` over the precedent |
| What we have run | `read_experiment_protocol`, `recall_observations`, and the failures — `record_failure`'s corpus is the most valuable and least read part |
| Conditions and their basis | `conditions_for_similar_reaction`, plus which conditions were *chosen* versus inherited |
| Thermal envelope | `thermal-safety-assessment` — and if there is no calorimetry, that is an unknown, not a section |
| Equipment fit | `unit-operation-sizing`; vessel, working volume, jacket, filter area. If the vessel is unnamed, ask |
| Unit operations | solvent swap, crystallisation, filtration, drying — each with the measurement it rests on |
| Safety and impurities | `screen_hazards`, `screen_genotoxic_alerts`, `ich_impurity_limit`; three separate findings, never merged |
| Mass efficiency | `green_metrics`, `stoichiometry_table` at the target scale |
| Analytics | `analytical-readiness` — is there a method, and does it measure what the decision needs |
| **Open items** | everything above that had no evidence |

Use `rescale_experiment_protocol` to put the charges at the target scale, and carry its caveats
into the open-items list: they are unknowns with names.

## Evidence discipline

Every claim traces to something retrieved. `assemble_evidence_pack` and
`request_development_report` exist for this, and the report harness already drops a synthesized
claim whose citations were not actually retrieved — do not work around that by restating an
uncited claim in prose.

**Keep evidenced history separate from transferred analogy.** "We ran this at 2 kg and got 71%" and
"a similar coupling generally tolerates this base" are different kinds of statement and a reader
making a scale decision needs to see which is which. A playbook is the second kind by construction.

## Reading risk across programmes

Where the ask spans programmes, the honest axis is *what the chemistry needs that nobody has
measured*: no calorimetry on an exothermic step, a crystallisation with no solubility curve, an
impurity with no named control point, a step whose only precedent is one bench run. Rank by that
and cite each. Do not rank by anything schedule-shaped; this system holds none of it.

## Close with the decision, not a summary

Name what the review supports: proceed, proceed with the named measurements first, or do not scale
this step yet. Then the open items. A readiness review that ends in a summary paragraph has made
the reader do the part you were asked for.
