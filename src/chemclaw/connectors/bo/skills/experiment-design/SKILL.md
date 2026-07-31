---
name: experiment-design
description: >-
  Judgment for answering "which experiment should I run next?" — turning a vague optimization
  goal and scattered historic runs into a concrete Bayesian-optimization problem, calling
  suggest_next_experiment, and presenting the proposal as something a human still runs.
tools:
  - suggest_next_experiment
  - generate_screening_design
  - propose_knowledge_note
---

# Experiment design

Holds the *judgment* for the next-experiment question; the mechanics are in
`suggest_next_experiment` (BoFire's ask step). A good suggestion is only as good as the problem
you hand it, so most of the work is framing, not the call.

**First decide the question is this one.** A surrogate needs a scalar objective over bounded
variables and enough runs to fit. When the chemist is instead walking one step day by day, and
what they want is a reading of where the series got to and one diagnostic to run next, that is
the `experiment-progression` skill — reasoned from the record, not from a model. Do not force a
line of enquiry into a design space just because a design space is what this tool takes.

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

- `suggest_next_experiment(problem, observations, count)` returns the candidate point(s), the
  `campaign_id` they belong to, and the `calc_refs` behind the decision space. Ask for a small
  batch (1–3) unless the user wants a screen.
- **These are proposals, not results.** Present each as conditions to run, note it rests on the
  cited historic runs, and be explicit about what the model is extrapolating (a solvent never
  tried, a temperature beyond the observed range) and any safety/selectivity risk there.
- **Quote the `campaign_id` back.** The campaign is identified by its decision space, so asking
  again about the same problem — with the run you just did added to `observations` — accumulates
  onto the same campaign instead of starting over. That id is how a chemist, or you in a later
  session, picks the thread back up.
- If the user wants the batch recorded, draft it through `propose_knowledge_note` with type
  `experiment-proposal` so a human approves it via the PR-gate before it becomes plan-of-record.
  Pass the returned `calc_refs` on that note: the descriptors that shaped the space came from real
  calculations, and citing them is what lets a stale one be traced to the experiments it
  suggested.
  That is the same type a reasoned proposal uses — the note body says which path produced it, and
  a reviewer approving "run this next" should not have to learn two note kinds for one decision.
  `bo-candidate` is a different thing and is not yours to write: it is what a *durable* campaign
  (`start_optimization_campaign`) mints for itself when a round completes.

## One shot vs. a campaign

`suggest_next_experiment` is the single, human-in-the-loop suggestion: it answers "what should I
run next?" inline, from observations you already have.

A fully automated loop that proposes, evaluates its own objective, and iterates over many rounds is
`start_optimization_campaign` — reach for that only when the objective can be computed without a
human in each round. It is durable and long-running, so it returns a job id immediately; poll it
with `get_durable_job_status`. Its recommendation is always proposed as a PR-gated note, so a human
reviews it; you do not decide whether the campaign is recorded.

State the campaign's `rationale` in the chemist's terms — the question this campaign should answer
and what prompted it — not a restatement of the parameter ranges. It is stored with the run and
printed on the note a reviewer signs, and it is what a session six months from now will read when
it asks `find_past_jobs` whether this optimization has already been done. Before starting a
campaign, run that search: a near-identical campaign that already ran is evidence to build on
(seed the new one with its observations), and only an *identical* one rejoins its result for free.

## The other DoE question: a categorical screen, not an adaptive suggestion

`suggest_next_experiment` and `start_optimization_campaign` both propose points *adaptively*, one
batch at a time. Sometimes the real ask is the classical, complete-up-front design instead —
"give me every combination of these catalyst/solvent/base choices to screen" before narrowing to
BO. That is `generate_screening_design(problem)`: a full-factorial design over the problem's
*categorical* parameters only. It raises if `problem` names a continuous parameter (temperature,
equivalents) rather than silently dropping it — reformulate a continuous factor as discrete levels
(e.g. "low"/"high") to include it, or use the adaptive tools instead if the space is genuinely
continuous. Present the returned runs as a batch a human executes, exactly like a BO suggestion.
