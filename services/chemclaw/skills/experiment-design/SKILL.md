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
   minimize E-factor. If the user names several, pick the one they lead with and say so; v1
   optimizes one objective.
2. **Choose the decision variables** the user can actually change: continuous (temperature,
   time, equivalents, concentration) with realistic bounds, categorical (solvent, catalyst,
   base) with the specific options in play. Do not invent variables the lab cannot set, and
   keep bounds physically sane.

   **When a categorical option is a molecule, give its structure.** Set the parameter's
   `structures` (category label → SMILES) for ligands, bases, solvents and catalysts. Each
   option is then described by computed electronic descriptors instead of being an opaque
   label, which is what lets the model say anything at all about an option nobody has run:
   without it the surrogate's prediction for an untried ligand is just the average of the
   ones you did run. It costs one fast calculation per option, cached thereafter. Two limits
   worth stating to the user: the descriptors are **electronic only** — two ligands differing
   mainly in bulk look similar — and a wrong SMILES silently describes the wrong molecule, so
   only supply structures you are sure of.
3. **Seed with real runs.** Gather the transformation's history (`find_similar_reactions`, an
   `optimization-campaign` note) and turn each run into an observation: its conditions →
   objective value. Mark `provenance` "measured" for lab data, "predicted" if you filled a
   value from a model. With no runs on file, the tool returns space-filling seed points — say
   the campaign is starting cold.

## Narrowing a categorical space before you frame it

When the user brings more candidate options than the campaign can carry — twelve ligands, eight
bases, a substrate scope — the choice of *which* to put in the design is itself a decision, and
it is made before the tool is called. A fast electronic ranking
(`compute_electronic_properties`, `predict_site_reactivity`; judgment in
`reactivity-descriptors`) is a legitimate way to shortlist, because ranking is what a
semiempirical method is actually good at and the cost of being wrong is one wasted run.

Two conditions on doing this. Say that the shortlist came from a calculation, not from data, so
the user can overrule it. And keep at least one option in the design that the ranking did *not*
favour — a campaign seeded only with what a model already liked cannot discover that the model
was wrong.

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
