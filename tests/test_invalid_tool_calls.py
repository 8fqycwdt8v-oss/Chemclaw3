"""An unparseable tool call, driven through a **compiled graph** rather than through the hook.

`agent/model_calls.py` is the mechanism and
`D-2026-08-30-an-unparseable-tool-call-is-an-ordinary-tool-failure` is the decision:
`PromoteInvalidToolCalls` moves the call from `AIMessage.invalid_tool_calls` onto `tool_calls`
behind a sentinel, and `refuse_unparsed_arguments` — innermost of the governance chain — refuses it
before the body runs. `tests/test_agent_observability_model.py` proves what each half *decides* by
calling the hooks directly. That is the right shape for a decision and it cannot establish that the
decision is connected to anything, which is the property `tests/test_state_channels.py` exists for
after three defects in one week where a hook returned the right value into a graph that dropped it.

**Everything this mechanism is worth is only observable in the graph**, because the entire design
is that a promoted call becomes an *ordinary* failing tool call:

- the audit row, the `tool_failed` carrying the model's own call id, and the `ToolMessage` the
  model reads are produced by middleware this module never touches;
- the tool body must not be entered — and the eleven in-process tools with no required argument
  would satisfy their own schema if the promotion dropped the malformed document, so this is a
  property of the composed chain rather than of either half;
- a valid call issued beside a broken one must still run, which no hook-level assertion can show;
- the model's correction is an ordinary graph iteration, so the loop cap counts it.

The defect itself is asserted the same way, as a **behavioural diff**: the identical script through
a graph *without* the promotion ends with prose and no tool call, which is
`D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed` reproduced rather than described.
"""

import asyncio
import inspect
from typing import Any, cast

import pytest
from langchain.agents import create_agent
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import tool

from chemclaw.agent.audit import AuditEvent, AuditSink, make_audit_middleware
from chemclaw.agent.langgraph_agent import tool_call_middleware
from chemclaw.agent.loop_cap import enforce_loop_cap, loop_capped
from chemclaw.agent.model_calls import PromoteInvalidToolCalls
from chemclaw.agent.profiles import AgentProfile
from chemclaw.agent.state import ChemclawState
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from chemclaw.core.turn_signals import _KEY as SIGNAL_KEY
from chemclaw.core.turn_signals import ToolFailureSignal

_ANSWER = "pKa 4.2"

#: What the tool bodies append when they are entered. A module-level list rather than a fixture
#: because the assertion that matters is an *absence* — a body that ran leaves a mark here, and a
#: spy the test constructs could be wired up wrongly and prove nothing.
_ENTERED: list[str] = []


@tool
def predict_pka(smiles: str) -> str:
    """Return this corpus's pKa for `smiles` (a double — the tool node is what is under test)."""
    _ENTERED.append(f"predict_pka({smiles})")
    return f"{_ANSWER} for {smiles}"


@tool
def find_notes(text: str) -> str:
    """A second tool, so a reply can carry a valid call beside a broken one."""
    _ENTERED.append(f"find_notes({text})")
    return f"notes about {text}"


@tool
def list_watches() -> str:
    """A tool with **no required argument** — the trap a promotion carrying `{}` would spring.

    Named for the real one in `agent/subscriptions.py`; a double here because the real body calls
    `require_actor()` and would raise off the request path, which would make "the body did not run"
    unfalsifiable.
    """
    _ENTERED.append("list_watches")
    return "no watches"


class _StreamingModel(GenericFakeChatModel):
    """A model that streams a scripted reply per call, tool-call fragments and all.

    Not `tests/fakes_langgraph.ScriptedChatModel`: that fake emits only *valid* tool-call
    arguments, and the whole subject here is what LangChain does with arguments that do not parse.
    Streaming rather than returning whole messages is the point — `stream_mode="messages"` is what
    a chemist receives, and the streamed path is the one that reaches production, where the
    provider reports `error=None` and the malformed document is the only field that survives.
    """

    script: list[dict[str, Any]] = []
    seen: list[list[BaseMessage]] = []

    def __init__(self, script: list[dict[str, Any]], **kwargs: Any) -> None:
        """Hold the script; `messages` is unused because `_stream` is fully overridden."""
        super().__init__(messages=iter([]), **kwargs)
        object.__setattr__(self, "_step", 0)
        self.script = script
        self.seen = []

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Accept the binding `create_agent` performs on every request and keep the script."""
        return self

    def _generate(
        self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        """The non-streaming path, as the sum of the same chunks — one script, two entry points.

        `create_agent` calls `ainvoke` rather than `astream` on any turn nothing is streaming
        (`graph.ainvoke`, and the model node's own choice), and a double that only overrode
        `_stream` fell through to `GenericFakeChatModel`'s exhausted iterator — a `StopIteration`
        the executor converts into a bare `RuntimeError`, which reads as a graph defect. Summing
        the chunks keeps the *shape* the streamed path produces, which is the whole subject here:
        `AIMessageChunk.__add__` is what runs the argument fragments through `parse_partial_json`
        and decides which field the call lands on.
        """
        chunks = list(self._stream(messages, stop, run_manager, **kwargs))
        merged = chunks[0].message
        for chunk in chunks[1:]:
            merged = merged + chunk.message
        return ChatResult(generations=[ChatGeneration(message=merged)])

    def _stream(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any):  # type: ignore[no-untyped-def]
        """Record the thread, then stream the next scripted reply: prose, then one call each.

        `calls` is a list of `(tool name, argument document)` because a reply carrying a broken
        call *beside* a valid one is a case with its own outcome, and it cannot be scripted at all
        if a reply may hold only one call.
        """
        self.seen.append(list(messages))
        step = object.__getattribute__(self, "_step")
        object.__setattr__(self, "_step", step + 1)
        reply = self.script[min(step, len(self.script) - 1)]
        yield ChatGenerationChunk(message=AIMessageChunk(content=reply["text"]))
        for index, (name, args) in enumerate(reply.get("calls", ())):
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": name,
                            "args": args,
                            "id": f"call-{step}-{index}",
                            "index": index,
                            "type": "tool_call_chunk",
                        }
                    ],
                )
            )


class _Recording(AuditSink):
    """An audit sink that keeps what it was given, so a test can read the trail."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> None:
        """Keep the row."""
        self.events.append(event)


def _governed_graph(model: Any, sink: AuditSink, *, extra: list[Any] | None = None) -> Any:
    """A compiled agent carrying the **production** middleware chain over doubles for tools.

    `tool_call_middleware` is the composed chain a turn really runs — both converters, the
    framing, the result bound, the announcer, the trail and every gate — and
    `tests/test_middleware_order.py` is what pins that this list is the one
    `build_langgraph_agent` hands to `create_agent`. Building through that function instead would
    bring the whole in-process registry, and none of the eleven no-required-argument tools in it
    has a body a test can watch: `list_watches` calls `require_actor()` and raises off the request
    path, which would make the absence assertion unfalsifiable.
    """
    audit = make_audit_middleware(correlation_id="test-correlation", actor="tester", sink=sink)
    return create_agent(
        model=model,
        tools=[predict_pka, find_notes, list_watches],
        state_schema=ChemclawState,
        middleware=[
            *(extra or []),
            PromoteInvalidToolCalls(),
            *tool_call_middleware(audit, AgentProfile(name="default")),
        ],
    )


async def _drive(graph: Any) -> tuple[list[ToolMessage], list[ToolFailureSignal], str]:
    """Run one turn to completion, draining the three channels a chemist's turn actually uses.

    The `custom` mode is drained for the reason `core/turn_signals._emit` states: it resolves
    LangGraph's writer off the ambient config and **drops the signal in silence** where there is
    none, so a test that called the announcer directly would pass over a chain whose announcements
    go nowhere. Only a compiled graph publishes on that channel.
    """
    results: list[ToolMessage] = []
    signals: list[ToolFailureSignal] = []
    streamed: list[str] = []
    stream = graph.astream(
        {"messages": [("user", "what is the pKa of ethanol")]},
        cast(Any, {"recursion_limit": 20}),
        stream_mode=["messages", "updates", "custom"],
    )
    async for emitted in stream:
        # `astream` with a list of modes yields `(mode, payload)`; the tuple arity is the coupling
        # `tests/test_upstream_surface.py` already names, so it is read here rather than re-typed.
        mode, payload = cast(tuple[str, Any], emitted)
        if mode == "messages":
            chunk, _metadata = payload
            if isinstance(chunk, AIMessageChunk) and chunk.text:
                streamed.append(chunk.text)
        elif mode == "custom":
            signal = (payload or {}).get(SIGNAL_KEY)
            if isinstance(signal, ToolFailureSignal):
                signals.append(signal)
        else:
            for update in (payload or {}).values():
                for message in (update or {}).get("messages", []) or []:
                    if isinstance(message, ToolMessage):
                        results.append(message)
    return results, signals, "".join(streamed)


@pytest.fixture(autouse=True)
def _clear_entered() -> Any:
    """Every case reads `_ENTERED` as an absence, so it starts empty for each."""
    _ENTERED.clear()
    yield
    _ENTERED.clear()


def test_langchains_converter_is_where_the_call_goes_missing() -> None:
    """The upstream fact the whole fix rests on, asserted against upstream's own converter.

    A truncated argument document yields `tool_calls: []` and a populated `invalid_tool_calls`. The
    agent loop iterates `tool_calls`, so the call is gone before any first-party code sees it —
    which is why the fix moves it back rather than trying to prevent the emission.

    Pinned here for `tests/test_upstream_surface.py`'s reason: if LangChain ever routed a truncated
    call back onto `tool_calls`, or dropped `error`, this turns red instead of the promotion
    quietly becoming a middleware with nothing to find.
    """
    from langchain_openai.chat_models.base import _convert_dict_to_message

    message = _convert_dict_to_message(
        {
            "role": "assistant",
            "content": "I will compute that.",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "predict_pka", "arguments": '{"smiles": "CC'},
                }
            ],
        }
    )
    assert isinstance(message, AIMessage)
    assert message.tool_calls == [], "a truncated call is not on the field the agent iterates"
    assert len(message.invalid_tool_calls) == 1
    assert message.invalid_tool_calls[0]["name"] == "predict_pka"
    assert "not valid JSON" in str(message.invalid_tool_calls[0]["error"])


def test_without_the_promotion_the_turn_proceeds_as_though_no_tool_were_wanted() -> None:
    """The defect, reproduced in a compiled graph: prose is served and the tool never runs.

    This is the baseline the next test is a diff against, and it is what
    `D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed` names — the model announced a
    lookup, the lookup silently did not happen, and the turn ended looking finished. Without the
    prose the same turn ends as `empty_answer` instead; with it, nothing anywhere says a call was
    dropped.
    """
    # `'{"smiles": }'` rather than a *truncated* document, because a truncation never reaches the
    # field under test: `parse_partial_json` completes any prefix of a valid object, so even
    # `'{oops'` arrives on `tool_calls` with `args={}` and the tool runs — measured, and the
    # boundary the last test in this file pins.
    model = _StreamingModel(
        [{"text": "I will look that up.", "calls": [("predict_pka", '{"smiles": }')]}]
    )
    graph = create_agent(model=model, tools=[predict_pka], state_schema=ChemclawState)
    results, signals, streamed = asyncio.run(_drive(graph))

    assert len(model.seen) == 1, "the graph asked once and accepted the answer"
    assert results == [], "no tool ran and no failure was reported"
    assert signals == []
    assert streamed == "I will look that up."
    assert _ENTERED == []


def test_a_promoted_call_crosses_the_whole_governance_chain() -> None:
    """The gain over every earlier design, stated as the four records that used to be missing.

    Each was previously absent or hand-built, and each is now produced by machinery this mechanism
    does not touch:

    - **an audit row**, under the `error` outcome — three ADRs argued that synthesising one would
      put a call that never ran into the trail, which was true of a call the tool chain never saw
      and is not true of one it refuses;
    - **a `tool_failed` carrying the model's own call id**, so a consumer pairs it with the
      `tool_call` event rather than with the tool *name*. The design this replaced dropped the id,
      which is what forced a suppression guard into `api/graph_stream.py`;
    - **`reason=None`**, because `UnparsedArguments` is deliberately not in `refusal_reason`'s
      table: a document that will not parse is a fault, not one of the five gates;
    - **a `ToolMessage` the model can act on**, rather than the call vanishing.

    And the tool body is **not entered**, which is the property the sentinel exists for.
    """
    sink = _Recording()
    model = _StreamingModel(
        [
            {"text": "", "calls": [("predict_pka", '{"smiles": }')]},
            {"text": f"I could not run that. {_ANSWER} is not something I can look up."},
        ]
    )
    results, signals, _streamed = asyncio.run(_drive(_governed_graph(model, sink)))

    assert _ENTERED == [], "the tool body ran on arguments the model never successfully expressed"

    rows = [event for event in sink.events if event.tool == "predict_pka"]
    assert [row.outcome for row in rows] == ["error"], f"trail: {[e.tool for e in sink.events]}"

    assert [(s.tool, s.call_id, s.reason) for s in signals] == [
        ("predict_pka", "call-0-0", None)
    ], "the announcement must carry the model's own id and classify as a fault, not a gate"

    assert [m.tool_call_id for m in results] == ["call-0-0"]
    assert "not valid JSON" in str(results[0].content)
    assert '{"smiles": }' in str(results[0].content), "the model is told what it actually sent"


def test_a_valid_call_beside_a_broken_one_still_runs() -> None:
    """The sibling survives, which is the case the discard-and-retry design got wrong.

    That design discarded the whole reply to ask again, so a turn asking for two tools and
    mis-serialising one ran neither — and the successful sibling's result was lost with it. Here
    the two calls are independent: one executes, one is refused, and the model sees both outcomes.
    """
    sink = _Recording()
    model = _StreamingModel(
        [
            {
                "text": "",
                "calls": [
                    ("predict_pka", '{"smiles": }'),
                    ("find_notes", '{"text": "buchwald"}'),
                ],
            },
            {"text": "Here is what I found."},
        ]
    )
    results, signals, _streamed = asyncio.run(_drive(_governed_graph(model, sink)))

    assert _ENTERED == ["find_notes(buchwald)"], "the valid call must run"
    by_id = {m.tool_call_id: str(m.content) for m in results}
    assert "notes about buchwald" in by_id["call-0-1"]
    assert "not valid JSON" in by_id["call-0-0"]
    assert [s.call_id for s in signals] == ["call-0-0"], "only the broken call is announced"


def test_a_promotion_cannot_execute_a_tool_that_needs_no_arguments() -> None:
    """The trap the sentinel exists for, and the count that makes it worth a sentinel.

    A promotion that dropped the malformed document and passed `{}` would satisfy the schema of
    every tool with no required argument, and the tool would *execute* on a request the model never
    successfully expressed. The count is asserted against the live registry rather than quoted from
    a docstring, so the claim in `agent/model_calls.py` has a producer: a tool added next year that
    takes no required argument widens this trap, and this number moving is the notice.
    """
    from chemclaw.agent.chemclaw_agent import _capability_tools

    registry = _capability_tools()
    no_required = [
        fn.__name__
        for fn in registry
        if not [
            name
            for name, parameter in inspect.signature(fn).parameters.items()
            if parameter.default is inspect.Parameter.empty and name != "self"
        ]
    ]
    assert len(no_required) >= 10, f"only {len(no_required)} of {len(registry)}: {no_required}"

    sink = _Recording()
    model = _StreamingModel(
        [
            {"text": "", "calls": [("list_watches", "not json at all")]},
            {"text": "I could not list them."},
        ]
    )
    results, signals, _streamed = asyncio.run(_drive(_governed_graph(model, sink)))

    assert _ENTERED == [], (
        "a tool with no required argument executed on a promoted call: the malformed document is "
        "not reaching the sentinel, so the promotion satisfies the schema instead of failing it"
    )
    assert [s.tool for s in signals] == ["list_watches"]
    assert "not valid JSON" in str(results[0].content)


def test_the_model_corrects_inside_its_own_loop_and_the_correction_runs() -> None:
    """The property the three hand-built substitutes existed to fake, obtained for free.

    The designs this replaces asked the model again from inside `wrap_model_call`, which is outside
    every bound the graph has — so each needed a hand-rolled "never a loop" ceiling, and neither
    the loop cap nor the spend cap could see the extra call. Here the correction is an ordinary
    graph iteration: the model reads the `ToolMessage`, re-issues the call, and it runs. That the
    iteration is *counted* is the next test — the two are split because a correction that ran and
    a correction the cap could see are different claims, and the old design satisfied only the
    first.
    """
    sink = _Recording()
    model = _StreamingModel(
        [
            {"text": "", "calls": [("predict_pka", '{"smiles": }')]},
            {"text": "", "calls": [("predict_pka", '{"smiles": "CCO"}')]},
            {"text": f"The answer is {_ANSWER}."},
        ]
    )
    graph = _governed_graph(model, sink, extra=[enforce_loop_cap])
    before = METRICS.value("chemclaw_invalid_tool_calls_total")
    results, signals, streamed = asyncio.run(_drive(graph))

    assert _ENTERED == ["predict_pka(CCO)"], "the model's own correction must run"
    assert [s.call_id for s in signals] == ["call-0-0"], "only the first attempt failed"
    assert [m.tool_call_id for m in results] == ["call-0-0", "call-1-0"]
    assert streamed.endswith(f"The answer is {_ANSWER}.")
    assert METRICS.value("chemclaw_invalid_tool_calls_total") == before + 1
    assert 'chemclaw_invalid_tool_calls_total{tool="predict_pka"}' in METRICS.render()


def test_a_corrected_turn_still_hits_the_runaway_cap() -> None:
    """The other half: the correction spends an iteration, so the cap still bounds the turn.

    A cap of 1 rather than the configured default, because the property is that a turn looping on
    malformed arguments is stopped by the *ordinary* guard. Under the design this replaces the
    extra provider call was invisible to `enforce_loop_cap` entirely, which is why that design had
    to carry a ceiling of its own.
    """
    sink = _Recording()
    original = settings.harness_max_loop_iterations
    settings.harness_max_loop_iterations = 1
    try:
        model = _StreamingModel([{"text": "", "calls": [("predict_pka", '{"smiles": }')]}])
        graph = _governed_graph(model, sink, extra=[enforce_loop_cap])
        final = asyncio.run(
            graph.ainvoke({"messages": [("user", "pKa?")]}, cast(Any, {"recursion_limit": 20}))
        )
    finally:
        settings.harness_max_loop_iterations = original

    assert loop_capped(final), "the cap must still stop a turn whose model call was promoted"
    assert len(model.seen) == 1, "the cap ended the run before a second attempt"


def test_a_streamed_truncation_is_completed_by_upstream_and_never_becomes_invalid() -> None:
    """The boundary of the mechanism: streaming never produces an invalid tool call at all.

    `D-2026-08-27-an-unparseable-tool-call-is-a-visible-failure` records the measurement. A tool
    call arriving as `tool_call_chunks` is merged by `AIMessageChunk.__add__`, which parses the
    accumulated document with `parse_partial_json` — and that function *completes* any document
    that is a prefix of a valid object, which is exactly what a cut stream or an exhausted token
    budget leaves behind. So the truncation the `BACKLOG` row named as the production cause lands
    on `tool_calls` as a call with half-written arguments, not on `invalid_tool_calls`, and
    `PromoteInvalidToolCalls` correctly finds nothing to promote.

    Measured against the live lane as well as here: `make live-storm --families F` runs
    `f-malformed-json` (`'{"text": "unterminated'`) through the real front door and the tool
    executes with `text="unterminated"`. Only a document that is *not* a prefix — garbage, a bare
    string, an unbalanced close — reaches the field the promotion reads.

    This is an **absence** assertion, the shape `tests/test_upstream_surface.py` uses for the same
    reason: if upstream stops completing prefixes, these calls start arriving as invalid, the
    promotion begins firing on them, and this test turning red is the signal to re-read the ADR
    rather than to discover the change through behaviour.
    """

    def merged(document: str) -> Any:
        return AIMessageChunk(content="") + AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "name": "predict_pka",
                    "args": document,
                    "id": "call-1",
                    "index": 0,
                    "type": "tool_call_chunk",
                }
            ],
        )

    # A prefix of a valid document: completed, valid, and the tool would run on it.
    prefix = merged('{"smiles": "CC')
    assert prefix.invalid_tool_calls == [], "a streamed truncation is not an invalid tool call"
    assert prefix.tool_calls[0]["args"] == {"smiles": "CC"}, "the cut value is silently completed"
    # Cut before the value: the argument disappears rather than the call.
    assert merged('{"smiles":').tool_calls[0]["args"] == {}

    # Only a non-prefix reaches the field the promotion reads.
    assert merged("not json at all").invalid_tool_calls, "garbage must still be surfaced"
