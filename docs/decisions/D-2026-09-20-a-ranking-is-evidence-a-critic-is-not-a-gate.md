# D-2026-09-20-a-ranking-is-evidence-a-critic-is-not-a-gate — hypotheses are generated in parallel, ranked by judged comparison, and never deleted by a model

**Status:** accepted · **Date:** 2026-09-20 · Re-opens the capability
`D-2026-08-15-a-capability-that-ships-off-is-not-a-capability` deleted, on the terms that ADR set.
Does **not** supersede `D-2026-08-16-a-second-judge-is-a-second-answer-about-the-same-answer`: its
decline stands and this change is built to obey it.

## Context

The ask: generate hypotheses with many parallel agents, critique them so only valuable ones reach
the chemist, rank them with an Elo number, and propose the experiments that would confirm or
disconfirm each — running them where the system's own tools can.

Three things in this tree already bear on that, and two of them are refusals.

**The nearest previous attempt was built and deleted.**
`D-2026-08-13-the-challenge-panel-is-generated-per-task-not-declared` built N independently-briefed
critics with quorum voting; `D-2026-08-15` deleted it with the specialist team — 1,442 lines of
agent code reachable in no shipped configuration — and named the terms for a return: a future team
"needs a reason to spawn that survives contact with `reject_widening` — isolation and parallelism
do, 'reach a tool I lack' does not — and a measurement whose denominator does not depend on the
model volunteering the behaviour being measured." Both conditions are met here and both are
discharged below.

**A critic empowered to change what shipped was measured at zero.** `D-2026-08-16` ran it: 39
flagged answers, all revised, 10 cleared — against a null control of re-scoring the same answers
unchanged, which cleared 2.0 per roll. The non-degenerate benefit was zero. Eight of the ten clears
were deletions, including a five-step protocol the user had explicitly asked for, and one answer
cleared by fabricating citations. Its conclusion: "Any loop scored on flag clearance learns exactly
that move."

**And a ranking score already exists in this tree and is deliberately thrown away.**
`science/bo/engine.py:323` drops BoFire's acquisition value before it reaches the model, because it
is "a ranking quantity in the strategy's own units, not a statement about the chemistry — and
carrying it would invite reading it as a confidence."

## Decision

Ship it, as a Temporal job, with the critic stripped of the power to delete and the rating stripped
of the power to look like a probability.

### It is a durable job, not a turn, and that was not a preference

The obvious shape — the model spawns helpers with `task` and ranks what comes back — cannot work,
for two independently sufficient reasons.

`agent/loop_cap.py` enforces `harness_max_loop_iterations` (25) as a **turn-wide** budget shared
across every branch of a fan-out. Ten generators plus the `n·log2(n)/2` comparisons a ranking needs
exhausts it several times over, and the failure is silent: the turn ends `loop_capped` with a
partial field rendered as a complete one.

And `agent/subagents.py:495` attenuates a helper to `caller − side_effecting_tools()`, which
contains `compute_xtb_energy`. **A helper cannot run a calculation** — so the half of the ask that
says "if the tools can settle it, it should simply happen" is unreachable from inside the shape that
looked obvious. An activity has neither limit.

### The rating is a Bradley-Terry fit reported with two intervals

Sequential Elo is order-dependent — the same comparisons in a different sequence give different
numbers — and yields no interval. Both are wrong for a fixed pool judged at once, and
`retrieval/fanout.py` already records why order-dependence is unacceptable where a chemist can see
it. So `hypotheses/rating.py` fits the model Elo approximates, on the Elo scale, with a Normal prior
that bounds the complete-separation case (an unbeaten hypothesis otherwise has an unbounded
maximum-likelihood rating, which is the common case in a small field rather than an edge case).

**Which error to report was got wrong first and fixed by measuring.** The likelihood depends only on
rating *differences*, so a marginal variance is mostly uncertainty about where the whole field sits,
and the marginals are correlated. Measured on twenty drawn comparisons between two hypotheses: the
marginal is ±285, combining two of them in quadrature gives ±403, and the difference is actually
known to ±76. The first implementation used the quadrature form and called a clean five-nil sweep
undecided. Two identified quantities are reported instead — each rating's error *within the field*,
and the pairwise difference from the full covariance — and `tests/test_hypotheses.py` pins them to
each other through an exact relation that a marginal cannot satisfy.

`Separation.decisive` is a posterior probability, not a significance test, because the fit has a
proper prior. It is explicitly a probability **about the ranking**: how confident the fit is that the
judges prefer one hypothesis, and nothing about either being true. That is this change's answer to
`engine.py:323`, and it is answered by construction rather than by promising to be careful —
`report.py` cannot print a rating without its interval and its comparison count, and refuses to name
a leader at all when the top pair is not decisive.

### The critic cannot delete anything

Only `hypotheses/screen.py` removes a candidate, by two rules a reader can check by hand: no usable
refutation condition, or the same refutation condition **and** the same claim as a survivor. Model
objections enter the tournament as evidence and cost a rating. This is the direct consequence of
`D-2026-08-16`: a critic scored on rejection learns to reject, and a ranking is reversible where a
deletion is not.

The de-duplication rule is worth stating separately, because prose similarity is exactly the
instrument `D-162` refuses ("a pattern-matched motive is indistinguishable downstream from
testimony"). The identity of a hypothesis here is its *refutation condition*, so two claims that
would be settled by the same experiment are only merged when the claims agree too. Two different
explanations discriminated by one experiment is the interesting shape, and collapsing it would
delete a real alternative silently.

### What it settles, and what it refuses to pretend to settle

A `physical` check becomes an `experiment-proposal` note in the shape
`skills/experiment-progression` §5 already requires. A `computable` check is **derived, reported and
not run**: `run_computable_check` returns `not-run` with its reason. Turning a free-text check into
validated tool arguments means inventing which molecule, which conformer, which solvent — and
shipping a plausible dispatcher would put fabricated inputs behind a verdict a chemist reads as
computed. That is worse than the gap, so the gap is stated and filed.

**Revisit when:** a structured check type exists whose arguments are validated against the target
tool's own signature offline, the way a `connection:` block already is. `BACKLOG.md` carries the
row; the file that would show it had fired is `durable/hypothesis_tournament.py::run_computable_check`
losing its early return.

### The tension with "propose exactly one experiment"

`skills/experiment-progression/SKILL.md` §5 is emphatic: "The technician runs one experiment
tomorrow. A list of five is a way of avoiding the question." That rule is right about what it
protects, and this feature does not overturn it. `report.summarise` leads with the single next
check and the ranked field follows as the argument for it. The list is the reasoning made
checkable, not a substitute for the answer.

## The measurement, and its honest limit

`evals/hypothesis_tournament.py` simulates the instrument against a constructed ordering, with a
shuffled ordering as the null `D-2026-08-16` requires. Measured over 300 runs per cell:

Reproducible with `make hypothesis-recovery`, at 1,000 runs per cell:

| judge accuracy | comparisons | top-1 | null top-1 | Spearman | null Spearman |
|---:|---:|---:|---:|---:|---:|
| 0.50 | 20 | 0.101 | 0.101 | +0.008 | −0.009 |
| 0.55 | 20 | 0.145 | 0.101 | +0.132 | −0.009 |
| 0.65 | 20 | 0.238 | 0.101 | +0.350 | −0.009 |
| 0.75 | 20 | **0.392** | 0.101 | +0.563 | −0.009 |
| 0.90 | 20 | 0.682 | 0.101 | +0.827 | −0.009 |
| 1.00 | 20 | 1.000 | 0.101 | +0.961 | −0.009 |

A perfect judge recovers the ordering exactly, so the pairing and the fit are sound. A judge with
*no* information sits exactly on the null, which is the control that matters and the one this eval
failed until the labels were fixed (below).

**The row that matters most is the 0.75 one.** At a plausible judge accuracy the top-rated
hypothesis is genuinely best **under 40% of the time** — four times the null, and nowhere near good
enough to present as an answer. That number is the empirical case for `leader_is_decisive` and for
the report's refusal to name a leader it cannot separate, and it is pinned in
`tests/test_hypothesis_eval.py` rather than left as a caveat somebody could drop.

**An earlier draft of this ADR published 0.48 for that row, and it was inflated by a defect in the
thing being measured.** `pair_round` broke a Swiss score tie by hypothesis *id*, so lexically-early
hypotheses drew a systematically easier bracket; because a Bradley-Terry fit is opponent-strength
aware, that turned identical records into different ratings. Measured with a coin-flip judge — zero
information, so every point of spread is an artefact — a field of ten came out with a **143-Elo
monotone spread ordered by id**, wider than the standard errors printed beside it. Production ids
are hashes of the statement, so rephrasing a hypothesis moved it up the chemist's table.

The eval could not see it, because its ground truth was `h0 > h1 > …` — the same order the bracket
rewarded. A judge carrying literally zero information scored Spearman **+0.22** with
`beats_null=True`. The null was shuffled and the *labels* were not, so the control did not control
for the one artefact the instrument had. Both are fixed together: the tie breaks on input position
and the workflow permutes the field by a hash of the question, and the eval re-assigns ids to truth
ranks every run. The corrected numbers make the argument for `leader_is_decisive` stronger.

**What this does not measure is whether a language model judging real chemistry is an accurate
judge.** It cannot: the ground truth is constructed. `backtest_shape()` states the corpus backtest
that would settle it — truncate `optimization-campaign` series before the decisive run, rank, and
compare against the recorded cause with two nulls — and records that it has never run, for
`evals/delegation.py`'s reason: no credential. Saying so is better than a number produced against a
mock and reported as a measurement.

## What review found, after the ADR first claimed green

Two adversarial reviews ran against the merged branch and between them found twelve defects, five
red gates and one false claim in this document. The ones worth carrying here:

- **The bracket artefact and the eval aligned with it**, above. The largest, and the only pair that
  had to be fixed together — changing the tiebreak without de-aligning the labels would have left
  no way to see whether it worked.
- **The screen merged two *opposite* hypotheses.** Similarity was Jaccard over word sets, which is
  invariant under role reversal, so "the aldehyde reacts faster than the ketone" and "the ketone
  reacts faster than the aldehyde" scored 1.0 on both fields and one was deleted as a duplicate.
  Two mutually exclusive explanations of one observation are the single most valuable thing a
  tournament can hold. Now compared on adjacent word pairs.
- **The summary told the chemist a calculation had run.** The verb was read off `check.kind`, and
  nothing runs a computable check, so the first line of every such result said "(ran; …)" — the
  exact failure this ADR's own "refuses to pretend to settle" section undertakes to avoid,
  inverted. The verb now comes from the outcome, and `run_computable_check` is actually called so
  its stated reason reaches a reader instead of sitting in a docstring.
- **A rejection rule that selected against concreteness.** A 12-character floor on `refuted_if`
  rejected `"yield > 90%"` (8 characters normalised) and `"pH drops"` while vaguer, longer text
  passed. Rejection is the only destructive rule here, so it counts words now.
- **Position bias was a reversal rate, whose no-bias value is 50%.** A judge with no order
  preference but ordinary noise reverses about half the pairs it sees twice, so the figure read
  "54% bias" for a judge that had none. It is now the first-position win rate, which is 0.5 for any
  order-independent judge, rescaled so 0.0 means no order effect — and suppressed entirely below
  eight decisive comparisons, because at one the estimator is identically 1.0.
- **A failed note write was reported as written**, putting `[[…]]` edges in the field note at ids
  nothing defines. **And the field note's id ignored the actor and context** the workflow id keys
  on, so two tournaments the system deliberately keeps apart overwrote each other's record.
- **The payload figure in `core/config/hypotheses.py` was wrong in the reassuring direction**: it
  counted `data` alone and claimed "roughly a factor of two in hand", while `summarise` re-rendered
  the same table into `summary` in the same `ToolMessage` — 57,215 combined against a 60,000 cap,
  a factor of 1.05. The prose table is bounded now and the comment states both halves.

## Two defects this change found in itself

**The rating fit cannot run in workflow code.** numpy's lazy submodule import reaches `os.putenv`,
which Temporal's sandbox refuses — `RestrictedWorkflowAccessError`, measured rather than
anticipated. Marking numpy pass-through would have silenced it and been the wrong repair: the fit is
computation rather than orchestration, so it is an activity, and its *result* is therefore in
workflow history. A replay now reproduces the ranking a chemist was shown even if numpy, BLAS or
this module's arithmetic changes underneath, which workflow-side computation could not have given.

**Model-authored text placed in a note body forges structure.** `retrieval/harness._as_evidence`
already argued this for *retrieved* text — measured, eight chunks became twenty-three bullets, of
which fifteen read as independent uncited evidence — and a hypothesis statement is a sentence a
model wrote. The two rules move to `kg.note.as_cell` as a third caller arrived, and every
model-authored span in a proposal body goes through it.

## Consequence

- New package `hypotheses/` (pure), one workflow, one agent tool, one config section, one note type.
- `rank_competing_hypotheses` is in `STATE_CHANGING_TOOLS`: it writes notes, and being there also
  subtracts it from every helper's surface, which is right for its own reason — a tournament spawned
  inside a helper would be a fan-out inside a fan-out, priced against a budget its caller cannot see.
- Ships **on**, unlike the panel this replaces. It is a tool the model may choose, not a stage
  imposed on every turn, so the failure mode `D-2026-08-15` deleted — a capability nothing reaches —
  and the one `D-2026-08-13` feared — a panel that over-flags — are both absent by construction.
- Observability: `chemclaw_hypothesis_tournaments_total{outcome}`,
  `chemclaw_hypothesis_screen_rejections_total{rule}` and `chemclaw_hypothesis_position_bias`.
  Every stage of this workflow catches and continues, so without them a run whose judge failed
  entirely — returning a ranked table built from the prior — was indistinguishable from a healthy
  one outside the log.
- `skills/competing-hypotheses/SKILL.md` carries the reading rules, and
  `skills/experiment-progression` now routes to the tool: it is loaded for exactly this trigger and
  did not know the tool existed.
- `make hypothesis-recovery` reproduces the table above without a credential.
- `make lint type test` green. `tests/test_hypotheses.py`, `tests/test_hypothesis_tournament.py`
  (driving the real compiled workflow) and `tests/test_hypothesis_eval.py`.
