"""`python -m chemclaw.cli.mock_llm` — an OpenAI-compatible mock, to drive the system hard.

A real model cannot be asked for the inputs that break this system — an empty function name, a
malformed argument document, hundreds of argument fragments, forty parallel calls, a turn with no
prose. A mock makes them a parameter. It speaks HTTP, both `/v1/chat/completions` (what
`ChatOpenAI` posts to) and `/v1/responses`, so the streaming assembler, middleware, budget
admission, audit sink and session store are all exercised.

Behaviours are validated at startup against the live tool surface, so one naming a tool or
argument the system lacks refuses to serve; `adversarial=True` opts out per behaviour. A mock may
be narrower than the endpoint it stands in for, never more forgiving
(`D-2026-09-07-a-mock-that-answers-unasked-hides-the-lane-that-asks-nothing`): usage is reported
only when asked for, input can be billed by request size, and oversize requests can be refused.
`tests/test_mock_llm_contract.py` holds the wire to those shapes.

A behaviour is chosen by a `[[name]]` marker in the newest user message that carries one, so a
later marker overrides an earlier one and an unmarked follow-up inherits the last; only when no
user message is marked does a whole-request scan run. Tool results count only after the newest
user message, so each turn calls its own tools.

The `e2e:*` behaviours (`cli/e2e_behaviours.py`, served under `--catalogue e2e`) read the request
through a `script` handed a `Conversation`; their `calls` are templates, and `_within_declared`
holds every scripted call to a tool and argument names a template declared.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from chemclaw.core.bounded import BoundedLru
from chemclaw.core.logging import configure_logging

logger = logging.getLogger(__name__)

# Where the mock listens. Dev-only, so a module constant rather than a config field.
MOCK_HOST = "127.0.0.1"
MOCK_PORT = 8820
# The address a caller configures to reach this mock, spelled once. `Settings.llm_base_url` ships
# this value (`test_the_default_gateway_is_the_mock_on_this_machine`), and
# `infra/live/processes.sh` compares the resolved setting against this string to decide whether to
# start the mock, rather than keeping a shell copy.
MOCK_BASE_URL = f"http://{MOCK_HOST}:{MOCK_PORT}/v1"


@dataclass
class ToolCall:
    """One function call the mock will emit, and how finely to slice its arguments.

    `fragments` streams the arguments in N deltas, each carrying the name as the Responses client
    does, to exercise the client against a real streamed call.
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

    A plan, not a reaction to the prompt: the storm must know exactly what the system was asked to
    do
    to check what it did.
    """

    name: str
    calls: list[ToolCall] = field(default_factory=list)
    text: str = "Done."
    # Seconds of pretend thinking before the first frame, then between frames; concurrency behaviour
    # is
    # about turns in flight, which a zero-latency mock never has.
    think_seconds: float = 0.0
    # Fail the HTTP call itself. NB `llm_max_retries=3`, so the SDK will retry this three times:
    # one injected failure is four requests, and a storm that forgot would mis-attribute the load.
    http_status: int = 200
    # Skip the startup validation below. Only the adversarial family sets this.
    adversarial: bool = False
    # Tokens reported as this request's input; without usage, budget admission is never pressured.
    #
    # A constant by default, for determinism. `None` bills the serialized request at
    # `input_tokens_per_char`, as a real gateway does, so a growing thread costs more and a factor
    # above
    # 0.25 drives `agent/context_budget._Calibration`'s ratio above 1 — the tightening branch a
    # constant
    # bill can never reach (the ratio clamps at 1.0).
    input_tokens: int | None = 900
    # Billed input tokens per character when `input_tokens` is None. 0.25 matches the chars/4
    # estimator; larger is a tokenizer this system undercounts.
    input_tokens_per_char: float = 0.25
    output_tokens: int = 120
    # The cached share of the input, as `prompt_tokens_details.cached_tokens` (read by
    # `langchain_openai` as `cache_read` and subtracted from priced input). Omitted when 0, as the
    # gateway does.
    cached_tokens: int = 0
    # `service_tier` on the response body. `priority` and `flex` prefix the cache keys
    # (`priority_cache_read`), which `turn_usage._cache_detail` matches by suffix; this drives that
    # over
    # the wire.
    service_tier: str = ""
    # Refuse any request whose billed input exceeds this with a gateway-shaped HTTP 400 ("prompt is
    # too
    # long: N tokens > M maximum", matched by `llm_provider._CONTEXT_LENGTH_MARKERS`). 0 means
    # never. A
    # property of the request, not the behaviour, which makes the `context_length` label reachable.
    refuse_over_input_tokens: int = 0
    # The `finish_reason` the streamed reply ends on, when not the natural one. `length` with a raw
    # argument document cut mid-string exercises `agent/model_calls._demote_cut_off_calls`. Empty
    # means
    # `tool_calls` when there are calls, `stop` otherwise.
    finish_reason: str = ""
    # Seconds over which the answer's text frames are spread, after `think_seconds`, so the Stop
    # button
    # and queued messages see a turn visibly producing tokens. 0 sends the text at once.
    stream_seconds: float = 0.0
    # Reads the request and returns the concrete pass (see the module docstring). `None` for every
    # behaviour that is a fixed plan, which is all of them outside `cli/e2e_behaviours.py`.
    script: Script | None = None


@dataclass(frozen=True)
class Message:
    """One message of a request, normalized across the two protocols this mock speaks.

    `role` uses chat-completions spelling; a Responses `developer` item and `instructions` read as
    `system`. `tool_calls` holds only names: a script asks only whether a call happened.
    """

    role: str
    text: str
    tool_calls: tuple[str, ...] = ()


def _text_of(content: Any) -> str:
    """The text of a message's `content`, whichever of the two shapes it has.

    A string is itself; a list of parts contributes each part's `text`. Anything else contributes
    nothing, so a marker is only found where a person or tool wrote text.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


@dataclass(frozen=True)
class Conversation:
    """The request a behaviour is answering, as the messages it holds — both protocols, one shape.

    Built from the request alone so the mock stays stateless and concurrent turns cannot interfere.
    A
    Responses continuation holds only the new `function_call_output` items, so all its tool results
    are this turn's.
    """

    messages: tuple[Message, ...]

    @classmethod
    def of(cls, payload: dict[str, Any]) -> Conversation:
        """Normalize a `/v1/chat/completions` or `/v1/responses` body into one message list."""
        found: list[Message] = []
        instructions = payload.get("instructions")
        if isinstance(instructions, str) and instructions:
            found.append(Message("system", instructions))
        for item in _items(payload):
            role = item.get("role")
            kind = item.get("type")
            if kind == "function_call":
                found.append(Message("assistant", "", (str(item.get("name", "")),)))
            elif kind == "function_call_output":
                found.append(Message("tool", _text_of(item.get("output"))))
            elif role in {"system", "developer"}:
                found.append(Message("system", _text_of(item.get("content"))))
            elif role in {"user", "assistant", "tool"}:
                calls = tuple(
                    str((call.get("function") or {}).get("name", ""))
                    for call in item.get("tool_calls") or []
                    if isinstance(call, dict)
                )
                found.append(Message(str(role), _text_of(item.get("content")), calls))
        return cls(tuple(found))

    def _last_user_index(self) -> int:
        """The index of the newest user message, or -1 when the request holds none."""
        for index in range(len(self.messages) - 1, -1, -1):
            if self.messages[index].role == "user":
                return index
        return -1

    @property
    def system_text(self) -> str:
        """Every system message's text, joined — the instructions as the model received them."""
        return "\n\n".join(m.text for m in self.messages if m.role == "system")

    @property
    def tool_results(self) -> tuple[str, ...]:
        """The tool results of *this* turn: every tool message after the newest user message."""
        return tuple(
            m.text for m in self.messages[self._last_user_index() + 1 :] if m.role == "tool"
        )

    def marker(self, known: Iterable[str]) -> str | None:
        """The behaviour the newest marked user message names, or `None` when none names one.

        Within one message, the marker first in its text wins.
        """
        names = set(known)
        for message in reversed(self.messages):
            if message.role != "user":
                continue
            for name in _MARKER.findall(message.text):
                if name in names:
                    return str(name)
        return None

    def marked_index(self, name: str) -> int:
        """The index of the newest user message carrying `[[name]]`, or -1."""
        for index in range(len(self.messages) - 1, -1, -1):
            message = self.messages[index]
            if message.role == "user" and f"[[{name}]]" in message.text:
                return index
        return -1

    def marked_text(self, name: str) -> str:
        """What the newest message carrying `[[name]]` says besides the marker, stripped."""
        index = self.marked_index(name)
        if index < 0:
            return ""
        return " ".join(self.messages[index].text.replace(f"[[{name}]]", " ").split())

    def called_since_marker(self, name: str) -> set[str]:
        """Tools called after the message carrying `[[name]]` and before this turn's message.

        How a multi-turn behaviour knows which turn it is on: calls from earlier turns of this
        behaviour
        only.
        """
        start = self.marked_index(name)
        end = self._last_user_index()
        if start < 0:
            return set()
        return {call for m in self.messages[start + 1 : end] for call in m.tool_calls}


# A behaviour marker: `[[name]]`, the name free of brackets. Read per user message by
# `Conversation.marker`, so the name space is whatever the catalogue serves.
_MARKER = re.compile(r"\[\[([^\[\]\s]+)\]\]")

# What a script is: the catalogue's behaviour and the request in, the pass to serve out.
Script = Callable[[Behaviour, Conversation], Behaviour]


def _items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The request's message items: `messages` (chat completions) then `input` (Responses).

    Both, because the contract test sends one body to both routes. A bare-string `input` is one user
    message.
    """
    items: list[dict[str, Any]] = []
    for key in ("messages", "input"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            items.append({"role": "user", "content": value})
        elif isinstance(value, list):
            items.extend(item for item in value if isinstance(item, dict))
    return items


def already_has_tool_results(payload: dict[str, Any]) -> bool:
    """Whether this request already carries the output of a tool call made *in this turn*.

    The agent re-invokes the model after each tool result, so a mock replaying its calls would loop
    to
    the iteration cap; once results are present it answers instead. Read from the request (a
    `function_call_output` item or a `role: "tool"` message after the newest user message), never
    from
    session state, so concurrent turns cannot race.
    """
    return bool(Conversation.of(payload).tool_results)


def _validate(behaviour: Behaviour) -> None:
    """Refuse a behaviour whose tool or arguments the real system would reject.

    Resolved against the live surface: `available_tool_names` is what the agent advertises, and the
    registry holds the callable whose signature the schema comes from.
    """
    from chemclaw.agent.chemclaw_agent import available_tool_names
    from chemclaw.core.tool_registry import registered_tools

    # Checked before the adversarial opt-out, which waives only the tool-surface check: refusing
    # over a
    # token count while billing a constant would refuse every request or none.
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


def _within_declared(declared: Behaviour, served: Behaviour) -> Behaviour:
    """`served`, once every call in it is shown to be one `declared`'s templates allow.

    Templates are validated at startup, but a script picks values at request time; each served call
    must name a templated tool and use only argument names that template declared. A breach is a
    catalogue defect, so it raises.

    Raises:
        ValueError: A served call names a tool, or an argument, no template of `declared` holds.
    """
    allowed: dict[str, set[str]] = {}
    for template in declared.calls:
        allowed.setdefault(template.tool, set()).update(template.arguments)
    for call in served.calls:
        if call.tool not in allowed:
            raise ValueError(
                f"scripted behaviour {declared.name!r} emitted {call.tool!r}, which none of its "
                f"templates declares ({sorted(allowed)})"
            )
        extra = set(call.arguments) - allowed[call.tool]
        if extra or call.raw_arguments is not None:
            raise ValueError(
                f"scripted behaviour {declared.name!r} passed {sorted(extra) or 'raw arguments'} "
                f"to {call.tool!r}, which its template does not declare"
            )
    return served


class MockLlm:
    """The scripted endpoint: a queue of behaviours, plus a count of what was actually asked of it.

    The counter lets the storm prove how many model calls were made, by reconciling it against the
    turn count.
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

        The chain matters for the Responses API: a continuation carries `previous_response_id` and
        only
        the `function_call_output`, so the marker is gone. Chat completions resends the whole
        conversation, so the marker scan finds it unaided. The newest marked user message decides;
        the
        fallback scans the serialized request in catalogue order.
        """
        previous = payload.get("previous_response_id")
        if isinstance(previous, str):
            name = self._chain.get(previous)
            if name is not None:
                return self._by_name[name]
        marked = Conversation.of(payload).marker(self._by_name)
        if marked is not None:
            return self._by_name[marked]
        text = json.dumps([payload.get("input", ""), payload.get("messages", "")])
        for name, behaviour in self._by_name.items():
            if f"[[{name}]]" in text:
                return behaviour
        return self._default

    def remember(self, response_id: str, behaviour: Behaviour) -> None:
        """Bind a minted response id to the behaviour that produced it, for the next call.

        Bounded (`BoundedLru`), because a soak mints one id per model call.
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

    The size is the serialized request — system message, skills listing, tool schemas — because
    `agent/context_budget` estimates the whole request. Never 0: `note_model_call` drops
    non-positive
    samples.
    """
    if behaviour.input_tokens is not None:
        return behaviour.input_tokens
    return max(1, round(len(json.dumps(payload)) * behaviour.input_tokens_per_char))


def _chat_usage(behaviour: Behaviour, billed_input: int) -> dict[str, Any]:
    """The chat-completions `usage` block, with the cached breakdown only when there is one.

    `prompt_tokens` includes the cached share (OpenAI's definition), so `cached_tokens` says how
    much
    of the bill was cheap rather than adding to it.
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

    `{"error": {"code": "invalid_request_error", "message": "prompt is too long: N tokens > M
    maximum", "type": "invalid_request_error", "param": null}}` — the wording
    `llm_provider._CONTEXT_LENGTH_MARKERS` matches.
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

    Returns the turn to encode, or the `JSONResponse` refusal that ends the request; one function so
    the two routes cannot diverge in the order of refusals. `mock.remember` and id minting stay in
    the
    routes because they are protocol-specific.
    """
    behaviour = mock.select(payload)
    # Later passes of the same turn answer instead of calling again (`already_has_tool_results`).
    # `dataclasses.replace`, not mutation: the catalogue is shared across concurrent turns. The text
    # is
    # carried through unchanged, even when empty, so `f-no-text` stays a turn that writes nothing. A
    # scripted behaviour decides its own passes, held to its declared templates.
    if behaviour.script is not None:
        behaviour = _within_declared(
            behaviour, behaviour.script(behaviour, Conversation.of(payload))
        )
    elif behaviour.calls and already_has_tool_results(payload):
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

    `status` is always `completed`; `in_progress` would make the client poll `GET /responses/{id}`.
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


async def _paced(behaviour: Behaviour) -> AsyncIterator[str]:
    """The answer's text in 40-character frames, spread over `stream_seconds` when it names any.

    Shared by both encoders. The pause precedes each frame after the first, so text starts at once.
    """
    chunks = [behaviour.text[i : i + 40] for i in range(0, len(behaviour.text), 40)]
    pause = behaviour.stream_seconds / max(len(chunks) - 1, 1) if behaviour.stream_seconds else 0.0
    for index, chunk in enumerate(chunks):
        if index and pause:
            await asyncio.sleep(pause)
        yield chunk


async def _stream(
    behaviour: Behaviour, model: str, response_id: str, billed_input: int
) -> AsyncIterator[str]:
    """The SSE frames for one turn, in the order the SDK's discriminated union accepts them.

    Every frame is built as the SDK's own model, so a malformed frame fails here rather than inside
    the client, where it would read as an application defect.
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

    # Validated into the SDK's `Response`, so a malformed body fails here rather than in the client.
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
        async for chunk in _paced(behaviour):
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

    Built through `ChatCompletionChunk` so a malformed chunk fails here. A tool call streams over an
    indexed slot: the first delta carries `id`, `type` and `function.name`, later ones only argument
    fragments against the same `index` — repeating the name would make the client assemble two
    calls.

    Args:
        behaviour: The turn to encode — its calls, its prose and its billed output.
        model: The model name to echo back on every chunk.
        completion_id: The `chatcmpl-…` id every chunk of this turn carries.
        billed_input: What `_billed_input_tokens` decided this request costs in input.
        include_usage: Whether the request sent `stream_options.include_usage`; usage is reported
        only
            when asked, as a gateway does.
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
        async for chunk_text in _paced(behaviour):
            yield frame({"delta": {"content": chunk_text}})

    # Usage rides the final frame, on the same chunk as `finish_reason` as the gateway sends it, and
    # meters the turn via `graph_usage_tokens`. Emitted only when the request asked: with
    # `llm_stream_usage` off every turn books zero, and the mock must show that rather than hide it.
    yield frame(
        {
            "delta": {},
            "finish_reason": behaviour.finish_reason
            or ("tool_calls" if behaviour.calls else "stop"),
        },
        **({"usage": _chat_usage(behaviour, billed_input)} if include_usage else {}),
    )
    yield "data: [DONE]\n\n"


def build_app(mock: MockLlm) -> FastAPI:
    """The routes the OpenAI SDK will actually reach, over this mock's behaviour set.

    `/v1/chat/completions` is what `ChatOpenAI` posts to; `/v1/responses` serves the Responses API.
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

        Both routes ask `decide_turn`; only the encoding (`_chat_stream`) differs, which
        `tests/test_mock_llm_contract.py::test_both_routes_decide_one_turn_the_same_way` checks. No
        `mock.remember`: there is no `previous_response_id`, and the resent conversation carries the
        marker.
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


def catalogue(name: str) -> list[Behaviour]:
    """The named behaviour set this process serves.

    Catalogues are not served together: `MockLlm.select` falls back to the first entry when no
    marker
    is present, so a union would leak one lane's default into another. `e2e` is the one deliberate
    union: the storm's list first (so the default is the storm's), then `cli/e2e_behaviours.py`'s
    entries namespaced `e2e:` (`tests/test_mock_llm_e2e.py` holds the absence of collisions).

    Imported lazily because each catalogue validates itself against the live tool surface.

    Raises:
        KeyError: No catalogue is called that, rather than silently serving the storm's.
    """
    from chemclaw.cli.delegation_behaviours import DELEGATION_BEHAVIOURS
    from chemclaw.cli.e2e_behaviours import E2E_BEHAVIOURS
    from chemclaw.cli.storm_behaviours import BEHAVIOURS

    catalogues = {
        "storm": BEHAVIOURS,
        "delegation": DELEGATION_BEHAVIOURS,
        "e2e": [*BEHAVIOURS, *E2E_BEHAVIOURS],
    }
    if name not in catalogues:
        raise KeyError(f"no behaviour catalogue called {name!r}; known: {sorted(catalogues)}")
    return catalogues[name]


def main(argv: list[str] | None = None) -> int:
    """Serve one behaviour catalogue until killed."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # A flag because a pod cannot use loopback: in a cluster (`deploy/kind/`) callers are other
    # pods.
    # The default stays loopback.
    parser.add_argument("--host", default=MOCK_HOST)
    parser.add_argument("--port", type=int, default=MOCK_PORT)
    parser.add_argument(
        "--catalogue",
        default="storm",
        choices=["storm", "delegation", "e2e"],
        help="which behaviour set to serve (default: the storm's; the kind cluster serves e2e)",
    )
    args = parser.parse_args(argv)

    behaviours = catalogue(args.catalogue)

    # The configured logging path rather than a bare `basicConfig`, so this process is swept by
    # the same redaction filter as every other entrypoint (`tests/test_logging.py` pins it).
    configure_logging()
    mock = MockLlm(behaviours)
    print(
        f"mock LLM serving {len(behaviours)} {args.catalogue} behaviour(s) on "
        f"http://{args.host}:{args.port}/v1"
    )
    uvicorn.run(build_app(mock), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
