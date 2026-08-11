"""`python -m chemclaw.cli.mock_llm` — an OpenAI **Responses API** mock, to drive the system hard.

The point is not to avoid paying for tokens. It is that a real model cannot be asked for the inputs
that actually break this system: an empty function name (STREAM-1), a malformed argument document,
four hundred argument fragments, forty parallel calls in one turn, or a turn with no prose at all.
Every one of those has been a live defect here, and none of them is reachable by prompting. A mock
makes them a parameter.

It speaks the wire, not the Python. `agent_framework.openai.OpenAIChatClient` resolves to
the **Responses** client — `client.responses.create/parse`, not chat-completions — and the
previous generation of this idea took 37 × HTTP 404 on exactly that point
(`docs/archive/load-test-2026-07.md`).
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

logger = logging.getLogger(__name__)

# Where the mock listens. A dev-only affordance, so a module constant rather than a config field —
# the same call `cli/connectors_dev.py` makes, and for the same reason: nothing in a deployment
# reads it.
MOCK_HOST = "127.0.0.1"
MOCK_PORT = 8820


@dataclass
class ToolCall:
    """One function call the mock will emit, and how finely to slice its arguments.

    `fragments` is the knob that matters. The OpenAI Responses client emits every
    `response.function_call_arguments.delta` carrying *both* the name and a non-empty argument
    fragment, and `api/runner_trace.py::ToolCallTrace.feed` treats "name and arguments" as a whole,
    non-streamed call — so N fragments may well produce N `ToolCallEvent`s each holding a partial
    document rather than one holding the reassembled JSON. That is a hypothesis, not a finding;
    this field is how the storm settles it by measurement.
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
    # Tokens reported on `response.completed`. Without a usage block `usage_tokens` records zero
    # and budget admission is silently never pressured — the run would "pass" a gate it never met.
    input_tokens: int = 900
    output_tokens: int = 120


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
    """
    payload_input = payload.get("input")
    if not isinstance(payload_input, list):
        return False
    return any(
        isinstance(item, dict) and item.get("type") == "function_call_output"
        for item in payload_input
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
        """
        previous = payload.get("previous_response_id")
        if isinstance(previous, str):
            name = self._chain.get(previous)
            if name is not None:
                return self._by_name[name]
        text = json.dumps(payload.get("input", ""))
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


def _response_object(response_id: str, model: str, behaviour: Behaviour) -> dict[str, Any]:
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
            "input_tokens": behaviour.input_tokens,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
            "output_tokens": behaviour.output_tokens,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": behaviour.input_tokens + behaviour.output_tokens,
        },
    }


async def _stream(behaviour: Behaviour, model: str, response_id: str) -> AsyncIterator[str]:
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
    body = Response.model_validate(_response_object(response_id, model, behaviour))
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


def build_app(mock: MockLlm) -> FastAPI:
    """The three routes the OpenAI SDK will actually reach, over this mock's behaviour set."""
    app = FastAPI(title="chemclaw-mock-llm")

    @app.post("/v1/responses")
    async def responses(request: Request) -> Any:
        """One turn: SSE when the client asked to stream, a single body when it did not."""
        payload = await request.json()
        behaviour = mock.select(payload)
        # Second and later passes of the same turn answer instead of calling again — see
        # `already_has_tool_results`. `dataclasses.replace` rather than mutation: the catalogue is
        # shared across every concurrent turn and must stay immutable.
        #
        # The text is carried through *unchanged*, including when it is empty. Substituting a
        # default here quietly defeated the scenarios whose whole point is a turn that writes
        # nothing: `f-no-text` reported `answered=True` on its first run, because this line had
        # helpfully invented an answer for it.
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
        model = str(payload.get("model", "mock"))
        # Minted here, not inside the stream, because the id has to be bound to this behaviour
        # *before* the next call in the chain can arrive asking about it.
        response_id = f"resp_{uuid.uuid4().hex}"
        mock.remember(response_id, behaviour)
        if payload.get("stream"):
            return StreamingResponse(
                _stream(behaviour, model, response_id), media_type="text/event-stream"
            )
        body = _response_object(response_id, model, behaviour)
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

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    mock = MockLlm(BEHAVIOURS)
    print(f"mock LLM serving {len(BEHAVIOURS)} behaviour(s) on http://{MOCK_HOST}:{args.port}/v1")
    uvicorn.run(build_app(mock), host=MOCK_HOST, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
