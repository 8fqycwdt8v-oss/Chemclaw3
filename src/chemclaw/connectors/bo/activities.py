"""Activities for the durable BO campaign.

All non-deterministic, heavy work lives here — BoFire strategy fitting (propose) and objective
evaluation — so the workflow stays deterministic and replayable. The objective is resolved by name
via `chemclaw.science.bo.objectives`, since a workflow cannot pass a callable into an activity.

Registered with `@durable_activity(bundle_queue("bo"))`, so `chemclaw.connectors.bo.worker` serves
them from the registry. Core's workers never import this module, so `bofire` and `botorch` stay out
of core (`tests/test_workflow_registry.py` asserts the import boundary).

Every activity heartbeats: the propose activities wrap the opaque BoFire fit in
`chemclaw.durable.heartbeat.beating`, so a stuck fit is noticed within
`bo_activity_heartbeat_timeout_seconds` rather than at `bo_activity_timeout_seconds`.
"""

import asyncio

from temporalio import activity

from chemclaw.connectors.bo.calculators import log_s_for
from chemclaw.connectors.queues import bundle_queue
from chemclaw.core.config import settings
from chemclaw.durable.heartbeat import beating
from chemclaw.durable.registry import durable_activity
from chemclaw.science.bo.campaign_record import record_suggestion
from chemclaw.science.bo.engine import initial_candidates, propose_candidates
from chemclaw.science.bo.objectives import get_objective
from chemclaw.science.bo.problem import Candidate, Observation, OptimizationProblem
from chemclaw.science.calc.postgres_store import default_store

# BoFire fitting is CPU-bound (GP fit + acquisition optimization); run it off the event loop so
# heartbeats and concurrent activities keep flowing.


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

    Heartbeats between candidates (a real progress report, keeping a batch of fast candidates alive)
    and inside each one, since an objective may be slow (`solubility_objective` calls an uncached
    calculator). A silent candidate would make Temporal retry the activity from the top, re-paying
    every evaluated candidate.
    """
    objective = get_objective(objective_name, log_s_for(default_store()))
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


@durable_activity(bundle_queue("bo"))
@activity.defn
async def record_campaign_run(
    problem: OptimizationProblem,
    candidates: list[Candidate],
    observations: list[Observation],
    actor: str,
    correlation_id: str,
    job_id: str,
) -> str:
    """Write one durable campaign record; return the campaign id.

    Called once per completed round (that round's proposed candidates, carrying the surrogate's
    `predicted_value`/`predicted_sd`) and once at the end (the best point). This is what lets
    `resume_campaign` find a campaign that ran durably.

    An activity because the write is I/O. `record_suggestion` is reused, so a database blip is
    swallowed (a finished campaign must not fail on its record) while a programming error is not.
    The actor and correlation id come from the run's memo, where `ConnectorJobWorkflow` puts
    `requested_by`.

    Args:
        problem: The decision space, which is also the campaign's identity.
        candidates: The round's proposed candidates, or — on the terminal call — the recommendation
            the run ended on.
        observations: Every point the campaign evaluated, which is the history a resume needs.
        actor: The Entra actor the run is attributed to, off the memo.
        correlation_id: The originating request, off the same memo.
        job_id: The idempotency key, since activities are retried: the workflow id on the terminal
            call and `"{workflow_id}:r{N}"` per round, so rounds do not dedupe against each other.

    Returns:
        The campaign id, so the workflow can report the handle a chemist quotes back.
    """
    recorded = await record_suggestion(
        problem,
        candidates=candidates,
        observations=observations,
        # The durable path evaluates through the objective registry, not cached calculations, so no
        # calculation is referenced.
        calc_refs=[],
        # No session id: the conversation is joined through core's `job_records`. The tuple shape is
        # `connectors.caller.caller_provenance`'s.
        provenance=(actor, "", correlation_id),
        job_id=job_id,
    )
    return recorded.campaign_id
