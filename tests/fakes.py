"""The two test doubles that fourteen modules were each writing their own copy of.

**Why a module rather than `conftest.py`.** `conftest` is what pytest loads and injects: fixtures,
hooks, collection policy. These are neither — they are objects a test constructs when it wants one,
imported by name like any other helper. `FakeSubmitter` lives in `conftest` for the same DRY reason
and is imported the same way, which is precisely the shape that argues for a separate module rather
than against one: a file pytest reads for hooks should not also be the suite's library, or every
new shared helper grows the thing loaded before every session.

**Why these two and not every fake in the suite.** A shared double earns its place when the copies
have already drifted or are already boilerplate at the call site — not merely when they look
similar. Both here qualify by measurement:

`FakeUpdate` — twenty streamed-update fakes across fourteen files, each hard-coding
`user_input_requests=[]`. That field is a *derived* property on the real update type, and
hard-coding it empty meant no fake could ever carry an approval request, so the runner's approval
branch was executed by no test in the suite until D-2026-08-08 fixed the single copy in
`tests/test_runner.py`. Thirteen copies still asserted a shape the real update does not have. One
class with the property derived fixes the *class* of defect: the next field the runner learns to
read is either derived here once or wrong everywhere at once, and the first is a much shorter
conversation.

`asgi_client` — the `ASGITransport` → `AsyncClient(base_url=…)` incantation, thirteen times in two
files, five of them inside near-identical `async def _drive()` wrappers. It takes an already-built
`app` rather than the arguments to build one, because half the call sites need the app afterwards
(`app.state.turn_semaphore`, `app.dependency_overrides`, `app.state.live_sessions`); hiding
`create_app` inside the helper would have served the other half and forced the first half back onto
the raw form, which is how a helper ends up used by three call sites out of seven.
"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import httpx
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage


class FakeUpdate:
    """One streamed update, shaped as `chemclaw.api.runner_trace` duck-types a real one.

    `text` and `contents` are plain attributes because that is what a real update's are from a
    reader's point of view. `user_input_requests` is not, and must not be: see the class docstring
    above.
    """

    def __init__(self, text: str = "", contents: Sequence[object] = ()) -> None:
        """Copy `contents` into a list, so appending to one update cannot reach another."""
        self.text = text
        self.contents: list[object] = list(contents)

    @property
    def user_input_requests(self) -> list[object]:
        """Derived from `contents`, exactly as a real update derives it.

        The real filter is on `content.user_input_request`; this uses `getattr` so a test's own
        content double reaches the branch without subclassing a concrete content class. An update
        carrying a `function_approval_request` therefore lands in the approval branch by
        construction, rather than because whoever wrote the fake remembered that the field exists.
        """
        return [
            content for content in self.contents if getattr(content, "user_input_request", False)
        ]


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


def fed(trace: Any, update: Any) -> list[Any]:
    """`trace.feed(update)` driven to completion from a synchronous test.

    `ToolCallTrace.feed` became a coroutine when a tool result grew somewhere to be stored (the
    write has to land before the event naming it is yielded), and the forty call sites that read
    it are ordinary `def` tests with no event loop. Turning all forty into `async def` would have
    made the suite's shape depend on an implementation detail of the trace; this keeps them
    reading exactly as they did.

    `asyncio.run` per call is honest here rather than wasteful: a trace built with no sink — which
    is every one of those tests — awaits nothing at all, so the loop is created and torn down
    around a coroutine that never suspends.
    """
    return asyncio.run(trace.feed(update))


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
