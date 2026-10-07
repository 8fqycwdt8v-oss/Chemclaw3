"""The `bo` connector's own durable workflow: the Bayesian-optimization campaign.

Wraps the ask/tell loop in Temporal so a long campaign is resumable: each round's propose and
evaluate are activities, the observation history is plain workflow state, and the best-so-far
reduction runs in the workflow (pure).

It returns a `ConnectorJobResult` and stops: core's `ConnectorJobWorkflow` supplies the idempotent
job id, actor attribution, session push-back and the graph write of the returned note. Served by
this bundle's worker on its own queue (so `bofire`/`botorch` stay out of the chat service), bound
to core only by the workflow type name and queue declared in `connector.yaml`.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from chemclaw.connectors.bo.activities import (
        evaluate_candidates,
        propose_initial,
        propose_next,
        record_campaign_run,
    )
    from chemclaw.connectors.bo.knowledge import note_from_campaign_result
    from chemclaw.core.config import settings
    from chemclaw.durable.awaiting import AwaitAnswerWorkflow, AwaitOutcome, AwaitRequest
    from chemclaw.durable.connector_job import ConnectorJobResult
    from chemclaw.science.bo.objectives import is_measured
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

# `connector_queue_wait_timeout` is passed at every dispatched activity, because
# `start_to_close_timeout` does not bound the wait for a worker; without it a bundle queue served by
# no pod is indistinguishable from a busy one until the parent's execution ceiling fires.
from chemclaw.durable.publish import (
    BAD_DATA_RETRY,
    calculation_retry,
    connector_queue_wait_timeout,
    remaining_queue_wait_timeout,
)
from chemclaw.durable.registry import durable_workflow


def _carry_on_if_history_is_filling_up(
    payload: dict[str, object],
    history: list[Observation],
    rounds_remaining: int,
    rounds_done: int,
    spent: timedelta,
) -> None:
    """Continue this campaign in a fresh run before Temporal's event history runs out.

    The full history is sent to activities every round, so history bytes grow quadratically and a
    long campaign would hit Temporal's hard limit and lose every paid evaluation. The trigger is
    `is_continue_as_new_suggested()`, the server's own signal, since bytes per round depend on batch
    size, parameter and objective counts.

    Safe mid-loop because the whole state is `CampaignCarryOver`; called only after a round
    completes, never between propose and evaluate. `rounds_done` travels so per-round record keys do
    not collide with the previous run's. `spent` travels because the execution timeout spans the
    whole
    continue-as-new chain while `workflow.info().workflow_start_time` resets per run.
    """
    if rounds_remaining <= 0 or not workflow.info().is_continue_as_new_suggested():
        return
    workflow.continue_as_new(
        args=[
            payload,
            CampaignCarryOver(
                history=history,
                rounds_remaining=rounds_remaining,
                rounds_done=rounds_done,
                spent_seconds=spent.total_seconds(),
            ).model_dump(mode="json"),
        ]
    )


async def _measure(
    spec: CampaignSpec,
    candidates: list[Candidate],
    round_label: str,
    actor: str,
    correlation_id: str,
    session_id: str,
) -> list[Observation]:
    """Suspend this campaign until somebody reports what these candidates actually did.

    What makes a real bench campaign expressible: propose a batch, wait for the plates, propose the
    next. A child workflow, not an activity, because a week-long wait has no start-to-close budget
    or
    heartbeat; the child holds the question and escalates on its own timer.

    The child id carries the round, so each round is its own wait even when conditions repeat. An
    expired wait ends the campaign with what it has rather than fitting a surrogate to a batch
    nobody
    ran.
    """
    request = AwaitRequest(
        kind="measurement",
        subject=(
            f"Run and report {len(candidates)} condition(s) for objective "
            f"{spec.problem.objective.name!r} ({round_label})"
        ),
        rationale=(
            "A Bayesian-optimization campaign is suspended on this batch: the next proposal is "
            "computed from these values and nothing else."
        ),
        requested_by=actor,
        session_id=session_id,
        correlation_id=correlation_id,
        deadline_days=settings.bo_measurement_deadline_days,
    )
    outcome = AwaitOutcome.model_validate(
        await workflow.execute_child_workflow(
            AwaitAnswerWorkflow.run,
            request.model_dump(mode="json"),
            id=f"{workflow.info().workflow_id}:await:{round_label}",
            task_queue=settings.background_task_queue,
            # Not the default `TERMINATE`: a terminate never runs workflow code, so the round's
            # `pending_requests` row would stay `waiting` forever in people's inboxes (retention
            # never
            # collects it). `REQUEST_CANCEL` delivers the `CancelledError` the wait handles,
            # settling the row;
            # `ABANDON` would leave the question answerable after the campaign is gone. Pinned by
            # `tests/test_awaiting.py` and, at this call site, `tests/test_bo_campaign.py`.
            parent_close_policy=ParentClosePolicy.REQUEST_CANCEL,
        )
    )
    if outcome.state != "answered":
        return []
    # The answer is opaque to the wait and typed here, so a malformed one fails the campaign naming
    # the
    # batch rather than being coerced into plausible numbers.
    return [Observation.model_validate(row) for row in outcome.payload.get("observations", [])]


def dispatches_left(rounds_remaining: int, seeding: bool) -> int:
    """How many activities this campaign still has to dispatch before it can return.

    The divisor `_queue_wait` shares the remaining execution budget by: two for the seed (propose,
    evaluate), three per round (propose, evaluate, record) and one terminal record. An error here is
    a
    fairness bug, never a safety one — each dispatch is bounded by what is left, so the sum cannot
    exceed the budget — which is why the count may be re-synced each round. Measured campaigns open
    a
    child instead of an activity but have no execution ceiling, so they are unaffected.

    Args:
        rounds_remaining: Rounds this campaign still owes.
        seeding: Whether the seed batch is still ahead of it.

    Returns:
        The number of dispatches left, always at least one.
    """
    return (2 if seeding else 0) + 3 * rounds_remaining + 1


class CampaignBudgetSpent(Exception):
    """This campaign's execution budget can no longer fund another activity.

    Raised by `_queue_wait`. Not a workflow failure: the campaign returns its best point and history
    rather than dispatching an activity the execution timeout would kill unreported.

    `run`'s round loop guards each round with `_cannot_afford_another_dispatch`, and the terminal
    `record_campaign_run` is wrapped. The seed's dispatches need neither: `Settings` refuses a
    ceiling
    that cannot fund one attempt, and a resumed run does not seed.
    """


@durable_workflow(bundle_queue("bo"))
# Let plain exceptions fail the workflow; otherwise the SDK retries the workflow task forever and
# the parent `ConnectorJobWorkflow` reports "running" indefinitely. See `durable/connector_job.py`.
@workflow.defn(failure_exception_types=[Exception])
class BoCampaignWorkflow:
    """Run a BO campaign durably and return the best point, the history, and the note to gate."""

    #: What this execution had spent before the current run started, carried across each
    #: continue-as-new because `workflow.info().workflow_start_time` is the run's own start.
    _carried_spend: timedelta = timedelta(0)

    #: Activities still to dispatch, so `_queue_wait` shares the remaining execution budget rather
    # than
    #: giving the first step most of it. Re-synced from `rounds_remaining` every round.
    _dispatches_left: int = 1

    def _spent(self) -> timedelta:
        """How much of this campaign's execution budget is gone, across the whole run chain.

        `workflow.now()` is the event time, so this is deterministic under replay.
        """
        return self._carried_spend + (workflow.now() - workflow.info().workflow_start_time)

    def _queue_wait(self) -> timedelta:
        """How long the *next* activity may sit unclaimed, given what this campaign has left.

        `connector_queue_wait_timeout()` funds one wait plus one attempt within the parent's
        ceiling, but
        a campaign dispatches several activities. So the remaining budget is divided by the
        dispatches
        still to come, and the result is the smaller of that share and the queue-wide bound (a
        property of
        the queue that does not shrink).

        Returns:
            The `schedule_to_start_timeout` for the next dispatch.

        Raises:
            CampaignBudgetSpent: What is left cannot fund another wait plus attempt.
        """
        queue_bound = connector_queue_wait_timeout()
        budget = workflow.info().execution_timeout
        if budget is None:
            # A campaign that suspends on a person has no execution ceiling
            # (`durable/connector_job.child_execution_timeout`), so only the queue-wide bound
            # applies.
            return queue_bound
        remaining = budget - self._spent()
        # Affordability is judged on what is left, never on the share: with many rounds the first
        # share is
        # smaller than one attempt even though the budget is untouched. The share may narrow a wait
        # but must
        # never refuse one.
        affordable = remaining_queue_wait_timeout(remaining, settings.bo_activity_timeout_seconds)
        if affordable is None:
            raise CampaignBudgetSpent
        share = max(self._dispatches_left, 1)
        self._dispatches_left = share - 1
        fair = remaining_queue_wait_timeout(remaining / share, settings.bo_activity_timeout_seconds)
        # A share below the floor is not a usable wait: a rolling or slow `bo` worker would expire
        # `schedule_to_start` and kill the campaign. The floor is a deployment fact
        # (`bo_queue_wait_floor_seconds`), kept inside both bounds by the `min` above.
        floor = timedelta(seconds=settings.bo_queue_wait_floor_seconds)
        return min(queue_bound, affordable, max(fair or timedelta(0), floor))

    def _cannot_afford_another_dispatch(self) -> bool:
        """Whether this campaign's execution ceiling can still fund a wait plus an attempt.

        A predicate rather than a `try`, because `_queue_wait` decrements `_dispatches_left` and a
        probe
        must not. Asks on the same basis as `_queue_wait` — what is left, undivided — so the two
        agree.

        Returns:
            True when the budget is spent and the loop must end with what it has.
        """
        budget = workflow.info().execution_timeout
        if budget is None:
            return False
        return (
            remaining_queue_wait_timeout(
                budget - self._spent(), settings.bo_activity_timeout_seconds
            )
            is None
        )

    async def _evaluate(
        self,
        spec: CampaignSpec,
        candidates: list[Candidate],
        round_label: str,
        timeout: timedelta,
        heartbeat_timeout: timedelta,
    ) -> list[Observation]:
        """Turn candidates into observations, by computing them or by asking for them.

        One place, so the seed and every round branch identically.
        """
        if is_measured(spec.objective_name):
            return await _measure(
                spec,
                candidates,
                round_label,
                workflow.memo_value("requested_by", settings.service_actor_id),
                workflow.memo_value("correlation_id", ""),
                workflow.memo_value("session_id", ""),
            )
        return list(
            await workflow.execute_activity(
                evaluate_candidates,
                args=[spec.objective_name, candidates],
                start_to_close_timeout=timeout,
                heartbeat_timeout=heartbeat_timeout,
                schedule_to_start_timeout=self._queue_wait(),
                # `calculation_retry`, not `BAD_DATA_RETRY`: a computed objective such as
                # `solubility_objective`
                # calls `cached_remote`, so `CalcBusyError` from a full calc pod is possible, and
                # Temporal's default
                # fast backoff would exhaust attempts within seconds. Same retryable types, wider
                # spacing.
                retry_policy=calculation_retry(),
            )
        )

    @workflow.run
    async def run(
        self, payload: dict[str, object], carried: dict[str, object] | None = None
    ) -> ConnectorJobResult:
        """Seed, then run `n_rounds` propose→evaluate rounds, durably.

        Takes the plain mapping core forwards (payload-in, envelope-out); it was validated against
        `CampaignSpec` by the generated tool and is re-validated here to get the typed object.

        `carried` is passed only when this workflow continues-as-new; a run receiving it skips
        seeding
        and resumes the loop. See `_carry_on_if_history_is_filling_up`.
        """
        spec = CampaignSpec.model_validate(payload)
        timeout = timedelta(seconds=settings.bo_activity_timeout_seconds)
        # Well under `timeout`, so a worker dying mid-round is noticed quickly rather than at the
        # full
        # start-to-close budget, with each retry repaying the round.
        heartbeat_timeout = timedelta(seconds=settings.bo_activity_heartbeat_timeout_seconds)

        if carried is None:
            self._dispatches_left = dispatches_left(spec.n_rounds, seeding=True)
            seed = await workflow.execute_activity(
                propose_initial,
                args=[spec.problem, spec.n_initial, spec.seed],
                start_to_close_timeout=timeout,
                heartbeat_timeout=heartbeat_timeout,
                schedule_to_start_timeout=self._queue_wait(),
                retry_policy=BAD_DATA_RETRY,
            )
            history = await self._evaluate(spec, seed, "seed", timeout, heartbeat_timeout)
            if not history:
                # The seed batch nobody reported (an expired wait). With no history, `propose_next`
                # and `best_of`
                # would raise and the chemist would see an internal precondition message, so end
                # here: there is no
                # best point to record and no note to draw.
                return ConnectorJobResult(
                    summary=(
                        f"campaign {spec.objective_name!r} ended with no evaluations: its seed "
                        f"batch of {len(seed)} condition(s) was never reported before the "
                        "measurement deadline, so no optimization was possible. Re-run it once "
                        "the results are in hand."
                    )
                )
            rounds_remaining = spec.n_rounds
            rounds_done = 0
        else:
            resumed = CampaignCarryOver.model_validate(carried)
            history = resumed.history
            rounds_remaining = resumed.rounds_remaining
            rounds_done = resumed.rounds_done
            # Before any dispatch, so `_queue_wait` is never asked against a budget this run
            # believes is untouched. See `CampaignCarryOver.spent_seconds`.
            self._carried_spend = timedelta(seconds=resumed.spent_seconds)

        actor = workflow.memo_value("requested_by", settings.service_actor_id)
        correlation_id = workflow.memo_value("correlation_id", "")

        space = discrete_candidate_count(spec.problem)
        budget_spent = False
        while rounds_remaining > 0:
            # Stop early if a purely discrete candidate set is exhausted.
            if space_exhausted(spec.problem, space, history, spec.batch):
                break
            # Re-synced every round: a measured campaign opens a child instead of dispatching, so
            # the running
            # count drifts (safe, per `dispatches_left`).
            self._dispatches_left = dispatches_left(rounds_remaining, seeding=False)
            # Stop when the execution ceiling can no longer fund a dispatch: an activity killed by
            # the
            # execution timeout reaches no workflow code and loses the run.
            #
            # One check per round covers its three dispatches: `_queue_wait` hands out `R/n - w -
            # a`, so the
            # next dispatch sees `(R - R/n + a)/(n - 1) = R/n + a/(n-1)`, more than the `R/n` this
            # check found
            # to exceed `w + a`. `CampaignBudgetSpent` mid-round is therefore unreachable, and stays
            # a named
            # failure if the reasoning is ever wrong.
            if self._cannot_afford_another_dispatch():
                budget_spent = True
                break
            proposed = await workflow.execute_activity(
                propose_next,
                args=[spec.problem, history, spec.batch, spec.seed],
                start_to_close_timeout=timeout,
                heartbeat_timeout=heartbeat_timeout,
                schedule_to_start_timeout=self._queue_wait(),
                retry_policy=BAD_DATA_RETRY,
            )
            measured = await self._evaluate(
                spec, proposed, f"r{rounds_done + 1}", timeout, heartbeat_timeout
            )
            if not measured:
                # A measured campaign whose batch nobody reported. Stop with what is in hand rather
                # than proposing round n+1 from a surrogate that never saw round n.
                break
            history += measured
            rounds_done += 1
            rounds_remaining -= 1
            # Record each round as it completes, so a campaign cancelled, terminated or failed
            # mid-run is still
            # found by `resume_campaign`; cancel-then-resume therefore serves as pause.
            #
            # Best-effort: `record_suggestion` swallows `_TRANSIENT_WRITE_FAILURES`, so a database
            # blip loses
            # that round's record with only a WARNING (making it strict is a backlog item). Keyed on
            # the round,
            # since `(campaign_id, job_id)` is the idempotency key. These rows carry the proposed
            # candidates'
            # `predicted_value`/`predicted_sd`; the terminal write records only the best point.
            #
            # Costs: event-history growth doubles (the continue-as-new trigger absorbs it), and each
            # row
            # snapshots the cumulative history, so stored bytes grow triangularly and retention does
            # not prune
            # `bo_campaigns`. Tracked in `docs/planning/BACKLOG.md`.
            await workflow.execute_activity(
                record_campaign_run,
                args=[
                    spec.problem,
                    proposed,
                    history,
                    actor,
                    correlation_id,
                    f"{workflow.info().workflow_id}:r{rounds_done}",
                ],
                start_to_close_timeout=timeout,
                heartbeat_timeout=heartbeat_timeout,
                schedule_to_start_timeout=self._queue_wait(),
                retry_policy=BAD_DATA_RETRY,
            )
            _carry_on_if_history_is_filling_up(
                payload, history, rounds_remaining, rounds_done, self._spent()
            )

        result = CampaignResult(best=best_of(spec.problem, history), history=history)

        # Terminal campaign record, keyed on the workflow id (stable across continue-as-new). The
        # actor
        # comes from the run's memo, which core sets on every connector job; the fallback is the
        # configured
        # service identity, as `require_actor` uses for runs started outside the wrapper. Skipped
        # when the
        # ceiling is spent — the per-round rows already make the campaign resumable. One dispatch is
        # left,
        # so the divisor is set to 1 rather than the stale value from the last round.
        self._dispatches_left = 1
        try:
            campaign_id: str | None = await workflow.execute_activity(
                record_campaign_run,
                args=[
                    spec.problem,
                    [Candidate(params=result.best.params)],
                    history,
                    actor,
                    correlation_id,
                    workflow.info().workflow_id,
                ],
                start_to_close_timeout=timeout,
                heartbeat_timeout=heartbeat_timeout,
                schedule_to_start_timeout=self._queue_wait(),
                retry_policy=BAD_DATA_RETRY,
            )
        except CampaignBudgetSpent:
            campaign_id = None
            budget_spent = True

        # Built here (the BO→note mapping is this domain's knowledge), published by core (the one
        # write path
        # that stamps provenance, best-effort). Always built; whether it is published is the
        # manifest's
        # `publish_to_graph`.
        note = note_from_campaign_result(spec.objective_name, spec.problem, result)
        best = result.best
        ending = (
            f"recorded as {campaign_id}"
            if campaign_id
            else "its terminal record could not be written, but every completed round is on file"
        )
        if budget_spent and rounds_remaining > 0:
            ending = (
                f"{ending}. It stopped with {rounds_remaining} round(s) unrun because the job's "
                "execution ceiling was spent — mostly waiting for a worker on the 'bo' queue. "
                "Re-run it to continue from here"
            )
        elif budget_spent:
            # Every round ran and only the terminal write could not be funded, so do not invite a
            # re-run.
            ending = (
                f"{ending}. Every round ran; only the terminal record was left unfunded when the "
                "job's execution ceiling was spent"
            )
        return ConnectorJobResult(
            summary=(
                f"campaign {spec.objective_name!r} finished after {len(history)} evaluation(s); "
                f"best objective {best.value:.6g} ({best.provenance}); "
                f"{ending}"
            ),
            data=result.model_dump(mode="json"),
            payload_kind=type(result).__name__,
            note=note,
        )
