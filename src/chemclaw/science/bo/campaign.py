"""Bayesian-optimization campaign loop (plan step 1d.4, engine level).

The ask/tell loop: seed with initial candidates, evaluate, then repeatedly
propose → evaluate → tell. `evaluate` is injected — an analytic function in tests,
a Phase-1c calculator (through the store) in real use. This plain async loop is
the in-process convenience form; the durable, resumable version is the Temporal
`BoCampaignWorkflow`, which reuses the same engine and the `best_of` reducer.
"""

import asyncio
from collections.abc import Awaitable, Callable

from chemclaw.science.bo.engine import initial_candidates, propose_candidates
from chemclaw.science.bo.problem import (
    MIN_SEED_OBSERVATIONS,
    CampaignResult,
    Candidate,
    Observation,
    OptimizationProblem,
    ParamValue,
    best_of,
    discrete_candidate_count,
    require_problem_yields_one_best_point,
    require_rounds_within_ceiling,
    space_exhausted,
)

# Evaluate a candidate's parameters to its objective value.
Evaluate = Callable[[dict[str, ParamValue]], Awaitable[float]]


async def _evaluate(
    candidates: list[Candidate], evaluate: Evaluate, provenance: str
) -> list[Observation]:
    """Evaluate each candidate into an observation."""
    observations = []
    for candidate in candidates:
        value = await evaluate(candidate.params)
        observations.append(
            Observation(
                params=candidate.params,
                value=value,
                provenance=provenance,
                surrogate_sd=candidate.predicted_sd,
            )
        )
    return observations


async def optimize(
    problem: OptimizationProblem,
    evaluate: Evaluate,
    *,
    n_initial: int = 5,
    n_rounds: int = 10,
    batch: int = 1,
    provenance: str = "predicted",
    seed: int | None = None,
) -> CampaignResult:
    """Run a BO campaign in-process and return the best observation plus history.

    **The two BoFire calls cross `asyncio.to_thread`**, which is what `connectors/bo/activities.py`
    already does for the identical `initial_candidates`/`propose_candidates` pair (it adds a
    Temporal heartbeat, which this in-process path has nothing to beat). A GP fit and its
    acquisition optimisation are pure synchronous CPU: run inline inside an `async def` they own
    the loop for their whole duration, and the only `await`s in this function are the `_evaluate`
    calls between rounds. Measured 2026-09-06 on a one-parameter problem, 5 seed points and 2
    rounds, with a 5 ms sampler on the same loop: **16,069.7 ms of uninterrupted stall against a
    16,071 ms call inline, 15.4 ms against a comparable call threaded** — the loop was not
    scheduled once in the first arm. The audit that found it measured single steps of 241 s. There
    is no `src/` caller today, which is why this was latent rather than live; the loop it would
    freeze is whichever one first awaits it.

    Args:
        problem: What to optimize.
        evaluate: Async objective evaluation for one candidate's parameters.
        n_initial: Space-filling points to seed the surrogate before it can guide.
            Must be at least `MIN_SEED_OBSERVATIONS` (BoFire's fitting floor) —
            rejected here so the campaign fails before spending any budget.
        n_rounds: Model-guided rounds after seeding. Bounded by `bo_max_rounds`
            (rejected here, before any budget is spent).
        batch: Candidates proposed (and evaluated) per round.
        provenance: Recorded on each observation (e.g. "predicted" vs "measured").
        seed: Per-campaign RNG seed for replicate runs; None uses the config default.

    Returns:
        The best observation found and the ordered evaluation history.
    """
    if n_initial < MIN_SEED_OBSERVATIONS:
        raise ValueError(
            f"n_initial must be >= {MIN_SEED_OBSERVATIONS}: the surrogate cannot fit on fewer"
        )
    require_rounds_within_ceiling(n_rounds)
    # Checked here, not at the `best_of` call below: this loop returns a single best observation, so
    # a trade-off has no answer for it — and discovering that *after* n_initial + n_rounds*batch
    # evaluations would spend the whole budget to raise. Same reason the rule above is here. Shared
    # with `require_campaign_startable` rather than restated, because this loop and the durable
    # workflow need the same three things of a problem and used to say so in two different wordings.
    require_problem_yields_one_best_point(problem)
    seeds = await asyncio.to_thread(initial_candidates, problem, n_initial, seed)
    history = await _evaluate(seeds, evaluate, provenance)
    space = discrete_candidate_count(problem)
    for _ in range(n_rounds):
        # A purely discrete space can be exhausted: once too few distinct candidates
        # remain to propose a full batch, stop rather than crash inside BoFire.
        if space_exhausted(problem, space, history, batch):
            break
        proposed = await asyncio.to_thread(propose_candidates, problem, history, batch, seed)
        history.extend(await _evaluate(proposed, evaluate, provenance))
    return CampaignResult(best=best_of(problem, history), history=history)
