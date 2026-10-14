# langgraph: `durability="sync"` raises inside a subgraph that has no checkpointer

**File against:** `langchain-ai/langgraph`. **Status:** draft, not filed. **Reproduced on:**
langgraph 1.2.11 (installed here) and 1.2.15 (latest on PyPI, 2026-10-12).

## Title

`durability="sync"` is inherited by a subgraph compiled with `checkpointer=False` and fails with
`AttributeError: 'AsyncPregelLoop' object has no attribute '_put_checkpoint_fut'`

## What happens

A run started with `durability="sync"` writes the mode into the run config, and every graph started
under that run reads it. A graph with no saver (compiled with `checkpointer=False`, or run with none
inherited) still honours `"sync"`: at the end of each superstep the loop awaits a checkpoint write it
never started, and raises `AttributeError` from inside whatever node invoked the child.

`Pregel._defaults` resolves the mode from the config without looking at the resolved checkpointer, so
the mismatch (`checkpointer is None`, `durability == "sync"`) is returned as asked.

## Minimal repro

```python
import asyncio

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict


class State(TypedDict):
    n: int


def child():
    g = StateGraph(State)
    g.add_node("bump", lambda s: {"n": s["n"] + 1})
    g.add_edge(START, "bump")
    g.add_edge("bump", END)
    return g.compile(checkpointer=False)  # a helper nothing resumes


async def run(durability: str) -> str:
    inner = child()

    async def call_child(state: State) -> State:
        return await inner.ainvoke(state)

    parent = StateGraph(State)
    parent.add_node("call_child", call_child)
    parent.add_edge(START, "call_child")
    parent.add_edge("call_child", END)
    graph = parent.compile(checkpointer=InMemorySaver())
    try:
        out = await graph.ainvoke(
            {"n": 0}, {"configurable": {"thread_id": "t"}}, durability=durability
        )
    except Exception as exc:
        return f"{durability}: FAILED {type(exc).__name__}: {exc}"
    return f"{durability}: ok {out}"


async def main() -> None:
    for mode in ("async", "exit", "sync"):
        print(await run(mode))


asyncio.run(main())
```

Output on 1.2.15:

```
async: ok {'n': 1}
exit: ok {'n': 1}
sync: FAILED AttributeError: 'AsyncPregelLoop' object has no attribute '_put_checkpoint_fut'
```

## Expected

A graph with no checkpointer has nothing to wait for. `"sync"` should degrade to the behaviour of
`"async"` (or `"exit"`) for it, the way a graph without a saver already does when `durability` is
left unset.

## Proposed change

Either of two small edits; the first is the more general:

1. In `Pregel._defaults`, return `"async"` when the resolved `checkpointer` is `None` and the
   requested durability is `"sync"`.
2. At the two waits in `pregel/main.py` (`loop._put_checkpoint_fut.result()` in `stream` and
   `await loop._put_checkpoint_fut` in `astream`), guard with
   `loop.checkpointer is not None` (or `getattr(loop, "_put_checkpoint_fut", None)`).

## Why it matters to an integrator

Any agent that delegates to ephemeral subagents compiled with `checkpointer=False` (deepagents'
`task` tool with `CompiledSubAgent` runnables, handoff peers, fan-out graphs) fails every delegation
when the host asks for `"sync"` durability, which is the mode that makes a turn resumable after a
pod dies.

## First-party shim, and what turns red when this is fixed

- Shim: `src/chemclaw/core/graph_durability.py::ignoring_inherited_durability` subclasses the
  compiled graph's class and overrides `_defaults` so a graph whose resolved checkpointer is `None`
  never resolves to `"sync"`. Applied in `src/chemclaw/agent/langgraph_agent.py` to helpers and
  peers.
- Pin: `tests/test_upstream_surface.py::test_the_durability_a_turn_asks_for_is_spelt_and_resolved_the_way_the_guard_relies_on`
  asserts upstream still returns `"sync"` for a saver-less graph. It covers fix 1 only; a W5.3 test
  should also run the repro above so fix 2 turns it red as well.
