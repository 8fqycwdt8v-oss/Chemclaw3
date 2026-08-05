"""Activities for the durable BO campaign (plan step 1d.4).

All the non-deterministic, heavy work lives here — BoFire strategy fitting
(propose) and objective evaluation — so the workflow stays deterministic and
replayable. The objective is resolved by name via `chemclaw.science.bo.objectives` because a
workflow cannot pass a Python callable into an activity.

**Registered on this bundle's own queue, not core's.** These carry
`@durable_activity(bundle_queue("bo"))`, so `chemclaw.connectors.bo.worker` assembles what it
serves from
the registry instead of a hand-written list. They previously carried no decorator at all, on the
reasoning that registering them would put `bofire` and `botorch` into core's background worker.
That reasoning was wrong about the mechanism: the registry is populated at *import* time, and
core's workers never import this module, so the decorator cannot move anything into core — while
the hand-maintained list it forced re-created, one level down, the "written, imported, absent from
the worker's list, never runs" failure `chemclaw.durable.registry` exists to prevent (D-118).
`tests/test_workflow_registry.py` now asserts the import boundary directly, which is the property
that was actually doing the work.

**Every activity heartbeats (Conn-F2).** `propose_initial`/`propose_next` wrap the BoFire fit and
acquisition step — a single opaque call with no unit boundary to report progress at, the same
shape as calc's two CREST jobs (`connectors.calc.activities`) — in the shared
`chemclaw.durable.heartbeat.beating` timer, so a stuck fit is noticed within
`bo_activity_heartbeat_timeout_seconds` instead of at the full `bo_activity_timeout_seconds`
budget. `evaluate_candidates` has a real unit boundary (one candidate) and so beats between them,
mirroring calc's per-species pattern — *and* wraps each evaluation in the same timer, because a
registered objective is not guaranteed sub-second
(`chemclaw.science.bo.objectives.solubility_objective` calls an uncached calculator) and a beat
between candidates says nothing while one is running.
"""

import asyncio

from temporalio import activity

from chemclaw.connectors.queues import bundle_queue
from chemclaw.core.config import settings
from chemclaw.durable.heartbeat import beating
from chemclaw.durable.registry import durable_activity
from chemclaw.science.bo.engine import initial_candidates, propose_candidates
from chemclaw.science.bo.objectives import get_objective
from chemclaw.science.bo.problem import Candidate, Observation, OptimizationProblem

# BoFire fitting is CPU-bound (GP fit + acquisition optimization); run it off the
# event loop so heartbeats and concurrent activities keep flowing (the same
# discipline as `calc.store.run_cached`).


@durable_activity(bundle_queue("bo"))
@activity.defn
async def propose_initial(
    problem: OptimizationProblem, n: int, seed: int | None = None
) -> list[Candidate]:
    """Space-filling seed candidates (random design) for a new campaign."""
    return await beating(
        asyncio.to_thread(initial_candidates, problem, n, seed),
        "sampling initial candidates",
        settings.bo_activity_heartbeat_timeout_seconds,
    )


@durable_activity(bundle_queue("bo"))
@activity.defn
async def propose_next(
    problem: OptimizationProblem,
    observations: list[Observation],
    n: int,
    seed: int | None = None,
) -> list[Candidate]:
    """Model-guided candidates from the observations so far (BoFire SOBO)."""
    return await beating(
        asyncio.to_thread(propose_candidates, problem, observations, n, seed),
        f"fitting the surrogate to {len(observations)} observation(s)",
        settings.bo_activity_heartbeat_timeout_seconds,
    )


@durable_activity(bundle_queue("bo"))
@activity.defn
async def evaluate_candidates(
    objective_name: str, candidates: list[Candidate]
) -> list[Observation]:
    """Evaluate each candidate with the named objective into observations.

    Heartbeats **both between candidates and inside one**, and needs both. The beat between them
    carries the honest progress report — "candidate 2/5" is a real unit boundary, the same shape
    calc's per-species jobs report at — and is what keeps a long batch of *fast* candidates alive,
    where no single evaluation ever runs long enough for a timer to fire.

    The timer inside covers the opposite case, which had no protection at all: a registered
    objective is not guaranteed fast (`solubility_objective` calls an uncached calculator), so one
    candidate slower than `bo_activity_heartbeat_timeout_seconds` went silent mid-evaluation.
    Temporal would declare the worker dead and retry the activity from the top, re-paying every
    candidate already evaluated in the batch — the silently-killed-and-retried shape REV-3 fixed
    for calc's CREST jobs and Conn-F2 fixed for the two propose activities, left open here because
    a per-candidate beat looks like it covers a per-candidate wait and does not.
    """
    objective = get_objective(objective_name)
    observations = []
    for index, candidate in enumerate(candidates, start=1):
        progress = f"evaluating candidate {index}/{len(candidates)}"
        activity.heartbeat(progress)
        value = await beating(
            objective(candidate.params),
            progress,
            settings.bo_activity_heartbeat_timeout_seconds,
        )
        observations.append(
            Observation(
                params=candidate.params,
                value=value,
                provenance="predicted",
                surrogate_sd=candidate.predicted_sd,
            )
        )
    return observations
