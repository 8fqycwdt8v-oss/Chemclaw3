# `hypotheses/` — competing explanations, ranked

Pure computation behind the hypothesis tournament. **No Temporal, no LangGraph, no model client**:
the orchestration that calls a model lives in `durable/hypothesis_tournament.py`, and this package
is the arithmetic between those calls. Same split as `science/bo` against the `bo` bundle.

| Module | What it holds |
|---|---|
| `models.py` | The objects the stages move between. `Hypothesis.refuted_if` is required at the schema level. |
| `rating.py` | The Elo-scale rating fit, its intervals, and pairwise separation. |
| `pairing.py` | Swiss pairing — pure functions over explicit state, so a workflow replay reproduces it. |
| `screen.py` | The two mechanical rules that may remove a candidate. Nothing else may. |
| `report.py` | The chemist-facing summary, the proposal body, the field note. |

## Three decisions worth knowing before changing anything here

**The rating is a Bradley-Terry fit, not the sequential Elo update.** Elo is an online estimator and
is order-dependent — the same comparisons in a different sequence give different numbers — and it
yields no interval. Both are wrong for a fixed pool judged all at once. `rating.py` fits the model
Elo approximates, on the Elo scale (`SCALE = 400`, `ANCHOR = 1500`), so the number reads as an Elo
while being reproducible and carrying a standard error.

**Two identified quantities are reported, and neither is the raw marginal error.** The likelihood
depends only on rating *differences*, so a marginal variance is mostly uncertainty about where the
whole field sits — and the marginals are correlated, so combining two of them to ask "is A ahead of
B" overstates the spread several-fold. Measured on twenty drawn comparisons: marginal ±285,
`hypot` ±403, actual difference ±76. So `Rating.standard_error` is the error *within the field* and
`RatingTable.difference` gives the pairwise one. Do not reintroduce a bare marginal.

**The critic cannot delete anything.** Only `screen.py`'s two rules remove a candidate: no usable
refutation condition, or the same refutation condition *and* the same claim as a survivor. Model
objections enter the tournament as evidence and cost a rating.
`D-2026-08-16-a-second-judge-is-a-second-answer-about-the-same-answer` measured the alternative — a
critic empowered to change what shipped scored zero against a null control, and eight of its ten
"improvements" were deletions.

## What is not here

Dispatching a `computable` check onto a real calculator. `run_computable_check` returns `not-run`
with its reason, because turning a free-text check into validated tool arguments means inventing
them. See the `BACKLOG.md` row.
