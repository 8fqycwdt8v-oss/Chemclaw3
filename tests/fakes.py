"""The test doubles that several modules were each writing their own copy of.

**Why a module rather than `conftest.py`.** `conftest` is what pytest loads and injects: fixtures,
hooks, collection policy. These are neither — they are objects a test constructs when it wants one,
imported by name like any other helper. `FakeWriter` lives in `conftest` for the same DRY reason
and is imported the same way, which is precisely the shape that argues for a separate module rather
than against one: a file pytest reads for hooks should not also be the suite's library, or every
new shared helper grows the thing loaded before every session.

**Why these and not every fake in the suite.** A shared double earns its place when the copies have
already drifted or are already boilerplate at the call site — not merely when they look similar:

`asgi_client` — the `ASGITransport` → `AsyncClient(base_url=…)` incantation, thirteen times in two
files, five of them inside near-identical `async def _drive()` wrappers. It takes an already-built
`app` rather than the arguments to build one, because half the call sites need the app afterwards
(`app.state.turn_semaphore`, `app.dependency_overrides`, `app.state.live_sessions`); hiding
`create_app` inside the helper would have served the other half and forced the first half back onto
the raw form, which is how a helper ends up used by three call sites out of seven.

**`FakeUpdate` and `fed` were the other half of this file and are gone.** They doubled the previous
engine's streamed *update* — a name on one content, then argument fragments on the next — for
`ToolCallTrace.feed`, which the LangGraph rebuild stopped calling: the graph hands a finished tool
call over, so the trace reassembles nothing and the doubles had no production shape left to
impersonate. A test that wants a scripted turn writes it as a `tests.fakes_turn.ScriptedTurn`,
which is rendered onto a real compiled graph.
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

    Subclassed because `create_agent`'s model node calls `.bind_tools(...)` on every request and
    `GenericFakeChatModel.bind_tools` raises `NotImplementedError` — measured, not assumed. Binding
    returns `self` here: the script already contains the tool call under test, so the point of the
    override is that the graph gets a model it can bind, not that the fake reasons about tools.

    What that costs is worth naming. This proves the *loop* — that a tool call is dispatched, run
    and fed back — and cannot prove that the tool schemas Chemclaw hands over are ones a real model
    can call. `test_every_in_process_tool_reaches_the_graph_unchanged` covers the surface, and only
    a live run covers the schemas; M12's re-validation is where that happens.
    """

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Accept the binding and keep replaying the script."""
        return self


def scripted(tool_name: str, tool_args: dict[str, Any]) -> ScriptedModel:
    """A model that calls `tool_name` once and then produces a final answer.

    Shared rather than copied. `BACKLOG` records nine definitions of one fake agent across the
    suite and this class was on its way to being the tenth: `test_middleware_order.py` needed the
    same "bind_tools must not raise" override that `test_langgraph_agent.py` already carried.
    """
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
