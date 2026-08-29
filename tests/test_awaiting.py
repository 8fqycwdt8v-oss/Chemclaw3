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
from pathlib import Path

from temporalio import workflow
from temporalio.client import Client

with workflow.unsafe.imports_passed_through():
    from temporalio.worker import Worker

    from chemclaw.core.config import settings
    from chemclaw.durable.awaiting import (
        AwaitAnswerWorkflow,
        AwaitOutcome,
        AwaitRequest,
        request_id_for,
    )
    from tests.temporal_env import pydantic_client, start_env_or_skip

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


def _worker(client: Client, projection: _Projection) -> Worker:
    """A worker serving the wait, with the projection activities replaced by recorders."""

    async def open_activity(payload: object) -> None:
        projection.opened.append(_field(payload, "request_id"))

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
