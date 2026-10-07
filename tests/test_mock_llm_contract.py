"""What `cli/mock_llm` promises the real gateway also does, driven over its own wire.

Every live lane runs against this mock, so a divergence from an OpenAI-compatible gateway makes a
control look exercised when it was not. The expectations were measured against a real gateway;
the tests need no network, driving `build_app` in process over `httpx.ASGITransport` with a real
`langchain_openai.ChatOpenAI` on top, so what is asserted is what the client assembles.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.messages.utils import count_tokens_approximately
from pydantic import SecretStr

from chemclaw.agent.context_budget import estimator_ratio, note_model_call, reset_calibration
from chemclaw.agent.llm_provider import classify_model_failure
from chemclaw.agent.turn_usage import graph_usage_tokens
from chemclaw.cli.delegation_behaviours import DELEGATION_BEHAVIOURS
from chemclaw.cli.e2e_behaviours import E2E_BEHAVIOURS
from chemclaw.cli.mock_llm import Behaviour, MockLlm, ToolCall, build_app
from chemclaw.cli.storm_behaviours import BEHAVIOURS as STORM_BEHAVIOURS


def _app(*behaviours: Behaviour) -> Any:
    """The mock serving exactly these behaviours, validated against the live tool surface."""
    return build_app(MockLlm(list(behaviours)))


async def _chat(app: Any, prompt: str, **model_kwargs: Any) -> AIMessageChunk:
    """One streamed turn against `app`, assembled by a real `ChatOpenAI`.

    In process over `ASGITransport` rather than on a socket: a port is a shared resource and this
    suite runs in parallel, and nothing about the contract under test is a property of TCP.
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mock"
    ) as client:
        from langchain_openai import ChatOpenAI

        llm = ChatOpenAI(
            model="mock",
            base_url="http://mock/v1",
            api_key=SecretStr("unused"),
            http_async_client=client,
            max_retries=0,
            **model_kwargs,
        )
        assembled: AIMessageChunk | None = None
        async for chunk in llm.astream([HumanMessage(prompt)]):
            assert isinstance(chunk, AIMessageChunk)
            assembled = chunk if assembled is None else assembled + chunk
        assert assembled is not None
        return assembled


def _post(app: Any, payload: dict[str, Any]) -> httpx.Response:
    """One non-streaming POST to `/v1/chat/completions`, for the shapes a client hides."""

    async def _drive() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://mock"
        ) as client:
            return await client.post("/v1/chat/completions", json=payload)

    return asyncio.run(_drive())


def test_streamed_usage_arrives_only_when_the_request_asked_for_it() -> None:
    """Streamed usage arrives only when the request asked for it.

    A gateway sends no usage without `stream_options`, so `ChatOpenAI(stream_usage=False)` assembles
    `usage_metadata: None`. The mock must match, or the escape hatch for endpoints rejecting
    `stream_options` looks metered in every lane while `turn_usage`, `api/budget.py`,
    `agent/spend_cap.py` and `context_budget.note_model_call` would in fact read nothing.
    """
    app = _app(Behaviour(name="plain", text="Done."))

    off = asyncio.run(_chat(app, "question [[plain]]", stream_usage=False))
    assert off.usage_metadata is None
    assert graph_usage_tokens(off).total == 0

    on = asyncio.run(_chat(app, "question [[plain]]", stream_usage=True))
    assert on.usage_metadata is not None
    assert on.usage_metadata["input_tokens"] == 900
    assert on.usage_metadata["output_tokens"] == 120
    assert graph_usage_tokens(on).total == 1020


def test_a_streamed_tool_call_assembles_to_exactly_one_call_and_a_tool_calls_finish() -> None:
    """A streamed tool call assembles to exactly one call and a `tool_calls` finish.

    As the gateway streams it: `id`/`type`/`function.name` once on an indexed slot, then
    argument-only fragments on the same `index`, and the terminal chunk carries `finish_reason:
    "tool_calls"` with usage. Three fragments, because repeating the name on a fragment makes a
    client assemble two calls.
    """
    app = _app(
        Behaviour(
            name="call",
            text="Answered.",
            calls=[ToolCall(tool="find_notes", arguments={"text": "benzene"}, fragments=3)],
        )
    )

    message = asyncio.run(_chat(app, "question [[call]]", stream_usage=True))

    assert len(message.tool_calls) == 1
    call = message.tool_calls[0]
    assert call["name"] == "find_notes"
    assert call["args"] == {"text": "benzene"}
    assert call["id"]
    assert message.response_metadata["finish_reason"] == "tool_calls"
    assert message.content == "Answered."
    assert message.usage_metadata is not None


def test_the_non_streaming_body_reports_the_same_turn_as_the_streamed_one() -> None:
    """Both protocols bill one turn identically; only the encoding differs.

    Non-streaming carries usage unconditionally on the gateway and here — `stream_options` is a
    streaming option — so this is the arm the fix above must *not* have changed.
    """
    app = _app(Behaviour(name="plain", text="Done."))

    body = _post(app, {"model": "mock", "messages": [{"role": "user", "content": "[[plain]]"}]})

    assert body.status_code == 200
    payload = body.json()
    assert payload["usage"] == {
        "prompt_tokens": 900,
        "completion_tokens": 120,
        "total_tokens": 1020,
    }
    assert payload["choices"][0]["finish_reason"] == "stop"
    assert payload["choices"][0]["message"] == {"role": "assistant", "content": "Done."}


def test_billed_input_follows_the_request_when_the_behaviour_names_no_constant() -> None:
    """Billed input follows the request size when the behaviour names no constant.

    A gateway bills in proportion to the prompt. `_Calibration.ratio()` clamps at 1.0 from below, so
    a constant bill could only ever show 1.0 and the calibration's tightening branch would be
    unreachable. `input_tokens=None` with factor 0.5 models an endpoint billing twice the
    approximate estimate, the direction the clamp lets through.
    """
    app = _app(
        Behaviour(name="plain", text="Done."),
        Behaviour(
            name="sized",
            text="Done.",
            input_tokens=None,
            input_tokens_per_char=0.5,
            output_tokens=0,
        ),
    )

    bills = []
    for chars in (25, 2_500, 20_000):
        body = _post(
            app,
            {
                "model": "mock",
                "messages": [{"role": "user", "content": "x" * chars + " [[sized]]"}],
            },
        )
        bills.append(body.json()["usage"]["prompt_tokens"])
    assert bills[0] < bills[1] < bills[2], bills

    # The same bill, read back through the client and folded into the calibration the way
    # `RecordContextCompaction` folds a real one.
    reset_calibration()
    try:
        for _ in range(5):
            prompt = HumanMessage("y" * 4_000 + " [[sized]]")
            answer = asyncio.run(_chat(app, str(prompt.content), stream_usage=True))
            billed = graph_usage_tokens(answer)
            note_model_call(count_tokens_approximately([prompt]), billed.input + billed.cache_read)
        assert estimator_ratio() > 1.0
    finally:
        reset_calibration()


def test_a_thread_over_the_endpoints_limit_is_refused_as_context_length() -> None:
    """A thread over the endpoint's limit is refused as a context-length 400.

    Matches the gateway's `400` with `"prompt is too long: ..."`, so
    `llm_provider._is_context_length`, `classify_model_failure`'s `context_length` label and the
    no-failover decision for a 400 are reachable from a lane. A request-level knob, so one behaviour
    serves a short thread and refuses a grown one, as a compaction lane needs.
    """
    app = _app(
        Behaviour(name="tight", text="Done.", input_tokens=None, refuse_over_input_tokens=200)
    )

    short = _post(app, {"model": "mock", "messages": [{"role": "user", "content": "[[tight]]"}]})
    assert short.status_code == 200

    long = _post(
        app,
        {"model": "mock", "messages": [{"role": "user", "content": "x" * 4_000 + " [[tight]]"}]},
    )
    assert long.status_code == 400
    error = long.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert "prompt is too long" in error["message"]

    try:
        asyncio.run(_chat(app, "[[tight]] " + "x" * 4_000, stream_usage=True))
    except Exception as exc:  # whichever class the client raises is what the classifier reads
        assert classify_model_failure(exc) == "context_length"
    else:
        pytest.fail("the mock served a request past its own limit")


def test_refusing_by_size_while_billing_a_constant_is_refused_at_startup() -> None:
    """Refusing by size while billing a constant is refused at startup.

    Such a behaviour would refuse every request or none, regardless of the thread.
    """
    with pytest.raises(ValueError, match="billing a constant"):
        MockLlm([Behaviour(name="bad", refuse_over_input_tokens=100)])


def test_a_cached_prefix_reaches_the_price_split_through_the_service_tier_prefix() -> None:
    """A cached prefix reaches the price split, through the service-tier prefix.

    The mock can emit `prompt_tokens_details.cached_tokens` so `turn_usage.graph_usage_tokens`'
    cache-read arithmetic (including tier prefixes like `priority_cache_read`) is driven over a
    wire. The cached share is inside `prompt_tokens`, so 900 billed with 400 cached is 500 priced
    input.
    """
    app = _app(
        Behaviour(name="cached", text="Done.", cached_tokens=400, service_tier="priority"),
    )

    message = asyncio.run(_chat(app, "question [[cached]]", stream_usage=True))

    assert message.usage_metadata is not None
    # Read off a plain dict: upstream's `InputTokenDetails` TypedDict declares only the bare
    # `cache_read`, which is the whole reason `turn_usage._cache_detail` matches by suffix
    # instead of by name — the tier-prefixed key is not in the type it arrives under.
    details = dict(message.usage_metadata["input_token_details"])
    assert details["priority_cache_read"] == 400
    usage = graph_usage_tokens(message)
    assert usage.cache_read == 400
    assert usage.input == 500
    assert usage.total == 1020


async def test_the_default_request_is_unchanged_on_the_wire() -> None:
    """A behaviour naming none of the new knobs emits exactly what every lane already gets.

    `prompt_tokens_details` and `service_tier` are absent rather than null when unused.
    """
    app = _app(Behaviour(name="plain", text="Done."))

    frames: list[dict[str, Any]] = []

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mock"
    ) as client:
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "mock",
                "stream": True,
                "stream_options": {"include_usage": True},
                "messages": [{"role": "user", "content": "[[plain]]"}],
            },
        ) as response:
            async for line in response.aiter_lines():
                if line.startswith("data: ") and line[6:].strip() != "[DONE]":
                    frames.append(json.loads(line[6:]))

    assert [frame.get("service_tier") for frame in frames] == [None] * len(frames)
    final = frames[-1]
    assert final["usage"]["prompt_tokens_details"] is None
    assert final["choices"][0]["finish_reason"] == "stop"


# ---------------------------------------------------------------------------------------------
# The two routes decide the same things, and only the encoding differs.


def _cross_wire_payload(name: str, *, chars: int = 0, tool_result: bool = False) -> dict[str, Any]:
    """One request body both handlers accept, so their decisions are comparable byte for byte.

    `select` and `already_has_tool_results` read both `messages` and `input`, so one body reaches
    the same behaviour on either route. It must be one body, since `_billed_input_tokens` bills
    `len(json.dumps(payload))` and two spellings would bill differently.
    """
    messages: list[dict[str, Any]] = [{"role": "user", "content": f"[[{name}]]" + "x" * chars}]
    if tool_result:
        # The second pass of a turn, in the shape `already_has_tool_results` reads.
        messages.append({"role": "tool", "tool_call_id": "call_probe", "content": "{}"})
    return {
        "model": "mock",
        "stream": True,
        # Ignored by `/v1/responses`, which always reports usage; asked for here so the chat arm
        # reports it too and the two bills are readable off the same request.
        "stream_options": {"include_usage": True},
        "messages": messages,
    }


def _frames(app: Any, route: str, payload: dict[str, Any]) -> tuple[int, Any]:
    """POST `payload` to `route` and return its status plus either its SSE frames or its body."""

    async def _drive() -> tuple[int, Any]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://mock"
        ) as client:
            async with client.stream("POST", route, json=payload) as response:
                if response.status_code != 200:
                    return response.status_code, json.loads(await response.aread())
                frames = [
                    json.loads(line[6:])
                    async for line in response.aiter_lines()
                    if line.startswith("data: ") and line[6:].strip() != "[DONE]"
                ]
                return response.status_code, frames

    return asyncio.run(_drive())


def _stats(app: Any) -> dict[str, int]:
    """`GET /__mock/stats`'s per-behaviour counter, the only readout of what `select` chose."""

    async def _drive() -> dict[str, int]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://mock"
        ) as client:
            body = await client.get("/__mock/stats")
            counts: dict[str, int] = body.json()["by_behaviour"]
            return counts

    return asyncio.run(_drive())


def _decided(app: Any, route: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Everything the two handlers' shared prelude decides, read back off whichever wire answered.

    Not the frame shapes, which differ by protocol and are pinned above: the selected behaviour,
    collapse to an answer, billed input, injected status, and size refusal.
    """
    before = _stats(app)
    status, body = _frames(app, route, payload)
    after = _stats(app)
    chosen = [name for name, count in after.items() if count > before.get(name, 0)]

    decided: dict[str, Any] = {
        "status": status,
        "behaviour": chosen,
        "error": body.get("error") if status != 200 else None,
        "billed_input": None,
        "tool_calls": [],
        "text": "",
    }
    if status != 200:
        return decided

    calls: list[str] = []
    text = ""
    billed: int | None = None
    for frame in body:
        if frame.get("type") == "response.output_item.added":  # /v1/responses
            calls.append(frame["item"]["name"])
        elif frame.get("type") == "response.output_text.delta":
            text += frame["delta"]
        elif frame.get("type") == "response.completed":
            billed = frame["response"]["usage"]["input_tokens"]
        elif frame.get("object") == "chat.completion.chunk":  # /v1/chat/completions
            delta = frame["choices"][0]["delta"] if frame["choices"] else {}
            for call in delta.get("tool_calls") or []:
                name = (call.get("function") or {}).get("name")
                if name is not None:
                    calls.append(name)
            text += delta.get("content") or ""
            if frame.get("usage"):
                billed = frame["usage"]["prompt_tokens"]
    decided["tool_calls"] = calls
    decided["text"] = text
    decided["billed_input"] = billed
    return decided


@pytest.mark.parametrize(
    "behaviour",
    [*STORM_BEHAVIOURS, *DELEGATION_BEHAVIOURS, *E2E_BEHAVIOURS],
    ids=lambda b: b.name,
)
@pytest.mark.parametrize(
    ("chars", "tool_result"),
    [(0, False), (20_000, False), (0, True)],
    ids=["short", "grown", "second-pass"],
)
def test_both_routes_decide_one_turn_the_same_way(
    behaviour: Behaviour, chars: int, tool_result: bool
) -> None:
    """Both routes decide one turn the same way, over every behaviour in every catalogue.

    `chat_completions` and the Responses handler are two transcriptions of one decision sequence.
    Every catalogue is driven, since lanes select behaviours by name from each. Three probes per
    behaviour cover request-dependent decisions: a short turn, one over `refuse_over_input_tokens`,
    and a pass carrying a tool result. `think_seconds` and `stream_seconds` are zeroed, as latency
    is not a decision.
    """
    served = replace(behaviour, think_seconds=0.0, stream_seconds=0.0)
    app = _app(served)
    payload = _cross_wire_payload(served.name, chars=chars, tool_result=tool_result)

    responses = _decided(app, "/v1/responses", payload)
    chat = _decided(app, "/v1/chat/completions", payload)

    assert responses == chat


def test_the_bind_address_defaults_to_loopback_and_a_pod_can_widen_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--host` lets a pod serve the mock to other pods; omitted, it binds loopback.

    Existing callers pass no flag and must keep exposing exactly what they did.
    """
    from chemclaw.cli import mock_llm

    bound: list[tuple[str, int]] = []
    monkeypatch.setattr(mock_llm, "catalogue", lambda _name: [])
    monkeypatch.setattr(
        "chemclaw.cli.mock_llm.uvicorn.run",
        lambda _app, host, port, **_kw: bound.append((host, port)),
    )
    mock_llm.main([])
    mock_llm.main(["--host", "0.0.0.0", "--port", "8821"])
    assert bound == [(mock_llm.MOCK_HOST, mock_llm.MOCK_PORT), ("0.0.0.0", 8821)]
