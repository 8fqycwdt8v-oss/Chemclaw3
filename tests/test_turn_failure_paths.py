"""What a turn does when it does not finish cleanly.

Three findings that share one shape: the happy path was tested and the exit was not.

- `run_turn` abandons the agent's stream on every exit that is not "exhausted" — a cancelled turn,
  a disconnected client, a raising consumer. MAF's `ResponseStream` releases the underlying
  generator and runs its cleanup hooks from `__anext__` only, and exposes no `aclose()`.
- The mid-turn resume drops `user_input_requests`, so a plan the model wanted signed off during a
  resume was never put to the chemist.
- `await_job_results` drops a *failed* job while its own docstring promises it does not, because
  `handle.result()` raises and `return_exceptions=True` swallowed the raise.
"""

import asyncio
from contextlib import aclosing
from types import SimpleNamespace
from typing import Any

import pytest
from agent_framework import AgentSession
from agent_framework._types import ResponseStream

from chemclaw.api.runner import run_turn
from chemclaw.core.config import settings


class _StreamingAgent:
    """A fake agent whose `run` returns a **real** `ResponseStream`, which is the subject here.

    The other fakes in this suite return a bare async generator, which Python closes for us on
    abandonment — so they cannot see this defect at all. The wrapper is exactly the thing that does
    not close.
    """

    mcp_tools: list[Any] = []

    def __init__(self, updates: int = 5) -> None:
        """Stream `updates` text chunks, recording what happened to the source generator."""
        self.updates = updates
        self.source_closed = False
        self.cleanup_ran = False

    def run(  # noqa: D102 - a fake agent's run, documented by its class
        self, message: str, *, stream: bool, session: AgentSession, **_options: Any
    ) -> Any:
        async def _source() -> Any:
            try:
                for index in range(self.updates):
                    yield SimpleNamespace(
                        text=f"chunk-{index}", contents=[], user_input_requests=[]
                    )
            finally:
                self.source_closed = True

        def _cleanup() -> None:
            self.cleanup_ran = True

        return ResponseStream(_source(), cleanup_hooks=[_cleanup])


def test_abandoning_a_turn_releases_the_agent_stream() -> None:
    """The finding: a turn that is not consumed to the end leaked the stream behind it.

    A client disconnect closes the async generator FastAPI is iterating, which raises
    `GeneratorExit` at the `yield` inside `run_turn` — every frame unwinds, and the `async for` over
    the agent's stream is simply abandoned. Measured on `ResponseStream` directly: the source
    generator's `finally` had not run and the cleanup hooks had not run at all (not on GC, not at
    loop shutdown). The generator *is* finalized eventually by asyncio's GC hook — 250 ms later in
    the probe, and only once nothing references it — which is the wrong guarantee for the object
    holding the HTTP response to the model open.
    """

    async def _run() -> _StreamingAgent:
        agent = _StreamingAgent()
        # `aclosing` is what FastAPI does to this generator when the client disconnects, and
        # `run_turn` is typed as a plain `AsyncIterator`, so the close goes through the protocol
        # rather than through an attribute mypy cannot see on that type.
        async with aclosing(run_turn(agent, AgentSession(session_id="abandon-1"), "hello")) as turn:
            # Consume as far as the first *token*, not the first event: the turn yields
            # `CapabilityDegradedEvent` before it opens the stream at all, and walking away there
            # would test a turn that never reached the agent.
            async for event in turn:
                if event.type == "token":
                    break
        return agent

    agent = asyncio.run(_run())
    assert agent.source_closed, "the agent's stream was abandoned without releasing its generator"
    assert agent.cleanup_ran, "the stream's cleanup hooks never ran"


def test_a_fully_consumed_turn_still_releases_the_stream_exactly_once() -> None:
    """The other direction: the ordinary path must keep working, and must not double-clean.

    `_run_cleanup_hooks` is idempotent upstream, so this asserts the ordinary exhaustion still runs
    it — a `_closing` that only fired on abandonment would pass the test above and change nothing
    about the path 99 % of turns take.
    """

    async def _run() -> _StreamingAgent:
        agent = _StreamingAgent()
        async for _ in run_turn(agent, AgentSession(session_id="exhaust-1"), "hello"):
            pass
        return agent

    agent = asyncio.run(_run())
    assert agent.source_closed
    assert agent.cleanup_ran


def test_a_consumer_error_inside_the_turn_still_releases_the_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The third exit shape, and the one the runner's own error handling produces.

    `run_turn` catches its body's exceptions and turns them into an `ErrorEvent` — which is a
    *normal* return for the caller and, before this, an abandoned stream underneath.
    """
    from chemclaw.api import runner

    def _boom(_update: Any) -> Any:
        raise RuntimeError("consumer failed mid-stream")

    monkeypatch.setattr(runner, "usage_tokens", _boom)

    async def _run() -> _StreamingAgent:
        agent = _StreamingAgent()
        async for _ in run_turn(agent, AgentSession(session_id="raise-1"), "hello"):
            pass
        return agent

    agent = asyncio.run(_run())
    assert agent.source_closed, "a failed turn abandoned its stream"
    assert agent.cleanup_ran


# --- The resume drops the approval prompt -----------------------------------------------------


class _ResumeApprovingAgent:
    """Launches a job on the first pass, then asks for approval on the resume."""

    mcp_tools: list[Any] = []

    def __init__(self) -> None:
        """Track which pass we are on, so the second one is the resume."""
        self.messages: list[str] = []

    def run(  # noqa: D102 - a fake agent's run, documented by its class
        self, message: str, *, stream: bool, session: AgentSession, **_options: Any
    ) -> Any:
        from chemclaw.core.turn_signals import record_job_started

        self.messages.append(message)
        first = len(self.messages) == 1

        async def _gen() -> Any:
            if first:
                record_job_started("qm-1", "qm")
                yield SimpleNamespace(text="starting", contents=[], user_input_requests=[])
            else:
                yield SimpleNamespace(
                    text=" done",
                    contents=[],
                    user_input_requests=[SimpleNamespace(prompt="Approve the revised plan?")],
                )

        return _gen()


def test_an_approval_raised_during_the_resume_reaches_the_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The finding: the resume is a second `agent.run` and it dropped this event.

    The first pass emits `ApprovalRequestEvent` for every `user_input_requests` entry; the resume
    loop simply had no such branch. The consequence is not a missing UI element — it is the plan
    gate silently not applying, because the turn continues as though the chemist had been asked.
    """
    monkeypatch.setattr(settings, "mid_turn_resume_enabled", True)
    monkeypatch.setattr(settings, "mid_turn_resume_timeout_seconds", 5.0)

    async def _results(session_id: str, job_ids: list[str], *, timeout_seconds: float) -> Any:
        return {job_ids[0]: {"status": "completed", "energy_hartree": -154.1}}

    monkeypatch.setattr("chemclaw.api.runner.await_job_results", _results)

    async def _collect() -> list[Any]:
        agent = _ResumeApprovingAgent()
        return [e async for e in run_turn(agent, AgentSession(session_id="resume-approve"), "go")]

    events = asyncio.run(_collect())
    prompts = [e.prompt for e in events if e.type == "approval_request"]
    assert prompts == ["Approve the revised plan?"], (
        "the resume's approval request never reached the chemist"
    )


# --- A failed job is dropped from the resume ---------------------------------------------------


class _FailingHandle:
    """A workflow handle whose result raises, as Temporal's does for a non-completed run."""

    def __init__(self, job_id: str) -> None:
        """Bind the id, so the fake client can report the same job from `describe`."""
        self.job_id = job_id

    async def result(self) -> Any:
        """Raise the way `handle.result()` raises for a failed workflow."""
        from temporalio.client import WorkflowFailureError

        raise WorkflowFailureError(cause=RuntimeError("activity failed after 5 attempts"))

    async def describe(self) -> Any:
        """Report the terminal status, which is what the status path reads."""
        from temporalio.client import WorkflowExecutionStatus

        return SimpleNamespace(status=WorkflowExecutionStatus.FAILED)


class _FakeClient:
    """Hands out `_FailingHandle`s — the whole Temporal surface these two paths touch."""

    def get_workflow_handle(self, job_id: str) -> _FailingHandle:
        """One handle per id."""
        return _FailingHandle(job_id)


def test_a_failed_job_is_reported_to_the_turn_rather_than_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The finding, and the docstring that always claimed the opposite.

    `handle.result()` raises for a failed workflow; `return_exceptions=True` captured the raise and
    the job vanished from the returned map — indistinguishable from a job that had not finished.
    The model then resumed with no mention of the failure, and a chemist reads silence as success.
    That is precisely what the function's `Returns:` said must not happen.
    """
    from chemclaw.agent import durable_tools, job_results

    async def _client() -> _FakeClient:
        return _FakeClient()

    monkeypatch.setattr(job_results, "connect", _client)
    monkeypatch.setattr(durable_tools, "connect", _client)

    collected = asyncio.run(
        job_results.await_job_results("s1", ["qm-1"], timeout_seconds=5.0),
    )

    assert "qm-1" in collected, "the failed job was dropped from the mid-turn resume"
    assert collected["qm-1"]["status"] == "failed"


def test_the_reported_status_is_the_terminal_one_not_a_word_invented_here(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled job is not a failed one, and the resume must not flatten the difference.

    The raise from `handle.result()` is the same object for failed, cancelled, timed-out and
    terminated runs, so composing a status word at the catch site would report all four as
    "failed". Asking `job_status` — the one place that maps a terminal Temporal status onto the
    word this system reports — keeps them apart and keeps the resume agreeing with
    `get_durable_job_status` about the same run.
    """
    from temporalio.client import WorkflowExecutionStatus

    from chemclaw.agent import durable_tools, job_results

    class _CancelledClient(_FakeClient):
        def get_workflow_handle(self, job_id: str) -> Any:
            handle = _FailingHandle(job_id)

            async def _describe() -> Any:
                return SimpleNamespace(status=WorkflowExecutionStatus.CANCELED)

            handle.describe = _describe  # type: ignore[method-assign]
            return handle

    async def _client() -> Any:
        return _CancelledClient()

    monkeypatch.setattr(job_results, "connect", _client)
    monkeypatch.setattr(durable_tools, "connect", _client)

    collected = asyncio.run(job_results.await_job_results("s1", ["qm-1"], timeout_seconds=5.0))
    assert collected["qm-1"]["status"] == "cancelled"


def test_a_still_running_job_is_still_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bound the fix must not cross: "not finished" is not a status to report.

    Without this, a fix that reported *every* job would turn the resume's "wait for what lands"
    into "always resume", handing the model a running job's empty result as though it were an
    outcome.
    """
    from chemclaw.agent import durable_tools, job_results

    class _NeverFinishing:
        def get_workflow_handle(self, job_id: str) -> Any:
            async def _result() -> Any:
                await asyncio.sleep(10)

            return SimpleNamespace(result=_result)

    async def _client() -> Any:
        return _NeverFinishing()

    monkeypatch.setattr(job_results, "connect", _client)
    monkeypatch.setattr(durable_tools, "connect", _client)

    collected = asyncio.run(job_results.await_job_results("s1", ["qm-1"], timeout_seconds=0.05))
    assert collected == {}
