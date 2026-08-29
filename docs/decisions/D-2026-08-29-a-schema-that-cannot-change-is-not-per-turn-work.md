# D-2026-08-29-a-schema-that-cannot-change-is-not-per-turn-work — the per-turn compile

**Status:** accepted · **Date:** 2026-08-29 · Found by CI failing the compile budget on the branch
carrying the eight infrastructure findings.

## Context

The eight infrastructure findings added five tools to the `default` surface, and CI failed
`tests/test_langgraph_connectors.py::test_compiling_the_graph_per_turn_stays_within_the_maf_agent_build_budget`
at a median of 419 ms against a 400 ms bound.

The first question was whether that was this branch's or the runner's, and the test's own docstring
gave a ready-made excuse for the second answer: it records that this CI runner class measured 340 ms
on an *unmodified* `main`, and says in as many words that "the margin this test relies on is thinner
on that hardware than the docstring above assumes". Taking that as the explanation would have been
picking the more articulate story.

Measured instead, 25 rounds per process, interleaved between this branch and `origin/main` in one
sandbox to control for drift:

| | run 1 | run 2 | run 3 |
| --- | --- | --- | --- |
| `origin/main` | 187.8 ms | 174.0 ms | 206.0 ms |
| this branch | 210.8 ms | 229.2 ms | 223.9 ms |

So the branch was **~36 ms (~19%) slower**, in the same direction in every pairing. It was this
branch's — five tools, in both the parent graph and the helper `_subagents` compiles beside it.

That is a real cost and it is also an absurd one: five function objects cannot be worth 36 ms. A
cumulative profile of one warm compile said where it actually goes:

- `langchain_core.tools.convert.tool` — **540 calls over 5 builds, 108 per build**.
- `create_schema_from_function` → pydantic `create_model` under all of them.
- **2.121 s of a 2.613 s total: about four fifths of the compile.**

`ToolNode.__init__` calls `langchain_core.tools.tool` on every plain callable it is handed, building
a pydantic model from the signature and docstring. `chemclaw.core.tool_registry` stores functions —
deliberately, and its docstring says why ("the framework derives its schema from the signature and
docstring, so the registry stores the function unchanged") — so the whole in-process surface was
being re-derived on every compile, twice per turn.

## Decision

**Derive each in-process capability tool's `BaseTool` once per process, and hand `ToolNode` the
object it would otherwise have built.** `agent/tool_schema.py` is a `functools.cache` over
`langchain_core.tools.tool`, keyed on the function; `build_langgraph_agent` maps the in-process half
of its tool list through it.

The premise is that **a first-party tool's schema cannot vary between turns**: it is a function of a
module-level callable's signature and docstring, both fixed at import. Compiling the graph per turn
stays exactly as it was and is still not negotiable — a connector session belongs to exactly one
turn — but re-deriving these schemas was never the per-turn part of it.

The connector tools are **not** cached, and that is the line: they arrive already built from that
turn's own MCP session, they are `BaseTool` instances already, and they pass through untouched.
Caching those would re-create the defect that forced per-turn compilation in the first place.

## Measured

Same sandbox, same interpreter, 25 rounds, interleaved against `origin/main`:

- **33 ms** unloaded, against ~205 ms on `main` the same hour — **6x**.
- **35 ms** with four cores saturated, against 140 ms recorded for the previous arrangement. The
  loaded/unloaded gap almost vanished, which is what removing an allocation-heavy pass predicts and
  is the strongest evidence the diagnosis was right rather than merely correlated.
- **14 ms** of the 33 is the helper graph, down from ~61 ms.

The ~36 ms this branch added is now smaller than the total.

## Consequences

- The budget test's bound moves **400 → 250 ms**, and the ADR says plainly that 250 is an estimate:
  this sandbox measured ~130 ms where CI measured 340, so the conservative transfer factor is ~2.6x
  and the expected CI figure is ~90 ms. 250 is ~2.7x that — the ratio the 270 bound held against its
  ~90 ms baseline and the 400 held against 130. Leaving it at 400 would have let a regression put
  twelve times the measured cost back before anything went red.
- The lever the previous docstring named as the remaining one — `_labelled(_skill_dirs())` running
  twice per turn because `_subagents` calls the builder again — is **retired rather than taken**. At
  14 ms for the entire helper there is nothing left in it to win.
- `tests/test_upstream_surface.py` gains the row this now depends on: `ToolNode` stores a prebuilt
  `BaseTool` **as the same object**. If upstream started copying or re-deriving what it is handed,
  the cache would stay correct and silently stop saving anything — the exact failure that file
  exists to turn red.
- `tests/test_tool_schema.py` asserts the wiring rather than the cache, because a memo nothing
  routes through saves nothing. It found, on the way, that **seven executor tools are still rebuilt
  every compile** — `read_file`, `write_file`, `edit_file`, `ls`, `glob`, `grep`, `task` — because
  upstream's `FilesystemMiddleware` and `SubAgentMiddleware` construct them inside the build instead
  of taking them from a registry. They are not reachable from here. That is where the remaining
  per-compile schema work lives, and it is the next thing to measure if this budget ever gets tight
  again.

## What this does not change

Nothing about capability, narrowing or gating. The object handed to the executor is the same object
`ToolNode` would have constructed; the audit middleware, the per-tool authorization gate and the
profile narrowing all run exactly where they ran. `tests/test_middleware_order.py`,
`tests/test_tool_authz.py`, `tests/test_scratchpad.py`, `tests/test_state_channels.py` and
`tests/test_langgraph_agent.py` are green unmodified, which is the evidence for that claim.
