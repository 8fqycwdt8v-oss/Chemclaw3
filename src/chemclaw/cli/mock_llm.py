"""`python -m chemclaw.cli.mock_llm` — an OpenAI-compatible mock, to drive the system hard.

The point is not to avoid paying for tokens. It is that a real model cannot be asked for the inputs
that actually break this system: an empty function name (STREAM-1), a malformed argument document,
four hundred argument fragments, forty parallel calls in one turn, or a turn with no prose at all.
Every one of those has been a live defect here, and none of them is reachable by prompting. A mock
makes them a parameter.

It speaks the wire, not the Python — **both chat protocols, because the engine changed under it.**
`/v1/responses` came first: the Microsoft Agent Framework's `OpenAIChatClient` resolved to the
Responses client, and the previous generation of this idea took 37 × HTTP 404 on exactly that point
(`docs/archive/load-test-2026-07.md`). The LangGraph rebuild builds a `ChatOpenAI`, which posts to
`/v1/chat/completions` — and nothing here followed, so from that day every credential-free lane got
a bare 404 and every turn died with no answer and no tool call. The lesson is the one the 404s
taught the first time, repeated because the *other* side moved: a mock of a protocol is pinned to
whichever client is actually built, and neither an ADR nor a docstring notices when that changes.

Talking HTTP rather than injecting a `BaseChatClient` is also the only way to exercise what actually
broke before: the streaming assembler, the middleware stack, budget admission, the audit sink and
the session store all sit between the socket and the agent, and the in-process scripted client in
`tests/` bypasses every one of them — its own docstring records passing green while production
failed 100 % of the time.

**The argument names come from the real tools, and this is the whole design.** LOAD-1: the previous
stub emitted `{"query": "benzene"}` where `find_notes` takes `text`, so every call died in the
parse-error branch *before the tool body ran*, and the run was published as "100 tool calls, the
tool path is genuinely exercised". Nothing was exercised. So a `Behaviour` here is validated at
startup against the live tool surface, and one naming a tool or an argument the system does not have
refuses to serve rather than quietly producing a green run over nothing. `adversarial=True` opts out
of that check — explicitly, per behaviour, because emitting what the real tool would reject is
precisely what the adversarial family is for.

**Every green result in the live lane is evidence about this file, so where it is kinder than a
gateway it disables a control**
(`D-2026-09-07-a-mock-that-answers-unasked-hides-the-lane-that-asks-nothing`).
Measured against a real OpenAI-compatible endpoint on 2026-09-07, three things here were generous
in ways that made a real deployment's failure unreachable: usage was reported on a stream that had
not asked for it (so `llm_stream_usage=False` — the escape hatch that books **zero** on every turn —
looked exactly like the metered configuration), the input bill was a constant (so
`context_budget`'s calibration could only ever read its 1.0 clamp), and no request could be refused
for its own size (so `classify_model_failure`'s `context_length` label had no lane at all).
`tests/test_mock_llm_contract.py` is what holds the wire to the measured shapes now, and it is also
the first thing in this suite to drive a turn through this mock at all — a frame-shape regression in
`_chat_stream` was caught by nothing before it. The general rule it leaves behind: **a mock may be
narrower than the endpoint it stands in for, never more forgiving** — an omission costs coverage
loudly, and a kindness costs it silently.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field, replace
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from chemclaw.core.bounded import BoundedLru
from chemclaw.core.logging import configure_logging

logger = logging.getLogger(__name__)

# Where the mock listens. A dev-only affordance, so a module constant rather than a config field —
# the same call `cli/connectors_dev.py` makes, and for the same reason: nothing in a deployment
# reads it.
MOCK_HOST = "127.0.0.1"
MOCK_PORT = 8820
# The address a caller must configure to reach this mock, spelled once so nothing has to rebuild it
# from the two constants above. `Settings.llm_base_url` ships exactly this value
# (`test_the_default_gateway_is_the_mock_on_this_machine` pins the two together), and
# `infra/live/processes.sh` decides whether to start the mock by comparing the resolved setting
# against *this* string rather than against a copy of it — a shell literal that has to match a
# Python default is a duplication that goes wrong silently, and did: the lane gated the mock on
# `$CHEMCLAW_LLM_BASE_URL` being the literal, nothing set that variable once it became a default,
# so `make live-up` started a front door pointed at a port nothing was serving.
MOCK_BASE_URL = f"http://{MOCK_HOST}:{MOCK_PORT}/v1"


@dataclass
class ToolCall:
    """One function call the mock will emit, and how finely to slice its arguments.

    `fragments` is the knob that matters. The OpenAI Responses client emits every
    `response.function_call_arguments.delta` carrying *both* the name and a non-empty argument
    fragment, which is the shape that once made the front door announce N `ToolCallEvent`s for one
    call, each holding a partial argument document. Nothing reassembles fragments any more — the
    graph hands a finished tool call over on its `updates` stream and `api/graph_stream.py`
    deliberately does not read the fragmented chunks — so this field now exercises the *client*
    against a real streamed call rather than a reassembler downstream of it.
    """

    tool: str
    arguments: dict[str, Any]
    fragments: int = 1
    # Emitted verbatim instead of `json.dumps(arguments)` when set — the only way to produce a
    # document the tool layer must reject (unbalanced braces, a bare string, 100 KB of nothing).
    raw_arguments: str | None = None


@dataclass
class Behaviour:
    """What the mock does for one turn: some tool calls, some text, and how slowly.

    A behaviour is deliberately a *plan*, not a reaction to the prompt. The storm needs to know
    exactly what the system was asked to do in order to check what it did; a mock that improvised
    would put the thing under test on both sides of the comparison.
    """

    name: str
    calls: list[ToolCall] = field(default_factory=list)
    text: str = "Done."
    # Seconds of pretend thinking before the first frame, then between frames. Real endpoints are
    # slow and this system's concurrency behaviour is entirely about what happens while turns are
    # in flight — a zero-latency mock measures a system nobody runs.
    think_seconds: float = 0.0
    # Fail the HTTP call itself. NB `llm_max_retries=3`, so the SDK will retry this three times:
    # one injected failure is four requests, and a storm that forgot would mis-attribute the load.
    http_status: int = 200
    # Skip the startup validation below. Only the adversarial family sets this.
    adversarial: bool = False
    # Tokens reported as this request's input. Without a usage block `usage_tokens` records zero
    # and budget admission is silently never pressured — the run would "pass" a gate it never met.
    #
    # **A constant by default, and `None` is what a calibration lane needs.** A real gateway bills
    # for the request it was sent: measured 2026-09-07 against the gateway, one prompt of 25, 2,500
    # and 100,000 characters billed 12, 321 and 12,509 input tokens. This mock billed 900 for all
    # three. That is not merely unrealistic — it disables a control:
    # `agent/context_budget._Calibration` divides billed by estimated (chars/4) and clamps the
    # result at 1.0 from below, so a lane whose billed input never grows can only ever observe a
    # ratio *under* 1 and therefore reports exactly 1.0 forever. Measured over 50 calls of a
    # 10,000-token estimate: `estimator_ratio()` = 1.0 on this mock's numbers, 1.34 on the fleet's
    # real ones. The EWMA, `agent_context_calibration_max_factor` and the whole ">1.0 tightens the
    # budget" branch that D-2026-08-28 and D-2026-09-04 rest on are unreachable from any lane.
    #
    # `None` therefore bills the *serialized request* at `input_tokens_per_char`, so a thread that
    # grows costs more and a behaviour that names a factor above 0.25 (the estimator's own chars/4)
    # drives a calibration ratio above 1 — including above `max_factor`, which is the arm nothing
    # but a hand-fed unit test has ever driven. The constant stays the default because a behaviour
    # asserting a fixed token count needs determinism, and because every existing lane is written
    # against it.
    input_tokens: int | None = 900
    # Billed input tokens per character of serialized request, when `input_tokens` is None. 0.25 is
    # `count_tokens_approximately`'s own chars/4, i.e. an endpoint this system estimates perfectly;
    # a larger value is a tokenizer this system undercounts, which is the direction that matters.
    input_tokens_per_char: float = 0.25
    output_tokens: int = 120
    # The cached share of the input, published as `prompt_tokens_details.cached_tokens` — the key
    # `langchain_openai._create_usage_metadata` turns into `input_token_details["cache_read"]` and
    # `agent/turn_usage.graph_usage_tokens` subtracts back out of priced input. Omitted from the
    # wire entirely when 0, which is what the gateway this mock is measured against does, so the
    # default request is byte-identical to the one every lane already gets.
    cached_tokens: int = 0
    # `service_tier` on the response body. Only `priority` and `flex` mean anything: upstream reads
    # the tier off the *response* and prefixes both cache keys with it (`priority_cache_read`), and
    # `turn_usage._cache_detail` matches by suffix for exactly that reason. That suffix match has
    # never been driven by anything but a hand-built mapping; this is the knob that lets a lane
    # drive it over the wire.
    service_tier: str = ""
    # Refuse any request whose billed input exceeds this, the way a real gateway refuses a thread
    # that no longer fits: HTTP 400, `invalid_request_error`, "prompt is too long: N tokens > M
    # maximum" — the vendor wording `llm_provider._CONTEXT_LENGTH_MARKERS` already matches.
    # 0 means never. This is the only failure `http_status` cannot express, because it is a property
    # of the *request* rather than of the behaviour: the same behaviour serves a short thread and
    # refuses a grown one, which is what makes `classify_model_failure`'s `context_length` label —
    # and the compaction policy that exists to prevent it — reachable from a lane at all.
    refuse_over_input_tokens: int = 0


def already_has_tool_results(payload: dict[str, Any]) -> bool:
    """Whether this request already carries the output of a previous tool call.

    **A model that always calls a tool never finishes.** The agent re-invokes the model after each
    tool result, so a mock that replays its behaviour verbatim each time drives the agent round its
    loop until the iteration cap — the first storm turn made 41 tool calls for a behaviour that
    declares one. A real model calls tools, reads what came back, and then answers, and the mock
    has to do the same or it is testing a runaway rather than the system.

    Detected from the request rather than from per-session state on purpose: the mock stays
    stateless, so concurrent turns cannot interfere with each other's step counters — which at the
    concurrency this harness offers would be a race that looked like an application defect.

    **Both request shapes, because the two protocols say it differently.** The Responses API carries
    a `function_call_output` item in `input`; chat completions carries a `role: "tool"` message in
    `messages`. Reading only the first would drive the chat-completions route round the loop until
    the iteration cap — the same runaway this function was written to stop, one protocol over.
    """
    payload_input = payload.get("input")
    if isinstance(payload_input, list) and any(
        isinstance(item, dict) and item.get("type") == "function_call_output"
        for item in payload_input
    ):
        return True
    messages = payload.get("messages")
    return isinstance(messages, list) and any(
        isinstance(item, dict) and item.get("role") == "tool" for item in messages
    )


def _validate(behaviour: Behaviour) -> None:
    """Refuse a behaviour whose tool or arguments the real system would reject (the LOAD-1 guard).

    Resolved against the live surface rather than a copy of it: `available_tool_names` is what the
    agent actually advertises, and the registry holds the callable whose signature the schema is
    derived from. A behaviour that passes here cannot fail for the reason every measurement in the
    previous load test failed.
    """
    from chemclaw.agent.chemclaw_agent import available_tool_names
    from chemclaw.core.tool_registry import registered_tools

    # Checked before the adversarial opt-out, because `adversarial` waives the *tool surface* check
    # — the thing a deliberately malformed call is for — and says nothing about this mock's own
    # arithmetic. A behaviour that refuses over a token count while billing a constant would refuse
    # every request or none, whatever the thread did, which is the opposite of the request-level
    # failure the knob exists to produce.
    if behaviour.refuse_over_input_tokens and behaviour.input_tokens is not None:
        raise ValueError(
            f"behaviour {behaviour.name!r} refuses over "
            f"{behaviour.refuse_over_input_tokens} input tokens while billing a constant "
            f"{behaviour.input_tokens}. Set `input_tokens=None` so the bill follows the request, "
            "or the refusal is a property of the behaviour rather than of the thread."
        )
    if behaviour.adversarial:
        return
    known = set(available_tool_names())
    by_name = {fn.__name__: fn for fn in registered_tools()}
    for call in behaviour.calls:
        if call.tool not in known:
            raise ValueError(
                f"behaviour {behaviour.name!r} calls {call.tool!r}, which the agent does not "
                f"advertise. Mark the behaviour adversarial if that is the point, or fix the name."
            )
        if call.raw_arguments is not None:
            raise ValueError(
                f"behaviour {behaviour.name!r} sends raw arguments for {call.tool!r}; that can "
                "only be deliberate, so mark the behaviour adversarial."
            )
        fn = by_name.get(call.tool)
        if fn is None:  # an MCP connector tool — its schema lives in the bundle, not in-process
            continue
        annotations = {k: v for k, v in getattr(fn, "__annotations__", {}).items() if k != "return"}
        unknown = set(call.arguments) - set(annotations)
        if unknown:
            raise ValueError(
                f"behaviour {behaviour.name!r} passes {sorted(unknown)} to {call.tool!r}, which "
                f"takes {sorted(annotations)}. This is exactly LOAD-1: the call would die in the "
                "parse-error branch before the tool body ran, and the run would report it as a "
                "tool call that happened."
            )


class MockLlm:
    """The scripted endpoint: a queue of behaviours, plus a count of what was actually asked of it.

    The counter is not bookkeeping. "No LLM calls were made" is a claim the storm has to be able to
    *prove*, and reconciling this number against the turn count is how — an unset API key proves
    only that Anthropic was not reached.
    """

    def __init__(self, behaviours: Iterable[Behaviour]) -> None:
        """Validate every behaviour against the live tool surface before serving any of them."""
        self._behaviours = list(behaviours)
        for behaviour in self._behaviours:
            _validate(behaviour)
        self._by_name = {b.name: b for b in self._behaviours}
        # response id -> behaviour name, so the second call of a turn continues the first.
        self._chain: BoundedLru[str, str] = BoundedLru(20_000)
        self.requests = 0
        self.by_behaviour: dict[str, int] = {}
        self._default = self._behaviours[0] if self._behaviours else Behaviour(name="empty")

    def select(self, payload: dict[str, Any]) -> Behaviour:
        """Pick the behaviour this request continues, by chain first and then by marker.

        Selection is by explicit marker rather than by matching the prompt, so a storm scenario and
        the behaviour it expects cannot drift apart.

        **The chain lookup is not an optimisation; without it the mock answers as the wrong
        behaviour.** Measured: the client's first call carries the user message (marker present),
        and its second carries `previous_response_id` plus *only* the `function_call_output` — the
        marker is gone. Falling back to the default there meant every turn's final prose came from
        whichever behaviour happened to be first in the catalogue, so `f-no-text` reported an answer
        it never wrote and the `text` field of every other behaviour was dead. That is LOAD-1's
        shape again, one layer up: the harness measuring something other than what it named.

        **Chat completions needs no chain, and that is a property of the protocol rather than an
        omission.** It has no `previous_response_id`; the client resends the whole conversation on
        every call, so the user message carrying the marker is present on the second pass and the
        marker scan below finds it unaided. The chain exists only because the Responses API drops
        everything but the tool output on continuation.
        """
        previous = payload.get("previous_response_id")
        if isinstance(previous, str):
            name = self._chain.get(previous)
            if name is not None:
                return self._by_name[name]
        text = json.dumps([payload.get("input", ""), payload.get("messages", "")])
        for name, behaviour in self._by_name.items():
            if f"[[{name}]]" in text:
                return behaviour
        return self._default

    def remember(self, response_id: str, behaviour: Behaviour) -> None:
        """Bind a minted response id to the behaviour that produced it, for the next call.

        Bounded, because a soak mints one id per model call and an unbounded map keyed by a
        generated id is the growth bug this codebase has already fixed three times. `BoundedLru` is
        the one eviction policy those four call sites were consolidated onto.
        """
        self._chain.put(response_id, behaviour.name)

    def record(self, behaviour: Behaviour) -> None:
        """Count one served request, per behaviour."""
        self.requests += 1
        self.by_behaviour[behaviour.name] = self.by_behaviour.get(behaviour.name, 0) + 1


def _fragments(document: str, count: int) -> list[str]:
    """Slice an argument document into `count` roughly equal pieces, never losing a character."""
    if count <= 1 or not document:
        return [document]
    size = max(1, len(document) // count)
    pieces = [document[i : i + size] for i in range(0, len(document), size)]
    return pieces


def _billed_input_tokens(behaviour: Behaviour, payload: dict[str, Any]) -> int:
    """What this request is billed for its input: a constant, or the request's own size.

    The size is the *serialized request* rather than the message text, because that is what the
    thing being calibrated measures: `agent/context_budget` estimates prefix and thread together —
    system message, skills listing, every bound tool schema — and comparing a bill for the messages
    against an estimate of the whole request is the half-a-comparison defect
    `D-2026-09-05-a-ratchet-that-re-derives-half-its-basis-bounds-half-a-request` is about, one
    layer down.

    Never 0: `note_model_call` drops a sample with a non-positive bill, so a mock that billed 0 for
    an empty request would look like a lane that measured nothing rather than one that measured a
    tiny request.
    """
    if behaviour.input_tokens is not None:
        return behaviour.input_tokens
    return max(1, round(len(json.dumps(payload)) * behaviour.input_tokens_per_char))


def _chat_usage(behaviour: Behaviour, billed_input: int) -> dict[str, Any]:
    """The chat-completions `usage` block, with the cached breakdown only when there is one.

    `prompt_tokens` *includes* the cached share — OpenAI's own definition, and what
    `turn_usage.graph_usage_tokens` subtracts back out — so a behaviour naming `cached_tokens`
    does not add to the bill, it says how much of it was cheap.
    """
    usage: dict[str, Any] = {
        "prompt_tokens": billed_input,
        "completion_tokens": behaviour.output_tokens,
        "total_tokens": billed_input + behaviour.output_tokens,
    }
    if behaviour.cached_tokens:
        usage["prompt_tokens_details"] = {"cached_tokens": behaviour.cached_tokens}
    return usage


def _oversize_refusal(billed_input: int, limit: int) -> JSONResponse:
    """The 400 a real gateway returns for a thread that no longer fits, field for field.

    Measured 2026-09-07 against the gateway: `{"error": {"code": "invalid_request_error",
    "message": "prompt is too long: 300024 tokens > 200000 maximum", "type":
    "invalid_request_error", "param": null}}`. The wording is the vendor's, relayed through the
    gateway, which is why `llm_provider._CONTEXT_LENGTH_MARKERS` matches on it — and this is the
    only way anything in this tree reaches `classify_model_failure`'s `context_length` label
    without hand-building an exception.
    """
    message = f"prompt is too long: {billed_input} tokens > {limit} maximum"
    return JSONResponse(
        {
            "error": {
                "code": "invalid_request_error",
                "message": message,
                "type": "invalid_request_error",
                "param": None,
            }
        },
        status_code=400,
    )


@dataclass(frozen=True)
class DecidedTurn:
    """What both chat routes have decided by the time they differ: a behaviour, a bill, a model."""

    behaviour: Behaviour
    billed_input: int
    model: str


def decide_turn(mock: MockLlm, payload: dict[str, Any]) -> DecidedTurn | JSONResponse:
    """The sequence of decisions both `/v1/responses` and `/v1/chat/completions` make, once.

    Either the turn to encode, or the `JSONResponse` that ends the request instead — a refusal is
    a decision, and returning it here is what keeps the two routes from each having to remember
    the order the refusals come in.

    **One function because the property is "the same sequence of decisions", and that was asserted
    in prose across two verbatim copies.** `chat_completions` said so in its own docstring while
    the sequence lived twice; the copies had already begun to drift in their *commentary* (only
    one carried the note below about the two refusals' order), which is how a copy drifts before
    it drifts. The storm's scenarios are written against these decisions rather than against
    either encoding, so a lane that passes on one protocol has to mean the same thing on the other.

    What deliberately stays outside: `mock.remember`, because only the Responses API has a
    `previous_response_id` to chain from, and the minting of a `resp_…`/`chatcmpl-…` id, because
    the id space is the protocol's.
    """
    behaviour = mock.select(payload)
    # Second and later passes of the same turn answer instead of calling again — see
    # `already_has_tool_results`. `dataclasses.replace` rather than mutation: the catalogue is
    # shared across every concurrent turn and must stay immutable.
    #
    # The text is carried through *unchanged*, including when it is empty. Substituting a default
    # here quietly defeated the scenarios whose whole point is a turn that writes nothing:
    # `f-no-text` reported `answered=True` on its first run, because this line had helpfully
    # invented an answer for it.
    if behaviour.calls and already_has_tool_results(payload):
        behaviour = replace(behaviour, calls=[])
    mock.record(behaviour)
    if behaviour.http_status != 200:
        # Deliberate transport failure. The SDK retries `llm_max_retries` times, so the storm
        # counts requests here rather than inferring them from turns.
        return JSONResponse(
            {"error": {"message": "injected failure", "type": "server_error"}},
            status_code=behaviour.http_status,
        )
    billed_input = _billed_input_tokens(behaviour, payload)
    # After the injected status, because a behaviour that declares a failure means that one.
    if behaviour.refuse_over_input_tokens and billed_input > behaviour.refuse_over_input_tokens:
        return _oversize_refusal(billed_input, behaviour.refuse_over_input_tokens)
    return DecidedTurn(
        behaviour=behaviour, billed_input=billed_input, model=str(payload.get("model", "mock"))
    )


def _response_object(
    response_id: str, model: str, behaviour: Behaviour, billed_input: int
) -> dict[str, Any]:
    """The `Response` body both the streaming and non-streaming paths report.

    `status` is always `completed`. `in_progress` or `queued` makes the client mint a continuation
    token and then poll `GET /responses/{id}` — a second protocol to implement for no coverage.
    """
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": model,
        "output": [],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": billed_input,
            "input_tokens_details": {
                "cached_tokens": behaviour.cached_tokens,
                "cache_write_tokens": 0,
            },
            "output_tokens": behaviour.output_tokens,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": billed_input + behaviour.output_tokens,
        },
    }


async def _stream(
    behaviour: Behaviour, model: str, response_id: str, billed_input: int
) -> AsyncIterator[str]:
    """The SSE frames for one turn, in the order the SDK's discriminated union accepts them.

    Every frame is constructed as the SDK's own model and dumped, rather than hand-written JSON:
    the SDK validates each event before the agent ever sees it, so a frame this mock got subtly
    wrong would raise inside the client and read as an application defect. Building through the
    model makes that failure impossible to ship.
    """
    from openai.types.responses import (
        Response,
        ResponseCompletedEvent,
        ResponseCreatedEvent,
        ResponseFunctionCallArgumentsDeltaEvent,
        ResponseFunctionToolCall,
        ResponseOutputItemAddedEvent,
        ResponseTextDeltaEvent,
    )

    # Validated into the SDK's own `Response` rather than passed as a dict: the client deserializes
    # every frame before the agent sees it, so a body this mock got subtly wrong would raise inside
    # SDK and read as an application defect. Building through the model makes that unshippable.
    body = Response.model_validate(_response_object(response_id, model, behaviour, billed_input))
    sequence = 0

    def frame(event: Any) -> str:
        return f"data: {event.model_dump_json()}\n\n"

    if behaviour.think_seconds:
        await asyncio.sleep(behaviour.think_seconds)

    yield frame(ResponseCreatedEvent(type="response.created", response=body, sequence_number=0))
    sequence += 1

    for index, call in enumerate(behaviour.calls):
        call_id = f"call_{uuid.uuid4().hex[:16]}"
        item_id = f"fc_{uuid.uuid4().hex[:16]}"
        yield frame(
            ResponseOutputItemAddedEvent(
                type="response.output_item.added",
                output_index=index,
                sequence_number=sequence,
                item=ResponseFunctionToolCall(
                    id=item_id,
                    type="function_call",
                    call_id=call_id,
                    name=call.tool,
                    arguments="",
                    status="in_progress",
                ),
            )
        )
        sequence += 1
        document = (
            call.raw_arguments
            if call.raw_arguments is not None
            else json.dumps(call.arguments, separators=(",", ":"))
        )
        for piece in _fragments(document, call.fragments):
            yield frame(
                ResponseFunctionCallArgumentsDeltaEvent(
                    type="response.function_call_arguments.delta",
                    delta=piece,
                    item_id=item_id,
                    output_index=index,
                    sequence_number=sequence,
                )
            )
            sequence += 1
            if behaviour.think_seconds:
                await asyncio.sleep(behaviour.think_seconds / max(call.fragments, 1))

    if behaviour.text:
        text_index = len(behaviour.calls)
        for chunk in (behaviour.text[i : i + 40] for i in range(0, len(behaviour.text), 40)):
            yield frame(
                ResponseTextDeltaEvent(
                    type="response.output_text.delta",
                    delta=chunk,
                    content_index=0,
                    item_id=f"msg_{response_id}",
                    output_index=text_index,
                    sequence_number=sequence,
                    logprobs=[],
                )
            )
            sequence += 1

    yield frame(
        ResponseCompletedEvent(type="response.completed", response=body, sequence_number=sequence)
    )
    yield "data: [DONE]\n\n"


async def _chat_stream(
    behaviour: Behaviour,
    model: str,
    completion_id: str,
    billed_input: int,
    *,
    include_usage: bool,
) -> AsyncIterator[str]:
    """The same turn as `_stream`, in chat-completions frames.

    A second encoding of one behaviour rather than a second mock, because the behaviour catalogue —
    and the LOAD-1 guard that validates it against the live tool surface — is the part with the
    value in it. Only the wire shape differs.

    Built through `ChatCompletionChunk` for the reason `_stream` builds through the Responses
    models: `langchain_openai` deserializes every frame before the agent sees it, so a chunk this
    mock got subtly wrong would raise inside the client and read as an application defect.

    The tool-call encoding is the part worth naming. Chat completions streams a call as deltas over
    an *indexed* slot: the first delta carries `id`, `type` and `function.name`, and every later one
    carries only an argument fragment against the same `index`. Sending the name again on a
    fragment makes the client assemble two calls out of one — the reassembly hazard `graph_stream`
    refuses to read calls from the token stream because of.

    Args:
        behaviour: The turn to encode — its calls, its prose and its billed output.
        model: The model name to echo back on every chunk.
        completion_id: The `chatcmpl-…` id every chunk of this turn carries.
        billed_input: What `_billed_input_tokens` decided this request costs in input.
        include_usage: Whether the request sent `stream_options.include_usage`. See the terminal
            frame below — a gateway reports streamed usage only when asked, and this mock used to
            report it either way.
    """
    from openai.types.chat import ChatCompletionChunk

    created = int(time.time())

    def frame(choice: dict[str, Any], **extra: Any) -> str:
        chunk = ChatCompletionChunk.model_validate(
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "finish_reason": None, **choice}],
                # Upstream reads the tier off the chunk and prefixes both cache keys with it, so
                # this has to ride every frame the usage might land on rather than the body alone.
                **({"service_tier": behaviour.service_tier} if behaviour.service_tier else {}),
                **extra,
            }
        )
        return f"data: {chunk.model_dump_json()}\n\n"

    if behaviour.think_seconds:
        await asyncio.sleep(behaviour.think_seconds)

    yield frame({"delta": {"role": "assistant"}})

    for index, call in enumerate(behaviour.calls):
        yield frame(
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": f"call_{uuid.uuid4().hex[:16]}",
                            "type": "function",
                            "function": {"name": call.tool, "arguments": ""},
                        }
                    ]
                }
            }
        )
        document = (
            call.raw_arguments
            if call.raw_arguments is not None
            else json.dumps(call.arguments, separators=(",", ":"))
        )
        for piece in _fragments(document, call.fragments):
            # No `name` and no `id`: this is a continuation of slot `index`, and repeating either
            # is what makes a client announce one call twice.
            yield frame(
                {"delta": {"tool_calls": [{"index": index, "function": {"arguments": piece}}]}}
            )
            if behaviour.think_seconds:
                await asyncio.sleep(behaviour.think_seconds / max(call.fragments, 1))

    if behaviour.text:
        for chunk_text in (behaviour.text[i : i + 40] for i in range(0, len(behaviour.text), 40)):
            yield frame({"delta": {"content": chunk_text}})

    # Usage rides the final frame — measured 2026-09-07 against the gateway, which puts it on the
    # same chunk as `finish_reason` rather than on a trailing choice-less one — and the turn is
    # metered from it by `graph_usage_tokens`. Omitting it meters every turn at zero and silently
    # disarms the budget guard under the storm, which is why it is here at all.
    #
    # **And it is emitted only when the request asked, because a gateway only answers when asked.**
    # Measured the same day, streaming with no `stream_options`: this mock put usage on 1 of 7
    # frames, the gateway on 0 of 7; through `ChatOpenAI(stream_usage=False)` the mock reported
    # `input_tokens: 900` and the gateway reported `usage_metadata: None`. `llm_stream_usage` exists
    # as the escape hatch for an endpoint that rejects `stream_options`, and turning it off books
    # **zero** on every turn — `turn_usage`, `api/budget.py`, `agent/spend_cap.py` and
    # `context_budget.note_model_call` all read that one field. Reporting usage unasked made that
    # lane invisible: every mock-driven run showed a fully metered turn for a configuration that
    # meters nothing. `_openai_compatible_model`'s docstring records this exact failure having
    # shipped once already, and the mock was the reason it could not recur *visibly*.
    yield frame(
        {"delta": {}, "finish_reason": "tool_calls" if behaviour.calls else "stop"},
        **({"usage": _chat_usage(behaviour, billed_input)} if include_usage else {}),
    )
    yield "data: [DONE]\n\n"


def build_app(mock: MockLlm) -> FastAPI:
    """The routes the OpenAI SDK will actually reach, over this mock's behaviour set.

    **Two chat protocols, because the engine changed under this file.** `/v1/responses` was the only
    one for as long as the conversation layer ran on the Microsoft Agent Framework, which spoke the
    Responses API. The LangGraph rebuild builds a `ChatOpenAI`, which posts to
    `/v1/chat/completions` — so from that day every credential-free lane (`make live-degradation`,
    `make live-storm`, `make live-soak`) got a bare `404 Not Found` from the mock and the turn died
    with no answer and no tool call. Measured before it was fixed: a degradation run scored 1/3 with
    "the turn produced no token or answer at all" while the mock's own counter read `requests: 0`.
    """
    app = FastAPI(title="chemclaw-mock-llm")

    @app.post("/v1/responses")
    async def responses(request: Request) -> Any:
        """One turn: SSE when the client asked to stream, a single body when it did not."""
        payload = await request.json()
        decided = decide_turn(mock, payload)
        if isinstance(decided, JSONResponse):
            return decided
        behaviour, billed_input, model = decided.behaviour, decided.billed_input, decided.model
        # Minted here, not inside the stream, because the id has to be bound to this behaviour
        # *before* the next call in the chain can arrive asking about it.
        response_id = f"resp_{uuid.uuid4().hex}"
        mock.remember(response_id, behaviour)
        if payload.get("stream"):
            return StreamingResponse(
                _stream(behaviour, model, response_id, billed_input),
                media_type="text/event-stream",
            )
        body = _response_object(response_id, model, behaviour, billed_input)
        body["output"] = [
            {
                "id": f"msg_{uuid.uuid4().hex[:16]}",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": behaviour.text, "annotations": []}],
            }
        ]
        return JSONResponse(body)

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Any:
        """The same turn as `/v1/responses`, for the protocol `ChatOpenAI` actually posts to.

        The decisions are literally the same ones — `decide_turn` is the single function both
        routes ask, because the storm's scenarios are written against those decisions rather than
        against either encoding. Only the encoding differs, and it differs in `_chat_stream`.
        `tests/test_mock_llm_contract.py::test_both_routes_decide_one_turn_the_same_way` drives
        the whole behaviour catalogue through both wires and compares what they decided, so the
        claim is checked rather than restated: this docstring used to assert the sameness in prose
        over two verbatim copies of the sequence.

        No `mock.remember`: chat completions has no `previous_response_id` to chain from, and the
        client resends the conversation, so `select` finds the marker on every pass unaided.
        """
        payload = await request.json()
        decided = decide_turn(mock, payload)
        if isinstance(decided, JSONResponse):
            return decided
        behaviour, billed_input, model = decided.behaviour, decided.billed_input, decided.model
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        if payload.get("stream"):
            options = payload.get("stream_options")
            include_usage = bool(isinstance(options, dict) and options.get("include_usage"))
            return StreamingResponse(
                _chat_stream(
                    behaviour, model, completion_id, billed_input, include_usage=include_usage
                ),
                media_type="text/event-stream",
            )
        # The non-streaming body always carries usage, on this mock and on the gateway measured
        # beside it: `stream_options` is a *streaming* option and has nothing to say here.
        body: dict[str, Any] = {
            "id": completion_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": behaviour.text},
                    "finish_reason": "stop",
                }
            ],
            "usage": _chat_usage(behaviour, billed_input),
        }
        if behaviour.service_tier:
            body["service_tier"] = behaviour.service_tier
        return JSONResponse(body)

    @app.post("/v1/embeddings")
    async def embeddings(request: Request) -> Any:
        """Only reached under `embedding_provider=openai_compatible`; `hash` needs no network."""
        payload = await request.json()
        inputs = payload.get("input") or [""]
        texts = inputs if isinstance(inputs, list) else [inputs]
        dim = 1536
        return JSONResponse(
            {
                "object": "list",
                "model": payload.get("model", "mock-embed"),
                "data": [
                    {"object": "embedding", "index": i, "embedding": [0.0] * dim}
                    for i, _ in enumerate(texts)
                ],
                "usage": {"prompt_tokens": 0, "total_tokens": 0},
            }
        )

    @app.get("/__mock/stats")
    async def stats() -> Any:
        """What the mock was asked for — the storm's proof that no real model was called."""
        return JSONResponse({"requests": mock.requests, "by_behaviour": mock.by_behaviour})

    return app


def main(argv: list[str] | None = None) -> int:
    """Serve the storm's behaviour set until killed."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=MOCK_PORT)
    args = parser.parse_args(argv)

    from chemclaw.cli.storm_behaviours import BEHAVIOURS

    # The configured logging path rather than a bare `basicConfig`, so this process is swept by
    # the same redaction filter as every other entrypoint (`tests/test_logging.py` pins it).
    configure_logging()
    mock = MockLlm(BEHAVIOURS)
    print(f"mock LLM serving {len(BEHAVIOURS)} behaviour(s) on http://{MOCK_HOST}:{args.port}/v1")
    uvicorn.run(build_app(mock), host=MOCK_HOST, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
