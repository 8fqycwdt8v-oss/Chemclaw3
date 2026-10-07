"""Shared test doubles that tests construct and import by name.

Kept out of `conftest.py`, which is for fixtures and hooks pytest loads, not a helper library.
`asgi_client` wraps the `ASGITransport` → `AsyncClient` setup; it takes an already-built `app`
because many callers need the app afterwards. A scripted turn belongs in
`tests.fakes_turn.ScriptedTurn`.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage


@asynccontextmanager
async def asgi_client(app: Any, **client_kwargs: Any) -> AsyncIterator[httpx.AsyncClient]:
    """An `httpx.AsyncClient` speaking in-process to `app`, closed on exit.

    `base_url` is supplied because `ASGITransport` needs an absolute URL to build a scope and the
    host is never meaningful; pass `timeout=` and friends through `client_kwargs`.
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", **client_kwargs
    ) as client:
        yield client


class ScriptedModel(GenericFakeChatModel):
    """A model that replays a fixed script, and accepts tool binding without honouring it.

    `create_agent` calls `.bind_tools(...)` on every request and `GenericFakeChatModel.bind_tools`
    raises, so binding returns `self`. This proves the loop (dispatch, run, feed back), not that the
    tool schemas are ones a real model can call; only a live run covers those.
    """

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Accept the binding and keep replaying the script."""
        return self


def scripted(tool_name: str, tool_args: dict[str, Any]) -> ScriptedModel:
    """A model that calls `tool_name` once and then produces a final answer."""
    return ScriptedModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[{"name": tool_name, "args": tool_args, "id": "call-1"}],
                ),
                AIMessage(content="done"),
            ]
        )
    )
