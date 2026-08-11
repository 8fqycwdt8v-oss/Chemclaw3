"""The graph engine drives the same event contract the MAF engine does (M8).

`api/events.py` is this migration's conformance boundary — "a LangGraph turn either emits the
agreed event stream or the migration is not done". These tests are where that is checked, so most
of them are about *sameness* rather than about LangGraph: the same event types, in the same order,
carrying the same fields, with the same trace left behind for the answer gate to score against.

The interesting assertions are the ones that would pass trivially if written loosely. A test that
asserted "a token event is emitted" would hold against an engine that emitted nothing else, so the
sequence is compared whole; and `test_the_trace_the_answer_gate_reads_is_populated` exists because
every scored property of an answer — grounding, unsupported claims, the citation gate — is
computed from `ToolCallTrace.outputs` and `called_tools` *after* the stream ends. An engine that
emitted a perfect event stream and left that trace empty would grade every answer as fabricated,
which is exactly the failure `docs/archive/live-grounded-2026-08-03.md` records.
"""

import asyncio
from typing import Any, cast

import pytest

from chemclaw.agent.audit import NullAuditSink
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.api.events import HandoffEvent, ToolCallEvent, ToolResultEvent
from chemclaw.api.graph_stream import _agent_of, graph_events
from chemclaw.api.runner_trace import ToolCallTrace
from tests.fakes_langgraph import ScriptedChatModel


class _Usage:
    """The token ledger's shape, recording what the stream fed it."""

    def __init__(self) -> None:
        self.seen: list[Any] = []

    def add(self, usage: Any) -> None:
        """Record one update's usage, whatever the provider reported."""
        self.seen.append(usage)


def _drive(script: list[Any], **kwargs: Any) -> tuple[list[Any], ToolCallTrace, _Usage]:
    """Run one scripted turn through the graph engine and collect everything it produced."""
    trace = ToolCallTrace()
    usage = _Usage()
    signals: list[Any] = []

    async def _run() -> list[Any]:
        graph = build_langgraph_agent(
            ScriptedChatModel(script), audit_sink=NullAuditSink(), **kwargs
        )
        return [
            event
            async for event in graph_events(
                graph,
                "hello",
                config={"configurable": {"thread_id": "t-1"}},
                trace=trace,
                on_signal=signals.append,
                usage=usage,
            )
        ]

    return asyncio.run(_run()), trace, usage


def test_a_scripted_turn_emits_the_same_event_sequence_as_the_maf_engine() -> None:
    """The conformance assertion: call, result, then the answer's tokens — in that order.

    `tests/test_service_events.py` pins the MAF engine's sequence for the equivalent turn; the
    `answer` is assembled by the runner *after* the stream on both engines, so what this module
    owns is everything up to it. The order matters as much as the membership — a surface renders
    the trace as a timeline, and a result announced before its call reads as a tool answering a
    question nobody asked.

    **The `question` between the call and its result is the ordering rule working, not noise.**
    `ask_clarifying_question` records a `QuestionSignal` while it runs, and both engines drain
    signals *before* the content of the update they arrived with — because a tool that ran while
    the model was producing an update ran before the text it then produced (RCH-4/RCH-5). So the
    signal lands after the call that caused it and before the result that closes it, which is the
    truthful transcript order.
    """
    events, _trace, _usage = _drive(
        [{"name": "ask_clarifying_question", "args": {"question": "which route?"}}, "the answer"]
    )
    assert [event.type for event in events] == [
        "tool_call",
        "question",
        "tool_result",
        "token",
    ]


def test_a_tool_call_carries_the_arguments_it_promises() -> None:
    """`ToolCallEvent.arguments` is a documented promise the MAF stream once broke for a year.

    D-138: the field was empty on every call ever emitted, because the reassembly read
    name-and-arguments off a single content that never had both. The graph engine takes calls from
    the `updates` stream, where they arrive whole — so this is the property that must not be lost
    by the engine that made it easy.
    """
    events, _trace, _usage = _drive(
        [{"name": "ask_clarifying_question", "args": {"question": "which route?"}}, "done"]
    )
    call = next(event for event in events if isinstance(event, ToolCallEvent))
    assert call.tool == "ask_clarifying_question"
    assert "which route?" in call.arguments


def test_a_tool_result_is_reported_under_the_name_of_the_call_it_answers() -> None:
    """A result carries no tool name of its own; it is matched to its call by id.

    Getting this wrong is not cosmetic — `ToolResultEvent.tool` is what a surface labels the value
    with, so a mismatch attributes one tool's number to another.
    """
    events, _trace, _usage = _drive(
        [{"name": "ask_clarifying_question", "args": {"question": "x"}}, "done"]
    )
    result = next(event for event in events if isinstance(event, ToolResultEvent))
    assert result.tool == "ask_clarifying_question"
    assert result.preview


def test_the_trace_the_answer_gate_reads_is_populated() -> None:
    """The stream is not the only output — the trace it leaves is what grades the answer.

    `build_answer_event` scores grounding against `trace.outputs` and `trace.called_tools`. An
    engine that emitted every event correctly and left these empty would route every answer to
    review as unsupported, and the events would give no hint why.
    """
    _events, trace, _usage = _drive(
        [{"name": "ask_clarifying_question", "args": {"question": "x"}}, "done"]
    )
    assert trace.called_tools == ["ask_clarifying_question"]
    assert len(trace.outputs) == 1


def test_a_turn_with_no_tool_call_emits_only_its_tokens() -> None:
    """The plain case, asserted because the interesting ones would hide a regression in it."""
    events, trace, _usage = _drive(["just an answer"])
    assert [event.type for event in events] == ["token"]
    assert trace.called_tools == []


def test_the_token_stream_carries_prose_and_never_a_tool_call_fragment() -> None:
    """Tool calls are read from `updates`, not from the token stream, and this is why.

    A provider streams a call's arguments as `tool_call_chunks` on the same message chunks that
    carry prose. Folding those into `TokenEvent` would stream raw JSON to the chemist as if it
    were the answer — and reassembling them instead is the path that produced two live defects
    (D-138, and the OpenAI-Responses case that announced ten `tool_call` events for one call). The
    scripted model emits a real call fragment, so this asserts the fragment does *not* surface.
    """
    events, _trace, _usage = _drive(
        [{"name": "ask_clarifying_question", "args": {"question": "which route?"}}, "the answer"]
    )
    tokens = "".join(event.text for event in events if event.type == "token")
    assert tokens == "the answer"
    assert "ask_clarifying_question" not in tokens


@pytest.mark.parametrize(
    ("namespace", "expected"),
    [
        ((), ""),
        (("evidence:7f3a",), "evidence"),
        (("supervisor:1", "safety:2"), "safety"),
    ],
)
def test_the_agent_attribution_is_read_from_the_subgraph_namespace(
    namespace: tuple[str, ...], expected: str
) -> None:
    """An event's `agent` is the specialist whose subgraph produced it; empty at the root.

    Empty-at-the-root is what makes the field additive: every event emitted before teams existed
    came from the one agent, so a consumer that ignores `agent` reads exactly what it read before.
    """
    assert _agent_of(namespace) == expected


def test_the_handoff_event_round_trips_with_its_discriminator() -> None:
    """The new union member serializes like every other one — `type` first, defaults omitted."""
    assert HandoffEvent(to="safety", reason="hazard check").model_dump() == {
        "type": "handoff",
        "to": "safety",
        "reason": "hazard check",
    }


def test_an_event_from_the_main_agent_carries_no_attribution() -> None:
    """The default path stays byte-identical for an existing consumer.

    Asserted on the wire form rather than on the model, because that is what a surface parses: an
    `agent` key appearing on every event would be a contract change for turns that have no team.
    """
    events, _trace, _usage = _drive(
        [{"name": "ask_clarifying_question", "args": {"question": "x"}}, "done"]
    )
    call = next(event for event in events if isinstance(event, ToolCallEvent))
    assert call.agent == ""
    assert "agent" not in call.model_dump(exclude_defaults=True)


def test_the_runner_serves_a_whole_turn_on_the_graph_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The M8 acceptance: `run_turn` drives a compiled graph and produces a real turn.

    Everything the runner owns is engine-neutral by construction — the budget ledger, the
    cancellation teardown, the rollback gate, the metrics, the answer assembly — so this asserts
    the one thing that had to be built: that the graph reaches all of it and comes out the far end
    as an `AnswerEvent` carrying the text the model streamed.

    It was written when `run_turn` still took a MAF agent alongside the graph factory, and passed
    `object()` in that slot so a branch failing to take could not be masked by an argument that
    would have worked. The slot is gone; what it was guarding is now structural.
    """
    from chemclaw.api.runner import run_turn

    class _Session:
        """The two attributes `run_turn` reads off a session on this path."""

        session_id = "s-graph-1"
        state: dict[str, Any] = {}

    def _factory(**_kwargs: Any) -> Any:
        return build_langgraph_agent(
            ScriptedChatModel(["the assembled answer"]), audit_sink=NullAuditSink()
        )

    async def _run() -> list[Any]:
        return [
            event
            async for event in run_turn(
                _Session(),  # type: ignore[arg-type]
                "what is the pKa?",
                connectors=[],
                graph_factory=_factory,
            )
        ]

    events = asyncio.run(_run())
    kinds = [event.type for event in events]
    assert kinds[-1] == "answer", kinds
    answer = events[-1]
    assert answer.text == "the assembled answer"
    assert "token" in kinds


def test_the_cap_marks_the_watch_the_runner_actually_reads() -> None:
    """One reader answers for both engines — the wiring that was missing.

    `chemclaw.api.runner` decides whether to emit `loop_cap_reached` and increment
    `chemclaw_turn_loop_caps_total` by calling `loop_hit_cap()`, which reads the ambient watch.
    Only `observe_loop_cap` — the MAF half — ever wrote it, while the graph engine kept its count
    in `model_calls`, which the runner never reads back. So a capped turn on this engine was
    externally identical to a finished one: the exact defect `lg_loop_cap` was written to fix,
    reintroduced one layer up.

    Driven at a cap of 1 because that is the value MAF's inference was blind at, and asserted on
    **both** records: the state count `loop_capped` reads, and the watch the runner reads. Before
    the fix the first held and the second did not, which is precisely how the defect hid.
    """
    from chemclaw.agent.loop_cap import (
        begin_loop_watch,
        end_loop_watch,
        lg_loop_cap,
        loop_capped,
        loop_hit_cap,
    )
    from chemclaw.core.config import settings

    original = settings.harness_max_loop_iterations
    settings.harness_max_loop_iterations = 1
    token = begin_loop_watch()
    try:
        assert not loop_hit_cap(), "the watch starts unmarked"
        # `@before_model` wraps it in a middleware object, so the hook is what runs per call.
        first = lg_loop_cap.before_model(cast(Any, {"model_calls": 0}), cast(Any, None))
        assert first == {"model_calls": 1}, "the first model call is not a cap"
        assert not loop_hit_cap(), "counting is not capping"

        capped = lg_loop_cap.before_model(cast(Any, {"model_calls": 1}), cast(Any, None))
        assert capped == {"jump_to": "end"}, capped
        assert loop_capped({"model_calls": 1}), "the state record missed the cap"
        assert loop_hit_cap(), "the cap fired but the runner's own reader never saw it"
    finally:
        end_loop_watch(token)
        settings.harness_max_loop_iterations = original


def test_a_capped_turn_actually_stops_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """**The test the unit test could not be.** A decision is not a guard until it is connected.

    `lg_loop_cap` counted correctly, decided correctly, and returned `{"jump_to": "end"}` on every
    call past the limit — and the loop kept going, because `before_model`'s conditional edge is
    built from the hook's `can_jump_to` declaration and there was none. Measured at a cap of 1: the
    hook fired five times, said "end" four times, and four further model/tool round-trips completed
    anyway. The same shape as the `to_regclass` guard M6 nearly shipped — a check that runs,
    answers correctly, and is wired to nothing.

    So this drives a whole turn through `run_turn` and asserts the two things a caller can observe:
    the loop **stopped** (one tool call, not the script's four), and the turn **said so**
    (`loop_cap_reached`, which is what lets a surface mark the answer partial). A unit test on the
    hook proves neither, and passed throughout.

    A cap of 1 deliberately: it is the value MAF's inference was blind at, and the value at which
    this defect is unambiguous.
    """
    from chemclaw.api.runner import run_turn
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "harness_enabled", True)
    monkeypatch.setattr(settings, "harness_max_loop_iterations", 1)

    class _Session:
        session_id = "s-capped"
        state: dict[str, Any] = {}

    def _factory(**_kwargs: Any) -> Any:
        # Four tool calls scripted; a working cap must consume exactly one of them.
        return build_langgraph_agent(
            ScriptedChatModel(
                [
                    {"name": "ask_clarifying_question", "args": {"question": f"q{i}"}}
                    for i in range(4)
                ]
            ),
            audit_sink=NullAuditSink(),
        )

    async def _run() -> list[Any]:
        return [
            event
            async for event in run_turn(
                _Session(),  # type: ignore[arg-type]
                "go",
                connectors=[],
                graph_factory=_factory,
            )
        ]

    events = asyncio.run(_run())
    kinds = [event.type for event in events]
    assert kinds.count("tool_call") == 1, f"the loop did not stop at the cap: {kinds}"
    codes = [event.code for event in events if event.type == "error"]
    assert "loop_cap_reached" in codes, kinds
