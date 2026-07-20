"""Bayesian-optimization campaign loop (plan step 1d.4, engine level).

The ask/tell loop: seed with initial candidates, evaluate, then repeatedly
propose → evaluate → tell. `evaluate` is injected — an analytic function in tests,
a Phase-1c calculator (through the store) in real use. This plain async loop is
the reusable core that the durable Temporal campaign workflow will wrap so a long
campaign survives worker restarts; keeping it framework-free makes it testable
without Temporal.
"""

from collections.abc import Awaitable, Callable

from pydantic import BaseModel

from bo.engine import initial_candidates, propose_candidates
from bo.problem import Candidate, Observation, OptimizationProblem, ParamValue

# Evaluate a candidate's parameters to its objective value.
Evaluate = Callable[[dict[str, ParamValue]], Awaitable[float]]


class CampaignResult(BaseModel):
    """The outcome of a campaign: the best point found and the full history."""

    best: Observation
    history: list[Observation]


def _is_better(problem: OptimizationProblem, candidate: float, incumbent: float) -> bool:
    """True if `candidate` improves on `incumbent` for the problem's direction."""
    if problem.objective.direction == "minimize":
        return candidate < incumbent
    return candidate > incumbent


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
) -> CampaignResult:
    """Run a BO campaign and return the best observation plus the history.

    Args:
        problem: What to optimize.
        evaluate: Async objective evaluation for one candidate's parameters.
        n_initial: Space-filling points to seed the surrogate before it can guide.
        n_rounds: Model-guided rounds after seeding.
        batch: Candidates proposed (and evaluated) per round.
        provenance: Recorded on each observation (e.g. "predicted" vs "measured").

    Returns:
        The best observation found and the ordered evaluation history.
    """
    history = await _evaluate(initial_candidates(problem, n_initial), evaluate, provenance)
    for _ in range(n_rounds):
        proposed = propose_candidates(problem, history, batch)
        history.extend(await _evaluate(proposed, evaluate, provenance))

    best = history[0]
    for observation in history[1:]:
        if _is_better(problem, observation.value, best.value):
            best = observation
    return CampaignResult(best=best, history=history)
