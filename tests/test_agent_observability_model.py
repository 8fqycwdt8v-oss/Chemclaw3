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
from typing import Any

import httpx2
import pytest
from langchain.agents.middleware import ModelRequest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage, HumanMessage

from chemclaw.agent.llm_provider import _failover_exceptions, classify_model_failure
from chemclaw.agent.model_calls import (
    RecordModelCalls,
    RepairInvalidToolCalls,
    invalid_tool_calls,
    model_call_middleware,
)
from chemclaw.core.metrics import METRICS


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


class _NamedTool:
    """The minimum a bound tool needs to expose for the invalid-tool-call label clamp: a name."""

    def __init__(self, name: str) -> None:
        self.name = name


def _request(messages: list[Any], tools: list[Any] | None = None) -> ModelRequest[Any]:
    """A `ModelRequest` carrying only what these middlewares read."""
    return ModelRequest(
        model=None,  # type: ignore[arg-type]
        system_prompt=None,
        messages=messages,
        tool_choice=None,
        tools=tools or [],
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
    assert invalid_tool_calls(ModelResponse(result=[broken])) == [
        ("compute_xtb_energy", "Unterminated string")
    ]
    # The bare-`AIMessage` return shape a `wrap_model_call` handler is also allowed to use.
    assert invalid_tool_calls(broken) == [("compute_xtb_energy", "Unterminated string")]
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
    # The broken attempt is not replayed to the provider, and not returned to the graph.
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
    assert answer.result == [broken]
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


def test_an_unbound_tool_name_is_clamped_off_the_metric(caplog: pytest.LogCaptureFixture) -> None:
    """A model-invented tool name never becomes a Prometheus series on the unauthenticated /metrics.

    `invalid_tool_calls` carries whatever the model emitted, unresolved against the bound tools, so
    booking it verbatim on `chemclaw_invalid_tool_calls_total{tool=...}` lets injected content
    ("emit a tool call named <secret>") exfiltrate through the label and blows the series cap. A
    name outside the bound surface is folded to `<unknown>`; the full name still reaches the
    operator-only WARNING.
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
    assert 'chemclaw_invalid_tool_calls_total{tool="<unknown>"}' in rendered
