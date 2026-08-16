"""The graph drives the event contract `api/events.py` declares (M8).

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
from langchain_core.messages import AIMessage, ToolMessage

from chemclaw.agent.audit import NullAuditSink
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.api.events import HandoffEvent, ToolCallEvent, ToolResultEvent
from chemclaw.api.graph_stream import _custom_event, graph_events
from chemclaw.api.runner_trace import ToolCallTrace
from chemclaw.core.turn_signals import _KEY as _SIGNAL_KEY
from chemclaw.core.turn_signals import HandoffSignal
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


def test_a_scripted_turn_emits_the_declared_event_sequence() -> None:
    """The conformance assertion: call, result, then the answer's tokens — in that order.

    The `answer` is assembled by the runner *after* the stream, so what this module owns is
    everything up to it. The order matters as much as the membership — a surface renders
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


def test_an_unrouted_turn_attributes_nothing() -> None:
    """Empty-at-the-root is what makes `agent` additive, and it is asserted on a real turn.

    This replaces a parametrized check on `_agent_of(namespace)` — a helper that has been deleted.
    It mapped `("evidence:7f3a",) → "evidence"` and passed for months against a namespace shape the
    engine never produces: `SubAgentMiddleware` runs a specialist inside the `task` tool, so the
    only frame is the parent's tool node and every specialist event was attributed to `"tools"`.
    The lesson is the assertion's *input*, not its logic — hand-written fixtures cannot establish
    what a graph emits, so the turn is driven and the events are read off it.
    """
    events, _trace, _usage = _drive(
        [{"name": "ask_clarifying_question", "args": {"question": "which route?"}}, "done"]
    )
    assert {event.agent for event in events if hasattr(event, "agent")} == {""}


def test_the_handoff_event_round_trips_with_its_discriminator() -> None:
    """The new union member serializes like every other one — `type` first, defaults omitted."""
    assert HandoffEvent(to="safety", reason="hazard check").model_dump() == {
        "type": "handoff",
        "to": "safety",
        "reason": "hazard check",
    }


@pytest.mark.parametrize(
    ("signal", "expected"),
    [
        (HandoffSignal(to="safety", reason="hazard check"), ("safety", "hazard check")),
        (HandoffSignal(to=""), ("", "")),
    ],
)
def test_a_handoff_signal_becomes_the_handoff_event_in_both_directions(
    signal: HandoffSignal, expected: tuple[str, str]
) -> None:
    """Entering a specialist and leaving it are the same event with a different `to`.

    Pinned as a pair because the hand back is the half that looks like a bug: `to=""` is a
    *declared* value — "control returned to the agent above" — and a reader that treated it as an
    unset field would drop exactly the event that closes a specialist's span in the trace.
    """
    event = _custom_event({_SIGNAL_KEY: signal}, lambda _s: None)
    assert isinstance(event, HandoffEvent)
    assert (event.to, event.reason) == expected


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


@pytest.mark.parametrize("cap", [1, 2, 3])
@pytest.mark.parametrize("jumping_after_model", [False, True])
def test_the_cap_stops_the_loop_at_exactly_its_limit(
    cap: int, jumping_after_model: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model runs exactly `cap` times, and both records say the cap fired.

    **Asserted against a compiled graph, not against the hook.** That is the module's own lesson:
    the hook counted correctly, decided correctly and returned `{"jump_to": "end"}` while the graph
    looped on regardless, because `before_model`'s conditional edge is built from the hook's
    `can_jump_to` declaration. A unit test on the hook passed throughout.

    **`jumping_after_model` is the case that sent the delegation to upstream back.** M14 replaced
    this hook with a `ModelCallLimitMiddleware` subclass, which counts in `after_model`; hooks there
    run in reverse list order, so a gate jumping to `model` ran first and short-circuited the
    increment — measured then at 2, 3, 4, 5 model calls for 0, 1, 2, 3 revision rounds against a cap
    of 2. The challenge panel's revision gate was exactly such a gate. Counting in `before_model` is
    what makes
    the count unskippable, and this parameter is what proves it: the version of this test that
    attached no other `after_model` middleware could not see the defect at all.
    """
    from langchain.agents import create_agent
    from langchain.agents.middleware import after_model, wrap_model_call
    from langchain_core.tools import tool

    from chemclaw.agent.loop_cap import (
        begin_loop_watch,
        end_loop_watch,
        enforce_loop_cap,
        loop_capped,
        loop_hit_cap,
    )
    from chemclaw.agent.state import ChemclawState
    from chemclaw.core.config import settings

    # The hook reads the limit from config rather than taking one, so the case has to set it.
    monkeypatch.setattr(settings, "harness_max_loop_iterations", cap)

    calls = {"n": 0}
    jumps = {"left": 2 if jumping_after_model else 0}

    @wrap_model_call
    def _count(request: Any, handler: Any) -> Any:
        """Count the calls that actually reach the model — a jump to `end` skips this."""
        calls["n"] += 1
        return handler(request)

    @after_model(can_jump_to=["model"])
    def _revise(state: Any, runtime: Any) -> dict[str, Any] | None:
        """Stand in for the challenge gate: jump back to the model a bounded number of times."""
        if jumps["left"] <= 0:
            return None
        jumps["left"] -= 1
        return {"jump_to": "model"}

    @tool
    def spin() -> str:
        """A tool that always invites another round, so only the cap can stop the loop."""
        return "again"

    middleware: list[Any] = [enforce_loop_cap, _count]
    if jumping_after_model:
        # *After* the cap in the list, which is where `build_langgraph_agent` puts the challenge
        # gate — `_harness_middleware` first, `_challenge_middleware` after it. `after_model` hooks
        # run in reverse list order, so this one runs *first* and its jump short-circuits everything
        # behind it. That is the arrangement that skipped an `after_model` counter's increment;
        # getting it the other way round reproduces nothing, which is worth knowing.
        middleware.append(_revise)

    graph = create_agent(
        model=ScriptedChatModel(script=[{"name": "spin", "args": {}} for _ in range(cap + 20)]),
        tools=[spin],
        state_schema=ChemclawState,
        middleware=cast(Any, middleware),
    )

    token = begin_loop_watch()
    try:
        assert not loop_hit_cap(), "the watch starts unmarked"
        final = graph.invoke(
            cast(Any, {"messages": [("user", "go")]}), cast(Any, {"recursion_limit": 200})
        )
        assert calls["n"] == cap, (
            f"the loop ran {calls['n']} model calls against a cap of {cap}"
            + (" — an after_model jump skipped the count" if jumping_after_model else "")
        )
        assert loop_capped(final), "the state record missed the cap"
        assert loop_hit_cap(), "the cap fired but the runner's own reader never saw it"
        assert not loop_capped({}), "an unmarked state read as capped"
    finally:
        end_loop_watch(token)


def test_a_capped_turn_actually_stops_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """**The test the unit test could not be.** A decision is not a guard until it is connected.

    The first-party cap counted correctly, decided correctly, and returned `{"jump_to": "end"}` on
    every call past the limit — and the loop kept going, because `before_model`'s conditional edge
    is built from the hook's `can_jump_to` declaration and there was none. Measured at a cap of 1:
    the hook fired five times, said "end" four times, and four further model/tool round-trips
    completed anyway. The same shape as the `to_regclass` guard M6 nearly shipped — a check that
    runs, answers correctly, and is wired to nothing.

    So this drives a whole turn through `run_turn` and asserts the two things a caller can observe:
    the loop **stopped** (one tool call, not the script's four), and the turn **said so**
    (`loop_cap_reached`, which is what lets a surface mark the answer partial). A unit test on the
    hook proves neither, and passed throughout.

    A cap of 1 deliberately: it is the value the inference this replaced was blind at, and the
    value at which this defect is unambiguous.
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


def test_a_failed_tool_call_produces_one_event_and_no_evidence() -> None:
    """`events.py` calls the result/failure pair exhaustive. It was not, for every failed call.

    `agent/tool_authz.answered_failure` rewrites a returned failure's `status` to `"success"` before
    the stream sees it — deliberately, so a provider does not read `is_error` as an invitation to
    retry — and its docstring names this module as the reader that therefore needs a
    status-independent test of "did this call fail". This module kept reading `status`.

    Two things went wrong, and the second is the one that changes an answer. A failed call emitted
    `tool_failed` *and* `tool_result`, so a consumer had to choose which to believe. And
    `trace.returned` appends to `ToolCallTrace.outputs`, which is the corpus `score_answer` grades
    an answer's grounding against — so "Error: nope is not a valid tool, try one of [...]" was fed
    to the citation gate as though it were something a tool had retrieved.

    Driven through a real compiled graph rather than a hand-built `ToolMessage`, because the whole
    defect is a disagreement between what the engine emits and what this module expected.
    """
    events, trace, _ = _drive([{"name": "definitely_not_a_tool", "args": {}}, "done"])

    kinds = [event.type for event in events if event.type in {"tool_failed", "tool_result"}]
    assert kinds == ["tool_failed"], (
        f"a failed call must produce exactly one of the pair, got {kinds}"
    )
    assert not trace.outputs, (
        "a failed call left its error text in the corpus the answer gate scores grounding "
        f"against: {trace.outputs}"
    )


def test_work_from_below_the_root_is_marked_and_its_plan_withheld() -> None:
    """`agent=""` means the main agent, so emitting a helper's work that way is a false statement.

    `agent` is threaded from the handoff pair, and nothing has raised a handoff since the specialist
    team was deleted — so it is permanently empty. `updates` payloads from a nested Pregel were then
    handled identically to the root's: a helper's tool calls and results joined
    `ToolCallTrace.outputs` and the parent session's fetchable refs indistinguishably from the
    supervisor's own work, and its `write_todos` surfaced as a root `PlanEvent` that *replaced* the
    supervisor's. Under `harness_autonomy="plan_only"` that is the checklist a chemist approves.

    **What this covers and what it does not.** The messages and the update shape are the
    engine's own (`AIMessage`/`ToolMessage`, and the `todos` key `TodoListMiddleware` writes), so
    the branch is
    driven with real types. What is *not* driven end-to-end is a genuine nested subagent emitting
    them — the scripted model cannot stand in for a helper's own model. The caller's namespace test
    is one line (`bool(namespace)`) and the token branch above has applied the same rule since M9.
    """
    from chemclaw.api.graph_stream import _from_update

    update = {
        "helper": {
            "messages": [
                AIMessage(content="", tool_calls=[{"name": "find_notes", "args": {}, "id": "c-9"}]),
                ToolMessage(content="two notes", tool_call_id="c-9"),
            ],
            "todos": [{"content": "the helper's own step", "status": "pending"}],
        }
    }

    async def _collect(**kwargs: Any) -> list[Any]:
        trace = ToolCallTrace()
        return [event async for event in _from_update(update, **kwargs, trace=trace, todos=[])]

    below = asyncio.run(_collect(agent="subagent", emit_plan=False))
    assert not [event for event in below if event.type == "plan"], (
        "a helper's todo list was emitted as the turn's plan; `PlanEvent` carries no `agent` "
        "field, so it cannot say whose it is and must not be shown as the supervisor's"
    )
    marked = {event.agent for event in below if event.type in {"tool_call", "tool_result"}}
    assert marked == {"subagent"}, (
        "work from below the root must not arrive attributed to the main agent"
    )

    root = asyncio.run(_collect(agent="", emit_plan=True))
    assert [event.type for event in root if event.type == "plan"] == ["plan"], (
        "the root's own plan must still be emitted, or this gate has simply turned plans off"
    )
