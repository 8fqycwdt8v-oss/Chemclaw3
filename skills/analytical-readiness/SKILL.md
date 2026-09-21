---
name: analytical-readiness
description: >-
  Use when the question is whether a number can be trusted or produced — the IPC, method
  readiness, a result on a limit, a stability trend. Stops where method development begins.
tools:
  - check_against_specification
  - estimate_stability_trend
  - system_suitability_report
  - replicate_precision
  - permitted_method_adjustment
  - peak_resolution
  - ich_impurity_limit
  - gather_evidence
  - ask_clarifying_question
---

# Is the analytics ready, and does the number mean what it says

## An IPC and a release test are different instruments

An **in-process control** answers *should I proceed* — is the reaction done, is the wash clean
enough, is it safe to cool. It is fast, it is often less precise, and its limit is a process
decision.

A **release test** answers *is this acceptable* — against a specification, with a validated method,
for a reader outside the lab.

Using one as the other is the mistake to watch for. An IPC that "passes" is not a release, and a
release method is usually too slow to hold a batch on.

Decide the analytic **before** the experiment, not after: `skills/hte-campaign-design` says this for
a plate and it generalises. A batch whose IPC was chosen afterwards is one that cannot answer the
question it was run for.

## The verdict has four values and two of them are not failures

`check_against_specification` returns one row per criterion: `within`, `outside`, `not_measured`,
`indeterminate`. The last two are answers.

- **`not_measured`** is an unanswered question, not a pass. A twelve-row specification with nine
  results has three open items, and a check written as a filter over results would report nine
  passes and look tested.
- **`indeterminate`** is a result the limit cannot be compared with — an area percent against a
  weight percent is the same unit and a different fact.

And `limit_within_uncertainty` flags a result that is inside the specification and
indistinguishable from outside it at the method's own precision. **That is what an analyst
escalates**, and it does not change the verdict because whether the number is under the limit is
arithmetic and whether to investigate is judgment. Report the flag when it is set; a bare pass
hides exactly the case worth a conversation.

## Suitability first, then the result

A result from a sequence that failed system suitability is not a result yet, whatever the
specification says about it. Where suitability numbers are available, read them
(`system_suitability_report` over a whole sequence rather than decomposing a table across
single-peak tools — a resolution computed between the wrong two peaks looks exactly like a correct
one). Where they are not, ask whether suitability was run rather than assuming it.

`replicate_precision` carries the compendial rule for how many injections a given RSD limit needs,
which is the part most often omitted.

## Where this stops

**This system holds no column, gradient, flow rate, wavelength or retention time**, and must not
invent one. Method *readiness* is in scope: is there a method, is it suitable, does it resolve the
impurity you care about, is the change you want inside `permitted_method_adjustment`. Method
*development* is not, and a plausible gradient is worse than no answer because it will be run.

Quoting a parameter from a cited record is not inventing one — repeating what the record says is
always allowed, and saying where it came from is what makes it usable.

## Stability trends

`estimate_stability_trend` fits a line through timepoints and reports where the one-sided 95% bound
meets a criterion, bounded by what ICH Q1E permits extrapolating. Three things to say every time:

- It is **not a shelf life**. It is where a trend meets a limit, under a linear model, from the
  points supplied.
- The **extrapolation window is bounded** deliberately, and a question that needs more than it
  permits needs more timepoints instead.
- A trend that is not significant against its own residual is **no trend**, and reporting a slope
  from three noisy points as a degradation rate is the failure mode here.

## What is still missing, and say so

There is no mass-balance check in this system — assay plus impurities against 100% is the cheapest
signal that an analytical set is wrong, and nothing here computes it. Where the numbers are in
front of you and they do not add up, say that, because a specification check over an internally
inconsistent result set will happily return `within` on every row.
