"""An unparseable tool call, driven through a **compiled graph** rather than through the hook.

`agent/model_calls.RepairInvalidToolCalls` is the fix
(`D-2026-08-27-a-refusal-is-not-a-crash`), and `tests/test_agent_observability_model.py` proves its
decisions by calling `awrap_model_call` directly with a hand-built `ModelRequest`. That is the
right shape for asserting *what it decides* and it cannot establish that the decision is connected
to anything — the property `tests/test_state_channels.py` exists for, after three defects in one
week where a hook returned the right value into a graph that dropped it. Two things here are only
true inside the graph:

- **`handler` may be called twice.** The repair asks the model again from inside one
  `wrap_model_call`. Whether upstream's real handler tolerates a second invocation is a fact about
  `create_agent`'s model node, not about the middleware, and a stub handler that appends to a list
  cannot report it.
- **`request.override(messages=…)` has to reach the provider.** The graph's model node could read
  the thread off state instead of off the request, in which case the correction would be composed,
  logged, counted — and never sent. That is the silent failure this repository keeps finding, so it
  is asserted against the messages a model double actually received.

The defect itself is asserted the same way, as a **behavioural diff**: the identical script through
a graph *without* the repair ends with prose and no tool call, which is
`D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed` reproduced rather than described.
"""

import asyncio
from collections.abc import Sequence
from typing import Any, cast

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import after_model
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from chemclaw.agent.loop_cap import enforce_loop_cap, loop_capped
from chemclaw.agent.model_calls import RepairInvalidToolCalls
from chemclaw.agent.state import ChemclawState
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS

# The tool the scripts below call, and the arguments the model *meant* to send. A real tool rather
# than a stub, because the property under test is that the repaired call reaches the tool node and
# comes back as a `ToolMessage` — which a tool that only exists in the model's imagination cannot
# show.
_ANSWER = "pKa 4.2"


@tool
def predict_pka(smiles: str) -> str:
    """Return this corpus's pKa for `smiles` (a double — the tool node is what is under test)."""
    return f"{_ANSWER} for {smiles}"


def _truncated_call() -> AIMessage:
    """The message a provider emits when a tool call's argument document is cut off.

    Built with `invalid_tool_calls` populated and `tool_calls` empty, which is exactly what
    `langchain_openai.chat_models.base._convert_dict_to_message` produces from truncated arguments
    — pinned by `test_langchains_converter_is_where_the_call_goes_missing` below, so this fixture
    cannot drift from the shape upstream actually builds.
    """
    return AIMessage(
        content="I will look that up.",
        tool_calls=[],
        invalid_tool_calls=[
            {
                "name": "predict_pka",
                "args": '{"smiles": "CC',
                "id": "call-1",
                "error": "Unterminated string starting at: line 1 column 12 (char 11)",
                "type": "invalid_tool_call",
            }
        ],
    )


def _valid_call() -> AIMessage:
    """The call the model re-issues once it has been told the first one did not parse."""
    return AIMessage(
        content="",
        tool_calls=[{"name": "predict_pka", "args": {"smiles": "CCO"}, "id": "call-2"}],
    )


class RecordingScriptedModel(BaseChatModel):
    """Replays a fixed script and records the message list it was handed on every call.

    Written here rather than added to `tests/fakes_langgraph.py` on that module's own rule — a
    double used by one test module stays private to it — and because `ScriptedChatModel` cannot
    express this script at all: it builds every turn through `_as_message`, which produces only
    prose or a *valid* `tool_calls` entry, and the whole subject here is the message shape it
    cannot make.

    Recording the prompt is the point. The repair appends its correction to the `ModelRequest`, and
    the only evidence that the request is what the model node sends is the thread this double was
    given on its second call.
    """

    script: list[AIMessage] = []
    seen: list[list[BaseMessage]] = []

    def __init__(self, script: Sequence[AIMessage], **kwargs: Any) -> None:
        """Take the turns to replay; `seen` starts empty and is appended to per call."""
        super().__init__(**kwargs)
        self.script = list(script)
        self.seen = []

    @property
    def _llm_type(self) -> str:
        """`BaseChatModel` requires it; nothing here reads it."""
        return "recording-scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Accept the binding `create_agent`'s model node performs and keep replaying.

        Returns `self` for `ScriptedChatModel`'s reason: the script already holds the calls under
        test, so binding must succeed rather than be reasoned about.
        """
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Record what was asked, answer with the next scripted turn."""
        self.seen.append(list(messages))
        index = min(len(self.seen) - 1, len(self.script) - 1)
        return ChatResult(generations=[ChatGeneration(message=self.script[index])])


def _graph(model: BaseChatModel, middleware: list[Any]) -> Any:
    """A compiled agent over the one tool, with exactly the middleware a case is about."""
    return create_agent(
        model=model,
        tools=[predict_pka],
        state_schema=ChemclawState,
        middleware=middleware,
    )


def _run(graph: Any, *, sync: bool) -> dict[str, Any]:
    """Drive one turn to completion on whichever of the two paths a case names.

    Both are driven because `RepairInvalidToolCalls` declares `wrap_model_call` *and*
    `awrap_model_call`, and `create_agent` puts a middleware declaring either into both chains —
    the trap `agent/compaction.RecordContextCompaction` records. A repair that worked on one path
    only would be green under `graph.ainvoke` and raise under `graph.invoke`, or silently repair
    nothing.
    """
    config = cast(Any, {"recursion_limit": 20})
    state = cast(Any, {"messages": [("user", "what is the pKa of ethanol")]})
    if sync:
        return cast(dict[str, Any], graph.invoke(state, config))
    return cast(dict[str, Any], asyncio.run(graph.ainvoke(state, config)))


def _tool_messages(final: dict[str, Any]) -> list[ToolMessage]:
    """Every tool result the finished turn produced."""
    return [m for m in final["messages"] if isinstance(m, ToolMessage)]


def test_langchains_converter_is_where_the_call_goes_missing() -> None:
    """The upstream fact the whole fix rests on, asserted against upstream's own converter.

    A truncated argument document yields `tool_calls: []` and a populated `invalid_tool_calls`. The
    agent loop iterates `tool_calls`, so the call is gone before any first-party code sees it —
    which is why the repair reads the other field rather than trying to prevent the emission.

    Pinned here for `tests/test_upstream_surface.py`'s reason: if LangChain ever routed a truncated
    call back onto `tool_calls`, or dropped `error`, this turns red instead of the repair quietly
    becoming a middleware with nothing to find.
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


@pytest.mark.parametrize("sync", [True, False], ids=["invoke", "ainvoke"])
def test_without_the_repair_the_turn_proceeds_as_though_no_tool_were_wanted(sync: bool) -> None:
    """The defect, reproduced in a compiled graph: prose is served and the tool never runs.

    This is the baseline the next test is a diff against, and it is what
    `D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed` names — the model announced a
    lookup, the lookup silently did not happen, and the turn ended looking finished. Without the
    prose the same turn ends as `empty_answer` instead; with it, nothing anywhere says a call was
    dropped.
    """
    model = RecordingScriptedModel([_truncated_call()])
    final = _run(_graph(model, []), sync=sync)

    assert len(model.seen) == 1, "the graph asked once and accepted the answer"
    assert _tool_messages(final) == [], "no tool ran"
    assert final["messages"][-1].content == "I will look that up."
    assert _ANSWER not in str(final["messages"][-1].content)


@pytest.mark.parametrize("sync", [True, False], ids=["invoke", "ainvoke"])
def test_the_repair_asks_again_inside_the_graph_and_the_tool_actually_runs(sync: bool) -> None:
    """The same script with the repair composed: the model is corrected and the call executes.

    Four properties, and each one needs the compiled graph:

    - upstream's real handler tolerates the second call the repair makes;
    - the correction composed onto the `ModelRequest` is in the thread the model was handed, so
      `override` reaches the provider rather than being discarded for the state's own message list;
    - the repaired call reaches the tool node — the failure is *visible* to the model as a result,
      not merely to a log;
    - the discarded attempt never lands in `messages`, so the transcript, the checkpoint and every
      later replay hold one assistant message per model step rather than a broken one beside it.
    """
    before = METRICS.value("chemclaw_invalid_tool_calls_total")
    model = RecordingScriptedModel([_truncated_call(), _valid_call(), AIMessage(content=_ANSWER)])
    final = _run(_graph(model, [RepairInvalidToolCalls()]), sync=sync)

    assert len(model.seen) == 3, "one repair attempt, then the ordinary loop"
    correction = model.seen[1][-1]
    assert "predict_pka" in str(correction.content)
    assert "Unterminated string" in str(correction.content)
    assert [m for m in model.seen[1] if isinstance(m, AIMessage) and m.invalid_tool_calls] == [], (
        "the attempt that did not parse must not be replayed to the provider"
    )

    results = _tool_messages(final)
    assert [str(m.content) for m in results] == [f"{_ANSWER} for CCO"]
    assert not [m for m in final["messages"] if isinstance(m, AIMessage) and m.invalid_tool_calls]
    assert METRICS.value("chemclaw_invalid_tool_calls_total") == before + 1
    assert 'chemclaw_invalid_tool_calls_total{tool="predict_pka"}' in METRICS.render()


@pytest.mark.parametrize("sync", [True, False], ids=["invoke", "ainvoke"])
def test_the_repair_is_invisible_to_the_graph_loop_because_it_never_jumps(sync: bool) -> None:
    """No hook is skipped and no iteration is consumed — the `after_model` constraint, measured.

    `D-2026-08-15-an-after-model-counter-is-a-counter-that-can-be-skipped` is the reason the repair
    lives in `wrap_model_call`: a middleware that jumps from `after_model` short-circuits every
    hook that runs later in that chain, and `enforce_loop_cap` counting in `before_model` is only
    unskippable while nobody jumps past it. An `after_model` repair with `jump_to: "model"` would
    have bought the correction by spending a loop iteration on it and by hiding the model's reply
    from every hook composed after itself.

    So the assertion is an equality between three numbers a jump would separate: the provider was
    called **once more** than the graph iterated, while the `after_model` probe and the loop
    counter each saw exactly one event per graph iteration.
    """
    observed: list[int] = []

    @after_model
    def _probe(state: Any, runtime: Any) -> None:
        """Count model responses that reach the hooks composed after the repair."""
        observed.append(len(state["messages"]))

    model = RecordingScriptedModel([_truncated_call(), _valid_call(), AIMessage(content=_ANSWER)])
    final = _run(_graph(model, [enforce_loop_cap, RepairInvalidToolCalls(), _probe]), sync=sync)

    assert len(model.seen) == 3, "three provider calls: the broken one, its repair, and the answer"
    assert len(observed) == 2, "two graph iterations — the repair is not one of them"
    assert final["model_calls"] == 2, "the repair consumed no iteration of the runaway cap"
    assert not loop_capped(final)


def test_a_repaired_turn_still_hits_the_runaway_cap() -> None:
    """The other half: the cap still fires on a repaired turn, so nothing was disarmed.

    A cap of 1 rather than the configured default, because the property is that the *first*
    `before_model` decision after a repaired model call is still made and still stops the loop —
    which is what a jump past `enforce_loop_cap` would have removed. Driven on the async path only:
    both paths are covered above, and what is under test here is the cap's edge rather than the
    repair's two hooks.
    """
    original = settings.harness_max_loop_iterations
    settings.harness_max_loop_iterations = 1
    try:
        model = RecordingScriptedModel([_truncated_call(), _valid_call(), AIMessage(content="x")])
        final = _run(_graph(model, [enforce_loop_cap, RepairInvalidToolCalls()]), sync=False)
    finally:
        settings.harness_max_loop_iterations = original

    assert loop_capped(final), "the cap must still stop a turn whose model call was repaired"
    assert len(model.seen) == 2, "the repair happened, and then the cap ended the run"


def test_a_streamed_truncation_is_completed_by_upstream_and_never_becomes_invalid() -> None:
    """The boundary of the repair: streaming never produces an invalid tool call at all.

    `D-2026-08-27-an-unparseable-tool-call-is-a-visible-failure` records the measurement. A tool
    call arriving as `tool_call_chunks` is merged by `AIMessageChunk.__add__`, which parses the
    accumulated document with `parse_partial_json` — and that function *completes* any document
    that is a prefix of a valid object, which is exactly what a cut stream or an exhausted token
    budget leaves behind. So the truncation the `BACKLOG` row named as the production cause lands
    on `tool_calls` as a call with half-written arguments, not on `invalid_tool_calls`, and
    `RepairInvalidToolCalls` correctly finds nothing to repair.

    Measured against the live lane as well as here: `make live-storm --families F` runs
    `f-malformed-json` (`'{"text": "unterminated'`) through the real front door and the tool
    executes with `text="unterminated"`. Only a document that is *not* a prefix — garbage, a bare
    string, an unbalanced close — reaches the field the repair reads.

    This is an **absence** assertion, the shape `tests/test_upstream_surface.py` uses for the same
    reason: if upstream stops completing prefixes, these calls start arriving as invalid, the
    repair begins firing on them, and this test turning red is the signal to re-read the ADR rather
    than to discover the change through behaviour.
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

    # Only a non-prefix reaches the field the repair reads.
    assert merged("not json at all").invalid_tool_calls, "garbage must still be surfaced"
