"""The in-process tool schemas are derived once per process, and the graph really gets them.

Two assertions, and they are deliberately not the same one. The first is about the cache: the same
function converts to the same object. The second is about the *wiring*, and it is the one that
matters — a memo nothing routes through is a memo that saves nothing, which is the shape this
repository keeps finding (a control that exists and has no caller). So the second reaches into a
compiled graph and asserts the object the executor would run is the cached one.

Why this is safe to share across turns, stated here because it is the whole premise: a first-party
capability tool is a module-level function, and its schema is derived from its signature and
docstring. Neither can differ between two turns. The per-turn objects are the *connector* tools,
which arrive already built from that turn's own MCP session and are passed through untouched —
`build_langgraph_agent` converts only the in-process half.
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
    """The saving is wired, not merely available.

    Compiling per turn is the rule (`build_langgraph_agent` says why), so this asserts the thing
    that costs nothing to get wrong: two builds must reuse the derived schemas rather than build
    a second set. Asserted by object identity, because equality would pass on a rebuild.

    **Scoped to the registry's own tools, and the exclusion is a finding rather than a
    convenience.** Seven names on the executor — `read_file`, `write_file`, `edit_file`, `ls`,
    `glob`, `grep` and `task` — *are* rebuilt on every compile, because upstream's
    `FilesystemMiddleware` and `SubAgentMiddleware` construct them inside the build rather than
    taking them from a registry. They are not reachable from here and are not what this cache is
    about; what they say is that the remaining per-compile schema work is upstream's, which is the
    next thing to measure if this budget ever gets tight again.
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
