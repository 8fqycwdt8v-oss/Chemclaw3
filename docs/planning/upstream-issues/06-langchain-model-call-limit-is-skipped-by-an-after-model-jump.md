# langchain: `ModelCallLimitMiddleware` counts in `after_model`, so another `after_model` hook can skip it

**File against:** `langchain-ai/langchain`. **Status:** draft, not filed. **Reproduced on:**
langchain 1.4.6 (latest on PyPI, 2026-10-14) and 1.3.16 (installed here).

## Title

`ModelCallLimitMiddleware` does not count a model call when a later-listed `after_model` hook jumps
away, so `run_limit` is not a bound

## What happens

The limiter increments `thread_model_call_count` and `run_model_call_count` in `after_model`.
`after_model` hooks run in reverse list order, so a middleware listed after the limiter runs first.
When it jumps (`jump_to="model"` for a retry or repair, or `"tools"`/`"end"`), the limiter's
`after_model` never runs for that call, the count is not advanced, and the loop is bounded only by the
graph's `recursion_limit`, which raises `GraphRecursionError` and discards the partial answer.

## Minimal repro

```python
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, AgentState, ModelCallLimitMiddleware
from langchain.agents.middleware.types import hook_config
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.errors import GraphRecursionError

calls = 0


class Model(GenericFakeChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self

    def _generate(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
        global calls
        calls += 1
        return super()._generate(messages, *args, **kwargs)


class JumpBackToModel(AgentMiddleware):
    """Stands in for any `after_model` hook that re-enters the model."""

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: AgentState, runtime: Any) -> dict[str, Any]:
        return {"jump_to": "model"}


def run(middleware: list[Any]) -> str:
    global calls
    calls = 0
    graph = create_agent(
        model=Model(messages=iter(AIMessage(content=f"turn {i}") for i in range(1000))),
        tools=[],
        middleware=middleware,
    )
    try:
        graph.invoke({"messages": [HumanMessage("go")]}, {"recursion_limit": 25})
        outcome = "returned"
    except GraphRecursionError:
        outcome = "GraphRecursionError"
    return f"{calls} model calls, {outcome}"


limiter = ModelCallLimitMiddleware(run_limit=3, exit_behavior="end")
print("limiter alone          :", run([limiter]))
print("limiter, then a jumper :", run([limiter, JumpBackToModel()]))
```

Output on 1.4.6:

```
limiter alone          : 1 model calls, returned
limiter, then a jumper : 8 model calls, GraphRecursionError
```

A `run_limit` of 3 allowed 8 model calls and ended in the error the limiter exists to prevent.

## Proposed change

Count in `before_model` (the count then includes the call about to be made and no jump from an
`after_model` can skip it), or document that the limiter must be listed last so its `after_model`
runs first.

## First-party shim, and what turns red when this is fixed

- Shim: `src/chemclaw/agent/loop_cap.py::enforce_loop_cap` counts model calls in `before_model` over
  the `ChemclawState.model_calls` channel; nothing in `src/` imports `ModelCallLimitMiddleware`.
- Pin: `tests/test_upstream_surface.py::test_the_after_model_call_cap_is_still_the_one_upstream_shape_this_repo_declines`
  asserts upstream still increments in `after_model` and not in `before_model`; it turns red when the
  count moves. The decision to revisit is
  `docs/decisions/D-2026-08-15-an-after-model-counter-is-a-counter-that-can-be-skipped.md`.
