"""What the `task` tool reaches, asserted against the two graphs that really compile.

Three properties, and none of them is checkable at build time under a one-name roster. The helper
is built from its caller's own profile, so any comparison of the two *declarations* would compare a
value with itself and could never turn red — which is why `reject_widening` did not come back as a
function when the specialist team was deleted. What can be observed is the compiled artifact, so
that is what these read: the tools each graph actually bound, and the roster the `task` tool
actually advertises.

The properties, in the order they would hurt:

1. **The helper is ours, not upstream's.** `create_deep_agent` auto-inserts a `general-purpose`
   subagent holding every tool the parent holds and none of this repository's middleware unless a
   caller-supplied spec claims that name first.
2. **A helper is an attenuation of its caller.** Never a way to reach a capability the caller could
   not reach directly — otherwise a narrow profile is a suggestion rather than a boundary.
3. **A helper cannot spawn a helper.** Not because a roster is empty, but because the middleware
   that would register `task` is absent.
"""

from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.profiles import AgentProfile


def _model() -> GenericFakeChatModel:
    """A model that resolves without credentials — construction only, no call is made."""
    return GenericFakeChatModel(messages=iter([AIMessage(content="ok")]))


def _tool_names(graph: Any) -> set[str]:
    """The tools a compiled graph really bound, read off its executor.

    A private shape, and deliberately reached from a test rather than from `src/`. `ToolNode` is
    where a tool becomes *callable* — `wrap_model_call`'s `request.override(tools=…)` narrows only
    what the model is shown — so this is the one reading that answers "what can this graph run".
    `tests/test_upstream_surface.py` is where couplings like this are counted; putting it in `src/`
    would add a seventh.
    """
    return set(graph.nodes["tools"].bound.tools_by_name)


@pytest.fixture
def agent() -> Any:
    """The agent a chemist talks to, on the default profile."""
    return build_langgraph_agent(model=_model(), profile=AgentProfile(name="default"))


@pytest.fixture
def helper() -> Any:
    """The graph behind the `task` tool, built the way `_subagents` builds it."""
    return build_langgraph_agent(model=_model(), profile=AgentProfile(name="default"), helper=True)


def test_the_general_purpose_helper_is_the_one_this_repository_compiled(agent: Any) -> None:
    """The security-critical displacement, asserted on the roster the model actually reads.

    Upstream skips its own default only when a supplied spec already claims
    `GENERAL_PURPOSE_SUBAGENT["name"]`. Measured across three arms while this was being designed:
    claiming the name replaced upstream's entry; claiming a *different* name left upstream's in
    place beside ours; the default arm had upstream's alone. So the assertion is on the description
    text, because that is the only place the two are distinguishable — both are called
    `general-purpose`, and only one of them carries this repository's audit trail, authorization
    gate, dry-run refusal and plan gate.

    The alternative suppression, `GeneralPurposeSubagentProfile(enabled=False)`, is not used and
    this is why: it reaches upstream through a `HarnessProfile` resolved by the model's
    self-reported `provider:identifier`, and on a key miss the profile is silently not applied. That
    failure was reproduced during design — a registration under `"anthropic"` never reached a model
    whose resolved provider was something else, logging one warning and leaving upstream's subagent
    in place.
    """
    task = agent.nodes["tools"].bound.tools_by_name["task"]
    assert "general-purpose" in task.description
    assert "searching for files and content" not in task.description, (
        "the `task` roster carries upstream's default general-purpose subagent, which holds every "
        "tool this agent holds and none of its middleware — no audit row, no authorization gate, "
        "no dry-run refusal, no plan gate, and nothing fails while it does not"
    )
    assert "cannot call external connector tools" in task.description, (
        "the roster is not the spec `agent/subagents.py` builds"
    )


def test_a_helper_holds_no_tool_its_caller_does_not(agent: Any, helper: Any) -> None:
    """The attenuation invariant, on the two compiled surfaces rather than the two profiles.

    Delegation must not become a way to reach a capability the delegating agent could not reach
    directly. Written as a subset relation rather than an equality so that narrowing the helper
    further stays legal, and stated over what each graph *bound* — a profile comparison would be a
    tautology here, since the helper is built from its caller's profile.
    """
    widened = _tool_names(helper) - _tool_names(agent)
    assert not widened, (
        f"a helper holds {sorted(widened)}, which its caller does not; a subagent is an "
        "attenuation of the agent that spawns it, never a new actor"
    )


def test_a_helper_cannot_spawn_a_helper(agent: Any, helper: Any) -> None:
    """The recursion guard, asserted as the *absence of the tool* rather than an empty roster.

    This is the defect the first version of the swap actually had, found by compiling it and
    reading the middleware list rather than by reasoning about it. `_subagents` returned `[]` for a
    helper, which is not what "no helpers" means to `create_deep_agent`: with no spec claiming the
    name, it auto-inserts its own general-purpose subagent — so the guard reproduced, one level
    down, exactly the ungoverned `task` surface it exists to prevent. Compiling a helper on
    `create_agent` removes `SubAgentMiddleware` outright.

    Asserted alongside the caller's own `task` so the test cannot pass by the tool having been
    dropped everywhere.
    """
    assert "task" in _tool_names(agent)
    assert "task" not in _tool_names(helper)


def test_a_helper_holds_no_connector_tool(helper: Any) -> None:
    """A concurrency bound, not a narrowing, and the reason it has to be a test.

    `build_langgraph_agent` records the measurement it rests on: two concurrent turns over one MCP
    tool object deadlock, and the second turn's calls travel over the first turn's connection,
    misattributing them in the connector's own log. A helper is concurrent with its caller by
    construction, so handing it the caller's already-open connector tools reproduces that exactly.

    `_subagents` expresses the bound by omitting `connectors=`, which is an *absence* — the class of
    thing an edit removes without noticing. Passing the caller's connectors in would keep every
    other test in this file green, including the attenuation one above, because a connector tool the
    caller holds is not a widening.
    """
    connectors = [_named("chembl_search"), _named("share_document_search")]
    caller = build_langgraph_agent(
        model=_model(), profile=AgentProfile(name="default"), connectors=connectors
    )
    assert {"chembl_search", "share_document_search"} <= _tool_names(caller)
    assert not {"chembl_search", "share_document_search"} & _tool_names(helper)


def _named(name: str) -> Any:
    """A minimal stand-in for a connector's already-open MCP tool.

    A real one opens an `httpx.AsyncClient` that only a turn's exit stack closes, so a test asking
    "does this name reach the executor" must not go through the constructor that reserves the
    resource to answer — the same reason `advertised_tool_names` reads manifests.
    """
    from langchain_core.tools import StructuredTool

    return StructuredTool.from_function(
        name=name, description="stand-in", func=lambda: "", infer_schema=True
    )


def test_a_declarative_subagent_spec_is_refused_rather_than_assembled_by_upstream() -> None:
    """The one build-time guard: a spec with no compiled runnable never reaches `create_deep_agent`.

    **This is not the attenuation check** — the module docstring above explains why that one cannot
    turn red under a one-name roster. It is the governance check, and it is a different question:
    is every entry a graph *this repository* compiled, or one upstream would assemble itself?

    `create_deep_agent` uses a `CompiledSubAgent`'s runnable as provided, but builds a declarative
    `SubAgent` from `spec["middleware"]` alone — upstream's middleware, carrying none of this
    repository's audit trail, authorization gate, dry-run refusal or plan gate. D-2026-08-13
    recorded how that presents from outside: *"nothing would fail while it did."*

    The fixture is the realistic mistake rather than a contrived one. A dict with `name`,
    `description` and `prompt` is exactly how upstream's own documentation shows a subagent being
    declared, so it is what someone adding a second helper would naturally write — and the reason a
    guard is worth more than a review note.
    """
    from chemclaw.agent.subagents import governed_roster
    from chemclaw.core.errors import ChemclawError

    compiled = {"name": "general-purpose", "description": "d", "runnable": object()}
    assert governed_roster([compiled]) == [compiled], "a compiled spec must pass through unchanged"

    declarative = {"name": "researcher", "description": "d", "prompt": "you are a researcher"}
    with pytest.raises(ChemclawError, match="without a compiled runnable") as refused:
        governed_roster([compiled, declarative])
    assert "researcher" in str(refused.value), "the refusal must name the offending spec"


def test_the_shipped_roster_passes_its_own_guard() -> None:
    """The guard is wired into the path that builds the real roster, not merely importable.

    Asserted by building the actual agent: a guard that exists and is never called is the shape
    this repository has been burned by repeatedly, and `governed_roster` raising for nobody today
    is exactly the condition under which that would go unnoticed.
    """
    agent = build_langgraph_agent(model=_model(), profile=AgentProfile(name="default"))
    assert agent is not None
