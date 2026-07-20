"""Activities for the durable BO campaign (plan step 1d.4).

All the non-deterministic, heavy work lives here — BoFire strategy fitting
(propose) and objective evaluation — so the workflow stays deterministic and
replayable. The objective is resolved by name via `bo.objectives` because a
workflow cannot pass a Python callable into an activity.
"""

from temporalio import activity

from bo.engine import initial_candidates, propose_candidates
from bo.objectives import get_objective
from bo.problem import Candidate, Observation, OptimizationProblem


@activity.defn
async def propose_initial(problem: OptimizationProblem, n: int) -> list[Candidate]:
    """Space-filling seed candidates (random design) for a new campaign."""
    return initial_candidates(problem, n)


@activity.defn
async def propose_next(
    problem: OptimizationProblem, observations: list[Observation], n: int
) -> list[Candidate]:
    """Model-guided candidates from the observations so far (BoFire SOBO)."""
    return propose_candidates(problem, observations, n)


@activity.defn
async def evaluate_candidates(
    objective_name: str, candidates: list[Candidate]
) -> list[Observation]:
    """Evaluate each candidate with the named objective into observations."""
    objective = get_objective(objective_name)
    observations = []
    for candidate in candidates:
        value = await objective(candidate.params)
        observations.append(
            Observation(params=candidate.params, value=value, provenance="predicted")
        )
    return observations
