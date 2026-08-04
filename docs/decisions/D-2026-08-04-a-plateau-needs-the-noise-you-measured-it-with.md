# D-2026-08-04-a-plateau-needs-the-noise-you-measured-it-with — A plateau needs the noise you measured it with

**Status:** accepted · **Date:** 2026-08-04 · **Implements:** W1 of
D-2026-08-04-what-bofire-does-when-you-actually-run-it

## Context

Two questions the BO surface asserted answers to and computed nothing for.

**"Have we plateaued?"** `campaign.optimize` runs exactly `n_rounds`; the only early stop is
`space_exhausted`, which is discrete-space exhaustion. `bo_regret` needs a known reference optimum
from a committed case file and is not agent-reachable. So the `experiment-design` skill's front
matter advertised "judging whether a campaign has plateaued" over a body that had no section on it
and a tool surface that had no number for it. Live probe `op-13` — a lab leader with twelve runs
asking "have we plateaued or is there more in it, I don't want to burn another two weeks" — was
graded **fabricated**, for asserting "the last 1–2% gains are real" against a ±2% assay
reproducibility *the chemist had stated in the same question*.

**"Why this point — exploring or exploiting?"** `Candidate.predicted_sd` was recovered onto the
suggestion by D-2026-08-01-trust-travels-on-the-value-line and returned in the objective's own units
with nothing beside it. The story audit called this "a rubric gap, not a computation gap": ±3 is an
exploit when the observed yields span 40 points and an excursion when they span 4, and neither the
return nor the skill said to make the comparison.

## Decision

**`assay_noise` is a required argument with no default.** It is the whole design. A plateau test
that supplied its own noise would reproduce `op-13`'s error with a tool's authority behind it; one
that demands the number cannot be answered without it. The tool description tells the model to get
it from the chemist and to *ask* if they have not said.

**The reading asks no surrogate.** `science/bo/progress.py` imports no BoFire: "the record stopped
moving" and "the model expects nothing" are different claims with different evidence, and answering
the first with the second is how a campaign gets talked into another fortnight. The skill tells the
model to make the second claim by calling `suggest_next_experiment` and comparing its candidates'
`predicted_value` against the *same* `assay_noise`. Keeping the module BoFire-free also keeps it
importable beside `problem.py`, which is the campaign job's `params_model` and is loaded in the
agent process that `tests/test_connector_isolation.py` keeps `torch` out of.

**Three numbers, because one verdict is not enough.**

- `evaluations_since_improvement` — how many evaluations since the running best last improved by
  **more than the noise**. This is the headline and it needs no window.
- `window_span` and `window_indistinguishable` — the spread of the most recent `window` raw values,
  which is the statement `op-13` actually needed: *the last four span 2.0 against a stated ±2.0, so
  they say nothing about each other.*
- `plateaued` — true only when there are enough observations **and** nothing has beaten the noise
  for a whole window.

**Both summaries are `computed_field`s**, the idiom `ScreenResult.verdict` and
`ScreeningDesign.summary` already carry: a bare property is not serialized, so the caveat would
never reach the model composing the answer. Below `bo_plateau_min_observations` the summary
**refuses** and says so explicitly — "which is different from saying the campaign is still
improving".

**The sd gets a scale.** `ExperimentSuggestion` carries an `ObjectiveScale` (what the objective
spans in the runs supplied) and a `summary` reading each candidate's `predicted_sd` against it, in
three bands — exploit, a step beyond, excursion — with a missing sd named as a space-filling seed
point rather than read as confidence. `connectors/bo/knowledge.py::_surrogate_belief` gains the same
spread in the same commit, so the note and the tool cannot drift into two answers to one question.

## What the measurement changed

**The planned `op-13` replay asserted the wrong thing, and the data said so.** The plan for this
wave read "`assay_noise=2.0` must return `plateaued=True`". It does not, and it should not: ordered
by equivalents, the jump from 83% to 88% at 2.2 eq is a real five-point gain and it happened four
runs from the end, so `evaluations_since_improvement` is **3** and a five-evaluation window is not
satisfied. What *is* true is narrower and is exactly the grader's own sentence: the last four runs
(88, 87, 88, 89) span 2.0 — one noise width — so they are not distinguishable from each other.

Rounding that up to "plateaued" would have been `op-13`'s error with the sign flipped and the same
overconfidence. Both facts are now separate fields, both are in the summary, and
`tests/test_bo_progress.py` pins each with the probe's verbatim numbers.

## Consequences

- Story 3.5 (`MISSING-TOOL`: nothing computes convergence) and 3.4 (`PARTIAL`: no reference scale
  for the sd) are served. 3.6 gets its defensible half — `design_space` beside `n_distinct` makes
  "best point in 11 proposals against a 96-cell grid" a computed claim, which `op-28` had to refuse
  and `op-27` fabricated.
- One new agent-facing tool on the `bo` bundle, read-only.
  `tests/test_connector_transport.py::test_the_agent_sees_exactly_the_manifest_allow_list` pins the
  manifest against the served surface, so a tool added to one and not the other fails CI.
- Two new settings, `bo_plateau_window` and `bo_plateau_min_observations`. The window is a working
  default, not a statistical claim, and the caller overrides it per question — `op-13`'s own reading
  needs `window=4`, because that is the tail the chemist pointed at.
- Nothing here reaches the durable campaign: `BoCampaignWorkflow` still runs its full round count.
  Stopping a campaign early on a plateau is a different decision, and it needs the surrogate half
  (is there posterior uncertainty left anywhere?) that W5 adds.
