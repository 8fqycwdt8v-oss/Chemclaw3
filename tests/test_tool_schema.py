"""The in-process tool schemas are derived once per process, and the graph really uses them.

One test covers the cache itself; the other asserts a compiled graph's executor holds the cached
objects, since a memo nothing routes through saves nothing. Sharing is safe because a first-party
tool's schema derives from a module-level signature and docstring; connector tools are per turn
and are passed through untouched.
"""

from typing import Any

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.tools import BaseTool

from chemclaw.agent.audit import NullAuditSink
from chemclaw.agent.chemclaw_agent import _capability_tools
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.tool_schema import as_structured_tool


def _model() -> GenericFakeChatModel:
    """A model that never runs — every assertion here is about construction."""
    return GenericFakeChatModel(messages=iter(["ok"] * 8))


def _executor_tools(agent: Any) -> dict[str, BaseTool]:
    """The tool objects the compiled graph's executor would actually run.

    Reads `nodes["tools"].bound.tools_by_name`, an upstream shape pinned in
    `tests/test_upstream_surface.py`.
    """
    return dict(agent.nodes["tools"].bound.tools_by_name)


def test_one_function_converts_to_one_tool_object_for_the_life_of_the_process() -> None:
    """The cache holds, keyed on the function itself."""
    fn = _capability_tools()[0]
    first = as_structured_tool(fn)
    assert first is as_structured_tool(fn)
    assert isinstance(first, BaseTool)
    assert first.name == fn.__name__


def test_two_compiles_hand_the_executor_the_same_tool_objects() -> None:
    """Two compiles hand the executor the same registry tool objects.

    Asserted by identity, since equality would pass on a rebuild. Scoped to registry tools: the
    filesystem and `task` tools are built inside upstream middleware on every compile and are out of
    this cache's reach.
    """
    model = _model()
    first = _executor_tools(build_langgraph_agent(model, audit_sink=NullAuditSink()))
    second = _executor_tools(build_langgraph_agent(model, audit_sink=NullAuditSink()))

    registered = {fn.__name__ for fn in _capability_tools()}
    shared = registered & set(first) & set(second)
    assert len(shared) > 20, (
        f"only {len(shared)} registry tools reached the executor; this test would pass vacuously"
    )
    rebuilt = sorted(name for name in shared if first[name] is not second[name])
    assert rebuilt == [], (
        f"{rebuilt} were re-derived on the second compile — `agent/tool_schema.py`'s cache is not "
        "on the path build_langgraph_agent hands to the executor"
    )


def test_the_cache_does_not_grow_with_the_number_of_turns() -> None:
    """Building more turns does not grow the unbounded schema cache.

    `functools.cache` is safe only while every caller passes a module-level function; generated job
    and template tools are fresh closures per call and would leak one entry per build if routed
    here. Asserted as "a turn adds nothing" rather than "equals the registry size", since other
    tests register probe tools.
    """
    model = _model()
    for _ in range(2):
        build_langgraph_agent(model, audit_sink=NullAuditSink())
    settled = as_structured_tool.cache_info()

    for _ in range(3):
        build_langgraph_agent(model, audit_sink=NullAuditSink())
    after = as_structured_tool.cache_info()

    assert after.currsize == settled.currsize, (
        f"three more builds added {after.currsize - settled.currsize} cache entries; something is "
        "minting a fresh callable per build and the cache grows without bound"
    )
    assert after.hits > settled.hits, "nothing reused the cache; every build re-derives schemas"
