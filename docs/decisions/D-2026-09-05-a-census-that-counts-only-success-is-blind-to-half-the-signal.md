# D-2026-09-05-a-census-that-counts-only-success-is-blind-to-half-the-signal — the distillation trigger gets a second arm, over recurring failure

**Status:** accepted · **Date:** 2026-09-05 · **Builds on:** D-2026-08-27 (count the trajectories
before building the distiller), D-161 (the anti-feedback rule),
D-2026-08-16-a-second-judge-is-a-second-answer-about-the-same-answer (the measurement it produced) ·
**Does not supersede** D-2026-08-27: its trigger, its definitions and its posture stand unchanged. ·
The gate boundary the same review raised is a separate decision,
`D-2026-09-05-the-gate-follows-behaviour-not-knowledge`.

## Context

An external framework (WikiSkill, arXiv 2608.27454) co-evolves an agent's skills with a persistent
wiki of failure-mode patterns: a maintainer compiles execution traces into the wiki, a proposer reads
the wiki to propose skill edits, and a held-out gate accepts or rolls back. Reviewing whether it is
relevant here surfaced three apparent objections in this repository's own record. Two do not survive
reading, and the third is narrower than it looks.

**The first was that this repository declined an automatic quality gate.** It did not.
`D-2026-08-16-a-second-judge-is-a-second-answer-about-the-same-answer` declined an LLM grader that
**rewrites a user-facing answer in-band**, on the answer hot path. An offline comparison of two
populations of runs over held-out cases is a different object, and the machinery for it is already
here and already in the `ci` target: `evals/ab.py`, `evals/baseline.py`, `make eval-strict`,
`make eval-baseline-check`, `durable/eval_drift.py`.

**The second was D-161's anti-feedback rule** — "raw session transcripts are the concrete case this
rules out". That exclusion is *conditional on D-161's own promotion arithmetic*, stated in the
sentence before it: support is a count of merged notes, so a miner reading anything else "produces
observations that can never accumulate support and therefore never promote — a write-only log with
extra steps". A procedural pattern read from traces has no promotion path into the graph at all; it
informs a proposal that goes through the gate whole. The objection is about a log nobody reads, and
does not reach a store whose defining property is having exactly one reader.

D-161 does contribute something, in the other direction: migration `025`'s CHECK that an observation
may never cite an observation — *"a self-confirming loop wearing the costume of cross-project
evidence"*. A wiki that accumulates from traces produced by agents running the skills that wiki
produced is that loop, and the external framework has no equivalent guard.

**The third objection is real and is this ADR's subject.** D-2026-08-27's census defines a
trajectory as a turn's tool-name sequence and recurrence as the identical sequence appearing in ≥ 2
sessions. That is the right instrument for the literature it names — SkillRL, SkillForge, the
self-evolving surveys — all of which abstract a recurring *successful procedure*. It cannot see a
recurring *failure*: a failure that keeps happening produces divergent sequences that end badly, not
a repeated one. So a corpus dense in repeated mistakes reports **zero** recurring classes and reads
as "not greenlit", when it is exactly the corpus the reviewed framework runs on.

That is D-2026-08-27's own stated worry arriving from the side it did not guard: *"an instrument
invented on the day it is needed measures whatever is convenient that day."* It was defined against
three shapes of the 2026 literature; this is a fourth, and it arrived after.

## Decision

### 1. The census gets a second arm, and the first one's meaning does not change

`chemclaw.cli.trajectory_census` now computes both. The added definitions, each held by a test:

- A **failed call** is a `ToolMessage` with `status == "error"`. Its tool *name* is not on the
  result — it is on the `AIMessage.tool_calls` entry that issued it, joined by `tool_call_id`. Both
  are in `session_messages`: the transcript writer stores the tool exchanges alongside the question
  and the answer, which `api/runner.py` records was a silent regression when they were left out.
- A **failure class** is that tool name. It **recurs** when it errored in ≥ 2 distinct sessions.
- A session **recovered** when the same tool later returned a non-error result in it. This is a
  proxy and is named as one in the code: it is the strongest thing this read-model can say without a
  model reading the transcript.
- A class was **repeated after recovery** when a session that recovered *precedes* a session that
  failed again — the case where the earlier session already demonstrated the procedure the later one
  lacked. Ordering uses the same session timestamps the first arm uses, and a missing stamp counts
  as not-repeated, for the reason D-2026-08-27 gives: an unknown order must not inflate the number
  that greenlights a build.

**The second arm's greenlight:** ≥ 3 recurring failure classes, across ≥ 3 sessions, with ≥ 1
repeated after recovery. **These are chosen by analogy with the first arm's, not measured**, and the
binding term is deliberately the last one — a recovery that precedes a repeat is the hard thing to
observe by accident; the class and session counts are guards against one noisy tool carrying the
verdict on its own.

`generator_greenlit` keeps the exact meaning D-2026-08-27 gave it and still answers *that* ADR's
question about procedure alone; `failure_greenlit` is the new arm; `any_greenlit` is the disjunction
and is what a reader should act on. `tests/test_trajectory_census.py` pins that the second arm cannot
move the first arm's verdict, and pins the blindness itself: a two-session corpus that hits one
failure by two different routes reports zero recurring classes and one recurring failure class.

### 2. A skill loop may not be scored on flag clearance

`D-2026-08-16`'s measurement, taken as a constraint on any future gate rather than as an objection to
one. Of ten flagged answers that cleared on revision, eight cleared by **deletion** — a
`screen_hazards` call offered on a diazo compound, a five-step protocol the user had asked for, a
mechanistic explanation. Its conclusion transfers verbatim: *"Any loop scored on flag clearance
learns exactly that move."* The `promised but not called` class is the sharpest case and is
mechanistic rather than statistical — 8 of 8 were "fixed" by deleting the promise, because a text
reviser structurally cannot call a tool.

So: a skill-evolution gate scores **task outcome** on held-out cases (`evals/ab.py`,
`evals/baseline.py`), never `score_answer`'s `review_required` rate. And its effect size must exceed
the judge's measured margin instability — the same ADR's null control clears 5.1% of flagged answers
on a re-roll with **no edit at all**, so a gate reading that signal cannot resolve anything smaller.

## What is deliberately not built

**The generator.** D-2026-08-27's posture is unchanged and is the reason this ADR ships an
instrument rather than a mechanism: define the measurement, build when it says to. This closes the
half of the measurement that was missing; it does not greenlight anything by itself.

**Nothing about the corpus changed.** Measured 2026-08-27: 0 sessions, 0 turns. Both arms report
zero on it, and the block on building anything is still deployment history rather than effort.

## Consequences

- `make trajectory-census` prints both arms and three verdicts, and `--json` carries the new keys
  additively. A reader who only knows `generator_greenlit` still gets the answer it always meant.
- The second arm reads `session_messages` rows the first arm skips entirely, so a deployment that
  prunes that table by retention now measures its retention window on both arms rather than one.
- **A self-confirming loop is the guard a future generator owes**, imported from D-161's migration
  `025` rather than invented: traces produced by an agent running a distilled skill are not
  independent evidence for that skill. Whoever builds the generator states how it is prevented, in
  its own ADR, before it runs.
- The BACKLOG row "Memory records; it does not change what the next turn does" keeps its trigger
  and gains this second arm, so the day a deployment has sessions the row is still one command.
- One finding from the reviewed framework is recorded because it contradicts the obvious design and
  costs nothing to carry: giving the *executing* agent access to the accumulated wiki measured
  **worse** than not (63.7% → 60.9%), while giving it to the *proposer* was the single largest
  ablation (+15.0pp). If that row is ever built, experience is compiled into skills rather than
  injected into the turn.
