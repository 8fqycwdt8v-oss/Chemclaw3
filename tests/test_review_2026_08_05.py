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
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from agent_framework import AgentSession, Content, Message
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ActivityError, ChildWorkflowError

from chemclaw.agent.session_store import PostgresHistoryProvider
from chemclaw.api.budget import BudgetTracker
from chemclaw.api.events import ToolCallEvent
from chemclaw.api.runner import run_turn
from chemclaw.api.runner_trace import ToolCallTrace
from chemclaw.connectors import jobs as jobs_module
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.durable.connector_job import failure_reason
from chemclaw.kg.note import Note
from chemclaw.kg.pr_gate import propose_note
from tests.fakes import FakeUpdate, fed
from tests.pg import migrated_db_or_skip

_SRC = Path(__file__).resolve().parents[1] / "src" / "chemclaw"


# --------------------------------------------------------------------------------------------
# The resume's tokens (runner.py) — a second `agent.run` nobody metered
# --------------------------------------------------------------------------------------------


class _ResumingAgent:
    """Two passes: the first launches a job and spends tokens, the second spends far more."""

    mcp_tools: list[Any] = []

    def __init__(self, first_tokens: int, second_tokens: int) -> None:
        self._tokens = (first_tokens, second_tokens)
        self.calls = 0

    def run(  # noqa: D102 - a fake agent's run, documented by its class
        self, message: str, *, stream: bool, session: AgentSession, **_options: Any
    ) -> Any:
        self.calls += 1
        tokens = self._tokens[min(self.calls, 2) - 1]
        first = self.calls == 1

        async def _gen() -> Any:
            if first:
                from chemclaw.core.turn_signals import record_job_started

                record_job_started("job-1", "calc")
            yield FakeUpdate(
                text="ok" if first else " and the answer",
                contents=[SimpleNamespace(usage_details={"total_token_count": tokens})],
            )

        return _gen()


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
            agent,
            AgentSession(session_id="resume-usage"),
            "compute it",
            budget=_Recording(),
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

    async def result(self) -> Any:  # noqa: D102 - the handle protocol
        inner = _child_failure()
        inner.__cause__ = RuntimeError(self._reason)
        outer = WorkflowFailureError(cause=inner)
        raise outer


def test_a_job_that_fails_inside_the_wait_is_framed_wherever_it_was_awaited() -> None:
    """The framing sits on the only function that awaits, so a second call site cannot forget it.

    It was written at one of the two awaits. The freshly-started branch had it; the *re-joined*
    branch — a second chemist asking for a job already running — did not, so a rejoined run that
    failed handed MAF a raw `WorkflowFailureError`. That type is neither a `ChemclawError` nor a
    `SubsystemUnavailableError`, so `agent.tool_authz.surface_domain_errors` passes it through and
    the model reads "Error: Function failed." — the wordless failure that three earlier incidents
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


def test_both_awaits_go_through_the_framing_function() -> None:
    """Structural, because the defect was a *missing call site* rather than a wrong one.

    A behavioural test can only cover the call sites it knows about; this asks the module whether
    anything awaits a workflow result outside `_await_briefly`, which is the property that made
    the re-joined branch wrong for a year.
    """
    tree = ast.parse((_SRC / "connectors" / "jobs.py").read_text(encoding="utf-8"))
    awaited = {
        node.value.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
    }
    assert "result" not in awaited, (
        "a workflow result is awaited outside `_await_briefly`, so its failure framing is optional "
        "again — put the await inside `_await_briefly` instead"
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
# The trace's empty-fragment guard (api/runner_trace.py)
# --------------------------------------------------------------------------------------------


def _fragment(call_id: str, name: str, arguments: Any) -> Any:
    """One streamed function-call content, in the shape the Responses API sends."""
    return SimpleNamespace(
        contents=[SimpleNamespace(call_id=call_id, name=name, arguments=arguments)]
    )


def test_an_empty_fragment_mid_document_does_not_split_one_call_into_two() -> None:
    """`arguments is None`, not `not arguments` — the distinction the comment claims, untested.

    The guard's own comment says it "reached a second live run still empty", and replacing it with
    the falsy test kept 160 tests green: the existing case places the empty fragment *before* any
    accumulation, where both spellings agree. Placed mid-document it is the storm's defect again —
    one call announced twice, the first carrying a truncated preview.
    """
    trace = ToolCallTrace()
    events: list[Any] = []
    for chunk in ('{"smi', "", 'les": "CCO"}'):
        events.extend(fed(trace, _fragment("c1", "predict_pka", chunk)))
    events.extend(fed(trace, _fragment("c1", "predict_pka", "")))
    calls = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(calls) == 1
    assert calls[0].arguments == '{"smiles": "CCO"}'


# --------------------------------------------------------------------------------------------
# The turn's own flush (api/runner.py)
# --------------------------------------------------------------------------------------------


class _UnparseableArgumentAgent:
    """Streams a call whose arguments never parse as JSON, and then ends the stream."""

    mcp_tools: list[Any] = []

    def run(  # noqa: D102 - a fake agent's run, documented by its class
        self, message: str, *, stream: bool, session: AgentSession, **_options: Any
    ) -> Any:
        async def _gen() -> Any:
            yield FakeUpdate(
                contents=[
                    SimpleNamespace(call_id="c9", name="find_notes", arguments="not json at all")
                ]
            )

        return _gen()


def test_a_call_whose_arguments_never_parse_still_reaches_the_stream() -> None:
    """`run_turn`'s closing `tool_trace.flush()`, which nothing exercised through a whole turn.

    The trace announces a call the moment its arguments parse; a provider that streams something
    other than JSON has no such moment, so the final flush is the only thing that keeps the call
    from vanishing. Deleting those two lines from `run_turn` left 160 tests green — `flush()` was
    tested on the object and never through the turn that has to call it.
    """

    async def _collect() -> list[Any]:
        return [
            e
            async for e in run_turn(
                _UnparseableArgumentAgent(), AgentSession(session_id="flush-1"), "find things"
            )
        ]

    calls = [e for e in asyncio.run(_collect()) if isinstance(e, ToolCallEvent)]
    assert [call.tool for call in calls] == ["find_notes"]


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


# --------------------------------------------------------------------------------------------
# The compaction watermark (agent/session_store.py) — a guard whose deletion destroys the turn
# --------------------------------------------------------------------------------------------


def _bulky_turn(index: int) -> list[Message]:
    """One turn's stored messages, with a payload large enough to force a compaction decision."""
    return [
        Message(role="user", contents=[Content.from_text(f"question {index}")]),
        Message(
            role="assistant",
            contents=[
                Content.from_function_call(call_id=f"w{index}", name="predict_pka", arguments={})
            ],
        ),
        Message(
            role="tool",
            contents=[
                Content.from_function_result(call_id=f"w{index}", result="payload " + "z" * 4000)
            ],
        ),
        Message(role="assistant", contents=[Content.from_text(f"answer {index}")]),
    ]


def test_compaction_never_deletes_the_turn_that_triggered_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `protected=` watermark, which three test files covered and none of them pinned.

    `plan_compaction`'s `protected` parameter *is* tested directly; what was untested is the call
    site's derivation of it from the watermark — the row ids this `save_messages` just inserted.
    Replacing it with `protected=set()` left 31 tests green across the three files that name
    compaction, and on a real database with a tight budget it deleted **every** row, including the
    turn being stored: rows after = 0, the new turn survived = False. A conversation that answers
    and then forgets the exchange it just had is the worst failure this store can have, and nothing
    would have said so.
    """
    monkeypatch.setattr(settings, "agent_durable_compaction_enabled", True)
    monkeypatch.setattr(settings, "agent_durable_compaction_min_rows", 4)
    monkeypatch.setattr(settings, "agent_context_token_budget", 50)

    async def _run() -> list[Message]:
        await migrated_db_or_skip()
        provider = PostgresHistoryProvider()
        session_id = "review-watermark"
        async with db.connection(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM session_messages WHERE session_id = %s", (session_id,)
                )
            await conn.commit()
        for index in range(4):
            await provider.save_messages(session_id, _bulky_turn(index))
        return await provider.get_messages(session_id)

    remaining = asyncio.run(_run())
    assert remaining, "compaction emptied the table, including the turn it was triggered by"
    rendered = " ".join(
        content.text or ""
        for message in remaining
        for content in message.contents
        if getattr(content, "text", None)
    )
    assert "question 3" in rendered and "answer 3" in rendered, (
        "the turn that triggered compaction was compacted away by it"
    )


def _agent_note(note_id: str, body: str) -> Note:
    """One agent-authored note, the only kind the PR-gate accepts."""
    return Note(id=note_id, type="job-result", created_by="agent", body=body)


# --------------------------------------------------------------------------------------------
# The mutant walk: what survived in the two files that hold two thirds of the survivors
# --------------------------------------------------------------------------------------------


def test_a_content_with_a_name_but_no_call_id_is_still_traced() -> None:
    """`key = call_id or name`, so a call identified only by its name is legitimate input.

    The guard reads `hasattr(content, "arguments") or hasattr(content, "call_id")`, and replacing
    the first half with a constant `False` survived every test: every fixture happened to carry a
    `call_id` too. A provider that identifies a call by name alone would have been dropped
    silently — the shape D-138 already cost this system once.
    """
    trace = ToolCallTrace()
    content = SimpleNamespace(name="find_notes", arguments='{"text": "suzuki"}')
    events = fed(trace, SimpleNamespace(contents=[content]))
    calls = [event for event in events if isinstance(event, ToolCallEvent)]
    assert [call.tool for call in calls] == ["find_notes"]


def test_an_update_with_no_contents_attribute_is_skipped_rather_than_fatal() -> None:
    """`getattr(update, "contents", None)` — the default is load-bearing and was untested.

    Removing it (so the attribute is required) survived the suite because every fake update in the
    tree has `contents`. A real provider's keep-alive or usage-only update need not, and the trace
    is duck-typed precisely because MAF's shapes vary by version: raising here would kill the turn.
    """
    assert trace_feed_is_empty(ToolCallTrace(), SimpleNamespace())


def trace_feed_is_empty(trace: ToolCallTrace, update: Any) -> bool:
    """Feed one update and report that it produced nothing — a name for the assertion above."""
    return fed(trace, update) == []


def test_one_unreadable_content_does_not_drop_the_ones_after_it() -> None:
    """`continue`, not `break`: updates carry several contents and only some are calls.

    Both `continue`s in `feed` could be turned into `break` with the suite green, because no test
    put a real call *after* an irrelevant content in one update. MAF routinely does — text and a
    function call arrive together — so this silently loses calls.
    """
    trace = ToolCallTrace()
    events = fed(
        trace,
        SimpleNamespace(
            contents=[
                SimpleNamespace(text="thinking out loud"),
                SimpleNamespace(call_id="c1", name="predict_pka", arguments='{"smiles": "CCO"}'),
            ]
        ),
    )
    calls = [event for event in events if isinstance(event, ToolCallEvent)]
    assert [call.tool for call in calls] == ["predict_pka"]

    # The second `continue` too: a fragment for a call this trace never saw opened is skipped, and
    # a real call after it in the same update must still be read.
    fresh = ToolCallTrace()
    after_orphan = fed(
        fresh,
        SimpleNamespace(
            contents=[
                SimpleNamespace(call_id="never-opened", arguments='{"x":'),
                SimpleNamespace(call_id="c2", name="find_notes", arguments='{"text": "a"}'),
            ]
        ),
    )
    assert [c.tool for c in after_orphan if isinstance(c, ToolCallEvent)] == ["find_notes"]


def test_a_call_flushed_without_a_name_is_announced_under_its_key() -> None:
    """`self._names.pop(key, key)` — the default is the fallback, and nothing reached it.

    Popping with `None`, or with no default at all, both survived the suite: every test's call has
    its name recorded first. A stream whose fragments arrive before (or without) the opening named
    content still has to announce *something*, and the call id is the only handle there is —
    `ToolCallEvent(tool=None)` would be a typed lie, and raising would kill the turn.
    """
    trace = ToolCallTrace()
    trace._names["c-keyed"] = "predict_pka"
    fed(trace, SimpleNamespace(contents=[SimpleNamespace(call_id="c-keyed", arguments='{"a":')]))
    del trace._names["c-keyed"]  # the opening content is gone, as a replayed stream would leave it
    calls = [event for event in trace.flush() if isinstance(event, ToolCallEvent)]
    assert [call.tool for call in calls] == ["c-keyed"]


def test_the_result_number_cap_is_a_ceiling_not_an_exclusive_bound(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`<=`, not `<`: a result with exactly the cap's worth of values is complete, not truncated.

    An off-by-one here logs a warning and drops a value on a result that was within budget, which
    is the same silent-incompleteness defect the cap exists to announce.
    """
    exact = ", ".join(str(n + 0.5) for n in range(settings.stream_max_result_numbers))
    with caplog.at_level(logging.WARNING, logger="chemclaw.api.runner_trace"):
        events = fed(
            ToolCallTrace(),
            SimpleNamespace(contents=[SimpleNamespace(call_id="c", name="calc", result=exact)]),
        )
    numbers = [getattr(event, "numbers", None) for event in events]
    assert any(n is not None and len(n) == settings.stream_max_result_numbers for n in numbers)
    # The list is the same length either way — `values[:cap]` of exactly `cap` values is itself —
    # so the *warning* is what tells the two bounds apart, and announcing a truncation that did not
    # happen is the same silent-incompleteness defect in reverse.
    assert not caplog.records, "a complete result was reported as truncated"


def test_propose_note_deduplicates_dependencies_and_keeps_the_subject_first() -> None:
    """The dependency loop's four survivors at once — dedup, order, and what lands in `files`.

    `seen`, the `in` test, the `continue` and both `append`s could each be broken with the suite
    green, because no test proposed a note *with* dependencies. A caller may legitimately list one
    twice (two computed properties of one compound), and writing a path twice in a commit is at
    best noise; dropping the subject note from position 0 is worse, since `NoteProposal.content`
    reads `files[0]`.
    """
    captured: list[Any] = []

    class _Capturing:
        async def submit(self, submission: Any) -> str:
            captured.append(submission)
            return str(submission.branch)

    subject = _agent_note("subject-note", "see [[dep-a]] and [[dep-b]]")
    dep_a = _agent_note("dep-a", "the first dependency")
    dep_b = _agent_note("dep-b", "the second dependency")
    # `dep_b` comes *after* the duplicate on purpose: with `break` in place of `continue` the
    # duplicate would end the loop and silently drop it, which is the mutation this pins.
    asyncio.run(propose_note(subject, _Capturing(), dependencies=[dep_a, dep_a, dep_b, subject]))

    paths = [file.path for file in captured[0].files]
    assert paths[0].endswith("subject-note.md"), "the subject note must stay at files[0]"
    assert len(paths) == 3, f"a dependency was dropped or duplicated: {paths}"
    assert any(path.endswith("dep-a.md") for path in paths)
    assert any(path.endswith("dep-b.md") for path in paths), (
        "the dependency after the duplicate was dropped"
    )


def test_propose_note_honours_an_explicit_knowledge_directory() -> None:
    """`knowledge_dir if knowledge_dir is not None else settings.knowledge_dir`, inverted, survived.

    Nothing passed the argument, so the default and the override were indistinguishable — and the
    inverted version writes every note into the *configured* directory while ignoring the caller,
    which for the ELN and memory jobs that pass one means notes landing in the wrong tree.
    """
    captured: list[Any] = []

    class _Capturing:
        async def submit(self, submission: Any) -> str:
            captured.append(submission)
            return str(submission.branch)

    asyncio.run(
        propose_note(_agent_note("scoped-note", "body"), _Capturing(), knowledge_dir="elsewhere")
    )
    assert captured[0].files[0].path.startswith("elsewhere/")
