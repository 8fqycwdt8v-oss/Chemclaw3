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
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from agent_framework import AgentSession

from chemclaw.agent.job_results import await_job_results
from chemclaw.agent.session_events import claim_unconsumed, record_session_event
from chemclaw.api.runner import run_turn
from chemclaw.core.config import settings
from chemclaw.core.turn_signals import record_job_started
from tests.pg import migrated_db_or_skip


class _JobLaunchingAgent:
    """Streams a first pass that launches a job, then a continuation pass."""

    mcp_tools: list[Any] = []

    def __init__(self, job_id: str) -> None:
        self._job_id = job_id
        self.messages: list[str] = []

    def run(  # noqa: D102 - a fake agent's run, documented by its class
        self,
        message: str,
        *,
        stream: bool,
        session: AgentSession,
        **_run_options: Any,
    ) -> Any:
        self.messages.append(message)
        first = len(self.messages) == 1
        job_id = self._job_id

        async def _gen() -> Any:
            if first:
                record_job_started(job_id, "qm")
                yield SimpleNamespace(text="starting", contents=[], user_input_requests=[])
            else:
                yield SimpleNamespace(
                    text=" the energy is -154.1", contents=[], user_input_requests=[]
                )

        return _gen()


def _events(agent: Any) -> list[Any]:
    async def _collect() -> list[Any]:
        return [e async for e in run_turn(agent, AgentSession(session_id="s1"), "compute it")]

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

    class _PlainAgent:
        mcp_tools: list[Any] = []
        messages: list[str] = []

        def run(  # noqa: D102 - a fake agent's run, documented by its class
            self,
            message: str,
            *,
            stream: bool,
            session: AgentSession,
            **_run_options: Any,
        ) -> Any:
            async def _gen() -> Any:
                yield SimpleNamespace(text="an answer", contents=[], user_input_requests=[])

            return _gen()

    _events(_PlainAgent())
    assert called == []


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

    class _AlwaysLaunching:
        mcp_tools: list[Any] = []

        def __init__(self) -> None:
            self.messages: list[str] = []

        def run(  # noqa: D102 - a fake agent's run, documented by its class
            self,
            message: str,
            *,
            stream: bool,
            session: AgentSession,
            **_run_options: Any,
        ) -> Any:
            self.messages.append(message)
            index = len(self.messages)

            async def _gen() -> Any:
                record_job_started(f"qm-{index}", "qm")
                yield SimpleNamespace(text=f"pass{index}", contents=[], user_input_requests=[])

            return _gen()

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
