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
    """Write the finished durable campaign into the campaign record; return the campaign id.

    **The gap this closes.** Both paths mint campaign ids from the same `campaign_id_for` space,
    and only the inline `suggest_next_experiment` ever wrote. So `resume_campaign` on a campaign
    that had run durably — hours of evaluation, a PR-gated recommendation, a real result — reported
    no such campaign, about work that was actually done (BO deep review, 2026-08-05).

    **Why an activity, and why here.** The write is I/O and non-deterministic, so it cannot live in
    the workflow; and psycopg is already in this bundle's worker process
    (`science.bo.campaign_record` imports it), so nothing new crosses a boundary.
    `record_suggestion` is reused unchanged — the inline path's rules about swallowing a database
    blip but never a programming error are exactly the rules wanted here, and a campaign that
    finished must not fail because a record of it could not be written.

    **The actor is real, not fabricated.** It is read from the run's memo by the caller and passed
    in, which is the mechanism core set up for precisely this: `ConnectorJobWorkflow` puts
    `requested_by` on the child's memo so a bundle whose backend runs under a shared service
    identity can still name the user behind a run (D-118). The backlog recorded this as blocked on
    a choice between threading identity through a seam built to keep it out and writing a
    fabricated actor into an audited column; it was neither, because the seam already carries it —
    `connectors/calc/workflows.py` has read the same memo in production since D-114.

    Args:
        problem: The decision space, which is also the campaign's identity.
        candidates: The recommendation this run ended on.
        observations: Every point the campaign evaluated, which is the history a resume needs.
        actor: The Entra actor the run is attributed to, off the memo.
        correlation_id: The originating request, off the same memo.
        job_id: This run's workflow id — the idempotency key, since an activity is retried by
            design and a duplicate would be a second identical entry in a history that is meant to
            record what was actually proposed.

    Returns:
        The campaign id, so the workflow can report the handle a chemist quotes back.
    """
    return await record_suggestion(
        problem,
        candidates=candidates,
        observations=observations,
        # The durable path evaluates through the objective registry rather than through descriptors
        # read off cached calculations, so there is nothing to reference. Empty is the accurate
        # statement, not a gap: `calc_refs` exists to trace a *stale* calculation to the suggestions
        # drawn from it, and no calculation was drawn from here.
        calc_refs=[],
        # No session id: the run is attributed to the actor and the request, and the conversation it
        # was launched from is core's to join through `job_records` rather than this bundle's to
        # duplicate. The tuple shape is `connectors.caller.caller_provenance`'s, reused so the two
        # writers hand `record_suggestion` the same thing.
        provenance=(actor, "", correlation_id),
        job_id=job_id,
    )
