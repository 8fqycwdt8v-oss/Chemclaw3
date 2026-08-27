"""Started jobs and PR-gate proposals reach the turn's event stream (gaps RCH-4, RCH-5).

`JobStartedEvent` and `PlanEvent` had been in the typed contract and rendered by the chat UI since
F2 — and were never emitted by anything. `plan_only` autonomy (the Helm chart's production default)
therefore asked a human to approve a plan the surface could not show, and a chemist whose turn
opened a knowledge PR was never told: the reference went into the model's context and the review
"human signs off" line existed only in a git host's UI.

The runner only sees the model's streamed updates, so tools hand these facts over out of band
through `chemclaw.core.turn_signals` (a contextvar, task-local like the ambient session/identity).
These
tests drive the real runner with a fake agent whose "tool" records a signal, and assert the events
come out interleaved in order.
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from chemclaw.agent.session import TurnSession
from chemclaw.api.runner import run_turn
from chemclaw.core.config import settings
from chemclaw.core.turn_signals import (
    JobSignal,
    record_job_started,
    record_proposal,
)
from tests.fakes_turn import Piece, ScriptedTurn
from tests.signals import collect_signals


class _SignallingAgent(ScriptedTurn):
    """An agent whose streamed turn records signals partway through, as a real tool would."""

    def __init__(self, *, jobs: list[tuple[str, str]], proposals: list[tuple[str, str]]) -> None:
        self._jobs = jobs
        self._proposals = proposals

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        yield "thinking"
        for job_id, kind in self._jobs:
            record_job_started(job_id, kind)
        for note_id, reference in self._proposals:
            record_proposal(note_id, reference)
        yield " done"


def _events(agent: ScriptedTurn) -> list[Any]:
    """Collect one turn's events, with no connectors and without the capability announcement.

    `connectors=[]` is stated rather than defaulted: omitting it means *every enabled connector*,
    which in a test process is six hosts that are not running — so the turn genuinely degrades and
    now says so (D-139). These tests are about signal ordering, and a turn that dials six dead hosts
    to assert an event list was asserting more than it meant to.

    `capability_degraded` is dropped for exactly the same reason and one more: no Temporal broker
    runs in a test process either, so every turn here truthfully opens by announcing the durable
    subsystem is down. That announcement has its own tests; keeping it in this list would mean
    every signal-ordering assertion doubled as an assertion about an unrelated outage.
    """

    async def _collect() -> list[Any]:
        return [
            event
            async for event in run_turn(
                TurnSession(session_id="s1"),
                "hi",
                connectors=[],
                graph_factory=agent.graph_factory,
            )
            if event.type != "capability_degraded"
        ]

    return asyncio.run(_collect())


def test_a_started_job_becomes_a_job_started_event() -> None:
    """The event the UI has always rendered is finally produced."""
    events = _events(_SignallingAgent(jobs=[("qm-abc", "qm")], proposals=[]))
    started = [e for e in events if e.type == "job_started"]
    assert len(started) == 1
    assert (started[0].job_id, started[0].kind) == ("qm-abc", "qm")


def test_a_proposed_note_becomes_a_note_proposed_event() -> None:
    """A chemist learns their contribution opened a branch, in the session that produced it."""
    events = _events(_SignallingAgent(jobs=[], proposals=[("playbook-1", "note/playbook-1")]))
    proposed = [e for e in events if e.type == "note_proposed"]
    assert len(proposed) == 1
    assert (proposed[0].note_id, proposed[0].reference) == ("playbook-1", "note/playbook-1")


def test_signals_are_ordered_between_the_tokens_around_them() -> None:
    """A signal surfaces where it happened, not batched at the end, so the transcript reads true.

    **The assertion is the invariant, not the transcript, and that is a measurement rather than a
    concession.** Under MAF the runner consumes the model's generator directly, so the sequence is
    exactly `token, job_started, note_proposed, token, answer`. Under LangGraph the tokens travel
    through `astream`'s queue, and a fake model that never suspends between chunks fills that queue
    before the consumer is scheduled once: measured, the consumer needs four event-loop hops inside
    the model's reply to dequeue the first chunk, so the same turn reads `job_started,
    note_proposed, token, token, answer`. That difference is a property of the stream's buffering —
    a real provider's chunks are separated by a network read — and not of the drain-first rule both
    engines implement, so pinning the exact list would pin the fake.

    What both engines must agree on, and what is asserted: the signals come out in the order they
    were recorded, and they are *not* batched at the end — a token still follows the last one.
    """
    events = _events(
        _SignallingAgent(jobs=[("report-1", "report")], proposals=[("r-1", "note/r-1")])
    )
    kinds = [e.type for e in events]
    assert kinds.index("job_started") < kinds.index("note_proposed"), kinds
    assert kinds[kinds.index("note_proposed") + 1] == "token", kinds
    assert kinds[-1] == "answer", kinds


def test_no_signals_means_no_extra_events() -> None:
    """A turn that starts nothing and proposes nothing streams exactly as before."""
    events = _events(_SignallingAgent(jobs=[], proposals=[]))
    assert {e.type for e in events} == {"token", "answer"}


def test_signals_are_isolated_per_turn() -> None:
    """A contextvar buffer, so two concurrent turns can never see each other's signals."""

    async def _two_turns() -> tuple[list[Any], list[Any]]:
        async def _one(job_id: str) -> list[Any]:
            agent = _SignallingAgent(jobs=[(job_id, "qm")], proposals=[])
            return [
                e
                async for e in run_turn(
                    TurnSession(session_id=job_id),
                    "hi",
                    connectors=[],
                    graph_factory=agent.graph_factory,
                )
            ]

        return await asyncio.gather(_one("job-a"), _one("job-b"))

    left, right = asyncio.run(_two_turns())
    assert [e.job_id for e in left if e.type == "job_started"] == ["job-a"]
    assert [e.job_id for e in right if e.type == "job_started"] == ["job-b"]


def test_recording_outside_a_graph_is_a_no_op_rather_than_an_error() -> None:
    """The same tools run where nothing is streaming; that must not blow up.

    This is the case the port made sharp. `get_stream_writer()` does not return `None` outside a
    runnable context — it raises `RuntimeError: Called get_config outside of a runnable context`
    (measured). A Temporal activity replaying a template step calls these same tools with no graph
    anywhere, so an unguarded publish would fail a durable job because it tried to *narrate*. The
    CLI and most tests are in the same position.
    """
    record_job_started("qm-1", "qm")
    record_proposal("n-1", "note/n-1")


def test_recording_from_a_governed_call_outside_a_graph_is_a_no_op() -> None:
    """The path the guard actually exists for, which the first version of it did not cover.

    `agent/tool_invocation.invoke_governed` runs a tool through the middleware chain in a Temporal
    activity — a runnable context with no graph. `get_stream_writer()` raises a *different*
    exception there than it does off any runnable context at all: `KeyError: '__pregel_runtime'`
    rather than `RuntimeError`, because the config exists and the runtime key in it does not.
    Catching only the second left a durable template step failing because a tool tried to narrate,
    and the unit test above passed throughout — it drives a bare call, which raises the other one.
    """
    from langchain_core.tools import StructuredTool

    async def _body() -> str:
        record_job_started("qm-3", "qm")
        return "launched"

    tool = StructuredTool.from_function(coroutine=_body, name="launch", description="launch a job")
    assert asyncio.run(tool.ainvoke({})) == "launched"


def test_a_signal_reaches_the_stream_from_inside_a_tool() -> None:
    """The other half: where a writer *does* exist, the publish actually lands.

    Asserted against a real graph rather than a patched writer, because the guard above swallows
    `RuntimeError` — and a guard that swallows everything is indistinguishable from one that
    swallows nothing unless something proves the success path too.
    """

    async def _record() -> str:
        record_job_started("qm-2", "qm")
        return "returned to the model"

    returned, signals = asyncio.run(collect_signals(_record))
    assert returned == "returned to the model"
    assert signals == [JobSignal(job_id="qm-2", kind="qm")]


def test_plan_is_absent_when_the_harness_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The classic agent has no todo list, so no PlanEvent is manufactured for it."""
    monkeypatch.setattr(settings, "harness_enabled", False)
    events = _events(_SignallingAgent(jobs=[], proposals=[]))
    assert not [e for e in events if e.type == "plan"]


def test_nothing_in_the_tree_can_open_a_durable_approval_hold() -> None:
    """An absence pinned, so re-adding the claim without a producer turns this red.

    D-032 built an asynchronous "Save this knowledge? [Yes]/[No]" hold and shipped every consumer
    of it — three HTTP routes, a Temporal workflow, an owner-scoped dependency and an
    `approval_request` stream event — while its only producer, `start_approval`, was called by
    nothing in `src/`. So `GET /approvals` could only ever return `[]`, and an owner-scoped
    decision route *looked* like a human sign-off that existed. That is the shape
    `D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution` deleted `record_handoff`
    for, and `D-2026-08-27-a-hold-nothing-can-open-is-not-a-hold` deletes this one for.

    Asserted as an absence rather than as plumbing: nothing under `src/` names the workflow, the
    starter or the turn signal. Whoever re-adds the hold fails this test, which is the point — the
    producer and the surface have to arrive in the same change. The PR-gate the synchronous
    `record_confirmed_answer` opens is untouched and is where the human decision is actually taken.
    """
    src = Path(__file__).resolve().parents[1] / "src" / "chemclaw"
    banned = ("InteractionApprovalWorkflow", "start_approval", "record_approval_request")
    offenders = sorted(
        f"{path.relative_to(src).as_posix()}: {name}"
        for path in src.rglob("*.py")
        for name in banned
        if name in path.read_text(encoding="utf-8")
    )
    assert offenders == [], (
        f"{offenders} re-introduces the D-032 approval hold. It was deleted because nothing could "
        "start one; re-adding any part of it needs a producer, the stream event back in the "
        "`Event` union, `tests/fixtures/turn_events_contract.json` regenerated, and a new ADR."
    )
