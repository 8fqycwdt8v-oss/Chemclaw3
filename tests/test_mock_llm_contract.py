"""What `cli/mock_llm` promises the real gateway also does — driven over its own wire.

**Every green result in the live lane is evidence about this mock.** `make live-storm`,
`live-soak`, `live-degradation`, `live-probes` and `infra/live/e2e-full-stack` all run against it,
so a divergence between what it emits and what an OpenAI-compatible gateway emits is not a
cosmetic inaccuracy — it is a control that reports itself working on traffic that could never have
exercised it. Measured 2026-09-07: nothing in this suite drove a turn through the mock's wire at
all (`grep -rln "chat.completion.chunk|ChatCompletionChunk" tests/` matched no file), so a
frame-shape regression in `_chat_stream` was caught by nothing, and four divergences had accumulated
behind that silence. `D-2026-09-07-a-mock-that-answers-unasked-hides-the-lane-that-asks-nothing`
records them.

**The numbers asserted here were measured against a real gateway, and the mock is what runs.** That
split is deliberate and is the only shape that works offline: the expectations come from
`https://api.anthropic.com/v1/chat/completions` — an OpenAI-compatible endpoint — probed on
2026-09-07, and each test says in its docstring what came back; the test itself needs no network
and no credential, because it drives `build_app` in process over `httpx.ASGITransport`. A real
`langchain_openai.ChatOpenAI` sits on top, so what is asserted is what the client *assembles*, not
what the mock intended — the same argument `cli/mock_llm` makes for talking HTTP rather than
injecting a chat client, one layer up.
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
    """`llm_stream_usage=False` must book zero here, because it books zero against a gateway.

    Measured 2026-09-07, streaming with no `stream_options`: the gateway put usage on **0 of 7**
    frames and `ChatOpenAI(stream_usage=False)` assembled `usage_metadata: None`. The mock put it
    on 1 of 7 unconditionally and reported `{"input_tokens": 900, …}` — so the one configuration
    `llm_stream_usage` exists to serve (an endpoint that rejects `stream_options`) looked, on every
    mock-driven lane, exactly like the metered one.

    That is not a cosmetic difference. `turn_usage`, `api/budget.py`, `agent/spend_cap.py` and
    `context_budget.note_model_call` all read this one field, so the escape hatch disarms metering
    — and `_openai_compatible_model`'s docstring records that failure having shipped once already,
    reached by a different route. The assertion is the red line: a deployment that turns the hatch
    off meters nothing, and this is where that is said rather than believed.
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
    """The frame shape nothing in this suite was watching, pinned against the gateway's own.

    Measured 2026-09-07 against the gateway: a streamed call arrives as an indexed slot carrying
    `id`/`type`/`function.name` once, then argument-only fragments on the same `index`, and the
    terminal chunk carries `finish_reason: "tool_calls"` with usage on that same chunk rather than
    on a trailing choice-less one. `fragments=3` here because repeating the name on a fragment is
    what makes a client assemble two calls out of one — the hazard `_chat_stream` documents and
    that no test could see.
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
    """A constant bill pins the estimator calibration at its clamp, whatever the lane does.

    Measured 2026-09-07: the gateway billed 12, 321 and 12,509 input tokens for prompts of 25,
    2,500 and 100,000 characters; the mock billed **900** for all three. `_Calibration.ratio()`
    clamps at 1.0 from below, so a lane whose bill never grows can only ever observe a ratio under
    1 and reports exactly 1.0 — the EWMA, `agent_context_calibration_max_factor` and the ">1.0
    tightens the budget" branch that D-2026-08-28 and D-2026-09-04 both rest on are unreachable
    from any measurement, only from hand-fed unit numbers.

    `input_tokens=None` with a factor of 0.5 is an endpoint whose tokenizer bills twice what
    `count_tokens_approximately` estimates — the direction that matters, because that is the one
    the clamp lets through.
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
    """The one request-level failure `http_status` cannot express, and the label it unlocks.

    Measured 2026-09-07, a 300,000-word prompt to the gateway: `400` with
    `{"error": {"code": "invalid_request_error", "message": "prompt is too long: 300024 tokens >
    200000 maximum", "type": "invalid_request_error", "param": null}}`. The mock answered **200**
    to the same request, so `llm_provider._is_context_length` — and therefore
    `classify_model_failure`'s `context_length` label, and the `_failover_exceptions` decision not
    to fail a 400 over — had no lane that could reach it. The marker it matches on
    (`"prompt is too long"`) had been added on faith; this is the arm that checks it.

    A *request*-level knob rather than a per-behaviour status because that is the whole point: the
    same behaviour serves a short thread and refuses a grown one, which is what a compaction lane
    needs and what an injected status can never be.
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
    """The two knobs only mean anything together, so the mock says so before it serves.

    A behaviour that refuses over a token count while billing a constant refuses every request or
    none, whatever the thread does — which is the per-behaviour failure `http_status` already is,
    wearing the name of a request-level one.
    """
    with pytest.raises(ValueError, match="billing a constant"):
        MockLlm([Behaviour(name="bad", refuse_over_input_tokens=100)])


def test_a_cached_prefix_reaches_the_price_split_through_the_service_tier_prefix() -> None:
    """`prompt_tokens_details.cached_tokens`, and the tier prefix that hides it.

    `turn_usage.graph_usage_tokens` carries ~60 lines of arithmetic about cache reads and the
    service-tier prefixes upstream puts on them (`priority_cache_read`), and every line of it was
    covered by hand-built mappings only: the mock omitted `prompt_tokens_details` entirely, so
    `chemclaw_cache_read_tokens_total` read 0 on every lane and no lane could have shown otherwise.
    Measured 2026-09-07, the gateway this gets probed against reports no
    `prompt_tokens_details` either — an OpenAI, vLLM or LiteLLM gateway does — so the mock is the
    only place the price split can be driven over a wire at all.

    The cached share is *inside* `prompt_tokens`, which is OpenAI's own definition and why the
    reader subtracts it: 900 billed with 400 cached is 500 of priced input, not 1,300.
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


def test_the_default_request_is_unchanged_on_the_wire() -> None:
    """A behaviour that names none of the new knobs emits exactly what every lane already gets.

    The regression guard on all of the above: `prompt_tokens_details` and `service_tier` are
    *absent* rather than null-valued when unused, so a live lane's frames are byte-identical to the
    ones it was passing on before this contract existed.
    """
    app = _app(Behaviour(name="plain", text="Done."))

    frames: list[dict[str, Any]] = []

    async def _drive() -> None:
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

    asyncio.run(_drive())

    assert [frame.get("service_tier") for frame in frames] == [None] * len(frames)
    final = frames[-1]
    assert final["usage"]["prompt_tokens_details"] is None
    assert final["choices"][0]["finish_reason"] == "stop"


# ---------------------------------------------------------------------------------------------
# The two routes decide the same things, and only the encoding differs.


def _cross_wire_payload(name: str, *, chars: int = 0, tool_result: bool = False) -> dict[str, Any]:
    """One request body both handlers accept, so their decisions are comparable byte for byte.

    `messages` rather than `input` on purpose, and it is not a chat-completions bias: `select`
    scans both keys and `already_has_tool_results` reads both shapes, so a single body reaches the
    same behaviour and the same collapse verdict on either route. It has to be *one* body rather
    than two equivalent ones because `_billed_input_tokens` bills `len(json.dumps(payload))` when
    the behaviour names no constant — two spellings of the same turn would bill differently and
    the comparison would be about the payloads instead of about the handlers.
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

    Deliberately *not* the frame shapes: those differ by protocol and are each pinned above. What
    is compared here is the sequence of decisions — which behaviour was selected, whether the
    turn collapsed to an answer, what the request was billed, whether a status was injected and
    whether the request was refused for its size.
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


@pytest.mark.parametrize("behaviour", STORM_BEHAVIOURS, ids=lambda b: b.name)
@pytest.mark.parametrize(
    ("chars", "tool_result"),
    [(0, False), (20_000, False), (0, True)],
    ids=["short", "grown", "second-pass"],
)
def test_both_routes_decide_one_turn_the_same_way(
    behaviour: Behaviour, chars: int, tool_result: bool
) -> None:
    """The property `chat_completions`'s docstring asserts in prose, driven over both wires.

    That docstring says the handler is "deliberately the *same sequence of decisions* as the
    Responses one", and until this existed the only thing holding the two copies together was that
    sentence — a property asserted in prose across two transcriptions of it, which is the shape
    this repository spends `D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit` arguing
    against. The whole storm catalogue is driven because the divergence that matters is the one in
    a behaviour nobody re-read: every lane in `infra/live/` selects by name out of *this* list.

    Three probes per behaviour, because three of the shared decisions are properties of the
    request rather than of the behaviour: a short turn, one grown past `refuse_over_input_tokens`,
    and a second pass carrying a tool result (the collapse to an answer). `think_seconds` is
    zeroed — latency is the one thing in a behaviour that is not a decision, and `f-slow` declares
    eight seconds of it.
    """
    served = replace(behaviour, think_seconds=0.0)
    app = _app(served)
    payload = _cross_wire_payload(served.name, chars=chars, tool_result=tool_result)

    responses = _decided(app, "/v1/responses", payload)
    chat = _decided(app, "/v1/chat/completions", payload)

    assert responses == chat
