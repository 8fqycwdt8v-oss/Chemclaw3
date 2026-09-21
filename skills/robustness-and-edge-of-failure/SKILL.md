---
name: robustness-and-edge-of-failure
description: >-
  Use in late development when the question is what the process tolerates rather than where the
  optimum is — robustness, the range we can hold, edge of failure before transfer.
tools:
  - generate_screening_design
  - suggest_next_experiment
  - campaign_progress
  - predict_outcome
  - structure_experiment_request
  - draft_experiment_protocol
  - experiment_arms_from_campaign
  - check_against_specification
  - similar_reactions
  - gather_evidence
  - ask_clarifying_question
---

# Robustness, and where the process falls over

## This is not optimization, and using an optimizer here answers a different question

An optimization campaign searches for the best point. A robustness study asks whether a **region**
can be held — and the two want different designs, different run counts and different conclusions.

If the chemist is still improving the process, that is `experiment-design` and a BO campaign. If
they have a setpoint they intend to run at and want to know what happens around it, this is a
deliberate perturbation around that point: each factor moved to the edges of the range the plant
can actually hold, with the setpoint replicated as the control.

`suggest_next_experiment` will propose the *informative* point, which in a robustness study is
usually not the point you want — you want the corners you are claiming, not the place the surrogate
is most uncertain. Use `generate_screening_design` over the ranges, and say what is confounded if
you reduce it.

## The ranges come from the equipment, not from the chemistry

Ask (`ask_clarifying_question`) what the plant can actually hold: how tightly the jacket controls
temperature, how reproducible the dose time is, what the charge accuracy is, how long the batch may
sit before the next step. **Those tolerances are the levels.** A ±10 °C robustness study on a
process the plant holds to ±2 °C is a study of a process nobody will run; ±2 °C when the plant
holds ±10 is a claim that will not survive.

Hold times deserve their own factor at scale. A step that is robust at two hours and fails at eight
is one whose bench version never waited.

## What you may conclude, and what you may not

**This system holds no critical process parameter, no proven acceptable range and no design space,
and this skill does not create one.** That denial is deliberate. What you may say is exactly what
the runs showed:

- *"Across the tested range of X, the outcome stayed within specification"* — with the range, the
  number of runs and the assay noise stated.
- *"At the low end of Y the impurity rose above its limit"* — an edge of failure that was actually
  observed.

What you may **not** say is that a parameter is critical or non-critical, that a range is proven
acceptable, or that a design space has been established. Those are regulatory terms with a
procedure behind them, and a screen is not it.

Where a chemist asks for a PAR or a design space in those words, say what is missing rather than
producing a near-miss: an answer shaped like the thing they asked for is the one that gets pasted
into a document.

## Replication is not optional here

A robustness claim is a claim about *noise*. Without replicates at the setpoint there is no pure
error estimate, so "the outcome did not change" is indistinguishable from "we could not have seen a
change". `campaign_progress` takes an assay noise for exactly this reason — get that number, from
replicates or from the method, before concluding anything about a difference.

A robustness study with no replicates and a flat result is the most confidently wrong output
available in this area.

## Reading it

`check_against_specification` per run rather than a yield comparison: robustness is about staying
inside limits, and a run that dropped five points of yield while staying in specification is a
different finding from one that stayed on yield and failed on an impurity. Report per criterion.

Where a factor genuinely did nothing across its whole tested range, say that plainly and say the
range — it is a useful result, and it is the one most often overstated into "this parameter does
not matter".
