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
`user_input_requests=[]`. That field is a *derived* property on MAF's `AgentResponseUpdate`, and
hard-coding it empty meant no fake could ever carry an approval request, so the runner's approval
branch was executed by no test in the suite until D-2026-08-08 fixed the single copy in
`tests/test_runner.py`. Thirteen copies still asserted a shape MAF does not have. One class with
the property derived fixes the *class* of defect: the next field the runner learns to read is
either derived here once or wrong everywhere at once, and the first is a much shorter conversation.

`asgi_client` — the `ASGITransport` → `AsyncClient(base_url=…)` incantation, thirteen times in two
files, five of them inside near-identical `async def _drive()` wrappers. It takes an already-built
`app` rather than the arguments to build one, because half the call sites need the app afterwards
(`app.state.turn_semaphore`, `app.dependency_overrides`, `app.state.live_sessions`); hiding
`create_app` inside the helper would have served the other half and forced the first half back onto
the raw form, which is how a helper ends up used by three call sites out of seven.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import httpx


class FakeUpdate:
    """One streamed update, shaped as `chemclaw.api.runner` duck-types MAF's update type.

    `text` and `contents` are plain attributes because that is what MAF's are from a reader's point
    of view. `user_input_requests` is not, and must not be: see the class docstring above.
    """

    def __init__(self, text: str = "", contents: Sequence[object] = ()) -> None:
        """Copy `contents` into a list, so appending to one update cannot reach another."""
        self.text = text
        self.contents: list[object] = list(contents)

    @property
    def user_input_requests(self) -> list[object]:
        """Derived from `contents`, exactly as MAF's `AgentResponseUpdate` derives it.

        MAF filters on `content.user_input_request`; this uses `getattr` so a test's own content
        double reaches the branch without having to subclass MAF's `Content`. An update carrying a
        `function_approval_request` therefore lands in the approval branch by construction, rather
        than because whoever wrote the fake remembered that the field exists.
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
