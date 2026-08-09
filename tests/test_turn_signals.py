"""Started jobs and PR-gate proposals reach the turn's event stream (gaps RCH-4, RCH-5).

`JobStartedEvent` and `PlanEvent` had been in the typed contract and rendered by the chat UI since
F2 — and were never emitted by anything. `plan_only` autonomy (the Helm chart's production default)
therefore asked a human to approve a plan the surface could not show, and a chemist whose turn
opened a knowledge PR was never told: the reference went into the model's context and the GxP
"human signs off" line existed only in a git host's UI.

The runner only sees the model's streamed updates, so tools hand these facts over out of band
through `chemclaw.core.turn_signals` (a contextvar, task-local like the ambient session/identity).
These
tests drive the real runner with a fake agent whose "tool" records a signal, and assert the events
come out interleaved in order.
"""

import asyncio
from typing import Any

import pytest
from agent_framework import AgentSession

from chemclaw.api.runner import run_turn
from chemclaw.core.config import settings
from chemclaw.core.turn_signals import (
    begin_turn,
    drain,
    end_turn,
    record_job_started,
    record_proposal,
)
from tests.fakes import FakeUpdate


class _SignallingAgent:
    """An agent whose streamed turn records signals partway through, as a real tool would."""

    mcp_tools: list[Any] = []

    def __init__(self, *, jobs: list[tuple[str, str]], proposals: list[tuple[str, str]]) -> None:
        self._jobs = jobs
        self._proposals = proposals

    def run(  # noqa: D102 - a fake agent's run, documented by its class
        self,
        message: str,
        *,
        stream: bool,
        session: AgentSession,
        **_run_options: Any,
    ) -> Any:
        async def _gen() -> Any:
            yield FakeUpdate(text="thinking")
            for job_id, kind in self._jobs:
                record_job_started(job_id, kind)
            for note_id, reference in self._proposals:
                record_proposal(note_id, reference)
            yield FakeUpdate(text=" done")

        return _gen()


def _events(agent: Any) -> list[Any]:
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
            async for event in run_turn(agent, AgentSession(session_id="s1"), "hi", connectors=[])
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
    """A signal surfaces where it happened, not batched at the end, so the transcript reads true."""
    events = _events(
        _SignallingAgent(jobs=[("report-1", "report")], proposals=[("r-1", "note/r-1")])
    )
    kinds = [e.type for e in events]
    # The tool ran after the first chunk of text and before the next, so that is where it appears.
    assert kinds == ["token", "job_started", "note_proposed", "token", "answer"]


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
                async for e in run_turn(agent, AgentSession(session_id=job_id), "hi", connectors=[])
            ]

        return await asyncio.gather(_one("job-a"), _one("job-b"))

    left, right = asyncio.run(_two_turns())
    assert [e.job_id for e in left if e.type == "job_started"] == ["job-a"]
    assert [e.job_id for e in right if e.type == "job_started"] == ["job-b"]


def test_recording_off_the_request_path_is_a_no_op() -> None:
    """The CLI and tests call the same tools with no turn in flight; that must not blow up."""
    record_job_started("qm-1", "qm")  # no begin_turn() — no buffer bound
    record_proposal("n-1", "note/n-1")
    assert drain() == []


def test_drain_clears_so_a_signal_is_emitted_once() -> None:
    """A drained signal must not resurface on the next update as a duplicate event."""
    token = begin_turn()
    try:
        record_job_started("qm-1", "qm")
        assert len(drain()) == 1
        assert drain() == []
    finally:
        end_turn(token)


def test_plan_is_absent_when_the_harness_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The classic agent has no todo list, so no PlanEvent is manufactured for it."""
    monkeypatch.setattr(settings, "harness_enabled", False)
    events = _events(_SignallingAgent(jobs=[], proposals=[]))
    assert not [e for e in events if e.type == "plan"]


def test_plan_is_emitted_from_the_harness_todo_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the harness on, the plan the loop is working is what the surface shows (RCH-5).

    Read from the harness's own `TodoProvider` store, not a parallel copy — a second representation
    would drift the moment the model revised its todos mid-turn.
    """
    monkeypatch.setattr(settings, "harness_enabled", True)

    plans = [["[ ] gather evidence"], ["[ ] gather evidence", "[x] compute pKa"]]
    calls = {"n": 0}

    async def _fake_titles(session: Any) -> list[str]:
        calls["n"] += 1
        return plans[min(calls["n"] - 1, len(plans) - 1)]

    monkeypatch.setattr("chemclaw.api.runner.todo_titles", _fake_titles)
    events = _events(_SignallingAgent(jobs=[], proposals=[]))
    emitted = [e.todos for e in events if e.type == "plan"]
    # Emitted when it first appears and again when it changes — never once per update.
    assert emitted == plans


def test_an_unchanged_plan_is_not_re_emitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A static plan streams once, not on every update (which would spam the transcript)."""
    monkeypatch.setattr(settings, "harness_enabled", True)

    async def _fake_titles(session: Any) -> list[str]:
        return ["[ ] one step"]

    monkeypatch.setattr("chemclaw.api.runner.todo_titles", _fake_titles)
    events = _events(_SignallingAgent(jobs=[], proposals=[]))
    assert len([e for e in events if e.type == "plan"]) == 1


def test_a_failing_plan_read_never_sinks_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plan is a display concern; failing to read it must not lose the chemist's answer."""
    monkeypatch.setattr(settings, "harness_enabled", True)

    async def _boom(session: Any) -> list[str]:
        raise RuntimeError("todo store unavailable")

    monkeypatch.setattr("chemclaw.api.runner.todo_titles", _boom)
    events = _events(_SignallingAgent(jobs=[], proposals=[]))
    assert events[-1].type == "answer"
    assert not [e for e in events if e.type == "error"]


def test_an_approval_signal_carries_the_holds_handle_to_the_stream() -> None:
    """An opened approval hold reaches the surface WITH the id that answers it (gap RCH-3).

    `ApprovalRequestEvent.approval_id` has always documented itself as the handle a surface posts
    to `POST /approvals/{id}/decision`, but nothing populated it: `start_approval` returns the id
    into the model's context, and the runner sees only the model's streamed updates. So every
    approval arrived renderable but unanswerable — and `service/static/app.js` returns early on an
    empty handle, so the Yes/No control never rendered at all.
    """
    from chemclaw.api.runner import _signal_event
    from chemclaw.core.turn_signals import record_approval_request

    token = begin_turn()
    try:
        record_approval_request("Save this to the knowledge graph? What is the pKa?", "approval-7")
        signals = drain()
    finally:
        end_turn(token)

    assert len(signals) == 1
    event = _signal_event(signals[0])
    assert event.type == "approval_request"
    assert event.approval_id == "approval-7"
    assert "pKa" in event.prompt


def test_a_plan_approval_still_has_no_handle() -> None:
    """The other approval kind — a plan prompt — has no durable hold and must stay handle-less.

    It is answered by the next turn, not by a decision endpoint, so an id there would point a
    surface at a hold that does not exist. The emptiness is load-bearing, not incidental.
    """
    from chemclaw.api.events import ApprovalRequestEvent

    assert ApprovalRequestEvent(prompt="Approve the plan?").approval_id == ""
