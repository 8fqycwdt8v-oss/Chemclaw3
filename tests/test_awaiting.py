r"""The durable wait: a question that outlives the turn that asked it.

Driven against a real broker, because the properties are about time and concurrency (deadlines,
escalation, a signal racing an expiry). The time-skipping server suits the days-long timers.
`test_the_tree_has_exactly_one_durable_wait` keeps it one primitive for every caller.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from temporalio import workflow
from temporalio.client import Client

with workflow.unsafe.imports_passed_through():
    from temporalio.client import WorkflowExecutionStatus, WorkflowHandle
    from temporalio.worker import UnsandboxedWorkflowRunner, Worker
    from temporalio.workflow import ParentClosePolicy

    from chemclaw.core.config import settings
    from chemclaw.durable import awaiting as awaiting_module
    from chemclaw.durable.awaiting import (
        AwaitAnswerWorkflow,
        AwaitOutcome,
        AwaitRequest,
        open_wait,
        request_id_for,
    )
    from tests.temporal_env import (
        pydantic_client,
        start_env_or_skip,
        start_local_env_or_skip,
    )

SRC = Path(__file__).resolve().parents[1] / "src" / "chemclaw"


class _Projection:
    """A stand-in for `pending_requests`, so the workflow is testable without a database.

    The store has its own tests; what these need is the *sequence* the workflow drives it through,
    which a real table would let a stray row confuse.
    """

    def __init__(self) -> None:
        self.opened: list[str] = []
        self.settled: list[tuple[str, str, str]] = []
        self.reminders: list[str] = []
        self.notified: list[object] = []


def _field(payload: object, name: str) -> str:
    """One field off an activity argument, whichever shape the converter handed over.

    The stand-ins declare `object`, so a mapping or a model may arrive; reading both avoids
    importing the workflow's private input models.
    """
    if isinstance(payload, dict):
        return str(payload.get(name, ""))
    return str(getattr(payload, name, ""))


async def _started(handle: WorkflowHandle[Any, Any], *, tries: int = 200) -> None:
    """Block until `handle` names a run the server has actually started.

    A child starts on the parent's next workflow task, so a terminate sent at once could precede it.
    Polled, not slept.
    """
    for _ in range(tries):
        try:
            await handle.describe()
            return
        except Exception:
            await asyncio.sleep(0.05)
    raise AssertionError(f"{handle.id} never started, so no policy had a child to act on")


async def _cancelled(handle: WorkflowHandle[Any, Any], *, tries: int = 200) -> None:
    """Block until `handle` has left `RUNNING`, so a status read cannot race the policy.

    The server applies a close policy after the parent closes; the cancelled arm is the slowest, so
    waiting on it covers the others.
    """
    for _ in range(tries):
        if (await handle.describe()).status != WorkflowExecutionStatus.RUNNING:
            return
        await asyncio.sleep(0.05)


@workflow.defn(name="_ParentOfAWait", sandboxed=False)
class _ParentOfAWait:
    """A stand-in for `ConnectorJobWorkflow._approve_effect`: start the wait, then block on it.

    At module scope because Temporal refuses `@workflow.run` on a local class, and taking the
    policy as an argument so one definition covers all three arms of the measurement below.
    """

    @workflow.run
    async def run(self, policy: int) -> str:
        """Open the wait as a child under `policy` and block until it answers."""
        return str(
            await workflow.execute_child_workflow(
                AwaitAnswerWorkflow.run,
                AwaitRequest(
                    kind="approval",
                    subject="approve the thing",
                    asked_of="qa-team",
                    requested_by="oid-asker",
                    deadline_days=7.0,
                ).model_dump(mode="json"),
                id=workflow.info().workflow_id + ":approval",
                task_queue=settings.background_task_queue,
                parent_close_policy=ParentClosePolicy(policy),
            )
        )


@workflow.defn(name="_ParentOfAWaitWithASession", sandboxed=False)
class _ParentOfAWaitWithASession:
    """`_ParentOfAWait` under `REQUEST_CANCEL`, with a session so the push-back actually runs.

    Separate from the first definition: that one varies the close policy over a sessionless wait,
    this one fixes the policy and varies where the cancellation lands. `_push` returns early without
    a session.
    """

    @workflow.run
    async def run(self, session_id: str) -> str:
        """Open the wait as a `REQUEST_CANCEL` child and block until it answers."""
        return str(
            await workflow.execute_child_workflow(
                AwaitAnswerWorkflow.run,
                AwaitRequest(
                    kind="approval",
                    subject="approve the thing",
                    asked_of="qa-team",
                    requested_by="oid-asker",
                    session_id=session_id,
                    deadline_days=7.0,
                ).model_dump(mode="json"),
                id=workflow.info().workflow_id + ":approval",
                task_queue=settings.background_task_queue,
                parent_close_policy=ParentClosePolicy.REQUEST_CANCEL,
            )
        )


def _worker(client: Client, projection: _Projection) -> Worker:
    """A worker serving the wait, with the projection activities replaced by recorders."""

    async def open_activity(payload: object) -> str:
        """Stands in for the projection, and must honour its contract.

        The real activity clamps to `awaiting_max_days` and returns the deadline the workflow
        schedules against, so the stub must return one too.
        """
        projection.opened.append(_field(payload, "request_id"))
        request: Any = payload["request"] if isinstance(payload, dict) else payload.request  # type: ignore[attr-defined]
        days = request["deadline_days"] if isinstance(request, dict) else request.deadline_days
        deadline = timedelta(days=max(0.0, min(float(days), settings.awaiting_max_days)))
        return (datetime.fromisoformat(_field(payload, "started_at")) + deadline).isoformat()

    async def settle_activity(payload: object) -> bool:
        projection.settled.append(
            (
                _field(payload, "request_id"),
                _field(payload, "state"),
                _field(payload, "answered_by"),
            )
        )
        return True

    async def remind_activity(request_id: str, count: int = 0) -> None:
        projection.reminders.append(request_id)

    async def notify_activity(payload: object) -> None:
        projection.notified.append(payload)

    from temporalio import activity

    return Worker(
        client,
        task_queue=settings.background_task_queue,
        workflows=[AwaitAnswerWorkflow],
        activities=[
            activity.defn(name="open_pending_request_activity")(open_activity),
            activity.defn(name="settle_pending_request_activity")(settle_activity),
            activity.defn(name="record_reminder_activity")(remind_activity),
            # The push-back the wait sends on open, on each reminder and on expiry.
            activity.defn(name="record_session_event_activity")(notify_activity),
        ],
    )


async def test_a_wait_returns_the_answer_that_arrives() -> None:
    """A signal releases the wait, and the outcome carries who answered and what they said."""
    async with await start_env_or_skip() as env:
        client = pydantic_client(env)
        projection = _Projection()
        async with _worker(client, projection):
            request = AwaitRequest(
                kind="measurement", subject="run four conditions", deadline_days=7
            )
            handle = await client.start_workflow(
                AwaitAnswerWorkflow.run,
                request.model_dump(mode="json"),
                id="await-answered",
                task_queue=settings.background_task_queue,
            )
            await handle.signal("provide", {"answered_by": "u-lab-1", "payload": {"yield": 0.71}})
            outcome = AwaitOutcome.model_validate(await handle.result())

    assert outcome.state == "answered"
    assert outcome.answered_by == "u-lab-1"
    assert outcome.payload == {"yield": 0.71}
    # The projection is opened once and settled once, as `answered`, by the actor who signalled.
    assert projection.opened == ["await-answered"]
    assert projection.settled == [("await-answered", "answered", "u-lab-1")]


async def test_the_first_answer_wins_and_later_ones_are_ignored() -> None:
    """A second signal cannot overwrite a delivered answer.

    Ignored rather than rejected: a signal has no reply channel, so raising would retry forever.
    `POST /pending/{id}/answer` returns 409; this holds even for a direct broker signal.
    """
    async with await start_env_or_skip() as env:
        client = pydantic_client(env)
        projection = _Projection()
        async with _worker(client, projection):
            handle = await client.start_workflow(
                AwaitAnswerWorkflow.run,
                AwaitRequest(subject="approve the route change").model_dump(mode="json"),
                id="await-twice",
                task_queue=settings.background_task_queue,
            )
            await handle.signal("provide", {"answered_by": "first", "payload": {"ok": True}})
            await handle.signal("provide", {"answered_by": "second", "payload": {"ok": False}})
            outcome = AwaitOutcome.model_validate(await handle.result())

    assert outcome.answered_by == "first"
    assert outcome.payload == {"ok": True}


async def test_a_deadline_that_passes_is_an_outcome_and_not_a_failure() -> None:
    """An unanswered question ends `expired` — reported, not raised, and never retried.

    This is the property a project leader's world depends on: "nobody answered" is an answer, and a
    wait that raised would be retried by Temporal rather than reported to the person who asked.
    """
    async with await start_env_or_skip() as env:
        client = pydantic_client(env)
        projection = _Projection()
        async with _worker(client, projection):
            outcome = AwaitOutcome.model_validate(
                await client.execute_workflow(
                    AwaitAnswerWorkflow.run,
                    AwaitRequest(
                        subject="report the stability pull",
                        # One day, chased every six hours: the time-skipping server runs this
                        # in milliseconds, and the numbers are what a real ask looks like.
                        deadline_days=1.0,
                        reminder_hours=6.0,
                    ).model_dump(mode="json"),
                    id="await-expired",
                    task_queue=settings.background_task_queue,
                )
            )

    assert outcome.state == "expired"
    assert outcome.answered_by == ""
    # Chased on the way: four six-hour intervals inside one day, the last of which reaches the
    # deadline rather than escalating again.
    assert outcome.reminders == 3
    assert projection.reminders == ["await-expired"] * 3
    assert projection.settled == [("await-expired", "expired", "")]


async def test_an_answer_arriving_mid_interval_is_seen_immediately() -> None:
    """The reminder interval is a timeout on the wait, not a polling tick.

    Sleep-then-check would hold a delivered answer for up to an interval while every final-state
    assertion still passed.
    """
    async with await start_env_or_skip() as env:
        client = pydantic_client(env)
        projection = _Projection()
        async with _worker(client, projection):
            handle = await client.start_workflow(
                AwaitAnswerWorkflow.run,
                AwaitRequest(
                    subject="confirm the assignment",
                    deadline_days=30.0,
                    reminder_hours=24.0,
                ).model_dump(mode="json"),
                id="await-midinterval",
                task_queue=settings.background_task_queue,
            )
            await handle.signal("provide", {"answered_by": "u-2", "payload": {}})
            outcome = AwaitOutcome.model_validate(await handle.result())

    assert outcome.state == "answered"
    # Answered before the first daily chase, over a thirty-day deadline.
    assert outcome.reminders == 0
    assert projection.reminders == []


def test_asking_the_same_question_of_the_same_people_is_one_wait() -> None:
    """The request id is derived from the ask, so two askers join one wait rather than opening two.

    Keyed on the question and routing, not the per-turn session or correlation id — like a shared
    cache row (D-011).
    """
    first = AwaitRequest(kind="measurement", subject="assay lot 42", asked_of="qc-team")
    same = AwaitRequest(
        kind="measurement",
        subject="assay lot 42",
        asked_of="qc-team",
        session_id="another-session",
        correlation_id="another-turn",
        rationale="a different reason, still the same question",
    )
    other = AwaitRequest(kind="measurement", subject="assay lot 43", asked_of="qc-team")

    assert request_id_for(first) == request_id_for(same)
    assert request_id_for(first) != request_id_for(other)


def test_the_tree_has_exactly_one_durable_wait() -> None:
    """One primitive with several callers, not one shape per caller.

    A `wait_condition` elsewhere would be a second deadline, escalation and set of races to get
    right.
    """
    waiting = sorted(
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if "workflow.wait_condition" in path.read_text(encoding="utf-8")
    )
    assert waiting == ["durable/awaiting.py"], (
        f"{waiting} hold a durable wait. There is one primitive: `AwaitAnswerWorkflow`. A second "
        "is a second deadline and a second set of races — start a child workflow instead."
    )


def test_the_answer_carries_no_authorization() -> None:
    """`Answer` has no roles field, and the route is what decides who may answer.

    A signal is unsigned (`D-2026-08-28-roles-do-not-cross-the-durable-boundary-unsigned`), so any
    entitlement lifted from one would be a forgery channel.
    """
    from chemclaw.durable.awaiting import Answer

    fields = set(Answer.model_fields)
    assert fields == {"answered_by", "payload"}, (
        f"`Answer` carries {sorted(fields)}. A signal is unsigned: `answered_by` is attribution, "
        "and anything role-shaped here would be a control that reads like one and is not."
    )
    # And the route that *does* decide is the one place naming an entitlement.
    route = (SRC / "api" / "routes" / "pending.py").read_text(encoding="utf-8")
    assert "GROUP_ROLE_PREFIX" in route and "_may_answer" in route


def test_the_migration_refuses_an_unattributed_answer() -> None:
    """The schema will not store `answered` without a timestamp and an actor.

    Asserted over the SQL: an unattributed answer is worse in an audit than no row.
    """
    sql = (
        Path(__file__).resolve().parents[1] / "infra" / "sql" / "076_pending_requests.sql"
    ).read_text(encoding="utf-8")
    assert "pending_requests_answer_is_attributed" in sql
    assert "answered_at IS NOT NULL AND answered_by <> ''" in sql


async def test_the_deadline_ceiling_is_applied_by_the_activity_every_caller_goes_through() -> None:
    """The deadline ceiling is applied by the activity every caller goes through.

    `open_pending_request_activity` clamps to `awaiting_max_days`, and the workflow takes `due_at`
    from its result so a replay reads the original deadline. Driven directly: no caller can skip it.
    """
    from chemclaw.durable.awaiting import (
        AwaitRequest,
        _OpenInput,
        open_pending_request_activity,
    )
    from tests.pg import migrated_db_or_skip

    await migrated_db_or_skip()
    started = datetime(2026, 1, 1, tzinfo=UTC)
    opened = await open_pending_request_activity(
        _OpenInput(
            request_id="req-clamp-probe",
            request=AwaitRequest(
                kind="measurement",
                subject="a wildly optimistic deadline",
                requested_by="u-1",
                deadline_days=3650.0,
            ),
            started_at=started.isoformat(),
            run_id="run-clamp",
        )
    )
    capped = datetime.fromisoformat(opened) - started
    assert capped <= timedelta(days=settings.awaiting_max_days), (
        f"a caller asked for 3650 days and got {capped.days}; the ceiling is "
        f"{settings.awaiting_max_days}"
    )

    # And a deadline inside the ceiling is passed through untouched.
    modest = await open_pending_request_activity(
        _OpenInput(
            request_id="req-clamp-probe-2",
            request=AwaitRequest(
                kind="measurement",
                subject="an ordinary deadline",
                requested_by="u-1",
                deadline_days=2.0,
            ),
            started_at=started.isoformat(),
            run_id="run-clamp",
        )
    )
    assert datetime.fromisoformat(modest) - started == timedelta(days=2)


def test_a_wait_started_as_a_child_settles_when_its_parent_dies() -> None:
    """A parent that ends *other* than by completing must not strand its wait `waiting` forever.

    A stranded row stays in every entitled inbox, unanswerable, and is never pruned. Measured over
    all three close policies: `TERMINATE` (the default) never resumes workflow code, `ABANDON`
    leaves a live question about dead work for up to `awaiting_max_days`, and only `REQUEST_CANCEL`
    reaches `run`'s cleanup. The asserted fact is `CANCELED` vs `TERMINATED`, not that the settle
    landed, which is a dispatch race. One real-time environment for all three arms.
    """
    from temporalio import activity

    async def _open(payload: object) -> str:
        request: Any = payload["request"] if isinstance(payload, dict) else payload.request  # type: ignore[attr-defined]
        days = request["deadline_days"] if isinstance(request, dict) else request.deadline_days
        started = datetime.fromisoformat(_field(payload, "started_at"))
        return (started + timedelta(days=float(days))).isoformat()

    async def _settle(payload: object) -> bool:
        """Registered because the wait calls it, and not recorded because it is not asserted."""
        return True

    async def _remind(request_id: str, count: int = 0) -> None: ...

    async def _notify(payload: object) -> None: ...

    policies = (
        ParentClosePolicy.TERMINATE,
        ParentClosePolicy.ABANDON,
        ParentClosePolicy.REQUEST_CANCEL,
    )

    async def _run() -> dict[str, str]:
        """Start one parent per policy, kill them all, and report what each child did."""
        async with await start_local_env_or_skip() as env:
            client = pydantic_client(env)
            async with Worker(
                client,
                task_queue=settings.background_task_queue,
                workflows=[AwaitAnswerWorkflow, _ParentOfAWait],
                workflow_runner=UnsandboxedWorkflowRunner(),
                activities=[
                    activity.defn(name="open_pending_request_activity")(_open),
                    activity.defn(name="settle_pending_request_activity")(_settle),
                    activity.defn(name="record_reminder_activity")(_remind),
                    activity.defn(name="record_session_event_activity")(_notify),
                ],
            ):
                parents = {
                    policy: await client.start_workflow(
                        _ParentOfAWait.run,
                        int(policy),
                        id=f"parent-dies-{policy.name.lower()}",
                        task_queue=settings.background_task_queue,
                    )
                    for policy in policies
                }
                # Every wait has to be genuinely open before its parent dies, or this measures
                # nothing: the child is what the policy acts on.
                children = {
                    policy: client.get_workflow_handle(f"{parent.id}:approval")
                    for policy, parent in parents.items()
                }
                for child in children.values():
                    await _started(child)
                for parent in parents.values():
                    await parent.terminate("the parent died some way other than completing")
                # One grace for the wave, so a status read cannot race the server applying the
                # policy. Bounded on the *cancelled* status rather than on a settle, for the reason
                # the docstring gives.
                await _cancelled(children[ParentClosePolicy.REQUEST_CANCEL])
                described = {p: await c.describe() for p, c in children.items()}
                # `status` is `WorkflowExecutionStatus | None` on the SDK's description; a child
                # this block has just awaited into a terminal or running state always carries one,
                # and an absent status is a bug in the probe rather than an outcome to assert on.
                return {
                    policy.name: d.status.name if d.status else "NO_STATUS"
                    for policy, d in described.items()
                }

    outcomes = asyncio.run(_run())

    assert outcomes["TERMINATE"] == "TERMINATED", (
        f"the shipped default left the child {outcomes['TERMINATE']}; a terminate never resumes "
        "workflow code, so the projection stays `waiting` for ever"
    )
    assert outcomes["ABANDON"] == "RUNNING", (
        f"ABANDON left the child {outcomes['ABANDON']}; the wait is still open and still "
        "answerable, about work that no longer exists, for the rest of its deadline"
    )
    assert outcomes["REQUEST_CANCEL"] == "CANCELED", (
        f"REQUEST_CANCEL left the child {outcomes['REQUEST_CANCEL']}; only a cancellation reaches "
        "`run`'s own `except asyncio.CancelledError`, which is what settles the row"
    )


def test_a_cancellation_arriving_before_the_timer_still_settles_the_row() -> None:
    """The wait's cleanup must cover every `await` it makes, not only the one it spends its life in.

    `D-2026-09-13-a-cancellation-arriving-before-the-timer-leaves-the-row-waiting`. Two arms:

    - The open activity writes the `waiting` row, so the `try` must enclose it; the first arm
      cancels
      while the open is held and asserts the settle.
    - Inside an activity a cancellation arrives as `ActivityError(cause=CancelledError)`, which
      `notify_session_best_effort` must not swallow; the second arm asserts both the status and the
      settle.

    One real-time environment and worker for both.
    """
    from temporalio import activity

    held = {"open": asyncio.Event(), "notify": asyncio.Event()}
    release = asyncio.Event()
    settled: list[str] = []

    async def _open(payload: object) -> str:
        request_id = _field(payload, "request_id")
        if request_id.startswith("cancel-in-open"):
            # Held, not slept: a sleep makes the arm a race against the box's speed, and the whole
            # point is that the cancellation arrives *while this activity is in flight*.
            held["open"].set()
            await release.wait()
        request: Any = payload["request"] if isinstance(payload, dict) else payload.request  # type: ignore[attr-defined]
        days = request["deadline_days"] if isinstance(request, dict) else request.deadline_days
        started = datetime.fromisoformat(_field(payload, "started_at"))
        return (started + timedelta(days=float(days))).isoformat()

    async def _settle(payload: object) -> bool:
        settled.append(_field(payload, "request_id"))
        return True

    async def _remind(request_id: str, count: int = 0) -> None: ...

    async def _notify(payload: object) -> None:
        if _field(payload, "session_id") == "sess-cancel-in-notify":
            held["notify"].set()
            await release.wait()

    async def _run() -> tuple[dict[str, str], list[str]]:
        async with await start_local_env_or_skip() as env:
            client = pydantic_client(env)
            async with Worker(
                client,
                task_queue=settings.background_task_queue,
                workflows=[AwaitAnswerWorkflow, _ParentOfAWaitWithASession],
                workflow_runner=UnsandboxedWorkflowRunner(),
                activities=[
                    activity.defn(name="open_pending_request_activity")(_open),
                    activity.defn(name="settle_pending_request_activity")(_settle),
                    activity.defn(name="record_reminder_activity")(_remind),
                    activity.defn(name="record_session_event_activity")(_notify),
                ],
            ):
                parents = {
                    arm: await client.start_workflow(
                        _ParentOfAWaitWithASession.run,
                        f"sess-cancel-in-{arm}",
                        id=f"cancel-in-{arm}",
                        task_queue=settings.background_task_queue,
                    )
                    for arm in ("open", "notify")
                }
                children = {
                    arm: client.get_workflow_handle(f"{parent.id}:approval")
                    for arm, parent in parents.items()
                }
                # Each child must actually be *inside* its activity before its parent dies, or the
                # arm measures the ordinary timer case the old clause already covered.
                for arm in ("open", "notify"):
                    await asyncio.wait_for(held[arm].wait(), timeout=60)
                for parent in parents.values():
                    await parent.terminate("the parent died while the child was in an activity")
                for child in children.values():
                    await _cancelled(child)
                described = {arm: await c.describe() for arm, c in children.items()}
                statuses = {
                    arm: d.status.name if d.status else "NO_STATUS" for arm, d in described.items()
                }
                # Released only now: the activities are held for the duration of the measurement,
                # so nothing finishes by outrunning the terminate.
                release.set()
                return statuses, list(settled)

    statuses, rows = asyncio.run(_run())

    assert "cancel-in-open:approval" in rows, (
        "a cancellation arriving while the open activity was in flight attempted no settle, so the "
        f"row it had just written stays `waiting` for ever; settled: {rows}"
    )
    assert "cancel-in-notify:approval" in rows, (
        "a cancellation arriving while the push-back activity was in flight attempted no settle; "
        f"settled: {rows}"
    )
    assert statuses["notify"] == "CANCELED", (
        f"the child whose push-back was cancelled is {statuses['notify']}: the cancellation was "
        "swallowed as a delivery failure and the wait went back to its seven-day timer, so the "
        "question is still live and still answerable about work that no longer exists"
    )


def test_every_wait_started_as_a_child_names_a_parent_close_policy() -> None:
    """Every wait started as a child names a parent close policy.

    The policy is a start option chosen by the caller, and omitting it silently picks the one that
    strands the row. Matched on both names appearing in one file (robust to reformatting), with a
    floor so an empty scan fails, over the whole package since callers live in bundles too.
    """
    starters = [
        path
        for path in SRC.rglob("*.py")
        if "execute_child_workflow" in (text := path.read_text(encoding="utf-8"))
        and "AwaitAnswerWorkflow.run" in text
    ]
    assert starters, (
        "nothing in `src/` starts the wait as a child any more — either every caller moved to a "
        "different primitive, or this guard is now asserting nothing"
    )
    offenders = [
        path.relative_to(SRC).as_posix()
        for path in starters
        if "parent_close_policy=ParentClosePolicy.REQUEST_CANCEL"
        not in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], (
        f"{offenders} start the durable wait as a child without naming a parent close policy, so "
        "it defaults to TERMINATE and the projection is stranded `waiting` when the parent dies"
    )


def test_a_re_ask_of_an_answered_question_opens_through_the_activity() -> None:
    """A re-ask of an answered question opens through the activity.

    `D-2026-09-13-an-answer-is-archived-so-the-question-can-be-asked-again`. `request_id_for` keys
    on `(kind, subject, asked_of)` and `open_wait` allows duplicates, so re-asking a standing
    question mints the same id; the answered row is archived and the reopen returns the requested
    deadline. Driven through the activity, since that is where a refusal would sit. The previous
    cycle's attribution survives.
    """
    from chemclaw.core.db import connect
    from chemclaw.durable import pending_store
    from chemclaw.durable.awaiting import _OpenInput, open_pending_request_activity
    from tests.pg import migrated_db_or_skip

    async def _run() -> None:
        await migrated_db_or_skip()
        request_id = "req-refused-open"
        started = datetime(2026, 1, 1, tzinfo=UTC)

        def _input(run_id: str) -> _OpenInput:
            return _OpenInput(
                request_id=request_id,
                request=AwaitRequest(
                    kind="measurement",
                    subject="the monthly stability pull",
                    requested_by="u-1",
                    deadline_days=7.0,
                ),
                started_at=started.isoformat(),
                run_id=run_id,
            )

        async with await connect(settings.postgres_dsn) as conn:
            await conn.execute("DELETE FROM pending_requests WHERE request_id = %s", (request_id,))
            await conn.execute(
                "DELETE FROM pending_request_answers WHERE request_id = %s", (request_id,)
            )
            await conn.commit()

        await open_pending_request_activity(_input("run-1"))
        await pending_store.settle_request(
            request_id, state="answered", answered_by="u-2", answer={"reading": 4}
        )

        # The same question again, as a new run.
        due_at = await open_pending_request_activity(_input("run-2"))
        assert due_at == (started + timedelta(days=7.0)).isoformat(), (
            f"the re-ask did not come back with the deadline it asked for: {due_at}"
        )

        reopened = await pending_store.get_request(request_id)
        assert reopened is not None and reopened.state == "waiting", (
            "the wait opened against a projection that still reads `answered`, so it is invisible "
            "in every inbox and unanswerable for its whole deadline"
        )

        # And the previous cycle's answer is where nothing can overwrite it.
        async with await connect(settings.postgres_dsn) as conn:
            cur = await conn.execute(
                "SELECT run_id, answered_by, answer FROM pending_request_answers "
                "WHERE request_id = %s",
                (request_id,),
            )
            archived = [(str(r[0]), str(r[1]), dict(r[2])) for r in await cur.fetchall()]
        assert archived == [("run-1", "u-2", {"reading": 4})], (
            f"the previous cycle's attribution is not in the archive: {archived}"
        )

    asyncio.run(_run())


def test_the_launch_idiom_joins_an_open_wait_and_reopens_a_settled_one(
    monkeypatch: Any,
) -> None:
    """`open_wait`'s three coupled decisions, run rather than described.

    Callers' tests patch `open_wait` away, so this is its only test: a deterministic id,
    `ALLOW_DUPLICATE`, and the already-started catch. Joining: while a wait is open, asking again
    returns `False`. Reopening: a completed wait (expiry or answer) must accept the same id again
    and return `True`, or a lapsed question becomes unaskable.
    """

    async def _run() -> None:
        async with await start_env_or_skip() as env:
            client = pydantic_client(env)

            async def _client() -> Client:
                return client

            monkeypatch.setattr(awaiting_module, "connect", _client)
            projection = _Projection()
            async with _worker(client, projection):
                request = AwaitRequest(
                    kind="measurement", subject="the monthly stability pull", deadline_days=7
                )

                first_id, opened = await open_wait(request)
                assert opened is True, "the first ask did not open the wait"
                assert first_id == request_id_for(request), (
                    "the launch minted an id `request_id_for` does not agree with, so a second "
                    "asker joins nothing and the projection keys on a row nobody else can find"
                )

                joined_id, joined = await open_wait(request)
                assert joined is False, (
                    "asking again while the wait is open reported a fresh start; the caller would "
                    "put a second notice in front of whoever is already being asked"
                )
                assert joined_id == first_id

                handle: WorkflowHandle[Any, Any] = client.get_workflow_handle(first_id)
                await handle.signal("provide", {"answered_by": "u-lab-1", "payload": {"n": 1}})
                outcome = AwaitOutcome.model_validate(await handle.result())
                assert outcome.state == "answered", "the fixture's premise: the wait is settled"

                reopened_id, reopened = await open_wait(request)
                assert reopened is True, (
                    "a settled question could not be asked again. That is what "
                    "REJECT_DUPLICATE and ALLOW_DUPLICATE_FAILED_ONLY do here, and it is why "
                    "`open_wait` states ALLOW_DUPLICATE rather than leaning on the SDK default"
                )
                assert reopened_id == first_id, "the re-ask is the same question and the same id"
                # A second run genuinely exists under that id; asserted via `describe` rather than
                # the projection recorder, whose activity may not have been dispatched yet.
                described = await client.get_workflow_handle(first_id).describe()
                assert described.status == WorkflowExecutionStatus.RUNNING, (
                    f"the re-ask returned True and started nothing: {described.status}"
                )

    asyncio.run(_run())
