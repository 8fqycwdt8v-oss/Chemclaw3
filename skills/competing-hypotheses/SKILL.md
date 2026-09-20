---
name: competing-hypotheses
description: >-
  Use when a result is puzzling and several explanations are genuinely in play — an impurity that
  appeared on scale-up, a yield that collapsed for no obvious reason — and the useful answer is
  the field of candidate causes with the evidence weighed across it rather than one explanation
  argued in a single pass. Covers when to start a tournament and when not to, and how to read a
  ranking back to a chemist without overclaiming: an Elo here orders the candidates this run
  generated, it is not a probability that any of them is true.
tools:
  - rank_competing_hypotheses
  - get_durable_job_status
  - gather_evidence
  - expand_note
  - suggest_next_experiment
  - screen_hazards
---

# Competing hypotheses

For the case where the honest answer is "here are four things it could be, and here is the one
experiment that separates them". `rank_competing_hypotheses` generates candidate explanations from
several independent framings at once, has each critiqued, ranks them by judged pairwise comparison
seeded with retrieved evidence, and files the experiments that would settle them.

## 1. Decide whether a tournament is the right instrument

Three cases, and only the first is this one.

- **Several explanations are genuinely in play, and they are qualitative or mechanistic.** This
  skill. The value is the *field* — what was considered and what came second — not a single answer.
- **One scalar objective over bounded numeric variables, with runs already done.** Use
  `suggest_next_experiment`. A fitted surrogate is a stronger instrument than a judged comparison,
  and it produces a calibrated number where a tournament produces a preference ordering. Do not
  run a tournament over a decision space.
- **Really only one explanation is in play.** Answer directly. A tournament over a field of one
  tells nobody anything, and the run costs roughly thirty model calls.

It is a durable job. It returns a job id; poll with `get_durable_job_status`. Never present a
ranking you have not received — if the launch failed, say what was attempted and what it would
have produced, and fall back to `gather_evidence` labelled as an ad-hoc cited analysis.

## 2. Read the rating for what it is

**An Elo here is a preference ordering over the hypotheses this run generated.** It is not a
probability that a hypothesis is correct, and it is not evidence about the chemistry. Say so when
you present one. The rating carries a standard error and a comparison count, and all three go to
the chemist together — a bare number will be read as a confidence.

**`leader_is_decisive: false` is an answer, not a failure.** It means the top hypotheses sit
inside their own uncertainty, so the order is not yet evidence. Report it as given. Do not resolve
an unseparated field into a confident leader because a leader reads better; the ranking is exactly
the thing that says you cannot yet.

Two numbers bound how much to trust the order:

- **`comparisons`** — a hypothesis compared once has been barely examined, whatever its rating.
- **`position_bias`** — how far the judge preferred whichever hypothesis it was shown first. 0.0 is
  no order effect. If it is high, the ranking is weak evidence and the *objections* and the
  discriminating checks are the useful output rather than the order.

## 3. Say what was ruled out, not only what won

The field is the point. A chemist returning to a stalled series wants the branches that were
eliminated as much as the one that leads — the same reason `failure-mode` notes exist. The result
carries every objection raised against each hypothesis and every candidate the screen rejected;
surface the objections against the leader especially, because they are the reasons it might still
be wrong.

## 4. Be exact about what was and was not run

Each hypothesis gets a discriminating check, and the check's `kind` decides what happened:

- **`physical`** — it needs a laboratory. The tournament writes an `experiment-proposal` note for
  the top few and returns their ids. It is a proposal: a human decides whether to run it.
- **`computable`** — this system's own tools can answer it, and the tournament runs it when every
  argument is *grounded*. Grounded means each molecule comes from a `compound` note in this
  deployment's corpus, resolved to a SMILES already in the record, and every other argument is
  either the target's own default or a value the job itself validates. The result carries a `ran:`
  line naming the exact call — the swept axis where there was one, and the job's own **unstated
  defaults**. Read it before reading the verdict. A number computed in the default solvent answers
  a different question from one computed in the solvent the hypothesis is about, and
  `symmetry_numbers=None` on the line means a reaction reported no free energy at all, or that a
  species ranking was computed at sigma=1 — neither of which is visible in the number.
- A computable check may also name a **reviewed procedure** — `tautomer-resolution`,
  `microspecies-profile`, `stereoisomer-ranking`, `bond-strength-survey` and the others this
  deployment enables. That is the only shape that answers a question about structures *nobody
  wrote down*: the procedure enumerates a molecule's tautomers, protonation states or breakable
  bonds and calculates over what it found. Its settings were measured rather than chosen, so where
  one fits the question it beats assembling the same steps by hand — and the `ran:` line names
  which inputs stayed at their defaults. Every such procedure ends by writing its own report, so
  the `detail` you read is a report **over** real results rather than a calculator's output
  directly; a procedure that runs no calculation at all is refused for exactly that reason.
- A computable check that could **not** be grounded is reported as not run, and the outcome
  carries a `refusal_code` naming which grounding rule failed — the subject was not a compound
  note, or had no structure, or the tool needs an argument nothing in the record supplies, or the
  swept axis is not one the job validates. Report the reason; do not work around it. A job taking
  atom indices, a torsion or a bond to cleave is refused because choosing them is choosing the
  answer — where a procedure enumerates those candidates first, name it instead
  (`bond-strength-survey` is exactly this), and where none does, the check is `physical`.
- Only the top few checks run, bounded by `hypothesis_max_calculations`, and a swept axis is
  bounded separately by `hypothesis_max_sweep_values` — one check sweeping a dozen solvents is a
  dozen conformer searches, which the check cap does not see. The ranking decides which checks are
  worth the compute, so one below the cut is reported as not run *for budget* rather than queued;
  an over-wide axis is refused rather than trimmed, because the values are reported beside the
  answer and dropping some would make that report wrong.

Never report a check as computed because its `kind` says `computable`. The verdict and the `ran:`
line are what say it happened.

## 5. Then name one experiment

The ranked field is the reasoning; it is not a substitute for an answer.
`skills/experiment-progression` §5 is right that "a list of five is a way of avoiding the
question" — the technician runs one experiment tomorrow. Lead with the single next check, and let
the table follow as the argument for it. Where the field did not separate, the right next check is
the one that best discriminates the hypotheses that are tied.

Safety is not optional. If a proposed check moves into a regime the series has not been in — a
higher temperature, a new oxidant, a change of scale — run `screen_hazards` on it or say plainly
that a safety review is needed first.

## What this skill is not for

Ranking answers, protocols or documents. It ranks *hypotheses*, and the thing that makes that
meaningful is `refuted_if`: a hypothesis with no observation that would contradict it is refused
before the tournament starts.
