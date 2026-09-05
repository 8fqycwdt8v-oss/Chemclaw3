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

# `connector_queue_wait_timeout` is passed at every dispatched activity below, and the reason
# is `durable/publish.py`'s: `start_to_close_timeout` starts when a worker picks the task up,
# so on its own it bounds none of the wait for one. A bundle queue served by no pod was
# otherwise indistinguishable from a busy one until the parent job's five-hour execution
# ceiling fired — a failure that names neither the queue nor the reason and reaches no
# workflow code. The bound is stated once, there, because a wait means something different on
# a bundle queue than on core's.
from chemclaw.durable.publish import (
    BAD_DATA_RETRY,
    calculation_retry,
    connector_queue_wait_timeout,
)
from chemclaw.durable.registry import durable_workflow


def _carry_on_if_history_is_filling_up(
    payload: dict[str, object],
    history: list[Observation],
    rounds_remaining: int,
    rounds_done: int,
) -> None:
    """Continue this campaign in a fresh run before Temporal's event history runs out.

    **The ceiling used to be a promise the workflow could not keep.** `bo_max_rounds` defaults to
    500 and its comment said it existed to stay inside the event-history limit — but the history is
    re-sent to `propose_next` every round, so bytes grow quadratically, and a measured
    178 bytes/`Observation` puts a batch-1 campaign over the 50 MB hard limit at round **441**. The
    server would terminate it there, losing every already-paid evaluation: exactly the failure the
    ceiling was written to prevent, at a round count the ceiling permits.

    That 441 is now optimistic by about sqrt(2): the per-round campaign-record write sends the same
    history to a second activity, so the quadratic term doubled (measured 17.25 MB -> 34.79 MB over
    441 rounds). The number is left as it was measured rather than re-derived, because it is cited
    as the *finding* that motivated this function, and the function does not depend on it — the
    trigger below is the server's own signal, which is the whole point.

    **The trigger is Temporal's own signal, not a round count.** `is_continue_as_new_suggested()`
    flips when the server sees history approaching its configured threshold, which is the only
    number that accounts for what this campaign actually carries — batch size, parameter count and
    objective count all change bytes-per-round, so any round count hard-coded here would be right
    for one problem shape and wrong for the rest.

    Continuing is safe mid-loop because the campaign's whole state is `CampaignCarryOver`: the spec
    is immutable and travels as the unread `payload`, and the observations are the only thing a
    round adds. Called *after* a round completes, never between the propose and the evaluate, so no
    already-paid evaluation is ever abandoned.

    `rounds_done` travels with it because the per-round campaign-record write keys its idempotency
    on the round index, and a continued run that restarted the count at zero would collide with the
    previous run's rows and silently drop them.
    """
    if rounds_remaining <= 0 or not workflow.info().is_continue_as_new_suggested():
        return
    workflow.continue_as_new(
        args=[
            payload,
            CampaignCarryOver(
                history=history, rounds_remaining=rounds_remaining, rounds_done=rounds_done
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

    **This is what makes a real screening campaign expressible.** Every registered objective is a
    function, which is what a *simulated* campaign needs and exactly what a chemist's campaign is
    not: BO's value at the bench is proposing a batch, waiting a week for the plates, and proposing
    the next. Before the durable wait existed there was nothing to suspend on, so `objective_name`
    could only ever name something computable and the loop ran to completion in seconds over
    numbers no chemist produced.

    A **child** workflow rather than an activity, and the distinction is the whole point: an
    activity has a start-to-close budget and a heartbeat, and a week is neither. The child holds the
    question, escalates on its own timer, and returns an outcome; this loop simply awaits it.

    The child's id carries the round, so two rounds of one campaign are two waits. That is a
    deliberate departure from `request_id_for`'s "asking twice is one wait" — round 4 of a campaign
    is not round 3, even when the conditions repeat, because the answer settles a different batch.

    An **expired** wait ends the campaign rather than continuing with a short history: proceeding
    would fit a surrogate to a batch nobody ran and propose round 5 from it, which is the inverted
    campaign in a different costume. `space_exhausted` already teaches this loop to stop early, so
    the caller sees a campaign that ended with what it had.
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
            # **Not the default**, and this is the longest-lived wait in the tree, so it is where
            # the default costs most. `execute_child_workflow` defaults to
            # `ParentClosePolicy.TERMINATE`, and a terminate never resumes workflow code — so a
            # campaign that ended any way other than by completing (a cancel, an operator
            # terminate) left this round's `pending_requests` row `waiting` with a `due_at` nothing
            # would ever act on. That row is permanent: `open_requests` keeps it in every entitled
            # person's inbox, the answer route signals a workflow that is gone and turns the
            # failure into a 503 telling them to try again, and `retention._NOT_PRUNED` refuses to
            # collect it. A fortnight of somebody being asked to run plates for a campaign that no
            # longer exists.
            #
            # `REQUEST_CANCEL` rather than `ABANDON`, measured across all three policies in
            # `tests/test_awaiting.py`: abandoning leaves the question live and answerable, so the
            # ask outlives the campaign it was for; cancelling delivers the `asyncio.CancelledError`
            # the wait was written to handle, and its detached settle takes the row out of the
            # inbox. `tests/test_bo_campaign.py` drives that on this call site rather than on a
            # stand-in, because a policy is a *start option* and only the starter can carry it.
            parent_close_policy=ParentClosePolicy.REQUEST_CANCEL,
        )
    )
    if outcome.state != "answered":
        return []
    # The answer is opaque to the wait and typed here, by the caller that asked — so a malformed
    # one fails this campaign with a message naming the batch, rather than being coerced into
    # plausible numbers somewhere no reviewer would look.
    return [Observation.model_validate(row) for row in outcome.payload.get("observations", [])]


@durable_workflow(bundle_queue("bo"))
# Its failures must be able to *be* failures: without this the SDK parks a plain exception raised
# in workflow code in an unbounded workflow-task-failure loop, so the parent
# `ConnectorJobWorkflow` waits forever and the chemist is told "running" indefinitely. Measured on
# a child reading an absent optional key from its payload (`exclude_none=True` drops one) — child
# RUNNING forever, parent waiting, session never told. See `durable/connector_job.py` for the trade.
@workflow.defn(failure_exception_types=[Exception])
class BoCampaignWorkflow:
    """Run a BO campaign durably and return the best point, the history, and the note to gate."""

    async def _evaluate(
        self,
        spec: CampaignSpec,
        candidates: list[Candidate],
        round_label: str,
        timeout: timedelta,
        heartbeat_timeout: timedelta,
    ) -> list[Observation]:
        """Turn candidates into observations, by computing them or by asking for them.

        The one branch a measured campaign needs, in one place, so the seed and every round take it
        identically — the alternative is the same `if` written twice with the second one eventually
        forgetting something the first learned.
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
                schedule_to_start_timeout=connector_queue_wait_timeout(),
                # **`calculation_retry` and not `BAD_DATA_RETRY`, because this activity reaches the
                # shared calculation backend.** A *computed* objective is
                # `science.bo.objectives.solubility_objective`, which calls `cached_remote` on a
                # cache miss, so `CalcBusyError` — the admission gate refusing a full pod — is one
                # of the failures this dispatch can see. Temporal's default 1/2/4/8 s against a
                # hold that is a whole calculation long is exactly what `calculation_retry`'s own
                # docstring calls "a small storm that then fails anyway": five attempts inside
                # fifteen seconds, and the round fails carrying the serving side's advice to retry.
                # The type list is identical, so a bad candidate still fails fast; only the spacing
                # differs, which is the property the calc bundle's own dispatch already has.
                retry_policy=calculation_retry(),
            )
        )

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
                schedule_to_start_timeout=connector_queue_wait_timeout(),
                retry_policy=BAD_DATA_RETRY,
            )
            history = await self._evaluate(spec, seed, "seed", timeout, heartbeat_timeout)
            rounds_remaining = spec.n_rounds
            rounds_done = 0
        else:
            resumed = CampaignCarryOver.model_validate(carried)
            history = resumed.history
            rounds_remaining = resumed.rounds_remaining
            rounds_done = resumed.rounds_done

        actor = workflow.memo_value("requested_by", settings.service_actor_id)
        correlation_id = workflow.memo_value("correlation_id", "")

        space = discrete_candidate_count(spec.problem)
        while rounds_remaining > 0:
            # Stop early if a purely discrete candidate set is exhausted.
            if space_exhausted(spec.problem, space, history, spec.batch):
                break
            proposed = await workflow.execute_activity(
                propose_next,
                args=[spec.problem, history, spec.batch, spec.seed],
                start_to_close_timeout=timeout,
                heartbeat_timeout=heartbeat_timeout,
                schedule_to_start_timeout=connector_queue_wait_timeout(),
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
            # Record the round *as it completes*, not only when the campaign does.
            #
            # The write used to happen once, after this loop. Everything a running campaign had
            # already paid for lived only in Temporal's own event history until then, so a campaign
            # cancelled, terminated, or failed non-retryably mid-run answered `resume_campaign`
            # with "no such campaign" about hours of real evaluation — the same gap the terminal
            # write closed for a campaign that *finishes*, left open for every other ending. It
            # also made cancellation lossy in a way that mattered: "pause this campaign" has no
            # signal handler and does not need one, because cancel-then-resume is the same thing
            # when the history survives the cancel.
            #
            # **Best-effort, and the guarantee has to be stated that way.** `record_suggestion`
            # catches `_TRANSIENT_WRITE_FAILURES` and returns normally, so this activity *succeeds*
            # on a round that was never persisted — no exception escapes, so `BAD_DATA_RETRY`
            # never fires and Temporal never re-runs it. That trade is right for the inline tool
            # (a database blip must not cost a chemist the suggestion already computed) and it is
            # inherited here rather than chosen, so a blip during a round loses that round's
            # observations and predictions permanently, with a WARNING and nothing else. The claim
            # is therefore "the history usually survives the cancel", not "always"; making it
            # always means letting the durable caller opt out of the swallow, which is a change to
            # `record_suggestion`'s contract and is on the backlog.
            #
            # Keyed on the round rather than the run, because `record_suggestion`'s idempotency is
            # `(campaign_id, job_id)` — a per-round write under the bare workflow id would dedupe
            # against round 1 and silently discard every round after it.
            #
            # The candidates recorded here are the ones actually proposed, carrying their
            # `predicted_value`/`predicted_sd`. The terminal write below records the *best* point
            # instead, which is a different statement and has no surrogate belief attached to it —
            # so the per-round rows are also the only place a campaign's predictions survive.
            #
            # **This doubles event-history growth, and that is the price of the guarantee.**
            # The history is now sent to two activities per round rather than one, so the
            # quadratic term doubles: measured on the Reizman problem at 173 B/observation
            # (the same order as the 178 B behind `_carry_on_if_history_is_filling_up`), a
            # batch-1 campaign books 17.25 MB of activity input over 441 rounds before this and
            # 34.79 MB after — 2.02x. It is a cost rather than a regression because the
            # continue-as-new trigger is Temporal's own dynamic signal and not a round count:
            # the campaign continues roughly twice as often and never approaches the limit. If
            # that frequency ever becomes the problem, the fix is to record every Nth round
            # rather than to send less history, because a row holding only one round's
            # observations would leave a resume with no evidence before it.
            #
            # **It costs stored bytes on the same argument, by a much larger factor.** Each row
            # snapshots the *cumulative* history, so N rounds store a triangular number of
            # observations rather than one final list: measured at 173 B/observation, a 500-round
            # batch-1 campaign stores 22.19 MB against the terminal write's 87.4 kB — **254x** —
            # and 87.45 MB at batch 4. `durable/retention.py` refuses to prune `bo_campaigns` and
            # `bo_suggestions` cascades from it, so nothing reclaims that. The snapshot is what
            # makes an interrupted campaign resumable at all, so it is the price of the guarantee
            # rather than an oversight — but it is a real number, unbounded over a deployment's
            # lifetime, and `docs/planning/BACKLOG.md` carries it with a trigger to revisit.
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
                schedule_to_start_timeout=connector_queue_wait_timeout(),
                retry_policy=BAD_DATA_RETRY,
            )
            _carry_on_if_history_is_filling_up(payload, history, rounds_remaining, rounds_done)

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
                actor,
                correlation_id,
                workflow.info().workflow_id,
            ],
            start_to_close_timeout=timeout,
            heartbeat_timeout=heartbeat_timeout,
            schedule_to_start_timeout=connector_queue_wait_timeout(),
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
