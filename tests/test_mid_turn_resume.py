"""A durable job's result can reach the same turn that launched it.

"Compute this, then reason about the result" needs a bounded wait a live turn can perform. The
properties that matter are the failure modes: the wait is opt-in, bounded and non-recursive, and
degrades to the result arriving on the next turn rather than to an error.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any
from unittest import mock

import pytest

from chemclaw.agent.job_results import await_job_results
from chemclaw.agent.session import TurnSession
from chemclaw.agent.session_events import claim_unconsumed, record_session_event
from chemclaw.api.runner import run_turn
from chemclaw.core.config import settings
from chemclaw.core.turn_signals import record_job_started
from tests.fakes_turn import Piece, ScriptedTurn
from tests.pg import migrated_db_or_skip


class _JobLaunchingAgent(ScriptedTurn):
    """Streams a first pass that launches a job, then a continuation pass."""

    def __init__(self, job_id: str) -> None:
        self._job_id = job_id
        self.messages: list[str] = []

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        self.messages.append(message)
        if len(self.messages) == 1:
            record_job_started(self._job_id, "qm")
            yield "starting"
        else:
            yield " the energy is -154.1"


def _events(agent: ScriptedTurn) -> list[Any]:
    """One turn's events through the runner.

    `connectors=[]` is stated explicitly, as in `tests/test_turn_signals.py`, so the runner does not
    build the deployment's connectors.
    """

    async def _collect() -> list[Any]:
        return [
            e
            async for e in run_turn(
                TurnSession(session_id="s1"),
                "compute it",
                connectors=[],
                graph_factory=agent.graph_factory,
            )
        ]

    return asyncio.run(_collect())


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn the (default-off) resume on for the tests that exercise it."""
    monkeypatch.setattr(settings, "mid_turn_resume_enabled", True)
    monkeypatch.setattr(settings, "mid_turn_resume_timeout_seconds", 5.0)


def test_the_result_reaches_the_same_turn(monkeypatch: pytest.MonkeyPatch, enabled: None) -> None:
    """One exchange, not two — the whole point of the gap."""

    async def _fake_wait(session_id: str, job_ids: list[str], *, timeout_seconds: float) -> Any:
        return {job_ids[0]: {"energy_hartree": -154.1}}

    monkeypatch.setattr("chemclaw.api.runner.await_job_results", _fake_wait)
    agent = _JobLaunchingAgent("qm-1")
    events = _events(agent)

    assert len(agent.messages) == 2, "the turn did not continue after the job completed"
    answer = next(e for e in events if e.type == "answer")
    assert "starting" in answer.text and "-154.1" in answer.text


def test_the_result_is_handed_to_the_model_as_framed_data(
    monkeypatch: pytest.MonkeyPatch, enabled: None
) -> None:
    """A workflow result is untrusted input, so the same framing as retrieved notes applies."""

    async def _fake_wait(session_id: str, job_ids: list[str], *, timeout_seconds: float) -> Any:
        return {"qm-1": {"energy_hartree": -154.1}}

    monkeypatch.setattr("chemclaw.api.runner.await_job_results", _fake_wait)
    agent = _JobLaunchingAgent("qm-1")
    _events(agent)
    continuation = agent.messages[1]
    assert "<retrieved-note" in continuation or "job-results" in continuation


def test_a_timeout_degrades_to_the_previous_behavior(
    monkeypatch: pytest.MonkeyPatch, enabled: None
) -> None:
    """No result in time is not an error: push-back still delivers it on the next turn."""

    async def _no_results(session_id: str, job_ids: list[str], *, timeout_seconds: float) -> Any:
        return {}

    monkeypatch.setattr("chemclaw.api.runner.await_job_results", _no_results)
    agent = _JobLaunchingAgent("qm-1")
    events = _events(agent)
    assert len(agent.messages) == 1  # no continuation
    assert events[-1].type == "answer"
    assert not [e for e in events if e.type == "error"]


def test_the_wait_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Holding a turn open holds an admission permit, so a deployment opts in deliberately."""
    assert settings.mid_turn_resume_enabled is False
    called = []

    async def _spy(session_id: str, job_ids: list[str], *, timeout_seconds: float) -> Any:
        called.append(job_ids)
        return {}

    monkeypatch.setattr("chemclaw.api.runner.await_job_results", _spy)
    agent = _JobLaunchingAgent("qm-1")
    _events(agent)
    assert called == [], "the resume ran without being enabled"
    assert len(agent.messages) == 1


def test_a_turn_that_starts_no_job_never_waits(
    monkeypatch: pytest.MonkeyPatch, enabled: None
) -> None:
    """An ordinary question must not pay a wait for jobs it never launched."""
    called = []

    async def _spy(session_id: str, job_ids: list[str], *, timeout_seconds: float) -> Any:
        called.append(job_ids)
        return {}

    monkeypatch.setattr("chemclaw.api.runner.await_job_results", _spy)

    class _PlainAgent(ScriptedTurn):
        """A turn that answers without launching anything."""

        async def stream(self, message: str) -> AsyncIterator[Piece]:
            yield "an answer"

    _events(_PlainAgent())
    assert called == []


def test_the_resume_continues_the_same_graph_with_the_job_results(
    monkeypatch: pytest.MonkeyPatch, enabled: None
) -> None:
    """A turn that launched a job answers once, from both halves.

    The continuation is a second `graph_events` over the same graph and `thread_id`, so the
    assertion is two model calls and one answer carrying text from each: a fresh graph would not
    have seen the first half, and no continuation would lack the number.
    """

    async def _fake_wait(session_id: str, job_ids: list[str], *, timeout_seconds: float) -> Any:
        return {job_ids[0]: {"energy_hartree": -154.1}}

    monkeypatch.setattr("chemclaw.api.runner.await_job_results", _fake_wait)
    agent = _JobLaunchingAgent("qm-1")

    async def _collect() -> list[Any]:
        return [
            event
            async for event in run_turn(
                TurnSession(session_id="s-graph-resume"),
                "compute it",
                connectors=[],
                graph_factory=agent.graph_factory,
            )
        ]

    events = asyncio.run(_collect())
    assert len(agent.messages) == 2, "the turn did not continue after the job completed"
    answer = next(e for e in events if e.type == "answer")
    assert "starting" in answer.text and "-154.1" in answer.text
    assert not [e for e in events if e.type == "error"], [e for e in events if e.type == "error"]


def test_the_resume_is_not_recursive(monkeypatch: pytest.MonkeyPatch, enabled: None) -> None:
    """A continuation that starts another job must not chain another wait.

    Otherwise one chemist turn could hold an admission permit indefinitely by launching a job from
    each continuation — the runaway the whole admission-control design exists to prevent.
    """
    waits = []

    async def _fake_wait(session_id: str, job_ids: list[str], *, timeout_seconds: float) -> Any:
        waits.append(job_ids)
        return {job_ids[0]: {"ok": True}}

    monkeypatch.setattr("chemclaw.api.runner.await_job_results", _fake_wait)

    class _AlwaysLaunching(ScriptedTurn):
        """A turn whose every pass launches another job."""

        def __init__(self) -> None:
            self.messages: list[str] = []

        async def stream(self, message: str) -> AsyncIterator[Piece]:
            self.messages.append(message)
            index = len(self.messages)
            record_job_started(f"qm-{index}", "qm")
            yield f"pass{index}"

    agent = _AlwaysLaunching()
    _events(agent)
    assert len(waits) == 1, "the resume waited more than once in a single turn"
    assert len(agent.messages) == 2


# --- REV-7: the wait must not consume the mailbox rows it is not waiting for ------------------


def test_the_wait_leaves_other_jobs_push_back_alone() -> None:
    """Waiting on job A leaves job B's push-back in the mailbox.

    Claiming `session_events` is destructive, so the wait asks Temporal about specific job ids and
    never touches the mailbox. Temporal is unreachable here, so the wait degrades to "no result yet"
    while B's row stays for the front door's events stream.
    """

    async def _run() -> list[str]:
        await migrated_db_or_skip()
        session_id = "rev7-bystander"
        # Start clean, then leave one push-back for a job this turn did not launch.
        await claim_unconsumed(session_id)
        await record_session_event(session_id, "job_completed", {"job_id": "job-b"})

        await await_job_results(session_id, ["job-a"], timeout_seconds=0.5)

        # Whatever is still unconsumed belongs to the stream that owns it.
        return [str(e.payload.get("job_id")) for e in await claim_unconsumed(session_id)]

    assert asyncio.run(_run()) == ["job-b"], (
        "the mid-turn wait consumed a push-back for a job it was not waiting on; the front door's "
        "event stream will never deliver it"
    )


class _UnreachableBroker:
    """A `connect()` that fails the way an outage does, before any handle exists."""

    async def __call__(self) -> object:
        from chemclaw.core.errors import SubsystemUnavailableError

        # One argument, as `core.temporal_client.connect` raises it: the message is the chemist-
        # facing sentence, so `%s` in the degradation renders it rather than an args tuple.
        raise SubsystemUnavailableError(
            "the durable execution backend (Temporal) is unreachable, so durable jobs cannot be "
            "started or inspected right now — nothing was queued by this call."
        )


class _UndecodableResult:
    """A workflow that completes normally but returns something that is not a connector envelope."""

    class _Handle:
        async def result(self) -> object:
            return {"not": "an envelope"}

    def get_workflow_handle(self, job_id: str) -> _Handle:
        return self._Handle()


@pytest.mark.parametrize(
    "connect_patch",
    [
        mock.patch("chemclaw.agent.job_results.connect", new=_UnreachableBroker()),
        mock.patch("chemclaw.agent.job_results.connect", return_value=_UndecodableResult()),
    ],
    ids=["broker-unreachable", "undecodable-result"],
)
def test_a_job_that_cannot_be_collected_is_counted_not_narrated_as_pending(
    connect_patch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A job that cannot be collected is counted as degraded, not narrated as pending.

    `_collect` is gathered with `return_exceptions=True`, so failures must be inspected per result:
    an unreachable broker, or a completed workflow whose result `completed_job_status` cannot decode
    (`ValueError`), each count once per job, since two lost jobs are twice the loss.
    """
    from chemclaw.core.metrics import METRICS

    before = METRICS.value("chemclaw_degraded_total")

    async def _run() -> dict[str, dict[str, Any]]:
        with connect_patch:
            return await await_job_results("s-1", ["job-a", "job-b"], timeout_seconds=5)

    with caplog.at_level(logging.ERROR, logger="chemclaw.agent.job_results"):
        collected = asyncio.run(_run())

    assert collected == {}, "nothing could be collected in either shape"
    assert METRICS.value("chemclaw_degraded_total") == before + 2, (
        "a job that could not be collected must leave a number behind, one per job"
    )
    assert 'chemclaw_degraded_total{subsystem="job_resume"}' in METRICS.render()
    named = [r.getMessage() for r in caplog.records]
    assert any("job-a" in m for m in named) and any("job-b" in m for m in named), (
        "the degradation must name the job the operator has to go look at"
    )


def test_a_failed_job_is_reported_with_the_products_own_reason() -> None:
    """A failed job is reported with the product's own reason, not the wrapper's.

    The gather result must be bound so a failed job is present, and the client-side
    `WorkflowFailureError` must be unwrapped to the cause written for a chemist (as
    `connectors/jobs.py` does), not "Workflow execution failed".
    """
    from temporalio.client import WorkflowFailureError
    from temporalio.exceptions import ActivityError, ApplicationError, ChildWorkflowError

    from chemclaw.agent.job_results import await_job_results

    reason = "unknown ALPB solvent '2-MeTHF'; valid names are water, thf, dmso"
    # The real chain a failed connector job produces:
    # WorkflowFailureError -> ChildWorkflowError -> ActivityError -> ApplicationError.
    activity = ActivityError(
        "activity failed",
        scheduled_event_id=1,
        started_event_id=2,
        identity="worker",
        activity_type="compute",
        activity_id="a1",
        retry_state=None,
    )
    activity.__cause__ = ApplicationError(reason)
    child = ChildWorkflowError(
        "child failed",
        namespace="ns",
        workflow_id="wf-1",
        run_id="run-1",
        workflow_type="ChildType",
        initiated_event_id=1,
        started_event_id=2,
        retry_state=None,
    )
    child.__cause__ = activity
    wrapper = WorkflowFailureError(cause=child)

    class _Handle:
        async def result(self) -> object:
            raise wrapper

    class _Client:
        def get_workflow_handle(self, job_id: str) -> _Handle:
            return _Handle()

    async def _run() -> dict[str, dict[str, object]]:
        with mock.patch("chemclaw.agent.job_results.connect", return_value=_Client()):
            return await await_job_results("s-1", ["job-bad"], timeout_seconds=5)

    collected = asyncio.run(_run())
    assert "job-bad" in collected, "a failed job was dropped rather than reported"
    assert collected["job-bad"]["status"] == "failed"
    assert collected["job-bad"]["summary"] == reason, (
        "the wrapper's generic sentence was reported instead of the product's own"
    )


def test_a_waiting_mailbox_reaches_the_models_next_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """A completion waiting in the mailbox reaches the model's next turn.

    Without an open browser the SSE stream delivers it to nobody, so the mailbox is read at turn
    start and appended to the chemist's message as framed data, chemist's words first.
    """
    from chemclaw.agent.session_events import SessionEvent
    from chemclaw.api import runner as runner_module

    class _Recorder(ScriptedTurn):
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def stream(self, message: str) -> AsyncIterator[Piece]:
            self.messages.append(message)
            yield "noted"

    claims: list[tuple[str, tuple[str, ...]]] = []

    async def _claim(session_id: str, *, kinds: Any = None, dsn: Any = None) -> list[SessionEvent]:
        claims.append((session_id, tuple(kinds or ())))
        return [
            SessionEvent(
                event_id=1,
                session_id=session_id,
                kind="job_completed",
                payload={"job_id": "calc-abc", "summary": "energy computed"},
            )
        ]

    async def _run() -> list[str]:
        await migrated_db_or_skip()
        monkeypatch.setattr(settings, "session_store", "postgres")
        monkeypatch.setattr(runner_module, "claim_unconsumed", _claim)
        agent = _Recorder()
        async for _event in run_turn(
            TurnSession(session_id="s-mailbox"),
            "and what came of it?",
            connectors=[],
            graph_factory=agent.graph_factory,
        ):
            pass
        return agent.messages

    messages = asyncio.run(_run())
    assert claims and claims[0][1] == ("job_completed", "job_failed"), (
        "the claim must be kind-scoped, or it destroys other consumers' events"
    )
    assert messages, "the model was never called"
    first = messages[0]
    assert first.startswith("and what came of it?"), "the chemist's words must lead"
    assert "calc-abc" in first and "job_failed" in first, (
        "the finished job never reached the model's input"
    )
