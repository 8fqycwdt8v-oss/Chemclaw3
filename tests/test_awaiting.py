r"""The durable wait: a question that outlives the turn that asked it.

Driven against a real broker rather than by calling the workflow body, because every property worth
asserting here is a property of *time and concurrency* — a deadline, an escalation, a signal racing
an expiry — and none of them exists when the body is called as a function. The time-skipping server
is the right instrument: the escalation timer is days long and the deadline is weeks.

Before this workflow existed, `grep -rn "workflow.signal\\|wait_condition\\|workflow.update" src/`
returned zero hits. `test_the_tree_still_has_exactly_one_durable_wait` pins that this stayed one
primitive rather than becoming one per caller, which is the whole argument for building it.
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
    from chemclaw.durable.awaiting import (
        AwaitAnswerWorkflow,
        AwaitOutcome,
        AwaitRequest,
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

    The stand-ins below declare `object`, so the pydantic converter leaves a mapping alone and
    passes a model through as itself. Reading both is cheaper than importing the workflow's private
    input models into a test, and it is the difference between asserting the sequence and asserting
    an empty string.
    """
    if isinstance(payload, dict):
        return str(payload.get(name, ""))
    return str(getattr(payload, name, ""))


async def _started(handle: WorkflowHandle[Any, Any], *, tries: int = 200) -> None:
    """Block until `handle` names a run the server has actually started.

    A child workflow is started by the parent's *next* workflow task, so a terminate sent the
    instant the parent starts can land before the child exists — and a close policy would have
    nothing to act on. Polled rather than slept on so the wait is as short as the box allows.
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

    A close policy is applied by the server *after* the parent closes, so all three arms below need
    one grace before they are read. Bounded on the cancelled arm because it is the slowest of the
    three to reach its terminal state — a terminate is immediate and an abandon changes nothing —
    so any policy the server was going to apply has been applied by then.
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

    A separate definition rather than another argument on the first, because the two measure
    different things and sharing one would make each arm's fixture read as the other's: that one
    varies the *close policy* over a sessionless wait, this one fixes the policy at the only one
    that reaches the cleanup clause and varies where in the wait the cancellation lands. The session
    is the whole point — `_push` returns early without one, so the push-back window this opens does
    not exist for a sessionless wait.
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

        The real activity owns the clamp against `awaiting_max_days` and *returns* the deadline the
        workflow schedules its timers against — one place, on the path every caller takes, because
        clamping at each launch site reached two of three. A stub that returned `None` made the
        workflow fail on the first line that used the value, which is the stub being wrong rather
        than the workflow: a recorder still has to answer what it is asked for.
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


def test_a_wait_returns_the_answer_that_arrives() -> None:
    """A signal releases the wait, and the outcome carries who answered and what they said."""

    async def _run() -> None:
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
                await handle.signal(
                    "provide", {"answered_by": "u-lab-1", "payload": {"yield": 0.71}}
                )
                outcome = AwaitOutcome.model_validate(await handle.result())

        assert outcome.state == "answered"
        assert outcome.answered_by == "u-lab-1"
        assert outcome.payload == {"yield": 0.71}
        # The projection is opened once and settled once, as `answered`, by the actor who signalled.
        assert projection.opened == ["await-answered"]
        assert projection.settled == [("await-answered", "answered", "u-lab-1")]

    asyncio.run(_run())


def test_the_first_answer_wins_and_later_ones_are_ignored() -> None:
    """A second signal cannot overwrite a delivered answer.

    Ignored rather than rejected, and the reason is structural: a signal has no reply channel, so
    raising would fail the workflow task and retry the send forever. The caller is told `409` by
    `POST /pending/{id}/answer`, which reads the store — this asserts the half that has to hold even
    when somebody reaches the broker directly.
    """

    async def _run() -> None:
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

    asyncio.run(_run())


def test_a_deadline_that_passes_is_an_outcome_and_not_a_failure() -> None:
    """An unanswered question ends `expired` — reported, not raised, and never retried.

    This is the property a project leader's world depends on: "nobody answered" is an answer, and a
    wait that raised would be retried by Temporal rather than reported to the person who asked.
    """

    async def _run() -> None:
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

    asyncio.run(_run())


def test_an_answer_arriving_mid_interval_is_seen_immediately() -> None:
    """The reminder interval is a timeout on the wait, not a polling tick.

    Written because the obvious implementation — sleep for the interval, then check — would hold a
    delivered answer for up to a day before acting on it, and would look correct in every test that
    only asserted the final state.
    """

    async def _run() -> None:
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

    asyncio.run(_run())


def test_asking_the_same_question_of_the_same_people_is_one_wait() -> None:
    """The request id is derived from the ask, so two askers join one wait rather than opening two.

    Keyed on the question and its routing and **not** on the session or correlation id, which change
    per turn: two chemists asking the lab for the same measurement should be one request in the
    lab's inbox, exactly as two identical calculations share one cache row (D-011).
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

    The whole case for building this was that a BO round awaiting plates, a gate awaiting a
    committee and an effect awaiting an approval are the same object. That case is only kept if the
    second caller reuses the first: a `wait_condition` appearing in another module is a second
    deadline, a second escalation and a second set of race conditions to get right.
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

    An absence pinned, for the reason `D-2026-08-28-roles-do-not-cross-the-durable-boundary-
    unsigned` gives: a signal is unsigned, so anything a workflow lifts out of one and treats as an
    entitlement is a forgery channel with an audit trail that names the impersonated user.
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

    Asserted over the SQL because it is a constraint rather than code: a row reading "somebody
    answered at some point" is worse in an audit than no row, which is the rule `note_proposals`
    already applies to a decision.
    """
    sql = (
        Path(__file__).resolve().parents[1] / "infra" / "sql" / "076_pending_requests.sql"
    ).read_text(encoding="utf-8")
    assert "pending_requests_answer_is_attributed" in sql
    assert "answered_at IS NOT NULL AND answered_by <> ''" in sql


def test_the_deadline_ceiling_is_applied_by_the_activity_every_caller_goes_through() -> None:
    """`awaiting_max_days` had no test, which is why the clamp reached two of three launch sites.

    It was first applied at each caller. `agent/pending_tools.py` and `connectors/jobs.py` got it;
    `connectors/bo/workflows.py` passed `bo_measurement_deadline_days` straight through, so a
    mis-set value opened a ten-year run on the broker — the thing the ceiling exists to prevent —
    while two docstrings went on saying the value was clamped.

    It now lives in `open_pending_request_activity`, which every wait goes through, and the workflow
    takes `due_at` from that activity's *result* so a replay reads the deadline the original
    execution used. This drives the activity directly: a caller cannot skip it, so neither can this.
    """
    from chemclaw.durable.awaiting import (
        AwaitRequest,
        _OpenInput,
        open_pending_request_activity,
    )
    from tests.pg import migrated_db_or_skip

    async def _run() -> None:
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

    asyncio.run(_run())


def test_a_wait_started_as_a_child_settles_when_its_parent_dies() -> None:
    """A parent that ends *other* than by completing must not strand its wait `waiting` forever.

    `execute_child_workflow` defaults to `ParentClosePolicy.TERMINATE`, and a terminate never
    resumes workflow code — so the wait's `except asyncio.CancelledError` clause, the whole reason
    the detached settle in `_settle` exists, was unreachable at every call site that started one.
    The projection row therefore stayed `waiting` with a `due_at` nothing would ever act on:
    permanently in every entitled person's inbox, and permanently unanswerable, because
    `POST /pending/{id}/answer` reads `waiting`, signals a workflow that is gone, and turns the
    failure into a 503 telling the caller to try again. `pending_requests` is in
    `retention._NOT_PRUNED`, so nothing collects it either — one immortal ghost per dead parent.

    Measured over all three policies rather than asserting the chosen one, because the obvious
    answer is the wrong one. `ABANDON` — "let the wait outlive the parent and expire on its own
    deadline" — leaves the child `RUNNING`, so a live, answerable question about work that no
    longer exists stays in the inbox for up to `awaiting_max_days`: 90 days of somebody being asked
    to do something pointless. `TERMINATE`, the default, never resumes workflow code at all.
    `REQUEST_CANCEL` is the only one that reaches `run`'s `except asyncio.CancelledError`, which is
    what the module wrote its detached settle for.

    **What is asserted is the child's status, and deliberately not that the settle landed.** The
    settle is scheduled from a workflow that is already cancelling, so whether the server dispatches
    it before the run closes is a race — driven here it landed on some passes and not others, with
    a 15 s grace. Asserting it would be a flaky test making a claim the code does not guarantee.
    `CANCELED` versus `TERMINATED` is the deterministic half and the one that discriminates: it
    says the wait got the chance to settle, which is exactly what the policy buys and what the
    default denied it. That the settle can still be missed is a real gap in the wait, wider than
    this policy, and it belongs in its own finding rather than in a assertion that flakes.

    All three in **one** environment, terminated together: three separate environments was the
    first shape and paid the startup three times over.

    Real-time rather than time-skipping: this is a test about a wall-clock broker event on runs
    that must still be `RUNNING` when it arrives, and the time-skipping server fast-forwards an
    idle workflow straight to its own timeout instead.
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

    `D-2026-09-13-a-cancellation-arriving-before-the-timer-leaves-the-row-waiting`. The sibling test
    above establishes that `REQUEST_CANCEL` is the only policy that reaches `run`'s
    `except` clause at all. What it does not establish — and said out loud it was not asserting — is
    that the clause is reachable from wherever the wait happens to be. It was not, in two ways, and
    both leave the permanent ghost that test's docstring describes: a `pending_requests` row stuck
    `waiting`, in every entitled person's inbox, unanswerable because the run it names is gone, and
    never collected because `pending_requests` is in `retention._NOT_PRUNED`.

    **The `try` started at the wait, and the row is written before it.**
    `open_pending_request_activity` is what creates the `waiting` row, and it sat *above* the
    `try` — so a cancellation landing while it was in flight committed the row and attempted no
    settle. Measured against a real broker with 12 parents terminated the instant their children
    existed: 12 rows opened, **10** settled, every child `CANCELED`. The deterministic form is the
    first arm here — the open activity blocks on an event, the parent is terminated while it is
    held, and the settle is asserted.

    **A cancellation does not always arrive as `asyncio.CancelledError`.** Blocked on
    `wait_condition` it does, which is why the loss looked like a dispatch race — it was measured at
    0 in six runs of 12 and 39 children once every child was *past* the open. Blocked inside an
    activity it is `ActivityError(cause=CancelledError)`, which the clause did not name.

    **And `notify_session_best_effort` caught exactly that pair and carried on**, which is worse
    than losing a settle: the child went back to waiting on its seven-day timer and was still
    `RUNNING` 30 s after its parent was terminated, with a live, answerable question about work that
    no longer exists. That is the second arm, and it asserts the status as well as the settle,
    because "cancelled but unsettled" and "never cancelled at all" are different failures.

    The two arms share one environment and one worker, for the reason the sibling gives: three
    environments paid the startup three times over. Real-time rather than time-skipping, because the
    subject is a wall-clock broker event on runs that must still be `RUNNING` when it arrives.
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
    """The policy above is only worth measuring if the call sites actually carry it.

    A `parent_close_policy` is a *start option*, so the wait cannot set its own: it is chosen by
    whoever starts it, and omitting it silently selects the one policy that strands the row. There
    is nothing at the wait's end that can notice, which is what makes this worth a scan.

    Matched on the two names appearing in one file rather than on the shape of the call, because a
    substring spanning a line break is a guard that goes quiet the first time somebody reformats
    the module — passing while asserting nothing, which is the failure mode this whole review kept
    finding. The floor below is the other half of that: an empty scan is a subset of everything.

    **Scoped to the whole package, because scoping it to `durable/` is what let the longest wait in
    the tree ship without the option.** The first version of this scan walked `durable/` alone and
    said so in a paragraph that then named the caller it was not reading —
    `connectors/bo/workflows.py::_measure`, whose wait is a fortnight long, so a parent that dies
    there strands the longest-lived row of any of them. A rule that names its own exception in prose
    is not a rule, and a bundle is exactly where the next caller will be written: the wait is one
    primitive with several callers by design, and where a caller lives is not a property this
    guard should care about.
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
    """The same question again is an ordinary act, and it used to fail the workflow.

    `D-2026-09-13-an-answer-is-archived-so-the-question-can-be-asked-again`. `request_id_for` keys
    on `(kind, subject, asked_of)` alone and `request_external_input` sets
    `WorkflowIDReusePolicy.ALLOW_DUPLICATE`, so re-asking a standing question — the monthly
    stability pull, the next campaign round's measurement, a re-launched approval — mints the same
    id on purpose. Meeting an `answered` row, `pending_store._OPEN` wrote nothing and this activity
    raised a **non-retryable** `ApplicationError`: the ask failed, and with it the workflow that
    made it (`ConnectorJobWorkflow._approve_effect` turns a failed approval into a refused job).

    The answer is archived now, so the reopen is allowed and the activity returns the deadline it
    was asked for. **Driven through the activity rather than the store**, because the store's own
    test covers the five shapes of the upsert and what this adds is that nothing between the two
    still refuses: the raise was here, not there.

    What the old test asserted — that the previous cycle's attribution survives — is asserted here
    too, in its new place.
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
