"""Bayesian-optimization campaign loop (plan step 1d.4, engine level).

The ask/tell loop: seed with initial candidates, evaluate, then repeatedly
propose → evaluate → tell. `evaluate` is injected — an analytic function in tests,
a Phase-1c calculator (through the store) in real use. This plain async loop is
the in-process convenience form; the durable, resumable version is the Temporal
`BoCampaignWorkflow`, which reuses the same engine and the `best_of` reducer.
"""

from collections.abc import Awaitable, Callable

from bo.engine import initial_candidates, propose_candidates
from bo.problem import (
    MIN_SEED_OBSERVATIONS,
    CampaignResult,
    Candidate,
    Observation,
    OptimizationProblem,
    ParamValue,
    best_of,
    discrete_candidate_count,
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
            Observation(params=candidate.params, value=value, provenance=provenance)
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
    history = await _evaluate(initial_candidates(problem, n_initial, seed), evaluate, provenance)
    space = discrete_candidate_count(problem)
    for _ in range(n_rounds):
        # A purely discrete space can be exhausted: once too few distinct candidates
        # remain to propose a full batch, stop rather than crash inside BoFire.
        if space_exhausted(space, history, batch):
            break
        proposed = propose_candidates(problem, history, batch, seed)
        history.extend(await _evaluate(proposed, evaluate, provenance))
    return CampaignResult(best=best_of(problem, history), history=history)
