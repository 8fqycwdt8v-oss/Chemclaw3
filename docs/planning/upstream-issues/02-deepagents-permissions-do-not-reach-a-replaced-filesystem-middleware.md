# deepagents: `permissions=` does not reach a caller-supplied `FilesystemMiddleware`

**File against:** `langchain-ai/deepagents`. **Status:** not filed — fixed upstream: deepagents 0.7.31 and later take a public `permissions=` keyword, and the replaced-middleware repro denies the write on 0.7.32. **Reproduced on:**
deepagents 0.7.25 (latest on PyPI, 2026-10-14) and 0.7.8 (installed here).

## Title

`create_deep_agent(permissions=...)` silently stops applying when the caller replaces
`FilesystemMiddleware` by name

## What happens

`create_deep_agent` builds its own `FilesystemMiddleware(..., _permissions=permissions)`. A caller who
supplies a `FilesystemMiddleware` through `middleware=` replaces that instance by `.name` (documented),
and the replacement carries no permissions: `permissions` reaches only the instance upstream built,
which is displaced. The only way to give the replacement its rules is the underscore-prefixed
constructor keyword `_permissions=`, which the docstring calls an internal detail that may move to the
backend layer.

The result is fail-open: the call site states deny rules, the agent compiles, and the first write
outside the allowed paths succeeds.

The usual reason to replace the instance is the public `tools=[...]` allow-list (withholding `execute`
or `delete`), which has no equivalent on `create_deep_agent`.

## Minimal repro

```python
import asyncio
from typing import Any

from deepagents import FilesystemPermission, create_deep_agent
from deepagents.backends import StateBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

DENY_ALL_WRITES = [FilesystemPermission(operations=["write"], paths=["/**"], mode="deny")]


class Model(GenericFakeChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


def write_call() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "write_file", "args": {"file_path": "/etc/x", "content": "hi"}, "id": "c1"}
        ],
    )


async def attempt(middleware: list[Any]) -> str:
    graph = create_deep_agent(
        model=Model(messages=iter([write_call(), AIMessage(content="done")])),
        permissions=DENY_ALL_WRITES,
        middleware=middleware,
    )
    out = await graph.ainvoke({"messages": [HumanMessage("write it")]})
    return str([m for m in out["messages"] if m.type == "tool"][0].content)[:100]


async def main() -> None:
    print("default middleware  :", await attempt([]))
    replaced = FilesystemMiddleware(backend=StateBackend(), tools=["read_file", "write_file"])
    print("replaced middleware :", await attempt([replaced]))


asyncio.run(main())
```

Output on 0.7.25:

```
default middleware  : Error: permission denied for write on /etc/x
replaced middleware : Updated file /etc/x
```

## Proposed change

Any one of these removes the gap; the first is the smallest and is what a replacing caller needs:

1. Promote `_permissions` to a public `permissions` keyword on `FilesystemMiddleware`.
2. Have `create_deep_agent` hand its `permissions` to a caller-supplied `FilesystemMiddleware` that
   replaces its own (or raise when `permissions` is given and the replacement was built without any).
3. Accept the tool allow-list on `create_deep_agent` itself (for example through a per-agent
   `HarnessProfile`, see draft 03), so the instance never needs replacing.

## First-party shim, and what turns red when this is fixed

- Shim: `src/chemclaw/agent/langgraph_agent.py::_middleware` passes
  `_permissions=filesystem_permissions()` to its own `FilesystemMiddleware`
  (`src/chemclaw/agent/scratchpad.py::filesystem_permissions`).
- Pin: `tests/test_upstream_surface.py::test_the_filesystem_middleware_still_takes_its_permissions_under_a_private_name`
  asserts `_permissions` is a parameter and `permissions` is not. It turns red when upstream
  promotes the keyword. `tests/test_scratchpad.py` drives an out-of-bounds write through the compiled
  graph and does not depend on the spelling.
