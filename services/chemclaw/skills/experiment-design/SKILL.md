---
name: experiment-design
description: >-
  Judgment for answering "which experiment should I run next?" — turning a vague optimization
  goal and scattered historic runs into a concrete Bayesian-optimization problem, calling
  suggest_next_experiment, and presenting the proposal as something a human still runs.
tools:
  - suggest_next_experiment
  - propose_knowledge_note
---

# Experiment design

Holds the *judgment* for the next-experiment question; the mechanics are in
`suggest_next_experiment` (BoFire's ask step). A good suggestion is only as good as the problem
you hand it, so most of the work is framing, not the call.

## Frame the problem from evidence

1. **Fix the objective.** One scalar, and its direction — maximize yield, minimize an impurity,
   minimize E-factor. If the user names several, **pick the one they lead with, say so
   explicitly, and call the tool for that one only.** Do not dress up a single-objective call
   as if it were a real multi-objective/Pareto optimization — if you also want to speak to the
   other objective, do it as a separate, clearly-labeled qualitative read of the cited evidence
   (e.g. "separately, the data shows degassing is what controls the impurity"), not as
   "candidates" implied to come from a trade-off computation that did not actually run.
2. **Choose the decision variables** the user can actually change: continuous (temperature,
   time, equivalents, concentration) with realistic bounds, categorical (solvent, catalyst,
   base) with the specific options in play. Do not invent variables the lab cannot set, and
   keep bounds physically sane — a bound outside the range a real run of this reaction class
   could survive (e.g. well past a solvent's reflux/decomposition point) is not something to
   quietly accept and flag only after the tool returns a point there. Confirm the bound with
   the chemist *before* calling the tool. The same applies to genuinely ambiguous input (e.g.
   a self-correcting or contradictory statement of the range) — ask which value they meant
   rather than silently picking an interpretation and proceeding.
3. **Seed with real runs.** Gather the transformation's history (`find_similar_reactions`, an
   `optimization-campaign` note) and turn each run into an observation: its conditions →
   objective value. Mark `provenance` "measured" for lab data, "predicted" if you filled a
   value from a model. With no runs on file, the tool returns space-filling seed points — say
   the campaign is starting cold.

## Call and present

- `suggest_next_experiment(problem, observations, count)` returns candidate point(s). Ask for a
  small batch (1–3) unless the user wants a screen.
- **These are proposals, not results.** Present each as conditions to run, note it rests on the
  cited historic runs, and be explicit about what the model is extrapolating (a solvent never
  tried, a temperature beyond the observed range) and any safety/selectivity risk there.
- If the user wants the batch recorded, draft it through `propose_knowledge_note` (type
  `experiment-batch`) so a human approves it via the PR-gate before it becomes plan-of-record.

## One shot vs. a campaign

`suggest_next_experiment` is the single, human-in-the-loop suggestion: it answers "what should I
run next?" inline, from observations you already have.

A fully automated loop that proposes, evaluates its own objective, and iterates over many rounds is
`start_optimization_campaign` — reach for that only when the objective can be computed without a
human in each round. It is durable and long-running, so it returns a job id immediately; poll it
with `get_durable_job_status`. Set `publish_to_graph` when the recommendation should be proposed as
a PR-gated note rather than only reported in chat.
