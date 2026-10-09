"""A real compiled agent graph for the replica process, over a model and tools a test steers.

`tests/replica_process.py` serves the real `create_app` with a scripted turn that has no graph and
no checkpoint. A claim about a turn that survives its process needs the real thing: the graph, its
Postgres checkpointer, its middleware chain (authorization, plan gate, audit, caps) and a model
whose position in the turn a test can name. This module supplies those two doubles:

- `ProbeModel` answers as a pure function of the thread: a message `steps=read,act` makes it call
  `probe_read` then `probe_act` and then answer. Being a function of the messages it is shown, it
  does not care which process it runs in, which is the property a resume depends on.
- `probe_read` and `probe_act` are in-process tools. `probe_act` is classified state-changing.

Every model call and tool call writes a file in `REPLICA_MARKS` named
`<tag>.model-<n>.<pid>.<id>` or `<tag>.tool-<name>-<n>.<pid>.<id>`, one per execution, so a test
counts executions rather than inferring them. `tag=<x>` in the message names the turn (replicas
outlive a test). `park-model-<n>` holds model call `n`, and `park-<name>` holds that tool's body,
until the file `<tag>` exists in the `REPLICA_GATE` directory.
"""

import asyncio
import json
import os
import re
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import StructuredTool

from chemclaw.agent.turn_graph import build_turn_agent

#: Tokens each model call reports, so a turn's booked spend names how many calls it paid for.
INPUT_TOKENS = 100
OUTPUT_TOKENS = 10

ACTING_TOOL = "probe_act"
READING_TOOL = "probe_read"


def _tag_of(text: str) -> str:
    """The turn's name, from `tag=<x>` in its message."""
    match = re.search(r"tag=(\w+)", text)
    return match.group(1) if match else "untagged"


def _mark(tag: str, name: str) -> None:
    """Record one execution of `name` in the turn `tag` for the test to count."""
    marks = Path(os.environ["REPLICA_MARKS"])
    (marks / f"{tag}.{name}.{os.getpid()}.{uuid.uuid4().hex[:6]}").write_text("")


def _wait_for_gate(tag: str) -> None:
    """Block (the caller is a thread) until the test opens `tag`'s gate."""
    gate = Path(os.environ["REPLICA_GATE"]) / tag
    while not gate.exists():
        time.sleep(0.02)


class ProbeModel(GenericFakeChatModel):
    """A model that is a function of the thread it is shown — see the module docstring."""

    def __init__(self, **kwargs: Any) -> None:
        """No script: `messages` is unused, the thread decides."""
        super().__init__(messages=iter([]), **kwargs)

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Accept the binding; the plan in the message decides what is called."""
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        """The non-streaming call, for a graph run that does not ask for message chunks."""
        merged: AIMessageChunk | None = None
        for chunk in self._stream(messages, stop, run_manager, **kwargs):
            piece = chunk.message
            merged = piece if merged is None else merged + piece  # type: ignore[assignment]
        if merged is None:
            raise RuntimeError("the probe model produced nothing")
        return ChatResult(generations=[ChatGeneration(message=merged)])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        """Take the next step of the plan the turn's message states, given the results so far."""
        start = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
        text = str(messages[start].content)
        done = sum(isinstance(m, ToolMessage) for m in messages[start + 1 :])
        # The step the plan is at: one per assistant message, whatever number of calls it made.
        step = sum(isinstance(m, AIMessage) for m in messages[start + 1 :])
        call_number = step + 1
        tag = _tag_of(text)
        _mark(tag, f"model-{call_number}")
        if f"park-model-{call_number}" in text:
            _wait_for_gate(tag)
            _mark(tag, f"model-{call_number}-woke")
        match = re.search(r"steps=([a-z,]*)", text)
        steps = [s for s in (match.group(1).split(",") if match else []) if s]
        usage = {
            "input_tokens": INPUT_TOKENS,
            "output_tokens": OUTPUT_TOKENS,
            "total_tokens": INPUT_TOKENS + OUTPUT_TOKENS,
        }
        if step < len(steps):
            # `acts` is two state-changing calls in one assistant message, run in parallel.
            names = (
                [ACTING_TOOL, ACTING_TOOL]
                if steps[step] == "acts"
                else [ACTING_TOOL if steps[step] == "act" else READING_TOOL]
            )
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": name,
                            "args": json.dumps(
                                {"n": call_number, "tag": tag, "hold": f"park-{name}" in text}
                            ),
                            "id": f"call-{call_number}-{index}",
                            "index": index,
                            "type": "tool_call_chunk",
                        }
                        for index, name in enumerate(names)
                    ],
                    usage_metadata=usage,  # type: ignore[arg-type]
                )
            )
            return
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content=f"final after {done} tool results",
                usage_metadata=usage,  # type: ignore[arg-type]
            )
        )


async def _run_probe(name: str, n: int, tag: str, hold: bool) -> str:
    """The body both tools share: say it ran, optionally park, say it finished."""
    _mark(tag, f"tool-{name}-{n}")
    if hold:
        await asyncio.to_thread(_wait_for_gate, tag)
    return f"{name} {n} ok"


async def probe_read(n: int, tag: str, hold: bool = False) -> str:
    """Read something, harmlessly and repeatably. Test double."""
    return await _run_probe(READING_TOOL, n, tag, hold)


async def probe_act(n: int, tag: str, hold: bool = False) -> str:
    """Change something outside the turn. Test double, classified state-changing."""
    return await _run_probe(ACTING_TOOL, n, tag, hold)


def probe_tools() -> list[StructuredTool]:
    """The two tools, as a connector would hand them to the graph."""
    return [
        StructuredTool.from_function(coroutine=probe_read, name=READING_TOOL),
        StructuredTool.from_function(coroutine=probe_act, name=ACTING_TOOL),
    ]


def classify_probe_tools() -> None:
    """Declare the probes as a connector manifest would; a tool no manifest classifies is acted.

    `probe_act` changes state and `probe_read` is `read_only`.
    """
    from chemclaw.connectors import registry

    registry.state_changing_tool_names = lambda: [ACTING_TOOL]
    registry.read_only_tool_names = lambda: [READING_TOOL]


def graph_factory(**kwargs: Any) -> Any:
    """The turn's real graph over `ProbeModel`, with the probe tools beside the turn's own."""
    kwargs["connectors"] = [*(kwargs.get("connectors") or []), *probe_tools()]
    return build_turn_agent(ProbeModel(), **kwargs)
