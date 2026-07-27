"""Activities for the durable BO campaign (plan step 1d.4).

All the non-deterministic, heavy work lives here — BoFire strategy fitting
(propose) and objective evaluation — so the workflow stays deterministic and
replayable. The objective is resolved by name via `bo.objectives` because a
workflow cannot pass a Python callable into an activity.

**Deliberately not decorated with `@durable_activity`.** `workflows.registry` assembles core's
two queues, and these run on this bundle's own worker (`connectors.bo.worker`, queue
`connector-bo`) — registering them there would put `bofire` and `botorch` back into core's
background worker, which is exactly the coupling the bundle removed. A connector's worker names
what it serves explicitly, because its queue is its own contract with the manifest rather than a
core deployment decision. `tests/test_workflow_registry.py` asserts the *absence*.
"""

import asyncio

from temporalio import activity

from bo.engine import initial_candidates, propose_candidates
from bo.objectives import get_objective
from bo.problem import Candidate, Observation, OptimizationProblem

# BoFire fitting is CPU-bound (GP fit + acquisition optimization); run it off the
# event loop so heartbeats and concurrent activities keep flowing (the same
# discipline as `calc.store.run_cached`).


@activity.defn
async def propose_initial(
    problem: OptimizationProblem, n: int, seed: int | None = None
) -> list[Candidate]:
    """Space-filling seed candidates (random design) for a new campaign."""
    return await asyncio.to_thread(initial_candidates, problem, n, seed)


@activity.defn
async def propose_next(
    problem: OptimizationProblem,
    observations: list[Observation],
    n: int,
    seed: int | None = None,
) -> list[Candidate]:
    """Model-guided candidates from the observations so far (BoFire SOBO)."""
    return await asyncio.to_thread(propose_candidates, problem, observations, n, seed)


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
