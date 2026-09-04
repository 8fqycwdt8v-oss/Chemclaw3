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

    async def remind_activity(request_id: str) -> None:
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
        request: Any = payload["request"] if isinstance(payload, dict) else payload.request  # type: ignore[index,union-attr]
        days = request["deadline_days"] if isinstance(request, dict) else request.deadline_days
        started = datetime.fromisoformat(_field(payload, "started_at"))
        return (started + timedelta(days=float(days))).isoformat()

    async def _settle(payload: object) -> bool:
        """Registered because the wait calls it, and not recorded because it is not asserted."""
        return True

    async def _remind(request_id: str) -> None: ...

    async def _notify(payload: object) -> None: ...

    policies = (
        ParentClosePolicy.TERMINATE,
        ParentClosePolicy.ABANDON,
        ParentClosePolicy.REQUEST_CANCEL,
    )

    async def _run() -> dict[str, tuple[str, list[str]]]:
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
                return {
                    policy.name: (await child.describe()).status.name
                    for policy, child in children.items()
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


def test_every_wait_started_as_a_child_names_a_parent_close_policy() -> None:
    """The policy above is only worth measuring if the call sites actually carry it.

    A `parent_close_policy` is a *start option*, so the wait cannot set its own: it is chosen by
    whoever starts it, and omitting it silently selects the one policy that strands the row. There
    is nothing at the wait's end that can notice, which is what makes this worth a scan.

    Matched on the two names appearing in one file rather than on the shape of the call, because a
    substring spanning a line break is a guard that goes quiet the first time somebody reformats
    the module — passing while asserting nothing, which is the failure mode this whole review kept
    finding. The floor below is the other half of that: an empty scan is a subset of everything.

    Scoped to this package, which is where the wait's callers in `durable/` live.
    `connectors/bo/workflows.py::_measure` starts the same child from the bundle side and needs the
    identical option; its wait is a fortnight long, so a parent that dies there strands the
    longest-lived row of any of them.
    """
    starters = [
        path
        for path in (SRC / "durable").rglob("*.py")
        if "execute_child_workflow" in (text := path.read_text(encoding="utf-8"))
        and "AwaitAnswerWorkflow.run" in text
    ]
    assert starters, (
        "no module in `durable/` starts the wait as a child any more — either the caller moved, in "
        "which case this scan belongs where it went, or this guard is now asserting nothing"
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


def test_a_wait_refused_by_the_projection_fails_instead_of_waiting_blind() -> None:
    """A wait whose projection belongs to somebody else's answer must not open at all.

    `_OPEN` refuses one case deliberately: a re-ask of an already-**answered** question, because
    reopening would blank the attribution `retention._NOT_PRUNED` keeps this table for. The refusal
    is right and its silence was not. `request_id_for` keys on (kind, subject, asked_of) alone and
    `request_external_input` sets `WorkflowIDReusePolicy.ALLOW_DUPLICATE`, so re-asking the same
    standing question is an ordinary act that mints the same id — and the workflow, told nothing,
    went on to wait against a row reading `answered`: absent from `open_requests`, refused 409 by
    the answer route, and unable to settle itself at the end, for the ninety days
    `awaiting_max_days` allows.

    Non-retryable, because no number of attempts changes whose answer is in that row. Failing here
    is what makes the conflict reach somebody — `ConnectorJobWorkflow._approve_effect` turns a
    failed approval into a refused job, which is the correct reading of "this could not be asked".
    """
    from temporalio.exceptions import ApplicationError

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

        await open_pending_request_activity(_input("run-1"))
        await pending_store.settle_request(
            request_id, state="answered", answered_by="u-2", answer={"reading": 4}
        )

        # The same question again, as a new run. The projection cannot become this run's.
        try:
            await open_pending_request_activity(_input("run-2"))
        except ApplicationError as exc:
            assert exc.non_retryable, "retrying cannot change whose answer is in the row"
            assert request_id in str(exc)
        else:
            raise AssertionError(
                "the wait opened against a projection that still reads `answered`, so it is "
                "invisible in every inbox and unanswerable for its whole deadline"
            )

        # And the previous cycle's answer is exactly where it was.
        stored = await pending_store.get_request(request_id)
        assert stored is not None
        assert (stored.state, stored.answered_by) == ("answered", "u-2")

    asyncio.run(_run())
