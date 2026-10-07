"""Started jobs and plans reach the turn's event stream.

The runner sees only the model's streamed updates, so tools hand these facts over out of band
through `chemclaw.core.turn_signals`, a task-local contextvar. These tests drive the real runner
with a fake agent whose tool records a signal, and assert the events come out in order.
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
    record_note_written,
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
            record_note_written(note_id, reference)
        yield " done"


def _events(agent: ScriptedTurn) -> list[Any]:
    """Collect one turn's events, with no connectors and without the capability announcement.

    `connectors=[]` is explicit because the default dials every enabled connector, none of which run
    here. `capability_degraded` is dropped because no Temporal broker runs either; that announcement
    has its own tests.
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


def test_a_recorded_note_becomes_a_note_recorded_event() -> None:
    """A chemist learns their contribution opened a branch, in the session that produced it."""
    events = _events(_SignallingAgent(jobs=[], proposals=[("playbook-1", "note/playbook-1")]))
    proposed = [e for e in events if e.type == "note_recorded"]
    assert len(proposed) == 1
    assert (proposed[0].note_id, proposed[0].reference) == ("playbook-1", "note/playbook-1")


def test_signals_are_ordered_between_the_tokens_around_them() -> None:
    """A signal surfaces where it happened, not batched at the end.

    The exact interleaving with tokens depends on `astream`'s buffering of a fake model, so the
    assertion is the invariant: signals come out in recording order, and a token still follows the
    last one.
    """
    events = _events(
        _SignallingAgent(jobs=[("report-1", "report")], proposals=[("r-1", "note/r-1")])
    )
    kinds = [e.type for e in events]
    assert kinds.index("job_started") < kinds.index("note_recorded"), kinds
    assert kinds[kinds.index("note_recorded") + 1] == "token", kinds
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
    """Recording outside a graph is a no-op rather than an error.

    `get_stream_writer()` raises `RuntimeError` outside a runnable context, and a Temporal activity
    or the CLI calls these same tools with no graph; narrating must not fail a durable job.
    """
    record_job_started("qm-1", "qm")
    record_note_written("n-1", "note/n-1")


def test_recording_from_a_governed_call_outside_a_graph_is_a_no_op() -> None:
    """Recording from a governed call outside a graph is a no-op.

    Inside `invoke_governed` there is a runnable context but no graph runtime, so
    `get_stream_writer()` raises `KeyError: '__pregel_runtime'` instead of `RuntimeError`.
    """
    from langchain_core.tools import StructuredTool

    async def _body() -> str:
        record_job_started("qm-3", "qm")
        return "launched"

    tool = StructuredTool.from_function(coroutine=_body, name="launch", description="launch a job")
    assert asyncio.run(tool.ainvoke({})) == "launched"


def test_a_signal_reaches_the_stream_from_inside_a_tool() -> None:
    """Where a writer does exist, the publish actually lands.

    Asserted against a real graph, since the guard above swallows errors and only a proven success
    path distinguishes it from swallowing everything.
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
    """Nothing in the tree can open a durable approval hold.

    A hold with consumers but no producer looks like a human sign-off that cannot happen. Asserted
    as an absence: nothing under `src/` names the workflow, the starter or the turn signal, so
    whoever re-adds it must ship producer and surface together.
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
