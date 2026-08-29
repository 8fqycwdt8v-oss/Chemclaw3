"""The model call had no taxonomy, no counter, no log and no span — and could drop a tool call.

The decision is `D-2026-08-27-a-refusal-is-not-a-crash`. Three failures met at this boundary and
each was separately invisible: a provider 429 had no counter distinct from the front door's own
*inbound* limiter, a context-length `BadRequestError` was classified `("internal", False)` — "do not
retry", about the one failure a shorter question fixes — and `RunnableWithFallbacks` absorbing 100%
of traffic onto the fallback endpoint produced no log line at all.

Beside them, an unparseable tool call vanished: LangChain puts it on `AIMessage.invalid_tool_calls`
and nothing in `src/` read that field.

The classification is driven with **real provider SDK exceptions**, constructed the way the SDK
constructs them. A test that classified a stand-in class would prove only that `isinstance` works.
"""

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast
from unittest.mock import patch

import httpx2
import pytest
from langchain.agents.middleware import ModelRequest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.tools import tool

from chemclaw.agent.llm_provider import _failover_exceptions, classify_model_failure
from chemclaw.agent.model_calls import (
    RecordModelCalls,
    RepairInvalidToolCalls,
    invalid_tool_calls,
    model_call_middleware,
)
from chemclaw.core import turn_signals
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from chemclaw.core.turn_signals import _KEY as SIGNAL_KEY
from chemclaw.core.turn_signals import ToolFailureSignal


def _openai_error(kind: str, status: int, message: str, code: str | None = None) -> Exception:
    """One real `openai` API error of `kind`, built the way the SDK builds it.

    The SDK's `APIStatusError` family takes the `httpx` response it was raised from, so the object
    under test carries the same `code`, `status_code` and message an endpoint would produce.
    """
    import openai

    request = httpx2.Request("POST", "https://internal.example/v1/chat/completions")
    body = {"error": {"message": message, "code": code}}
    response = httpx2.Response(status, request=request, json=body)
    error: Exception = getattr(openai, kind)(message, response=response, body=body["error"])
    return error


@tool
def predict_pka(smiles: str) -> str:
    """Stand in for the tool surface a request was made with — its *name* is what matters here."""
    return "4.2"


@tool
def find_notes(text: str) -> str:
    """A second bound tool, so a reply can carry a valid call beside a broken one."""
    return "no notes"


class _NamedTool:
    """The minimum a bound tool needs to expose for the invalid-tool-call label clamp: a name.

    Kept beside the real `@tool` above rather than in place of it: `_metric_label` reads `.name`
    off whatever the request bound, and the two shapes — a LangChain tool object and a bare
    duck-typed one — are the two a `ModelRequest` can actually carry.
    """

    def __init__(self, name: str) -> None:
        self.name = name


def _request(messages: list[Any], tools: list[Any] | None = None) -> ModelRequest[Any]:
    """A `ModelRequest` carrying only what these middlewares read.

    `tools` defaults to the one-tool surface these cases name, because the unparseable-call metric
    clamps its label against exactly this list: a request that bound nothing can only ever produce
    the `unknown` bucket, which would make the clamp's own test vacuous.
    """
    return ModelRequest(
        model=None,  # type: ignore[arg-type]
        system_prompt=None,
        messages=messages,
        tool_choice=None,
        tools=[predict_pka] if tools is None else tools,
        response_format=None,
        state={"messages": messages},
        runtime=None,
    )


def test_the_taxonomy_is_the_one_the_failover_set_already_knew() -> None:
    """`classify_model_failure` reuses `_failover_exceptions` rather than restating it.

    The point of the reuse is that this seam has *always* distinguished "the endpoint is down" from
    "the request is wrong" — failover depends on it — and nothing recorded the distinction. A second
    table would be a second answer to one question, which is how a failover and a metric come to
    disagree about what a transport failure is.
    """
    for kind in _failover_exceptions():
        assert issubclass(kind, BaseException)
    connection = _openai_error("InternalServerError", 500, "upstream is down")
    assert isinstance(connection, _failover_exceptions())
    assert classify_model_failure(connection) == "transport"


def test_a_rate_limit_and_a_timeout_are_not_transport() -> None:
    """Order is the classification: `APITimeoutError` subclasses `APIConnectionError`.

    A linear scan that tested transport first would report every timeout as a dead endpoint, which
    is the difference between "the provider is throttling us" and "the provider is gone" — two
    outages with different remedies.
    """
    import openai

    request = httpx2.Request("POST", "https://internal.example/v1/chat/completions")
    assert classify_model_failure(openai.APITimeoutError(request)) == "timeout"
    assert classify_model_failure(_openai_error("RateLimitError", 429, "slow down")) == (
        "rate_limited"
    )


def test_the_context_length_error_is_finally_its_own_outcome() -> None:
    """The one failure mode `agent/compaction.py` exists to prevent, and it was unmeasurable.

    It arrives as an ordinary `BadRequestError`, so `api/runner._classify` fell through to
    `("internal", False)` — the chemist was told "internal error, do not retry" about the one
    failure that a shorter question fixes.
    """
    openai_shape = _openai_error(
        "BadRequestError",
        400,
        "This model's maximum context length is 128000 tokens.",
        code="context_length_exceeded",
    )
    assert classify_model_failure(openai_shape) == "context_length"

    # Anthropic's spelling, which sets no `code` at all — so the message is the only signal there.
    anthropic_shape = _openai_error("BadRequestError", 400, "prompt is too long: 210000 tokens")
    assert classify_model_failure(anthropic_shape) == "context_length"


def test_an_unrecognised_failure_is_error_rather_than_a_guess() -> None:
    """A 401 must not be laundered into an outage — the label space stays meaningful."""
    assert classify_model_failure(_openai_error("AuthenticationError", 401, "bad key")) == "error"
    assert classify_model_failure(ValueError("something else")) == "error"


def test_a_model_call_is_counted_and_timed_by_provider() -> None:
    """`chemclaw_model_calls_total{provider,outcome}` — the series that did not exist."""
    before = METRICS.observations("chemclaw_model_call_duration_seconds")[0]

    async def _handler(request: ModelRequest[Any]) -> Any:
        return ModelResponse(result=[AIMessage(content="ok")])

    asyncio.run(
        RecordModelCalls().awrap_model_call(_request([HumanMessage(content="hi")]), _handler)
    )

    exposition = METRICS.render()
    assert 'chemclaw_model_calls_total{outcome="ok"' in exposition
    assert METRICS.observations("chemclaw_model_call_duration_seconds")[0] == before + 1


def test_a_failed_model_call_is_counted_under_its_outcome_and_logged_with_its_class(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The WARNING names the provider and the exception class — never the provider's message.

    A provider's error text can quote the request, and the request is the chemist's question. What
    an operator needs is the family and the class, which is exactly what separates a rate limit from
    a dead endpoint from a thread that no longer fits.
    """
    before = METRICS.value("chemclaw_model_calls_total")
    failure = _openai_error("RateLimitError", 429, "please slow down, quota 12345 exceeded")

    async def _handler(request: ModelRequest[Any]) -> Any:
        raise failure

    with caplog.at_level(logging.WARNING):
        with pytest.raises(type(failure)):
            asyncio.run(
                RecordModelCalls().awrap_model_call(
                    _request([HumanMessage(content="hi")]), _handler
                )
            )

    assert METRICS.value("chemclaw_model_calls_total") == before + 1
    assert 'chemclaw_model_calls_total{outcome="rate_limited"' in METRICS.render()
    assert "RateLimitError" in caplog.text
    assert "rate_limited" in caplog.text
    # The provider's own words stay out of the line — they can carry the request.
    assert "quota 12345" not in caplog.text


def test_an_unparseable_tool_call_is_found_where_nothing_looked() -> None:
    """`AIMessage.invalid_tool_calls` — read by nothing in `src/` before this.

    The agent iterates `tool_calls`, so a call whose arguments did not parse produced no
    `tool_failed`, no `tool_result`, no audit row and no span. With prose beside it the turn
    proceeded as though no tool had been needed.
    """
    broken = AIMessage(
        content="",
        tool_calls=[],
        invalid_tool_calls=[
            {
                "name": "compute_xtb_energy",
                "args": '{"smiles": "CC',
                "id": "call-1",
                "error": "Unterminated string",
                "type": "invalid_tool_call",
            }
        ],
    )
    found = invalid_tool_calls(ModelResponse(result=[broken]))
    # The parse error is quoted, unlike the name beside it: it carries the model's own document
    # and reaches a log line the default formatter does not escape (`_bounded_reason`).
    assert [(call.name, call.error) for call in found] == [
        ("compute_xtb_energy", "'Unterminated string'")
    ]
    # The malformed document itself is carried, because on the streaming path it is the only field
    # that survives — see `test_the_correction_carries_the_arguments_because_the_error_does_not`.
    assert found[0].arguments == repr('{"smiles": "CC')
    # The bare-`AIMessage` return shape a `wrap_model_call` handler is also allowed to use.
    assert [(call.name, call.error) for call in invalid_tool_calls(broken)] == [
        ("compute_xtb_energy", "'Unterminated string'")
    ]
    assert invalid_tool_calls(ModelResponse(result=[AIMessage(content="fine")])) == []


def test_an_unparseable_tool_call_is_counted_and_the_model_is_asked_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One repair attempt, and what the model is asked is a correction it can act on.

    The correction goes into the *request* only, so the discarded attempt never reaches graph
    state, the transcript or the checkpoint: the session records one assistant message, the one the
    model meant to send.
    """
    before = METRICS.value("chemclaw_invalid_tool_calls_total")
    broken = AIMessage(
        content="",
        invalid_tool_calls=[
            {
                "name": "predict_pka",
                "args": '{"smiles": "CC',
                "id": "call-1",
                "error": "Unterminated string",
                "type": "invalid_tool_call",
            }
        ],
    )
    good = AIMessage(content="", tool_calls=[])
    seen: list[list[Any]] = []

    async def _handler(request: ModelRequest[Any]) -> Any:
        seen.append(list(request.messages))
        return ModelResponse(result=[broken if len(seen) == 1 else good])

    with caplog.at_level(logging.WARNING):
        answer = asyncio.run(
            RepairInvalidToolCalls().awrap_model_call(
                _request([HumanMessage(content="what is the pKa")], [_NamedTool("predict_pka")]),
                _handler,
            )
        )

    assert len(seen) == 2, "the model was asked exactly once more"
    correction = seen[1][-1]
    assert isinstance(correction, HumanMessage)
    assert "predict_pka" in correction.text
    assert "Unterminated string" in correction.text
    # The broken attempt is not replayed to the provider, and its *call* is not returned to the
    # graph. Its prose is — see `test_the_recorded_message_is_the_text_the_turn_streamed` — and
    # here there is none, so the repaired reply is returned unchanged.
    assert broken not in seen[1]
    assert answer.result == [good]
    assert METRICS.value("chemclaw_invalid_tool_calls_total") == before + 1
    assert 'chemclaw_invalid_tool_calls_total{tool="predict_pka"}' in METRICS.render()


def test_a_second_unparseable_reply_is_returned_with_an_error_rather_than_a_third_attempt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The bound is one retry, and the *second* reply is what the turn continues with.

    Returning the first instead would be choosing the reply that is known to be broken; asking a
    third time spends tokens on the same answer while the turn's runaway cap — which counts in
    `before_model` — cannot see a retry taken inside one model call.
    """
    broken = AIMessage(
        content="I will compute that.",
        invalid_tool_calls=[
            {
                "name": "predict_pka",
                "args": "{",
                "id": "call-1",
                "error": "Expecting property name",
                "type": "invalid_tool_call",
            }
        ],
    )
    calls = 0

    async def _handler(request: ModelRequest[Any]) -> Any:
        nonlocal calls
        calls += 1
        return ModelResponse(result=[broken])

    with caplog.at_level(logging.ERROR):
        answer = asyncio.run(
            RepairInvalidToolCalls().awrap_model_call(_request([HumanMessage("x")]), _handler)
        )

    assert calls == 2
    # The second reply is what the turn continues with, carrying the prose of the first — both
    # were streamed, and the fixture returns the same object for both, so the prose appears twice.
    assert answer.result[0].tool_calls == broken.tool_calls
    assert answer.result[0].text == "I will compute that.I will compute that."
    assert "twice" in caplog.text


def test_a_clean_reply_costs_no_extra_model_call() -> None:
    """The negative case: the repair must be inert on every turn that does not need it."""
    calls = 0

    async def _handler(request: ModelRequest[Any]) -> Any:
        nonlocal calls
        calls += 1
        return ModelResponse(result=[AIMessage(content="the pKa is 4.2")])

    asyncio.run(RepairInvalidToolCalls().awrap_model_call(_request([HumanMessage("x")]), _handler))
    assert calls == 1


def test_the_repair_wraps_the_recorder_so_both_attempts_are_booked() -> None:
    """Order is nesting, and it is what makes the cost of a malformed emission visible.

    `create_agent` nests `wrap_model_call` in list order, so the recorder being *inside* the repair
    is what books two model calls for one repaired turn — which is what happened.
    """
    assert [type(entry).__name__ for entry in model_call_middleware()] == [
        "RepairInvalidToolCalls",
        "RecordModelCalls",
    ]


class _StreamingModel(GenericFakeChatModel):
    """A model that streams a scripted reply per call, tool-call fragments and all.

    Not `tests/fakes_langgraph.ScriptedChatModel`: that fake emits only *valid* tool-call
    arguments, and the whole subject here is what LangChain does with arguments that do not parse.
    Streaming rather than returning whole messages is the point — `stream_mode="messages"` is what
    a chemist receives, and it is emitted per **model call**, which is the thing the repair
    middleware's own docstring used to get wrong.
    """

    script: list[dict[str, Any]] = []

    def __init__(self, script: list[dict[str, Any]], **kwargs: Any) -> None:
        """Hold the script; `messages` is unused because `_stream` is fully overridden."""
        super().__init__(messages=iter([]), **kwargs)
        object.__setattr__(self, "_step", 0)
        self.script = script

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Accept the binding `create_agent` performs on every request and keep the script."""
        return self

    def _stream(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any):  # type: ignore[no-untyped-def]
        """Stream the next scripted reply: its prose, then one fragment per scripted call.

        `calls` is a list of `(tool name, argument document)` because a reply carrying a broken
        call *beside* a valid one is a case with its own outcome — the whole reply is discarded —
        and it cannot be scripted at all if a reply may hold only one call.
        """
        step = object.__getattribute__(self, "_step")
        object.__setattr__(self, "_step", step + 1)
        reply = self.script[step]
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


async def _drive(
    script: list[dict[str, Any]],
) -> tuple[str, str, list[dict[str, Any]], list[ToolFailureSignal]]:
    """Run one turn of a compiled graph over `script`.

    Returns `(streamed text, recorded assistant text, the tool calls the graph actually ran, the
    tool failures the turn announced)`.

    A **compiled** graph, because the divergence this exists to catch is invisible to a direct
    hook call: `RepairInvalidToolCalls` returns one message, and the middleware's unit tests
    therefore all passed while the stream carried two replies. Only `graph.astream` sees what the
    front door sees.

    The `custom` mode is read for the same reason, one gap later. `record_tool_failure` resolves
    LangGraph's writer off the ambient config and drops the signal in silence where there is none
    (`core/turn_signals._emit`), so a unit test calling the hook directly would pass over a
    middleware whose announcements go nowhere. Draining the channel a compiled graph publishes on
    is the only assertion that the chemist would actually have seen them.
    """
    from langchain.agents import create_agent

    graph = create_agent(
        model=_StreamingModel(script),
        tools=[predict_pka, find_notes],
        middleware=[RepairInvalidToolCalls()],
    )
    streamed: list[str] = []
    recorded: list[str] = []
    ran: list[dict[str, Any]] = []
    announced: list[ToolFailureSignal] = []
    stream = graph.astream(
        {"messages": [HumanMessage(content="what is the pKa of acetic acid")]},
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
                announced.append(signal)
        else:
            for update in (payload or {}).values():
                for message in (update or {}).get("messages", []) or []:
                    if isinstance(message, AIMessage):
                        recorded.append(message.text)
                        ran.extend(dict(call) for call in message.tool_calls or [])
    return "".join(streamed), "".join(recorded), ran, announced


def test_the_recorded_message_is_the_text_the_turn_streamed() -> None:
    """The invariant this middleware owes a chemist, and the one it broke.

    Measured before the fix, on this exact graph: the client received
    `"Let me compute that. Here it is: pKa 4.2."` — both attempts, because `stream_mode="messages"`
    emits per **model call** — while the message the graph recorded held only the second. The
    front door concatenates those root `TokenEvent`s into the persisted answer
    (`api/runner._stream_into`), so the turn had two records of itself that disagreed, and the one
    the corpus grades was not the one the session stored.

    The discarded attempt cannot be un-streamed: it is the *first* call, which on every turn that
    needs no repair is the only one, so suppressing it suppresses the streaming path rather than a
    discarded attempt — and no stream mode retracts a token already emitted. So the record carries
    the prose instead, and this asserts the equality rather than either half of it.
    """
    streamed, recorded, _ran, _announced = asyncio.run(
        _drive(
            [
                {"text": "Let me compute that. ", "calls": [("predict_pka", '{"smiles": }')]},
                {"text": "Here it is: pKa 4.2."},
            ]
        )
    )
    assert streamed == "Let me compute that. Here it is: pKa 4.2."
    assert recorded == streamed


def test_a_reply_needing_no_repair_streams_once_and_is_recorded_once() -> None:
    """The negative case, and the guard on the fix: no prose is duplicated on an ordinary turn."""
    streamed, recorded, ran, announced = asyncio.run(_drive([{"text": "The pKa is 4.76."}]))
    assert streamed == "The pKa is 4.76." and recorded == streamed and ran == []
    assert announced == []


def test_a_truncated_argument_document_repairs_itself_and_never_reaches_this_middleware() -> None:
    """The reachability claim this module made was wrong, and the correction is measurable.

    The docstring said a truncated argument document — "what a real model emits when a stream is
    cut" — is what reaches `invalid_tool_calls`. LangChain runs streamed tool-call fragments
    through `parse_partial_json`, which closes the unterminated string and the open brace, so that
    case never arrives: it lands on `tool_calls`, parsed, with `invalid_tool_calls` empty. What
    does arrive is output that partial parsing cannot close.
    """
    streamed, _recorded, ran, _announced = asyncio.run(
        _drive(
            [
                {"text": "computing. ", "calls": [("predict_pka", '{"smiles": "CC')]},
                {"text": "pKa 4.2."},
            ]
        )
    )
    # The call ran, with the document closed for it: no repair, no second attempt's prose.
    assert [call["args"] for call in ran] == [{"smiles": "CC"}]
    assert streamed == "computing. pKa 4.2."


def test_a_valid_call_discarded_with_a_broken_one_is_named_rather_than_dropped_in_silence(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed`, in the module that cites it.

    A reply carrying one unparseable call *and* one valid one is discarded whole — the repair
    returns the second attempt, so the valid call never reaches `ToolNode`. Measured before the
    fix: `predict_pka` was dropped with no log, no metric, no audit row and no word to the model,
    while the correction it was sent said "none of them ran" over a count that named only the
    unparseable one.
    """
    broken = AIMessage(
        content="",
        tool_calls=[{"name": "predict_pka", "args": {"smiles": "CC"}, "id": "call-ok"}],
        invalid_tool_calls=[
            {
                "name": "compute_xtb_energy",
                "args": "{oops",
                "id": "call-bad",
                "error": None,
                "type": "invalid_tool_call",
            }
        ],
    )
    seen: list[list[Any]] = []

    async def _handler(request: ModelRequest[Any]) -> Any:
        seen.append(list(request.messages))
        return ModelResponse(result=[broken if len(seen) == 1 else AIMessage(content="done")])

    with caplog.at_level(logging.WARNING):
        asyncio.run(
            RepairInvalidToolCalls().awrap_model_call(_request([HumanMessage("pKa?")]), _handler)
        )

    correction = seen[1][-1].text
    assert "predict_pka" in correction, "the discarded valid call is named to the model"
    assert "did not run either" in correction
    assert "predict_pka" in caplog.text or "1 valid call(s)" in caplog.text


def test_the_correction_carries_the_arguments_because_the_error_does_not() -> None:
    """On the streaming path `error` is `None`, so a tool name was the whole correction.

    Measured on the aggregated `AIMessageChunk` LangChain produces for a streamed reply:
    `invalid_tool_calls[0]["error"]` is `None`, so `invalid_tool_calls` fell back to the literal
    `"arguments did not parse"` and the model — which cannot see its own discarded reply — was told
    a tool name and nothing else. `args`, the malformed document, was read and thrown away.
    """
    broken = AIMessage(
        content="",
        invalid_tool_calls=[
            {
                "name": "predict_pka",
                "args": '{"smiles": }',
                "id": "call-1",
                "error": None,
                "type": "invalid_tool_call",
            }
        ],
    )
    seen: list[list[Any]] = []

    async def _handler(request: ModelRequest[Any]) -> Any:
        seen.append(list(request.messages))
        return ModelResponse(result=[broken if len(seen) == 1 else AIMessage(content="ok")])

    asyncio.run(
        RepairInvalidToolCalls().awrap_model_call(_request([HumanMessage("pKa?")]), _handler)
    )
    assert '{"smiles": }' in seen[1][-1].text


def test_a_model_invented_tool_name_cannot_mint_a_metric_series() -> None:
    """The label was the model's own string, on the metric that fires when its output is malformed.

    `agent/audit.py::metric_tool_name` makes this argument for the tool path — a hallucinated name
    minted a permanent time series, and model output is attacker-influenceable. This metric had the
    same hole with a docstring claiming it did not: `"It is a bounded literal"` was true only of the
    `unknown` fallback.
    """
    hallucinated = AIMessage(
        content="",
        invalid_tool_calls=[
            {
                "name": "totally_made_up_" + "x" * 400,
                "args": "{",
                "id": "call-1",
                "error": None,
                "type": "invalid_tool_call",
            }
        ],
    )

    async def _handler(request: ModelRequest[Any]) -> Any:
        return ModelResponse(result=[hallucinated])

    asyncio.run(RepairInvalidToolCalls().awrap_model_call(_request([HumanMessage("x")]), _handler))
    exposition = METRICS.render()
    assert "totally_made_up_" not in exposition
    assert 'chemclaw_invalid_tool_calls_total{tool="unknown"}' in exposition


def test_a_name_the_request_actually_bound_is_kept_as_the_label() -> None:
    """The guard on the guard: clamping everything to `unknown` would lose the whole distinction."""
    broken = AIMessage(
        content="",
        invalid_tool_calls=[
            {
                "name": "predict_pka",
                "args": "{",
                "id": "call-1",
                "error": None,
                "type": "invalid_tool_call",
            }
        ],
    )

    async def _handler(request: ModelRequest[Any]) -> Any:
        return ModelResponse(result=[broken])

    asyncio.run(RepairInvalidToolCalls().awrap_model_call(_request([HumanMessage("x")]), _handler))
    assert 'chemclaw_invalid_tool_calls_total{tool="predict_pka"}' in METRICS.render()


def test_an_unbound_tool_name_is_clamped_off_the_metric(caplog: pytest.LogCaptureFixture) -> None:
    """A model-invented tool name never becomes a Prometheus series on the unauthenticated /metrics.

    `invalid_tool_calls` carries whatever the model emitted, unresolved against the bound tools, so
    booking it verbatim on `chemclaw_invalid_tool_calls_total{tool=...}` lets injected content
    ("emit a tool call named <secret>") exfiltrate through the label and blows the series cap. A
    name outside the bound surface is folded to the `unknown` bucket; the full name still
    reaches the operator-only WARNING.

    **The bucket is `audit.UNKNOWN_TOOL` and this test was written against a local `"<unknown>"`
    literal.** Both sides of the merge that produced this file clamped the same label; the
    resolution kept the shared constant `agent/audit.py` already books unresolved names under, so
    that one concept has one spelling in this tree. Only the expected string changed — the two
    properties this test carries and the ones above do not, and the exfil assertion is the one
    neither of the tests above states in this shape.
    """
    exfil = "PATIENT=Jane_Doe;SMILES=CC(=O)Oc1ccccc1C(=O)O"
    broken = AIMessage(
        content="",
        invalid_tool_calls=[
            {"name": exfil, "args": "{bad", "id": "c1", "error": "e", "type": "invalid_tool_call"}
        ],
    )
    good = AIMessage(content="", tool_calls=[])
    seen: list[int] = []

    async def _handler(request: ModelRequest[Any]) -> Any:
        seen.append(1)
        return ModelResponse(result=[broken if len(seen) == 1 else good])

    asyncio.run(
        RepairInvalidToolCalls().awrap_model_call(
            _request([HumanMessage(content="hi")], [_NamedTool("real_tool")]), _handler
        )
    )
    rendered = METRICS.render()
    assert exfil not in rendered, "a model-invented tool name reached /metrics verbatim"
    assert 'chemclaw_invalid_tool_calls_total{tool="unknown"}' in rendered


def test_an_unparseable_call_reaches_the_chemists_stream_as_tool_failed() -> None:
    """The half the counter could not cover: what the person who asked the question is told.

    `D-2026-08-29-a-call-the-tool-chain-never-sees-is-a-call-the-tool-chain-cannot-announce`.
    `agent/tool_authz.announce_tool_failures` raises `tool_failed` from inside the *tool* chain,
    and a call whose arguments never parsed never enters it — `ToolNode` iterates `tool_calls`.
    Measured against the live stack on two behaviours differing only in whether the argument
    document parses: `'{"query": "benzene"}'` produced `tool_call` + `tool_failed`, and
    `'{"text": }'` produced one `error/empty_answer` saying "after 0 tool call(s)" — a sentence
    that tells a chemist their question was too broad about a turn in which the model asked for
    exactly the right tool, twice.

    **One announcement for one lost call, though the model emitted it twice.** The chemist asked
    for one thing and did not get it; two red rows for one unmet intent is noise. The operator's
    counter still reads 2, because "how often did the model emit malformed output" is a different
    question — `test_the_counter_counts_attempts_while_the_stream_counts_losses` pins the pair.
    """
    before = METRICS.value("chemclaw_invalid_tool_calls_total")
    _streamed, _recorded, ran, announced = asyncio.run(
        _drive(
            [
                {"text": "", "calls": [("predict_pka", '{"smiles": }')]},
                {"text": "", "calls": [("predict_pka", '{"smiles": }')]},
            ]
        )
    )
    assert ran == [], "a call whose arguments do not parse never runs"
    assert [signal.tool for signal in announced] == ["predict_pka"]
    assert METRICS.value("chemclaw_invalid_tool_calls_total") == before + 2
    assert '{"smiles": }' in announced[0].message
    assert "did not run" in announced[0].message
    # No id and no reason, and both are load-bearing. `call_id` means "match this to the
    # `tool_call` event", and there is none — measured, an unparseable call announces nothing at
    # all — so an id here would point at something never emitted. `reason` names the five *gates*;
    # a document that will not parse is an ordinary fault, not a control working.
    assert announced[0].call_id == ""
    assert announced[0].reason is None


def test_a_valid_call_discarded_with_a_broken_one_is_announced_to_the_chemist_too() -> None:
    """The silent drop the correction already named to the model, said to the person as well.

    A reply carrying one unparseable call and one valid one is discarded whole, so the valid call
    does not run either. The model is told (`_DISCARDED_VALID`) and can re-issue it; the chemist
    could not be told by anything, because that call never reached the tool chain either — and
    unlike the model, they have no way to ask for it again.
    """
    _streamed, _recorded, ran, announced = asyncio.run(
        _drive(
            [
                {
                    "text": "",
                    "calls": [
                        ("predict_pka", '{"smiles": }'),
                        ("find_notes", '{"text": "acetic acid"}'),
                    ],
                },
                {"text": "I could not read my own call."},
            ]
        )
    )
    assert ran == [], "the whole reply is discarded, valid call included"
    assert [signal.tool for signal in announced] == ["predict_pka", "find_notes"]
    assert "not valid JSON" in announced[0].message
    assert "did not run either" in announced[1].message


def test_a_repair_that_works_announces_nothing_because_nothing_was_lost() -> None:
    """The regression this middleware's first version shipped, measured on all three readers.

    Announcing at the moment of discard read "a discarded call did not run", which is false when
    the repair works: the model re-issues it and it runs. Measured on this exact graph before the
    fix — a turn that **answered**, with every tool succeeding — `evals/live` recorded
    `tools_failed=['predict_pka', 'find_notes']` and `failed_loudly=True`, `_TurnLedger` booked two
    `tool_failures` beside two successful `tool_calls`, and `Chemclaw3_ui` renders a `reason`-less
    failure in the danger red with a `failed` badge, so the chemist read two red rows above a good
    answer. `find_notes` was never invoked at all.

    The valid call discarded with the broken one is the sharp end: it is announced only if the
    repaired reply does not ask for it again, which here it does.
    """
    _streamed, _recorded, ran, announced = asyncio.run(
        _drive(
            [
                {
                    "text": "",
                    "calls": [
                        ("predict_pka", '{"smiles": }'),
                        ("find_notes", '{"text": "acetic acid"}'),
                    ],
                },
                {
                    "text": "",
                    "calls": [
                        ("predict_pka", '{"smiles": "CC(=O)O"}'),
                        ("find_notes", '{"text": "acetic acid"}'),
                    ],
                },
                {"text": "The pKa is 4.76."},
            ]
        )
    )
    assert sorted(call["name"] for call in ran) == ["find_notes", "predict_pka"]
    assert announced == [], "a call the model re-issued and ran is not a call that failed"


def test_a_valid_call_the_repair_does_not_reissue_is_still_announced() -> None:
    """The other direction, and the reason the rule is not simply "announce the second attempt".

    A parseable call thrown away with a broken reply is lost for good if the model does not ask for
    it again — and unlike the model, which is told about it in the correction, the chemist has no
    way to ask. So the repair succeeding does not by itself clear the first reply: what clears a
    call is the repaired reply asking for it.
    """
    _streamed, _recorded, ran, announced = asyncio.run(
        _drive(
            [
                {
                    "text": "",
                    "calls": [
                        ("predict_pka", '{"smiles": }'),
                        ("find_notes", '{"text": "acetic acid"}'),
                    ],
                },
                {"text": "", "calls": [("predict_pka", '{"smiles": "CC(=O)O"}')]},
                {"text": "The pKa is 4.76."},
            ]
        )
    )
    assert [call["name"] for call in ran] == ["predict_pka"]
    assert [signal.tool for signal in announced] == ["find_notes"]
    assert "did not run either" in announced[0].message


def test_the_counter_counts_attempts_while_the_stream_counts_losses() -> None:
    """The two records answer different questions, and an earlier ADR claimed they could not differ.

    `D-2026-08-29-a-call-the-tool-chain-never-sees-is-a-call-the-tool-chain-cannot-announce` closed
    with "an operator's count and a chemist's stream cannot disagree about how many calls were
    lost". They do disagree, by design: the counter is per malformed *emission* — what an operator
    alerts on and pays for — and the stream is per *unmet intent*. Pinned here so neither can be
    quietly changed into the other.
    """
    before = METRICS.value("chemclaw_invalid_tool_calls_total")
    _streamed, _recorded, _ran, announced = asyncio.run(
        _drive(
            [
                {"text": "", "calls": [("predict_pka", '{"smiles": }')]},
                {"text": "", "calls": [("predict_pka", '{"smiles": }')]},
                {"text": "I could not read my own call."},
            ]
        )
    )
    assert METRICS.value("chemclaw_invalid_tool_calls_total") == before + 2
    assert len(announced) == 1


def test_the_parse_error_is_bounded_before_it_reaches_the_chemist() -> None:
    """`error` was the one field `invalid_tool_calls` claimed to bound and did not.

    It is not merely unbounded but reliably *large*: LangChain's `parse_tool_call` folds the entire
    raw argument document into the exception message, which `langchain_openai` stores verbatim — so
    a 100 kB malformed document arrived twice, once truncated in `arguments` and once whole in
    `error`. Measured before the fix with the budget at 200 chars: `arguments` 201, `error`
    100,260, and that error reached a 100,587-char `ToolFailedEvent.message` and a 100,861-char
    corrective `HumanMessage` — the latter appended below `context_compaction_middleware`, where
    nothing can reduce it.

    Driven through the real `langchain_openai` converter rather than a hand-built entry, because
    the size comes from upstream's exception text and a fixture would just be this test asserting
    its own string.
    """
    from langchain_openai.chat_models.base import _convert_dict_to_message

    document = '{"smiles": "' + "C" * 100_000 + '" '
    converted = _convert_dict_to_message(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "predict_pka", "arguments": document},
                }
            ],
        }
    )
    assert isinstance(converted, AIMessage)
    assert len(converted.invalid_tool_calls[0]["error"] or "") > 100_000, (
        "upstream stopped embedding the document in the error; this test's premise is gone"
    )
    budget = settings.agent_audit_max_arg_chars
    broken = invalid_tool_calls(
        AIMessage(content="", invalid_tool_calls=converted.invalid_tool_calls)
    )
    assert len(broken[0].error) <= budget + 1
    assert len(broken[0].arguments) <= budget + 1


@contextmanager
def _capturing_signals(sink: list[Any]) -> Iterator[None]:
    """Collect what `record_tool_failure` publishes when no graph is there to publish into.

    `core/turn_signals._emit` resolves LangGraph's writer off the ambient config and **drops the
    signal in silence** where there is none — which is right (the same tools run in a Temporal
    activity) and is exactly why a hook called directly proves nothing about the announcement. The
    graph-driven tests above go through a real compiled graph; these three need the shapes a graph
    cannot script — a raising second model call, the synchronous hook, a thousand broken calls — so
    they stand a writer up instead.
    """
    with patch.object(
        turn_signals, "get_stream_writer", lambda: lambda payload: sink.append(payload[SIGNAL_KEY])
    ):
        yield


def _broken(name: str = "predict_pka", error: str | None = None) -> AIMessage:
    """One reply carrying a single unparseable call, in the shape LangChain produces."""
    return AIMessage(
        content="",
        invalid_tool_calls=[
            {
                "name": name,
                "args": '{"smiles": }',
                "id": "c1",
                "error": error,
                "type": "invalid_tool_call",
            }
        ],
    )


def test_two_calls_to_one_tool_with_one_reissued_loses_exactly_one() -> None:
    """The multiplicity rule `_announce_unrun` imports `Counter` for, which nothing could see.

    Its docstring argues the case at length — "a model that emitted two calls to one tool and
    re-issued one of them has lost the other, and a set difference would call that even" — and
    every test drove one call per tool, so swapping the `Counter` for a set difference passed the
    whole suite. That is a claim that a control exists, which is the shape this repository's own
    lessons flag.
    """
    _streamed, _recorded, ran, announced = asyncio.run(
        _drive(
            [
                {
                    "text": "",
                    "calls": [("predict_pka", '{"smiles": }'), ("predict_pka", '{"smiles": }')],
                },
                {"text": "", "calls": [("predict_pka", '{"smiles": "CC(=O)O"}')]},
                {"text": "The pKa is 4.76."},
            ]
        )
    )
    assert [call["name"] for call in ran] == ["predict_pka"], "one of the two was re-issued"
    assert [signal.tool for signal in announced] == ["predict_pka"], (
        "the other was lost, and a set difference would have called the two even"
    )


def test_a_repair_that_breaks_a_different_tool_announces_both() -> None:
    """The first reply's loss and the second's are distinct calls, and every test conflated them.

    Both existing scripts drove the *same* broken document twice, so "announce the repaired reply's
    failure" and "announce the first reply's failure" produced byte-identical output and no test
    could tell which rule was implemented.
    """
    _streamed, _recorded, ran, announced = asyncio.run(
        _drive(
            [
                {"text": "", "calls": [("predict_pka", '{"smiles": }')]},
                {"text": "", "calls": [("find_notes", '{"text": }')]},
                {"text": "I could not read either call."},
            ]
        )
    )
    assert ran == []
    assert sorted(signal.tool for signal in announced) == ["find_notes", "predict_pka"]


def test_the_parse_error_keeps_its_reason_and_reaches_the_chemist() -> None:
    """Two defects in one field: the reason was truncated away, and nothing asserted it arrived.

    Deleting `{error}` from `_UNPARSEABLE_FAILURE` survived the whole suite, because every
    graph-driven test takes the *streamed* shape where the provider reports `error=None`. And the
    bound was head-only, so on the non-streaming shape what survived was the argument document a
    second time — LangChain folds it into the exception message — while the `JSONDecodeError`
    reason at the tail, the only part not already printed beside it, was cut off.
    """
    from langchain_openai.chat_models.base import _convert_dict_to_message

    document = '{"smiles": "CC(=O)Oc1ccccc1C(=O)O", solvent: "water", "note": "several fields"}'
    converted = _convert_dict_to_message(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "predict_pka", "arguments": document},
                }
            ],
        }
    )
    assert isinstance(converted, AIMessage)
    broken = invalid_tool_calls(
        AIMessage(content="", invalid_tool_calls=converted.invalid_tool_calls)
    )[0]
    # The reason itself, not the exception's class name — upstream puts the class first and the
    # sentence last, and the tail is deliberately what survives. Upstream's "For troubleshooting,
    # visit: …" boilerplate rides between them and eats about half the budget, which is why the
    # assertion is on the reason rather than on the whole tail.
    assert "Expecting property name" in broken.error, "the reason is what this field adds"
    assert len(broken.error) <= settings.agent_audit_max_arg_chars + 2

    signals: list[Any] = []
    replies = [ModelResponse(result=[converted]), ModelResponse(result=[AIMessage(content="ok")])]

    async def _handler(request: ModelRequest[Any]) -> Any:
        return replies.pop(0)

    with _capturing_signals(signals):
        asyncio.run(
            RepairInvalidToolCalls().awrap_model_call(_request([HumanMessage("pKa?")]), _handler)
        )
    assert signals and "Expecting property name" in signals[0].message


def test_the_parse_error_is_escaped_so_it_cannot_forge_a_log_line() -> None:
    """`_bounded_text` deliberately does not `repr`, and extending that to `error` was a defect.

    The reason not to quote is about the *name*: `_metric_label` compares it against the bound
    tools, and a quoted name matches none of them. The parse error is different — it embeds the
    model's own document, which is attacker-influenceable through every retrieved corpus this tree
    frames as untrusted, and `log_json` defaults to **false**, so an embedded newline forged a
    second log line under the default formatter.
    """
    forged = "oops\n2026-08-29 ERROR chemclaw.audit: actor=admin action=approve result=granted"
    broken = invalid_tool_calls(_broken(error=forged))[0]
    assert "\n" not in broken.error, "a raw newline in the parse error reaches the WARNING"
    assert "\\n" in broken.error, "the newline is escaped rather than deleted"


def test_a_reply_full_of_broken_calls_is_bounded_at_every_sink() -> None:
    """Nothing capped how many unrunnable calls one reply could hold.

    `agent_max_parallel_tool_calls` bounds calls that *run*. Measured with every field at its own
    ceiling, 1000 malformed calls produced an **841 kB** corrective `HumanMessage` — appended by
    `_retry_request` from the innermost middleware, below `context_compaction_middleware`, so the
    budget is already computed and nothing reduces it — plus 2000 stream events. The remainder is
    counted rather than dropped, because a bound that says nothing is the truncation this module
    exists to end one level up.
    """
    limit = settings.agent_max_reported_lost_calls
    flood = AIMessage(
        content="",
        invalid_tool_calls=[
            {
                "name": "predict_pka",
                "args": '{"smiles": }',
                "id": f"c{index}",
                "error": None,
                "type": "invalid_tool_call",
            }
            for index in range(limit * 5)
        ],
    )
    signals: list[Any] = []
    prompts: list[list[Any]] = []
    replies = [ModelResponse(result=[flood]), ModelResponse(result=[AIMessage(content="ok")])]

    async def _handler(request: ModelRequest[Any]) -> Any:
        prompts.append(list(request.messages))
        return replies.pop(0)

    with _capturing_signals(signals):
        asyncio.run(
            RepairInvalidToolCalls().awrap_model_call(_request([HumanMessage("pKa?")]), _handler)
        )
    assert len(signals) == limit + 1, "the listed calls, plus one notice for the remainder"
    assert f"{limit * 4} further tool call(s)" in signals[-1].message
    correction = prompts[1][-1].text
    assert f"and {limit * 4} more not listed here" in correction
    assert len(correction) < 20_000, f"the correction ran to {len(correction)} characters"


def test_a_repair_call_that_raises_still_tells_the_chemist_what_was_lost() -> None:
    """The counter fired and the chemist heard nothing — the asymmetry this module exists to end.

    `_count_invalid` runs before the retry and `_announce_unrun` after it, so a 429, a timeout or a
    context-length refusal on the *second* model call booked the operator's record and left the
    person who asked with no `tool_failed` at all. The turn still ends in a visible `ErrorEvent`,
    so this is not the silent death — but it is the operator/chemist split the change was written
    to remove, reachable through the one path the ordering did not cover.
    """
    signals: list[Any] = []
    calls = 0

    async def _handler(request: ModelRequest[Any]) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(result=[_broken()])
        raise RuntimeError("the endpoint went away")

    with _capturing_signals(signals), pytest.raises(RuntimeError):
        asyncio.run(
            RepairInvalidToolCalls().awrap_model_call(_request([HumanMessage("pKa?")]), _handler)
        )
    assert [signal.tool for signal in signals] == ["predict_pka"]


def test_the_sync_path_announces_what_the_async_path_announces() -> None:
    """Both hooks are declared, and only one was ever driven.

    `create_agent` puts a middleware declaring either into *both* chains, so the synchronous half
    is live for every `graph.invoke()` caller — `cli/chat.py` and the template activities among
    them — and no test reached its announcement.
    """
    replies = [
        ModelResponse(result=[_broken()]),
        ModelResponse(result=[AIMessage(content="ok")]),
    ]
    signals: list[Any] = []

    def _handler(request: ModelRequest[Any]) -> Any:
        return replies.pop(0)

    with _capturing_signals(signals):
        RepairInvalidToolCalls().wrap_model_call(_request([HumanMessage("pKa?")]), _handler)
    assert [signal.tool for signal in signals] == ["predict_pka"]
