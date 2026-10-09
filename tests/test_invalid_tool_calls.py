"""An unparseable tool call, driven through a compiled graph rather than through the hook.

`PromoteInvalidToolCalls` (`agent/model_calls.py`) moves a call from
`AIMessage.invalid_tool_calls` onto `tool_calls` behind a sentinel, and `refuse_unparsed_arguments`,
below every deciding gate, refuses it before the body runs.
`tests/test_agent_observability_model.py` tests each hook's decision; what the mechanism is worth is
only observable in the graph, because a promoted call becomes an ordinary failing tool call:

- the audit row, the `tool_failed` carrying the model's call id, and the `ToolMessage` come from
  middleware this module never touches;
- the tool body is not entered (tools with no required argument would accept `{}`);
- a valid call beside a broken one still runs;
- the model's correction is an ordinary graph iteration, counted by the loop cap.

The defect is shown as a behavioural diff: the same script without the promotion ends in prose
with no tool call.
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

    Not `ScriptedChatModel`, which emits only valid arguments. Streaming matters because it is the
    production path, where the provider reports `error=None` and the malformed document is the only
    field that survives.
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
        """The non-streaming path, as the sum of the same chunks: one script, two entry points.

        `create_agent` calls `ainvoke` on turns nothing streams. Summing the chunks keeps the
        streamed shape, since `AIMessageChunk.__add__` is what parses the argument fragments and
        decides which field a call lands on.
        """
        chunks = list(self._stream(messages, stop, run_manager, **kwargs))
        merged = chunks[0].message
        for chunk in chunks[1:]:
            merged = merged + chunk.message
        return ChatResult(generations=[ChatGeneration(message=merged)])

    def _stream(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any):  # type: ignore[no-untyped-def]
        """Record the thread, then stream the next scripted reply: prose, then one call each.

        `calls` is a list so a reply can carry a broken call beside a valid one.
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
        # How the provider says it stopped, on the last chunk: `length` is the output budget running
        # out. A list models a gateway repeating the value on a trailing chunk, which the merge
        # concatenates.
        finishes = reply.get("finish", [])
        for finish in [finishes] if isinstance(finishes, str) else finishes:
            yield ChatGenerationChunk(
                message=AIMessageChunk(content="", response_metadata={"finish_reason": finish})
            )


class _Recording(AuditSink):
    """An audit sink that keeps what it was given, so a test can read the trail."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> None:
        """Keep the row."""
        self.events.append(event)


def _governed_graph(model: Any, sink: AuditSink, *, extra: list[Any] | None = None) -> Any:
    """A compiled agent carrying the production middleware chain over doubles for tools.

    `tool_call_middleware` is the chain a turn runs, and `tests/test_middleware_order.py` pins it as
    the list `build_langgraph_agent` uses. The real registry's no-argument tools have no body a test
    can watch, which would make the absence assertion unfalsifiable.
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
    """Run one turn to completion, draining the three channels a chemist's turn uses.

    `core/turn_signals._emit` drops a signal silently where no LangGraph writer is configured, so
    only a compiled graph shows that the announcements arrive.
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
    """LangChain's converter is where the call goes missing.

    A malformed argument document yields `tool_calls: []` and a populated `invalid_tool_calls`, and
    the agent loop iterates only `tool_calls`. Pinned so an upstream change turns this red rather
    than leaving the promotion with nothing to find.
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
    """Without the promotion the turn proceeds as though no tool were wanted.

    The baseline the next test diffs against: the model announced a lookup, it silently did not
    happen, and the turn ended looking finished.
    """
    # A malformed rather than truncated document: `parse_partial_json` completes any prefix of a
    # valid object, so a truncation lands on `tool_calls` (pinned by the last test in this file).
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
    """A promoted call crosses the whole governance chain.

    Produced by machinery this mechanism does not touch:

    - an audit row under the `error` outcome;
    - a `tool_failed` carrying the model's own call id, so a consumer pairs it with the `tool_call`;
    - `reason=None`, because a document that will not parse is a fault, not one of the gates;
    - a `ToolMessage` the model can act on.

    And the tool body is not entered, which is what the sentinel is for.
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
    """A valid call beside a broken one still runs.

    The two calls are independent: one executes, one is refused, and the model sees both outcomes.
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
    """A promotion cannot execute a tool that needs no arguments.

    Passing `{}` would satisfy every such tool's schema. The count of such tools is read from the
    live registry, so a new one moves the number visibly.
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


def test_no_tool_in_the_registry_declares_the_sentinel_as_a_parameter() -> None:
    """No tool in the registry declares the sentinel as a parameter.

    `refuse_unparsed_arguments` reads the key off the call's args and raises, so a tool declaring it
    would have every call refused.
    """
    from chemclaw.agent.chemclaw_agent import _capability_tools
    from chemclaw.agent.model_calls import _UNPARSED_ARGUMENTS

    offenders = [
        fn.__name__
        for fn in _capability_tools()
        if _UNPARSED_ARGUMENTS in inspect.signature(fn).parameters
    ]
    assert offenders == [], (
        f"{offenders} declare {_UNPARSED_ARGUMENTS!r}, so every call to them would be refused "
        "before the body by `refuse_unparsed_arguments`"
    )


def test_the_model_corrects_inside_its_own_loop_and_the_correction_runs() -> None:
    """The model corrects inside its own loop, and the correction runs.

    The correction is an ordinary graph iteration: the model reads the `ToolMessage`, re-issues the
    call, and it runs. That the iteration is counted is the next test.
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
    """A corrected turn still hits the runaway cap.

    A cap of 1, because the property is that a turn looping on malformed arguments is stopped by the
    ordinary `enforce_loop_cap`.
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
    # One capped call, then the one wrap-up call a graph at the cap is owed to write its answer
    # (`loop_cap.AnswerAtTheCap` is not attached here, so its tools stay on and the next arrival at
    # the cap is what ends the run) — never a third.
    assert len(model.seen) == 2, "the cap did not bound the turn to its call plus one wrap-up"


def test_the_guard_sits_below_every_gate_that_decides_including_the_plan_gate() -> None:
    """The guard sits below every gate that decides, including the plan gate.

    Asserted as a relation, not an index, under a profile with the harness enabled (which appends
    `enforce_plan_approval` and `stamp_plan_link`): every deciding gate sits outside this guard, so
    a promoted call crosses them all before being refused. The plan gate's handler is never reached
    for a promoted call, deliberately: unparsed arguments are not a request for a gate to decide on.
    """
    from chemclaw.agent.langgraph_agent import tool_governance_middleware

    def names(profile: AgentProfile) -> list[str]:
        return [
            getattr(entry, "name", None) or getattr(entry, "__name__", None) or type(entry).__name__
            for entry in tool_governance_middleware(object(), profile)
        ]

    gated = names(AgentProfile(name="gated", harness_enabled=True, harness_autonomy="plan_only"))
    guard = gated.index("refuse_unparsed_arguments")

    # Every deciding gate is outside the guard, so a promoted call crosses all of them.
    for gate in (
        "announce_tool_failures",
        "enforce_tool_authz",
        "refuse_writes_on_dry_run",
        "refuse_repeated_calls",
    ):
        assert gated.index(gate) < guard, f"{gate} nests inside the guard, so it never sees a call"

    # And the three that are inside it, named rather than assumed absent. The last is the
    # ownership check, which sits as close to the effect as the chain allows and is no decision a
    # promoted call could be refused by.
    assert gated[guard + 1 :] == [
        "enforce_plan_approval",
        "stamp_plan_link",
        "refuse_when_claim_lost",
    ], (
        "the entries below the guard changed; the plan gate not seeing a promoted call is a "
        "property this test exists to keep deliberate"
    )
    ungated = AgentProfile(name="ungated", harness_enabled=False)
    assert names(ungated)[-2:] == ["refuse_unparsed_arguments", "refuse_when_claim_lost"], (
        "without the harness the guard is the last decision, which is why the false claim "
        "survived review"
    )


def test_a_streamed_truncation_is_completed_by_upstream_and_never_becomes_invalid() -> None:
    """A streamed truncation is completed by upstream and never becomes invalid.

    `AIMessageChunk.__add__` parses with `parse_partial_json`, which completes any prefix of a valid
    object, so a cut stream lands on `tool_calls` with half-written arguments. Only a document that
    is not a prefix (garbage, a bare string, an unbalanced close) reaches `invalid_tool_calls`. An
    absence
    assertion: if upstream stops completing prefixes, this turns red.
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


@pytest.mark.parametrize("finish", ["length", "max_tokens", ["length", "length"]])
def test_a_call_cut_off_at_the_output_limit_does_not_run_on_upstreams_guess(
    finish: str | list[str],
) -> None:
    """A call cut off at the output limit does not run on upstream's guess.

    `parse_partial_json` completes `'{"smiles": "CC'` to a valid call, but the reply's
    `finish_reason` says it was cut. Demoted and promoted, it crosses the same chain as a malformed
    call, and the `ToolMessage` names the output limit. A gateway repeating `finish_reason` on a
    trailing chunk concatenates it to `"lengthlength"`, so equality is not enough.
    """
    sink = _Recording()
    model = _StreamingModel(
        [
            {"text": "", "calls": [("predict_pka", '{"smiles": "CC')], "finish": finish},
            {"text": "The call was cut off; I will not guess the molecule."},
        ]
    )
    results, signals, _streamed = asyncio.run(_drive(_governed_graph(model, sink)))

    assert _ENTERED == [], "the tool ran on a molecule the provider cut off mid-document"
    assert [row.outcome for row in sink.events if row.tool == "predict_pka"] == ["error"]
    assert [(s.tool, s.call_id, s.reason) for s in signals] == [("predict_pka", "call-0-0", None)]
    assert [m.tool_call_id for m in results] == ["call-0-0"]
    refusal = str(results[0].content)
    assert "output-token limit" in refusal, refusal
    assert "not valid JSON" not in refusal, "the model would be sent to fix JSON it wrote correctly"


def test_a_reply_that_finished_normally_still_runs_its_calls() -> None:
    """The other direction: `stop` is a finished document, and it runs exactly as before."""
    sink = _Recording()
    model = _StreamingModel(
        [
            {"text": "", "calls": [("predict_pka", '{"smiles": "CCO"}')], "finish": "stop"},
            {"text": f"{_ANSWER}."},
        ]
    )
    results, signals, _streamed = asyncio.run(_drive(_governed_graph(model, sink)))

    assert _ENTERED == ["predict_pka(CCO)"]
    assert signals == []
    assert _ANSWER in str(results[0].content)
