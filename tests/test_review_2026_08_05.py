"""What the 2026-08-05 CHECKMATE deep review found, pinned so it cannot come back.

Every test here exists because a *mutation* survived: the guard it names could be deleted, or the
branch it names could be taken, and the whole suite stayed green. That is the specific failure G7
asks about — "do the tests prove the acceptance criterion, or only mock behaviour" — and the
answer over the live/durable spine was no in six places at once.

They are together in one file rather than scattered into the six modules' own test files for one
reason: each of these is a *claim the code makes about itself* — a docstring, an ADR, a comment
naming an incident — and a claim's test is easiest to keep honest when the claim and the mutation
that survived it are recorded side by side.
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


def test_the_mid_turn_resume_meters_its_own_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one feature that adds a second unbounded model call was invisible to the cost guard.

    Measured before the fix on exactly this shape: the turn spent 1,000 tokens before the wait and
    5,000 after it, and the budget booked **1,000** — 83 % of the turn unmetered, so a runaway
    resume could not trip the refusal D-144 exists to be. The `TurnCost` row and
    `chemclaw_tokens_total` were short by the same amount.
    """
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

    async def _drive() -> None:
        async for _ in run_turn(
            TurnSession(session_id="resume-usage"),
            "compute it",
            budget=_Recording(),
            connectors=[],
            graph_factory=agent.graph_factory,
        ):
            pass

    asyncio.run(_drive())
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
    """The framing sits on the only function that awaits, so a second call site cannot forget it.

    It was written at one of the two awaits. The freshly-started branch had it; the *re-joined*
    branch — a second chemist asking for a job already running — did not, so a rejoined run that
    failed handed MAF a raw `WorkflowFailureError`. That type is neither a `ChemclawError` nor a
    `SubsystemUnavailableError`, so `agent.tool_authz.surface_domain_errors` passes it through
    and the model reads a wordless failure — which three earlier incidents
    established is read as *proceed*.
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


# The functions in `connectors/jobs.py` whose whole purpose is to turn a failed workflow result
# into a framed, readable cause. An `await handle.result()` anywhere else is the defect this file
# exists for: a raw `WorkflowFailureError` reaching a caller that renders it as "the job failed".
#
# `failed_job_reason` joined `_await_briefly` on 2026-08-27. It is not a loosening — it is the same
# walk, extracted so the *status* path can render a cause too. Measured before it existed, a failed
# job polled through `get_durable_job_status` answered `summary=None, result={}`, while the two
# other collectors both rendered the reason; that tool's docstring tells the model to poll, so the
# one surface with no cause was the primary one.
_FRAMES_A_FAILED_RESULT = frozenset({"_await_briefly", "failed_job_reason"})


def test_every_workflow_result_await_frames_its_failure() -> None:
    """Structural, because the defect was a *missing call site* rather than a wrong one.

    A behavioural test can only cover the call sites it knows about; this asks the module whether
    anything awaits a workflow result outside the functions that frame the failure, which is the
    property that made the re-joined branch wrong for a year.

    Checked against the *enclosing function* rather than against a set of awaited attribute names.
    That is stricter than the shape it replaces, not looser: the old check asserted that `result`
    appeared nowhere in the module's awaited attributes at all, so it could only ever be satisfied
    by there being exactly one framing function, and adding a second — however correct — read as a
    regression. Naming the enclosing functions says what the rule actually is.
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
    """`chemclaw_jobs_started_total` was booked after the inline wait, which the common case skips.

    Five of the seven declared jobs carry `inline_wait_seconds` — every `calc` job — so the counter
    that operators read as "durable work launched" systematically missed them, while
    `chemclaw_job_runtime_seconds_total` (booked from the job record, written either way) kept
    counting their runtime. Starts and runtime were read off one dashboard and only one was true.
    """
    source = (_SRC / "connectors" / "jobs.py").read_text(encoding="utf-8")
    counted = source.index('m.increment("chemclaw_jobs_started_total")')
    waited = source.index("if job.inline_wait_seconds is not None:\n            finished")
    assert counted < waited, (
        "the start counter must be booked before the inline wait, or every job that answers inside "
        "the turn goes uncounted"
    )


def test_every_durable_launch_keeps_the_failed_only_reuse_policy() -> None:
    """D-011's idempotency contract, asserted rather than described.

    Four launch sites pass `ALLOW_DUPLICATE_FAILED_ONLY` and every mention of it in `tests/` was
    *prose* — changing the connector-job launcher to `ALLOW_DUPLICATE`, which silently recomputes a
    **completed** job, left 447 tests green. The policy is the one line that makes "a stored result
    is never recomputed" true, so it is worth naming the sites.
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
    """Depth is not specificity — the fix a live run corrected within the hour, now tested directly.

    The only coverage was a test that needs a Temporal server and skips offline, so the four lines
    of the walk were unexecuted in every CI run. Reverting the walk to the innermost frame — the
    version D-2026-08-04 corrected — left 458 tests green.
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
    """A threshold in code is a threshold an operator cannot move without a release.

    `_ARG_PREVIEW_CHARS = 200` also carried the comment "mirrors the audit trail truncation" beside
    a literal, while the audit trail's own budget was ENV-overridable — so raising one for a fuller
    audit moved one of the two and the claim quietly stopped being true.
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
    """The dependency loop's survivors at once — dedup, and the order that is now an invariant.

    `seen`, the `in` test, the `continue` and both `append`s could each be broken with the suite
    green, because no test recorded a note *with* dependencies. A caller may legitimately list one
    twice (two computed properties of one compound), and writing a path twice in a commit is at
    best noise.

    **The order assertion is inverted from what this test used to hold, and that is the change.**
    It required the subject at `files[0]`, because the PR-gate's `NoteProposal.content` read that
    slot. There is no proposal record now, and a reader can see a half-written unit, so
    `record._build_write` writes **dependencies first** — a note must never appear in the graph
    before what it cites (`D-2026-09-05-the-gate-is-deleted-not-dormant`).
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
    """`knowledge_dir if knowledge_dir is not None else settings.knowledge_dir`, inverted, survived.

    Nothing passed the argument, so the default and the override were indistinguishable — and the
    inverted version writes every note into the *configured* directory while ignoring the caller,
    which for the ELN and memory jobs that pass one means notes landing in the wrong tree.
    """
    captured: list[Any] = []

    class _Capturing:
        async def write(self, write: Any) -> WriteOutcome:
            captured.append(write)
            return WriteOutcome(reference=f"commit://{len(captured)}")

    asyncio.run(
        record_note(_agent_note("scoped-note", "body"), _Capturing(), knowledge_dir="elsewhere")
    )
    assert captured[0].files[0].path.startswith("elsewhere/")
