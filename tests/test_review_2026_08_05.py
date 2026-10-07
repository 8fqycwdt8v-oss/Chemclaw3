"""Guards whose deletion previously left the whole suite green, pinned together.

Each test names a claim the code makes about itself (a docstring or comment) and fails if the
guard behind that claim is removed.
"""

import ast
import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ActivityError, ChildWorkflowError

from chemclaw.agent.session import TurnSession
from chemclaw.api.budget import BudgetTracker
from chemclaw.api.runner import run_turn
from chemclaw.api.runner_trace import ToolCallTrace
from chemclaw.connectors import jobs as jobs_module
from chemclaw.core.config import settings
from chemclaw.durable.connector_job import failure_reason
from chemclaw.kg.note import Note
from chemclaw.kg.record import WriteOutcome, record_note
from tests.fakes_turn import Chunk, Piece, ScriptedTurn

_SRC = Path(__file__).resolve().parents[1] / "src" / "chemclaw"


# --------------------------------------------------------------------------------------------
# The resume's tokens (runner.py) — a second `agent.run` nobody metered
# --------------------------------------------------------------------------------------------


class _ResumingAgent(ScriptedTurn):
    """Two passes: the first launches a job and spends tokens, the second spends far more."""

    def __init__(self, first_tokens: int, second_tokens: int) -> None:
        self._tokens = (first_tokens, second_tokens)
        self.calls = 0

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        from chemclaw.core.turn_signals import record_job_started

        self.calls += 1
        tokens = self._tokens[min(self.calls, 2) - 1]
        first = self.calls == 1
        if first:
            record_job_started("job-1", "calc")
        yield Chunk("ok" if first else " and the answer", output_tokens=tokens)


async def test_the_mid_turn_resume_meters_its_own_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mid-turn resume meters its own tokens, so the budget sees the whole turn."""
    booked: list[int] = []
    monkeypatch.setattr(settings, "mid_turn_resume_enabled", True)
    monkeypatch.setattr(settings, "mid_turn_resume_timeout_seconds", 5.0)
    monkeypatch.setattr(settings, "budget_enabled", True)

    class _Recording(BudgetTracker):
        def record(self, session_id: str, user: str | None, tokens: int) -> None:
            booked.append(tokens)

    async def _results(session_id: str, job_ids: list[str], *, timeout_seconds: float) -> Any:
        return {job_ids[0]: {"energy_hartree": -154.1}}

    monkeypatch.setattr("chemclaw.api.runner.await_job_results", _results)
    agent = _ResumingAgent(first_tokens=1000, second_tokens=5000)

    async for _ in run_turn(
        TurnSession(session_id="resume-usage"),
        "compute it",
        budget=_Recording(),
        connectors=[],
        graph_factory=agent.graph_factory,
    ):
        pass

    assert agent.calls == 2, "the resume must actually have run for this to mean anything"
    assert booked == [6000]


# --------------------------------------------------------------------------------------------
# The inline wait (connectors/jobs.py) — a failure with no words, and a start nobody counted
# --------------------------------------------------------------------------------------------


class _FailingHandle:
    """A workflow handle whose result raises the client's wrapper, as a real failed run does."""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    async def result(self) -> Any:
        inner = _child_failure()
        inner.__cause__ = RuntimeError(self._reason)
        outer = WorkflowFailureError(cause=inner)
        raise outer


def test_a_job_that_fails_inside_the_wait_is_framed_wherever_it_was_awaited() -> None:
    """A job that fails inside the wait is framed wherever it was awaited, including on rejoin.

    A raw `WorkflowFailureError` is neither a `ChemclawError` nor a `SubsystemUnavailableError`, so
    it would reach the model as a wordless failure, which is read as "proceed".
    """
    with pytest.raises(jobs_module.ConnectorJobError) as caught:
        asyncio.run(
            jobs_module._await_briefly(
                _FailingHandle("unknown ALPB solvent '2-methyltetrahydrofuran'"),
                5.0,
                "compare",
                "compare-1",
            )
        )
    assert "'compare' job ran and failed" in str(caught.value)
    assert "2-methyltetrahydrofuran" in str(caught.value)


# The functions in `connectors/jobs.py` that turn a failed workflow result into a framed cause;
# an `await handle.result()` anywhere else would hand a raw `WorkflowFailureError` to a caller.
_FRAMES_A_FAILED_RESULT = frozenset({"_await_briefly", "failed_job_reason"})


def test_every_workflow_result_await_frames_its_failure() -> None:
    """Every workflow-result await sits inside a function that frames its failure.

    Structural, because the defect is a missing call site that a behavioural test cannot know
    about. Checked by enclosing function, so adding another framing function is allowed.
    """
    tree = ast.parse((_SRC / "connectors" / "jobs.py").read_text(encoding="utf-8"))
    unframed: list[str] = []
    for parent in ast.walk(tree):
        if not isinstance(parent, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        if parent.name in _FRAMES_A_FAILED_RESULT:
            continue
        for node in ast.walk(parent):
            if (
                isinstance(node, ast.Await)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr == "result"
            ):
                unframed.append(f"{parent.name}:{node.lineno}")
    assert not unframed, (
        f"a workflow result is awaited at {unframed}, outside the functions that frame its "
        f"failure ({sorted(_FRAMES_A_FAILED_RESULT)}) — so a raw WorkflowFailureError reaches the "
        "caller and its cause is rendered as nothing. Await it inside one of those instead."
    )


def test_a_job_that_answers_inside_the_turn_still_counts_as_started() -> None:
    """A job that answers inside the turn still counts as started.

    Most jobs carry `inline_wait_seconds`, so booking the start after the wait would miss them.
    """
    source = (_SRC / "connectors" / "jobs.py").read_text(encoding="utf-8")
    counted = source.index('m.increment("chemclaw_jobs_started_total")')
    waited = source.index("if job.inline_wait_seconds is not None:\n            finished")
    assert counted < waited, (
        "the start counter must be booked before the inline wait, or every job that answers inside "
        "the turn goes uncounted"
    )


def test_every_durable_launch_keeps_the_failed_only_reuse_policy() -> None:
    """Every durable launch keeps the `ALLOW_DUPLICATE_FAILED_ONLY` reuse policy.

    That policy is what makes "a stored result is never recomputed" true; `ALLOW_DUPLICATE` would
    silently recompute a completed job.
    """
    launchers = [
        _SRC / "connectors" / "jobs.py",
        _SRC / "agent" / "durable_tools.py",
        _SRC / "templates" / "registry.py",
    ]
    for path in launchers:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        policies = [
            ast.unparse(keyword.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            for keyword in node.keywords
            if keyword.arg == "id_reuse_policy"
        ]
        assert policies, f"{path.name} starts no workflow with a stated reuse policy"
        assert all(policy.endswith("ALLOW_DUPLICATE_FAILED_ONLY") for policy in policies), (
            f"{path.name} launches with {policies}; anything but the failed-only policy lets "
            "a completed run recompute, which is exactly what D-011 forbids"
        )


# --------------------------------------------------------------------------------------------
# failure_reason (durable/connector_job.py) — a pure function whose only test needs a broker
# --------------------------------------------------------------------------------------------


def _child_failure() -> ChildWorkflowError:
    """The structural frame Temporal puts around a failed child, with its keyword-only fields."""
    return ChildWorkflowError(
        "Child Workflow execution failed",
        namespace="default",
        workflow_id="w",
        run_id="r",
        workflow_type="ConnectorJobWorkflow",
        initiated_event_id=1,
        started_event_id=2,
        retry_state=None,
    )


def _chain(*messages: str) -> BaseException:
    """A Temporal-shaped failure chain: structural frames outside, application message inside."""
    innermost: BaseException = RuntimeError(messages[-1])
    current = innermost
    for message in reversed(messages[:-1]):
        wrapper: BaseException = RuntimeError(message)
        wrapper.__cause__ = current
        current = wrapper
    child = _child_failure()
    child.__cause__ = current
    return child


def test_failure_reason_stops_at_the_first_application_frame() -> None:
    """The failure reason stops at the first application frame, not the innermost one.

    Tested directly so the walk is executed without a Temporal server.
    """
    reason = failure_reason(
        _chain(
            "unknown ALPB solvent '2-methyltetrahydrofuran'; common valid names are water, thf",
            "String value for epsilon was not found among database of solvents",
        )
    )
    assert reason.startswith("unknown ALPB solvent")
    assert "epsilon" not in reason, "the library's internals are true and useless to a chemist"


def test_failure_reason_skips_both_workflow_side_wrappers() -> None:
    """`ChildWorkflowError → ActivityError → the message` is the shape a real child failure has."""
    activity = ActivityError(
        "Activity task failed",
        scheduled_event_id=1,
        started_event_id=2,
        identity="worker",
        activity_type="run_job",
        activity_id="a1",
        retry_state=None,
    )
    activity.__cause__ = RuntimeError("the calculation did not converge")
    child = _child_failure()
    child.__cause__ = activity
    assert failure_reason(child) == "the calculation did not converge"


def test_failure_reason_never_returns_an_empty_sentence() -> None:
    """A wordless failure is the defect this exists to prevent, so an empty message falls back."""
    assert failure_reason(RuntimeError("")) == "RuntimeError"


# --------------------------------------------------------------------------------------------
# The wire budgets, now settings rather than literals (G3)
# --------------------------------------------------------------------------------------------


def test_the_two_wire_budgets_are_configuration_rather_than_literals() -> None:
    """The two wire budgets are configuration, not literals, so they move together with the audit.
    """
    source = (_SRC / "api" / "runner_trace.py").read_text(encoding="utf-8")
    assert "settings.agent_audit_max_arg_chars" in source
    assert "settings.stream_max_result_numbers" in source
    assert settings.agent_audit_max_arg_chars > 0
    assert settings.stream_max_result_numbers > 0


def _agent_note(note_id: str, body: str) -> Note:
    """One agent-authored note, the only kind the PR-gate accepts."""
    return Note(id=note_id, type="job-result", created_by="agent", body=body)


# --------------------------------------------------------------------------------------------
# The mutant walk: what survived in the two files that hold two thirds of the survivors
# --------------------------------------------------------------------------------------------


def test_the_result_number_cap_is_a_ceiling_not_an_exclusive_bound(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`<=`, not `<`: a result with exactly the cap's worth of values is complete, not truncated.

    An off-by-one here logs a warning and drops a value on a result that was within budget, which
    is the same silent-incompleteness defect the cap exists to announce.
    """
    exact = ", ".join(str(n + 0.5) for n in range(settings.stream_max_result_numbers))
    with caplog.at_level(logging.WARNING, logger="chemclaw.api.runner_trace"):
        event = asyncio.run(ToolCallTrace().returned("c", exact))
    assert len(event.numbers) == settings.stream_max_result_numbers
    # The list is the same length either way — `values[:cap]` of exactly `cap` values is itself —
    # so the *warning* is what tells the two bounds apart, and announcing a truncation that did not
    # happen is the same silent-incompleteness defect in reverse.
    assert not caplog.records, "a complete result was reported as truncated"


def test_record_note_deduplicates_dependencies_and_writes_them_before_the_subject() -> None:
    """`record_note` deduplicates dependencies and writes them before the subject.

    A note must never appear in the graph before what it cites, and a dependency listed twice is
    written once.
    """
    captured: list[Any] = []

    class _Capturing:
        async def write(self, write: Any) -> WriteOutcome:
            captured.append(write)
            return WriteOutcome(reference=f"commit://{len(captured)}")

    subject = _agent_note("subject-note", "see [[dep-a]] and [[dep-b]]")
    dep_a = _agent_note("dep-a", "the first dependency")
    dep_b = _agent_note("dep-b", "the second dependency")
    # `dep_b` comes *after* the duplicate on purpose: with `break` in place of `continue` the
    # duplicate would end the loop and silently drop it, which is the mutation this pins.
    asyncio.run(record_note(subject, _Capturing(), dependencies=[dep_a, dep_a, dep_b, subject]))

    paths = [file.path for file in captured[0].files]
    assert paths[-1].endswith("subject-note.md"), "the subject is written after what it cites"
    assert len(paths) == 3, f"a dependency was dropped or duplicated: {paths}"
    assert any(path.endswith("dep-a.md") for path in paths)
    assert any(path.endswith("dep-b.md") for path in paths), (
        "the dependency after the duplicate was dropped"
    )


def test_record_note_honours_an_explicit_knowledge_directory() -> None:
    """`record_note` honours an explicit knowledge directory over the configured one."""
    captured: list[Any] = []

    class _Capturing:
        async def write(self, write: Any) -> WriteOutcome:
            captured.append(write)
            return WriteOutcome(reference=f"commit://{len(captured)}")

    asyncio.run(
        record_note(_agent_note("scoped-note", "body"), _Capturing(), knowledge_dir="elsewhere")
    )
    assert captured[0].files[0].path.startswith("elsewhere/")
