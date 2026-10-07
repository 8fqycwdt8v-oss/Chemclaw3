"""Shared doubles for the LangGraph engine's tests.

One definition for a double several test modules need, so they do not assert against subtly
different models. A double used once stays private to its module.
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

    `create_agent` calls `.bind_tools(...)` on every request and `GenericFakeChatModel.bind_tools`
    raises, so binding returns `self`. `_stream` is overridden because upstream splits `content`, so
    a tool-call turn (empty content) would yield no chunks and raise under `astream`; a tool call is
    streamed as a `tool_call_chunks` fragment, as a real provider sends it. This exercises the loop,
    not the tool schemas.
    """

    def __init__(self, script: Sequence[Any] | None = None, **kwargs: Any) -> None:
        """Build from a script of turns, or from `GenericFakeChatModel`'s own `messages` iterator.

        Each script entry is either a string (a final answer) or a mapping `{"name", "args"}` (one
        tool call).
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

    Read off the message list because `create_agent`'s output schema carries only `messages`.
    """
    return [
        str(message.content) for message in messages if message.__class__.__name__ == "ToolMessage"
    ]
