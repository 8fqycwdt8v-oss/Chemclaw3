"""Shared doubles for the LangGraph engine's tests.

The counterpart of `tests/fakes.py`, which holds the streamed-update double for the same
reason: a double that four test modules need is a double that must have one definition, or the
suite ends up asserting against four subtly different models. `tests/fakes.py`'s own docstring
records what the alternative cost — twenty fakes across fourteen files hard-coded
`user_input_requests` empty, and the runner's approval branch went untested for months.

Extracted at the third caller rather than the first (`test_langgraph_agent.py`,
`test_langgraph_connectors.py`, `test_langgraph_stream.py`, `test_agent_team.py`), which is the
repo's Rule of Three. A double used once stays private to the module that uses it.
"""

import json
from collections.abc import Iterable, Iterator, Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk


class ScriptedChatModel(GenericFakeChatModel):
    """A model that replays a fixed script, binds tools, and streams the way a real one does.

    Subclassed because `create_agent`'s model node calls `.bind_tools(...)` on every request and
    `GenericFakeChatModel.bind_tools` raises `NotImplementedError` — measured, not assumed. Binding
    returns `self` here: the script already contains the tool call under test, so the point of the
    override is that the graph gets a model it can bind, not that the fake reasons about tools.

    **`_stream` is overridden for a reason that only shows up under `astream`.** Upstream streams a
    message by splitting its `content`, so a tool-call turn — whose content is empty, because the
    call is in `tool_calls` — yields no chunks at all and LangChain raises `ValueError: No
    generations found in stream`. Measured the first time this fake was driven through
    `graph_events`. A tool call is therefore streamed as a `tool_call_chunks` fragment, which is
    the shape a real provider sends and the shape `api/graph_stream` deliberately does *not* read
    calls from — so this also keeps that decision honest rather than untested.

    What the fake still cannot prove is worth naming: it exercises the *loop*, not the tool schemas
    Chemclaw hands a provider. Only a live run covers those, and M12 is where that happens.
    """

    def __init__(self, script: Sequence[Any] | None = None, **kwargs: Any) -> None:
        """Build from a script of turns, or from `GenericFakeChatModel`'s own `messages` iterator.

        Each script entry is either a string (a final answer) or a mapping `{"name", "args"}`
        (one tool call). That covers every shape these tests need and keeps the call sites
        readable, which matters more here than generality: a test whose fixture needs explaining
        is a test nobody re-reads when it fails.
        """
        if script is not None:
            kwargs["messages"] = iter([_as_message(step, i) for i, step in enumerate(script)])
        super().__init__(**kwargs)

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Accept the binding and keep replaying the script."""
        return self

    def _stream(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        """Stream the next scripted turn — prose as text, a tool call as a call fragment."""
        message = next(self.messages)
        assert isinstance(message, AIMessage)  # `_as_message` only ever produces these
        if message.tool_calls:
            call = message.tool_calls[0]
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": call["name"],
                            "args": json.dumps(call["args"]),
                            "id": call["id"],
                            "index": 0,
                            "type": "tool_call_chunk",
                        }
                    ],
                )
            )
            return
        yield ChatGenerationChunk(message=AIMessageChunk(content=message.content))


def _as_message(step: Any, index: int) -> AIMessage:
    """One script entry as the assistant message it stands for."""
    if isinstance(step, str):
        return AIMessage(content=step)
    return AIMessage(
        content="",
        tool_calls=[
            {"name": step["name"], "args": step.get("args", {}), "id": f"call-{index + 1}"}
        ],
    )


def scripted_call(tool_name: str, tool_args: dict[str, Any]) -> ScriptedChatModel:
    """A model that calls `tool_name` once and then produces a final answer."""
    return ScriptedChatModel([{"name": tool_name, "args": tool_args}, "done"])


def tool_outputs(messages: Iterable[Any]) -> list[str]:
    """Every tool result in a finished turn — what the model was actually handed.

    Read off the message list rather than off graph state because `create_agent`'s output schema
    carries `messages` and nothing else, so this is the only place a tool's result is observable
    after the turn.
    """
    return [
        str(message.content) for message in messages if message.__class__.__name__ == "ToolMessage"
    ]
