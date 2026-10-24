# langchain: `ContextEditingMiddleware.awrap_model_call` does its work on the event loop

**File against:** `langchain-ai/langchain`. **Status:** filed as [langchain#41449](https://github.com/langchain-ai/langchain/issues/41449) (2026-10-24); reproduced on 1.4.7. **Reproduced on:**
langchain 1.4.6 (latest on PyPI, 2026-10-14) and 1.3.16 (installed here).

## Title

`ContextEditingMiddleware.awrap_model_call` deep-copies the message list and runs every edit
synchronously on the event loop

## What happens

`awrap_model_call` calls `deepcopy(list(request.messages))`, then each `ContextEdit.apply` (token
counting included), inline in the coroutine before awaiting the handler. For a long thread that is
CPU-bound work of tens of milliseconds per model call during which every other coroutine on the
process is stalled: other sessions' streams, health probes, heartbeats.

## Minimal repro

```python
import asyncio
import time
from typing import Any

from langchain.agents.middleware import ClearToolUsesEdit, ContextEditingMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


def thread(pairs: int) -> list[Any]:
    messages: list[Any] = [HumanMessage("go")]
    for index in range(pairs):
        messages.append(
            AIMessage("", tool_calls=[{"name": "t", "args": {"n": index}, "id": f"c{index}"}])
        )
        messages.append(ToolMessage("x " * 3000, tool_call_id=f"c{index}"))
    return messages


class Request:
    def __init__(self, messages: list[Any]) -> None:
        self.messages = messages
        self.model = None
        self.system_message = None
        self.tools: list[Any] = []

    def override(self, **updates: Any) -> "Request":
        return Request(updates.get("messages", self.messages))


async def handler(request: Request) -> str:
    return "ok"


async def main() -> None:
    middleware = ContextEditingMiddleware(edits=[ClearToolUsesEdit(trigger=1, keep=3)])
    request: Any = Request(thread(400))
    worst = 0.0

    async def ticker() -> None:
        nonlocal worst
        while True:
            before = time.perf_counter()
            await asyncio.sleep(0.005)
            worst = max(worst, time.perf_counter() - before - 0.005)

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0.05)
    started = time.perf_counter()
    await middleware.awrap_model_call(request, handler)
    elapsed = time.perf_counter() - started
    await asyncio.sleep(0.05)
    task.cancel()
    print(f"awrap_model_call took {elapsed * 1000:.0f} ms; worst ticker delay {worst * 1000:.0f} ms")


asyncio.run(main())
```

Output on 1.4.6 (a shared 4-CPU host): `awrap_model_call took 37 ms; worst ticker delay 38 ms`. The
ticker was due every 5 ms.

## Proposed change

Either move the copy and the edits into `asyncio.to_thread` in `awrap_model_call`, or split the
synchronous part into a protected method (`_edit(request) -> ModelRequest`) so an async subclass can
choose where it runs without re-implementing `wrap_model_call`.

## First-party shim, and what turns red when this is fixed

- Shim: `src/chemclaw/agent/compaction.py::OffLoopContextEditing` runs upstream's synchronous
  `wrap_model_call` in a worker thread with a handler that captures the prepared request, so the
  upstream body is inherited rather than copied.
- Pin: `tests/test_upstream_surface.py::test_the_editing_middleware_hands_its_edited_request_to_its_handler`
  asserts the handler is called exactly once with the edited list. That is the contract the shim
  needs; it does not turn red when upstream fixes the blocking. A W5.3 test should assert
  `ContextEditingMiddleware.awrap_model_call` still has no `to_thread`/executor call in its source,
  so a fix turns it red and the subclass can be deleted.
