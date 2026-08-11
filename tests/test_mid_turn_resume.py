"""A durable job's result can reach the same turn that launched it (gap AGT-2).

The system's defining interaction — "compute this, then reason about the result" — was split in
two: a tool returned a job id, the turn ended, and the result arrived as push-back the session only
picked up on its *next* turn. Both halves of the machinery existed (the F3-T3 mailbox, the D-058
todo flip); the missing piece was a bounded wait a live turn could perform.

The properties that matter are the failure modes, not the happy path: the wait is opt-in, bounded,
non-recursive, and degrades to the *previous* behavior (result on the next turn) rather than to an
error — a database blip must not cost a chemist an answer the model already produced.
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

    async def stream(self, message: str) -> AsyncIterator[Piece]:  # noqa: D102 - see the base class
        self.messages.append(message)
        if len(self.messages) == 1:
            record_job_started(self._job_id, "qm")
            yield "starting"
        else:
            yield " the energy is -154.1"


def _events(agent: ScriptedTurn) -> list[Any]:
    """One turn's events, driven on whichever engine is configured.

    `connectors=[]` is stated for the reason it is stated in `tests/test_turn_signals.py` and one
    more: on the graph engine the runner hands this list to the graph builder, and the default is
    the other engine's connector representation.
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

        async def stream(  # noqa: D102 - see `ScriptedTurn`
            self, message: str
        ) -> AsyncIterator[Piece]:
            yield "an answer"

    _events(_PlainAgent())
    assert called == []


def test_the_resume_continues_the_same_graph_with_the_job_results(
    monkeypatch: pytest.MonkeyPatch, enabled: None
) -> None:
    """A turn that launched a job answers once, from both halves — the whole of AGT-2.

    The continuation is a second `graph_events` over the *same* graph and the same `thread_id`,
    which is why the assertion is two model calls and one answer carrying text from each: a
    continuation that started a fresh graph would answer without having seen the first half, and a
    continuation that never ran would answer without the number.

    It was written as `test_the_graph_resume_never_reaches_for_the_turns_agent`, pinning that
    `run_turn` did not call `.run` on the `None` the front door passed in the agent slot — a real
    crash under `CHEMCLAW_MID_TURN_RESUME_ENABLED=true`, covered by nothing, on an
    operator-settable knob. That slot no longer exists, so the defect has no surface and only the
    behaviour it protected is left to pin.
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

        async def stream(  # noqa: D102 - see `ScriptedTurn`
            self, message: str
        ) -> AsyncIterator[Piece]:
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
    """The defect: waiting on job A used to consume — and discard — job B's push-back.

    `await_job_results` tailed `session_events`, and that claim is *destructive*: it consumed every
    unconsumed `job_completed` row for the session, kept only the ids it wanted, and dropped the
    rest. The front door's `/sessions/{id}/events` stream, the consumer those rows belong to, never
    saw them. The old docstring argued the front door "would already have claimed" them — a race,
    not a guarantee, since both consumers poll the same rows.

    Now the wait asks Temporal about the specific job ids and never touches the mailbox, so B's row
    is still there afterwards. Temporal is unreachable in this sandbox, which is *fine and is the
    point*: the wait degrades to "no result yet" while the mailbox stays intact. On the old code it
    consumed B before failing, and this fails.
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
    """The degradation this module says it counts must actually be counted, per job.

    `_collect` is gathered with `return_exceptions=True`, so nothing ever raised *out of* the
    gather; the only exception `wait_for` can produce is `TimeoutError`, which the preceding clause
    already handled. The `except Exception -> degraded(logger, "job_resume", ...)` block was
    therefore unreachable, and measurement confirmed it: with the broker down, `collected` was `{}`,
    no `job_resume` series was created, and the only trace was the INFO line reading "no result
    yet" — which asserts the job is still pending when it could not be reached at all. That is the
    exact sentence this module's own comment condemns one clause up, left live for the broker while
    it was fixed for a failed workflow.

    The second shape is the one the fix also has to cover: a workflow that *completes* and returns
    something `completed_job_status` cannot decode raises `ValueError`, which is not a
    `WorkflowFailureError`, so it too vanished into the gather with no count and no log.

    Counted per job rather than once for the batch, because the label answers "how much of this
    turn was lost", and two unreachable jobs are twice the loss of one.
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
    """A failed job must arrive with the sentence written for a chemist, not the wrapper's.

    Two defects, one line apart. `gather(return_exceptions=True)`'s result was never bound, so a
    failed job was simply absent and the runner skipped the resume — leaving the model to finish the
    turn on its pre-wait text, narrating a success that did not happen. And the first fix passed the
    client-side `WorkflowFailureError` straight to `failure_reason`, which stops at the first frame
    and yields "Workflow execution failed" — discarding "unknown ALPB solvent '2-MeTHF'; valid names
    are …", the diagnostic that tells the chemist what to change. `connectors/jobs.py` documents
    exactly that unwrapping in a comment.
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
