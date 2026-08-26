"""The `bo` connector's own durable workflow: the Bayesian-optimization campaign (plan step 1d.4).

Wraps the ask/tell loop in Temporal so a long campaign is resumable and survives worker restarts:
each round's propose and evaluate are activities, and the observation history is carried as
workflow state (plain data, so replay is deterministic). The best-so-far reduction runs in the
workflow (pure). Objective evaluation is heavy and non-deterministic, hence an activity resolved
by name.

**This is the reference connector-owned workflow** (D-110/D-111), and what it does *not* do is
the point. It returns a `ConnectorJobResult` and stops: core's `ConnectorJobWorkflow` supplies
the idempotent job id, the actor attribution, the session push-back, and the PR-gate publish of
the note this returns. It is served by this bundle's own worker on its own task queue, so
`bofire`/`botorch` run nowhere near the chat service — and the only thing binding it to core is
the workflow *type name* and that queue, both strings in `connector.yaml`.

Moving it here was a one-line manifest change plus this file's return type, which is the
property the seam was built to have.
"""

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from chemclaw.connectors.bo.activities import (
        evaluate_candidates,
        propose_initial,
        propose_next,
        record_campaign_run,
    )
    from chemclaw.connectors.bo.knowledge import note_from_campaign_result
    from chemclaw.core.config import settings
    from chemclaw.durable.connector_job import ConnectorJobResult
    from chemclaw.science.bo.problem import (
        CampaignCarryOver,
        CampaignResult,
        CampaignSpec,
        Candidate,
        Observation,
        best_of,
        discrete_candidate_count,
        space_exhausted,
    )

from chemclaw.connectors.queues import bundle_queue
from chemclaw.durable.publish import BAD_DATA_RETRY
from chemclaw.durable.registry import durable_workflow


def _carry_on_if_history_is_filling_up(
    payload: dict[str, object], history: list[Observation], rounds_remaining: int
) -> None:
    """Continue this campaign in a fresh run before Temporal's event history runs out.

    **The ceiling used to be a promise the workflow could not keep.** `bo_max_rounds` defaults to
    500 and its comment said it existed to stay inside the event-history limit — but the history is
    re-sent to `propose_next` every round, so bytes grow quadratically, and a measured
    178 bytes/`Observation` puts a batch-1 campaign over the 50 MB hard limit at round **441**. The
    server would terminate it there, losing every already-paid evaluation: exactly the failure the
    ceiling was written to prevent, at a round count the ceiling permits.

    **The trigger is Temporal's own signal, not a round count.** `is_continue_as_new_suggested()`
    flips when the server sees history approaching its configured threshold, which is the only
    number that accounts for what this campaign actually carries — batch size, parameter count and
    objective count all change bytes-per-round, so any round count hard-coded here would be right
    for one problem shape and wrong for the rest.

    Continuing is safe mid-loop because the campaign's whole state is `CampaignCarryOver`: the spec
    is immutable and travels as the unread `payload`, and the observations are the only thing a
    round adds. Called *after* a round completes, never between the propose and the evaluate, so no
    already-paid evaluation is ever abandoned.
    """
    if rounds_remaining <= 0 or not workflow.info().is_continue_as_new_suggested():
        return
    workflow.continue_as_new(
        args=[
            payload,
            CampaignCarryOver(history=history, rounds_remaining=rounds_remaining).model_dump(
                mode="json"
            ),
        ]
    )


@durable_workflow(bundle_queue("bo"))
# Its failures must be able to *be* failures: without this the SDK parks a plain exception raised
# in workflow code in an unbounded workflow-task-failure loop, so the parent
# `ConnectorJobWorkflow` waits forever and the chemist is told "running" indefinitely. Measured on
# a child reading an absent optional key from its payload (`exclude_none=True` drops one) — child
# RUNNING forever, parent waiting, session never told. See `durable/connector_job.py` for the trade.
@workflow.defn(failure_exception_types=[Exception])
class BoCampaignWorkflow:
    """Run a BO campaign durably and return the best point, the history, and the note to gate."""

    @workflow.run
    async def run(
        self, payload: dict[str, object], carried: dict[str, object] | None = None
    ) -> ConnectorJobResult:
        """Seed, then run `n_rounds` propose→evaluate rounds, durably.

        Takes the plain mapping core forwards rather than a typed argument: the connector
        contract is payload-in, envelope-out, and the payload has already been validated against
        `CampaignSpec` by the generated tool before the workflow started. Re-validating it here
        is the cheap way to get the typed object back without core needing to know this type.

        `carried` is the second argument *this workflow gives itself* when it continues-as-new,
        and is absent on every start core makes — hence the default. A run that receives it skips
        seeding and picks the loop up where the previous run left off. See `_carry_on` for why the
        loop can end this way at all.
        """
        spec = CampaignSpec.model_validate(payload)
        timeout = timedelta(seconds=settings.bo_activity_timeout_seconds)
        # Comfortably shorter than `timeout` (Conn-F2): without it, a worker that dies mid-round
        # is only noticed at the full start-to-close budget, the same silently-killed-and-retried
        # shape REV-3 fixed for calc's CREST jobs — up to `activity_max_attempts` restarts from
        # zero, each paying the round's full cost again.
        heartbeat_timeout = timedelta(seconds=settings.bo_activity_heartbeat_timeout_seconds)

        if carried is None:
            seed = await workflow.execute_activity(
                propose_initial,
                args=[spec.problem, spec.n_initial, spec.seed],
                start_to_close_timeout=timeout,
                heartbeat_timeout=heartbeat_timeout,
                retry_policy=BAD_DATA_RETRY,
            )
            history: list[Observation] = await workflow.execute_activity(
                evaluate_candidates,
                args=[spec.objective_name, seed],
                start_to_close_timeout=timeout,
                heartbeat_timeout=heartbeat_timeout,
                retry_policy=BAD_DATA_RETRY,
            )
            rounds_remaining = spec.n_rounds
        else:
            resumed = CampaignCarryOver.model_validate(carried)
            history = resumed.history
            rounds_remaining = resumed.rounds_remaining

        space = discrete_candidate_count(spec.problem)
        while rounds_remaining > 0:
            # Stop early if a purely discrete candidate set is exhausted.
            if space_exhausted(space, history, spec.batch):
                break
            proposed = await workflow.execute_activity(
                propose_next,
                args=[spec.problem, history, spec.batch, spec.seed],
                start_to_close_timeout=timeout,
                heartbeat_timeout=heartbeat_timeout,
                retry_policy=BAD_DATA_RETRY,
            )
            history += await workflow.execute_activity(
                evaluate_candidates,
                args=[spec.objective_name, proposed],
                start_to_close_timeout=timeout,
                heartbeat_timeout=heartbeat_timeout,
                retry_policy=BAD_DATA_RETRY,
            )
            rounds_remaining -= 1
            _carry_on_if_history_is_filling_up(payload, history, rounds_remaining)

        result = CampaignResult(best=best_of(spec.problem, history), history=history)

        # Write the campaign record, so `resume_campaign` can find work this path actually did.
        # Both paths mint ids from one `campaign_id_for` space and only the inline tool ever wrote,
        # so a durably-run campaign reported "no such campaign" about hours of evaluation.
        #
        # The actor comes off the run's **memo**, which core sets on every connector job for exactly
        # this (`durable/connector_job.py`, D-118) — the same read
        # `connectors/calc/workflows.py` has
        # made since F5. Nothing is threaded through the payload, and nothing is fabricated: the
        # fallback is the configured service identity, which is what `require_actor` falls back to
        # for a run started outside the wrapper (a test, a manual re-drive).
        #
        # The workflow id is the idempotency key. It is stable across a continue-as-new, so a
        # campaign that carried over does not write twice.
        campaign_id = await workflow.execute_activity(
            record_campaign_run,
            args=[
                spec.problem,
                [Candidate(params=result.best.params)],
                history,
                workflow.memo_value("requested_by", settings.service_actor_id),
                workflow.memo_value("correlation_id", ""),
                workflow.info().workflow_id,
            ],
            start_to_close_timeout=timeout,
            heartbeat_timeout=heartbeat_timeout,
            retry_policy=BAD_DATA_RETRY,
        )

        # The recommendation as a PR-gated note (step 1d.5) — *built* here, because the BO→note
        # mapping is this domain's knowledge, and *published* by core, because the PR-gate is
        # the review boundary and a connector must not be able to reach around it. Best-effort
        # publishing (a failed git write must never fail a completed campaign) is core's
        # discipline too, so this workflow no longer carries it.
        # Always built, never conditional: whether it is *published* is the manifest's
        # `publish_to_graph`, which core reads. The spec used to carry a second, model-authored
        # switch that could suppress it, which meant a campaign could finish and leave nothing
        # behind (D-157).
        note = note_from_campaign_result(spec.objective_name, spec.problem, result)
        best = result.best
        return ConnectorJobResult(
            summary=(
                f"campaign {spec.objective_name!r} finished after {len(history)} evaluation(s); "
                f"best objective {best.value:.6g} ({best.provenance}); "
                f"recorded as {campaign_id}"
            ),
            data=result.model_dump(mode="json"),
            payload_kind=type(result).__name__,
            note=note,
        )
