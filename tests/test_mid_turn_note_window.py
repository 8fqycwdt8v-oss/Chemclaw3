"""A note the runner adds mid-turn must not cost the model the chemist's question.

The verifier's revision round and the mid-turn job resume each re-enter the turn's thread with a
`user`-position note the chemist never wrote. The context window cuts on conversation groups and
never past the newest one; a note that opened a group made itself the newest, so a thread over
budget was sent as `[system, note]` and the model could only ask what the question was. Driven
through `run_turn` on the real compiled graph, with the budget the axis: ample is the control,
tight is the case, and the tight arm asserts the request really was over budget.
"""

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any, cast

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

import chemclaw.agent.verifier as verifier_module
import chemclaw.api.runner as runner
from chemclaw.agent.audit import NullAuditSink
from chemclaw.agent.checkpointer import checkpointer, close_checkpointer
from chemclaw.agent.compaction import _opens_a_group
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.session import TurnSession
from chemclaw.agent.session_store import PostgresHistoryProvider, SessionOwnerStore
from chemclaw.agent.state import turn_config
from chemclaw.agent.verifier import ClaimCheck, VerificationResult
from chemclaw.api.events import AnswerEvent
from chemclaw.core.config import settings
from chemclaw.core.turn_signals import record_job_started
from tests.fakes_langgraph import ScriptedChatModel
from tests.pg import create_checkpoint_tables, migrated_db_or_skip

_QUESTION = "Is a TFA Boc deprotection quenched into bicarbonate in the same vessel safe?"
_FLAGGED = "Yield was 90% [[reaction-a]]."
_GROUNDED = "The evidence does not settle the yield [[reaction-a]]."
#: A tool result big enough to put the thread over the tight budget on its own.
_EVIDENCE = "reaction-a: TFA/DCM, 40 C, CO2 evolution on quench. " * 200
#: Estimated tokens the thread may cost in the tight arm — far below `_EVIDENCE` alone.
_TIGHT = 300
_AMPLE = 10_000_000


#: Every message list the model was sent in the current turn, in call order.
_SENT: list[list[BaseMessage]] = []


class _RecordingModel(ScriptedChatModel):
    """A scripted model that records each request in `_SENT`."""

    def __init__(self, script: list[Any]) -> None:
        """Replay `script`, as `ScriptedChatModel` does."""
        super().__init__(script)

    def _stream(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        """Record the request, then replay the next scripted turn."""
        _SENT.append(list(messages))
        yield from super()._stream(messages, stop, run_manager, **kwargs)


@tool
def lookup_evidence(query: str) -> str:
    """Return the recorded evidence for `query`."""
    return _EVIDENCE


@tool
def start_job(query: str) -> str:
    """Start a durable job for `query`."""
    record_job_started("qm-1", "qm")
    return "job qm-1 started"


def _budget(monkeypatch: pytest.MonkeyPatch, thread_tokens: int) -> None:
    """Set the window to `thread_tokens` estimated tokens for the thread, prefix not charged."""
    monkeypatch.setattr(settings, "agent_context_prefix_basis", 0)
    monkeypatch.setattr(settings, "agent_context_token_budget", thread_tokens)
    monkeypatch.setattr(settings, "agent_tool_result_clear_trigger", thread_tokens)
    monkeypatch.setattr(settings, "agent_context_calibration_enabled", False)
    monkeypatch.setattr(settings, "llm_context_window_tokens", 0)


def _grades_by_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """A verifier that flags `_FLAGGED` and passes anything else."""
    monkeypatch.setattr(settings, "verifier_enabled", True)
    monkeypatch.setattr(settings, "verifier_confidence_threshold", 0.7)
    monkeypatch.setattr(settings, "answer_review_max_rounds", 1)

    async def _verify(answer: str, *_: Any, **__: Any) -> VerificationResult:
        if answer.strip() != _FLAGGED:
            return VerificationResult(claims=[], confidence=1.0, verified_by="judge")
        return VerificationResult(
            claims=[ClaimCheck(text="Yield was 90%", supported=False)],
            confidence=0.2,
            verified_by="judge",
        )

    monkeypatch.setattr(verifier_module, "verify_turn_answer", _verify)


def _turn(
    monkeypatch: pytest.MonkeyPatch,
    store: str,
    script: list[Any],
    tools: list[Any],
) -> tuple[list[list[BaseMessage]], list[BaseMessage]]:
    """Drive one turn through `run_turn` on the real graph, on `store`'s session store.

    Returns the requests the model was sent and the thread the turn left (empty off Postgres).
    `tools` arrive the way a connector's do, through the seam `_open_turn_surface` calls.
    """
    _SENT.clear()
    session_id = f"s-note-window-{uuid.uuid4().hex}"
    monkeypatch.setattr(settings, "session_store", store)

    def _factory(**kwargs: Any) -> Any:
        kwargs["audit_sink"] = NullAuditSink()
        return build_langgraph_agent(model=_RecordingModel(script), **kwargs)

    async def _open(_stack: Any, _specs: Any) -> tuple[list[Any], list[str]]:
        return list(tools), []

    async def _run() -> tuple[list[Any], list[BaseMessage]]:
        history = None
        if store == "postgres":
            await migrated_db_or_skip()
            await create_checkpoint_tables()
            # Any saver a previous test published belongs to a closed loop.
            await close_checkpointer()
            await SessionOwnerStore().record(session_id, "oid-note-window", None)
            history = PostgresHistoryProvider()
        try:
            events = [
                event
                async for event in runner.run_turn(
                    TurnSession(session_id=session_id),
                    _QUESTION,
                    connectors=[],
                    graph_factory=_factory,
                    history=history,
                )
            ]
            if store != "postgres":
                return events, []
            config = cast(RunnableConfig, turn_config(session_id))
            saved = await (await checkpointer()).aget_tuple(config)
            assert saved is not None, "the turn stored no thread at all"
            return events, list(saved.checkpoint["channel_values"]["messages"])
        finally:
            await close_checkpointer()

    monkeypatch.setattr(runner, "open_connector_specs", _open)
    events, thread = asyncio.run(_run())
    assert next(e for e in events if isinstance(e, AnswerEvent)).text
    return list(_SENT), thread


def _sees_question(request: list[BaseMessage]) -> bool:
    return any(isinstance(m, HumanMessage) and m.content == _QUESTION for m in request)


def _thread_tokens(request: list[BaseMessage]) -> int:
    return count_tokens_approximately([m for m in request if m.type != "system"])


def _assert_every_request_sees_the_question(sent: list[list[BaseMessage]], budget: int) -> None:
    assert sent, "the model was never called"
    blind = [i for i, request in enumerate(sent) if not _sees_question(request)]
    assert not blind, f"request(s) {blind} reached the model without the chemist's question"
    if budget == _TIGHT:
        # The premise: the re-entry request was over budget, so the window had to decide.
        assert _thread_tokens(sent[-1]) > _TIGHT


@pytest.mark.parametrize("store", ["postgres", "memory"])
@pytest.mark.parametrize("budget", [_AMPLE, _TIGHT], ids=["ample", "tight"])
def test_the_revision_round_still_shows_the_model_the_question(
    monkeypatch: pytest.MonkeyPatch, budget: int, store: str
) -> None:
    """The revision request carries the question however tight the window is."""
    _budget(monkeypatch, budget)
    _grades_by_text(monkeypatch)

    sent, _ = _turn(
        monkeypatch,
        store,
        [{"name": "lookup_evidence", "args": {"query": "tfa"}}, _FLAGGED, _GROUNDED],
        [lookup_evidence],
    )

    assert len(sent) == 3, "the turn did not revise"
    assert runner._REVISION_NOTE in str(sent[-1][-1].content)
    _assert_every_request_sees_the_question(sent, budget)


@pytest.mark.parametrize("store", ["postgres", "memory"])
@pytest.mark.parametrize("budget", [_AMPLE, _TIGHT], ids=["ample", "tight"])
def test_the_job_resume_still_shows_the_model_the_question(
    monkeypatch: pytest.MonkeyPatch, budget: int, store: str
) -> None:
    """The continuation with a job's results carries the question however tight the window is."""
    _budget(monkeypatch, budget)
    monkeypatch.setattr(settings, "mid_turn_resume_enabled", True)

    async def _results(session_id: str, job_ids: list[str], *, timeout_seconds: float) -> Any:
        return {job_ids[0]: {"energy_hartree": -154.1, "log": _EVIDENCE}}

    monkeypatch.setattr(runner, "await_job_results", _results)

    sent, _ = _turn(
        monkeypatch,
        store,
        [{"name": "start_job", "args": {"query": "tfa"}}, "Started.", "The energy is -154.1."],
        [start_job],
    )

    assert len(sent) == 3, "the turn did not resume"
    assert "job-results" in str(sent[-1][-1].content)
    _assert_every_request_sees_the_question(sent, budget)


def test_a_persisted_job_results_note_still_opens_no_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resume's note outlives the turn, so its marker must survive the Postgres checkpointer.

    Otherwise the next turn reads it back as a group of its own, and a later window can cut the
    question that note continues away from it.
    """
    _budget(monkeypatch, _AMPLE)
    monkeypatch.setattr(settings, "mid_turn_resume_enabled", True)

    async def _results(session_id: str, job_ids: list[str], *, timeout_seconds: float) -> Any:
        return {job_ids[0]: {"energy_hartree": -154.1}}

    monkeypatch.setattr(runner, "await_job_results", _results)

    _, thread = _turn(
        monkeypatch,
        "postgres",
        [{"name": "start_job", "args": {"query": "tfa"}}, "Started.", "The energy is -154.1."],
        [start_job],
    )

    humans = [m for m in thread if isinstance(m, HumanMessage)]
    assert any("job-results" in str(m.content) for m in humans), f"no note on {thread}"
    assert [str(m.content) for m in humans if _opens_a_group(m)] == [_QUESTION]
    assert isinstance(thread[-1], AIMessage)
