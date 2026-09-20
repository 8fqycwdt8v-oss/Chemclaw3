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
- **`computable`** — this system holds tools that could answer it, **and it was not run**.
  Automatic dispatch is not built, because turning a free-text check into tool arguments means
  inventing which molecule, conformer and solvent. Never report such a check as computed. If it
  matters, call the calculator yourself with arguments you can justify from the record.

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
