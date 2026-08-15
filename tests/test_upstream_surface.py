"""Every upstream shape this repository depends on, asserted in one place.

**Why this file exists.** Layer 1 leans hard on `langchain`, `langgraph` and `deepagents`, which is
the decision (D-2026-08-10) and not a problem. The problem is the handful of places that depend on
something upstream never promised: a state key's *name*, a tool's *name*, a private constant, a
baked default. Each of those was recorded in the docstring of whichever module happened to need it,
which meant a dependency bump could quietly invalidate six sentences spread across six files and
nothing would go red until a live turn behaved oddly.

Prose is evidence about what its author believed. A test is evidence about what upstream does. So
every such dependency is asserted here, once, with the first-party code that would break named in
the failure message — and the modules that rely on them say "pinned in
`tests/test_upstream_surface.py`" instead of restating the shape.

**What belongs here.** A dependency on an upstream *name or shape* that upstream does not publish
as API: a state channel key, a tool name, a private module constant, a default this repository
overrides. What does not belong here is upstream *behaviour* — that is asserted where it is used,
against a compiled graph, because a behaviour assertion that runs in isolation is exactly the kind
that passes while the thing it describes is disconnected (`agent/loop_cap.py` records what that
cost).

**When one of these fails**, the fix is never to update the number here and move on. Each assertion
names the first-party module that reads the shape; go and read it, decide whether the dependency is
still the right one, and record the answer in an ADR if it changed. That is the whole point of
having them in one file: a bump becomes one conversation instead of six surprises.
"""

from typing import get_type_hints

import pytest


def test_the_todo_middleware_still_names_the_plan_channel_todos() -> None:
    """`todos` is the plan, and three first-party readers spell it by hand.

    `agent/plan_state.py` reads it out of the graph state to answer `GET /sessions/{id}/plan`
    between turns, `agent/plan_gate.py` reads it to decide what a plan approval is an approval
    *of*, and `agent/state.ChemclawState` extends `PlanningState` for it. A rename upstream is a
    silent fail-open in the gate — an approval bound to a plan nobody can find — so it must be a
    red build instead.
    """
    from langchain.agents.middleware.todo import PlanningState

    assert "todos" in get_type_hints(PlanningState, include_extras=True), (
        "TodoListMiddleware no longer declares `todos`; agent/plan_state.py and agent/plan_gate.py "
        "both read that key by name"
    )


def test_the_plan_is_written_by_a_tool_called_write_todos() -> None:
    """`plan_gate` refuses a gated call that arrives beside a plan rewrite, and knows it by name.

    Deliberately a literal in `agent/plan_gate.py` rather than an import: the gate must fail loudly
    if upstream renames the tool, not silently stop recognising a plan rewrite and let the pair
    through. This is the assertion that makes "loudly" true.
    """
    from langchain.agents.middleware import TodoListMiddleware

    names = {tool.name for tool in TodoListMiddleware().tools}
    assert "write_todos" in names, (
        "TodoListMiddleware renamed its tool; agent/plan_gate._PLAN_WRITE_TOOL and "
        "agent/chemclaw_agent.harness_tool_names both depend on `write_todos`"
    )


def test_a_subagent_still_cannot_see_the_parent_s_todos() -> None:
    """`plan_gate._plan_behind` has a fallback that exists only because of this exclusion.

    `SubAgentMiddleware` strips `todos` from the state it hands a specialist, so inside a subagent
    the gate cannot read the plan from state at all and falls back to `session_todos()`. Without
    that fallback every specialist's state-changing call was refused under the shipped `plan_only`
    posture — measured. If upstream ever stops excluding the key, the fallback becomes dead code
    and should be deleted rather than left as an unexplained second path.
    """
    from deepagents.middleware.subagents import _EXCLUDED_STATE_KEYS

    assert "todos" in _EXCLUDED_STATE_KEYS, (
        "subagents now inherit `todos`; agent/plan_gate._plan_behind's session fallback is "
        "no longer needed and should be removed rather than left in place"
    )


def test_the_skills_middleware_still_caches_under_skills_metadata() -> None:
    """`ReloadingSkillsMiddleware` re-declares exactly this channel, and nothing else.

    The whole subclass is one field: `skills_metadata` redeclared as an `UntrackedValue` so
    upstream's `if "skills_metadata" in state` short-circuit cannot fire on turn two. Rename the
    key upstream and the subclass silently stops reloading — the listing goes stale and a caller
    who lost a role keeps being offered its skills.
    """
    from deepagents.middleware.skills import SkillsState

    hints = get_type_hints(SkillsState, include_extras=True)
    assert "skills_metadata" in hints, (
        "SkillsMiddleware renamed its cache channel; "
        "agent/langgraph_agent.ReloadingSkillsState redeclares `skills_metadata` by name"
    )
    # The *annotation* as well as the name, because the redeclaration has to reproduce upstream's
    # `PrivateStateAttr` and once did not — which put the role-narrowed listing into the graph's
    # input schema, where a caller could replace it. `tests/test_state_channels.py` asserts our
    # side.
    assert "OmitFromSchema" in repr(hints["skills_metadata"]), (
        "SkillsMiddleware no longer marks `skills_metadata` private; "
        "agent/langgraph_agent.ReloadingSkillsState copies that marker and should stop"
    )


def test_private_state_attr_is_still_where_the_skills_state_reaches_for_it() -> None:
    """`ReloadingSkillsState` must import `PrivateStateAttr`, and there is only one place to get it.

    It is **not** in `langchain.agents.middleware.__all__` — unlike `ModelCallLimitMiddleware`,
    `hook_config` and `Runtime`, which are — so `agent/langgraph_agent.py` reaches into
    `langchain.agents.middleware.types` for it. That is a real coupling to a non-exported name,
    accepted because the alternative is dropping the marker, which is a security property
    (see `tests/test_state_channels.py`). Pinned here so a move is a red build rather than a silent
    loss of the marker.
    """
    from langchain.agents.middleware import types

    assert hasattr(types, "PrivateStateAttr"), (
        "PrivateStateAttr moved; agent/langgraph_agent.ReloadingSkillsState imports it from "
        "langchain.agents.middleware.types because it is not re-exported by the package"
    )


def test_the_model_call_limit_keeps_its_per_run_counter_unreadable() -> None:
    """The reason `ChemclawState.loop_capped` exists at all.

    `CappedModelCallLimit` delegates counting to upstream and records only the *fact*, because
    upstream's per-run counter is `UntrackedValue` (never checkpointed) **and** `PrivateStateAttr`
    (stripped from what the run returns). If upstream ever makes it readable, the first-party field
    becomes redundant and should go.
    """
    from langchain.agents.middleware.model_call_limit import ModelCallLimitState

    # `repr` of the whole annotation rather than `__metadata__`: the field is
    # `NotRequired[Annotated[...]]`, so the metadata hangs off the *inner* `Annotated` and reading
    # it from the outer `NotRequired` silently returns nothing — which would make this assertion
    # pass for the wrong reason the day upstream dropped either marker.
    hints = get_type_hints(ModelCallLimitState, include_extras=True)
    annotation = repr(hints["run_model_call_count"])
    assert "UntrackedValue" in annotation and "OmitFromSchema" in annotation, (
        "ModelCallLimitMiddleware's run counter is now readable from a finished run; "
        "agent/state.ChemclawState.loop_capped exists only because it was not"
    )


def test_create_agent_still_bakes_a_recursion_limit_this_repo_overrides() -> None:
    """`turn_config` chooses the step ceiling because upstream's choice is effectively no ceiling.

    9999 supersteps is thousands of model calls, and reaching it raises `GraphRecursionError`,
    which discards the partial answer `agent/loop_cap.py` deliberately lets out. If upstream ever
    picks a sane default this override stays anyway — but the docstring claiming 9999 must not be
    allowed to rot.

    **This assertion was vacuous and is the reason the file's own rule is stated so firmly.** It
    used
    to be `"recursion_limit" in inspect.getsource(factory)` — one identifier in an 81 KB module.
    Mutation-tested: it still passed with the default changed to 25, and still passed with the
    baking
    deleted entirely as long as the word survived in a comment, while its failure message claimed
    "create_agent no longer sets a recursion_limit". A source-text grep is a *behaviour* assertion
    in
    disguise, which the module docstring above says does not belong here. The value is on the
    compiled graph, so read it.
    """
    from langchain.agents import create_agent

    from tests.fakes_langgraph import ScriptedChatModel

    baked = create_agent(model=ScriptedChatModel(["x"]), tools=[]).config
    assert baked is not None and baked.get("recursion_limit") == 9_999, (
        f"create_agent's baked recursion_limit is {baked and baked.get('recursion_limit')}, not "
        "9999 — agent/state.turn_config's docstring describes displacing that number, and "
        "core/config/agent.agent_recursion_limit is sized against it"
    )


def test_the_mcp_adapter_still_calls_a_tool_with_no_read_timeout() -> None:
    """The open backlog row, pinned so it closes itself when upstream fixes it.

    `langchain_mcp_adapters` calls `session.call_tool` with no `read_timeout_seconds`, so a
    connector that never answers blocks the turn forever — measured: a 4 s tool still blocked at
    25 s. This asserts the *absence*, so the day upstream adds the parameter this test fails and
    the first-party timeout wrapper can be deleted. A test that pins a bug is how a workaround gets
    removed instead of outliving its reason.
    """
    import inspect

    from langchain_mcp_adapters import tools

    source = inspect.getsource(tools)
    assert "read_timeout_seconds" not in source, (
        "langchain-mcp-adapters now supports a call timeout — remove the first-party workaround "
        "and close the BACKLOG row"
    )


def test_the_v3_stream_transformer_extension_point_is_present() -> None:
    """The **restart condition** for the deferred v3 migration — not a live dependency.

    Nothing in `src/` imports any of this: the v3 front door was built, measured and reverted,
    because v3 reports token usage only at `message-finish` and a turn abandoned mid-message booked
    0 tokens where the current driver books ~30 — making "drop the connection just before the
    answer" a free bypass of the token budget.

    It is asserted anyway because the rest of that migration is known-good and cheap to restart:
    `stream_events(version="v3")` owns `stream_mode`/`subgraphs` and so retires `astream`'s tuple
    arity, the largest unpromised-shape read left in this tree. If this seam disappears, the restart
    condition is gone too and the deferred backlog row should be closed rather than left implying
    work that is no longer possible.
    """
    from langchain.agents.middleware import AgentMiddleware
    from langgraph.stream._types import StreamTransformer
    from langgraph.stream.transformers import (
        CustomTransformer,
        MessagesTransformer,
        SubgraphTransformer,
        UpdatesTransformer,
    )

    assert hasattr(AgentMiddleware, "transformers"), (
        "AgentMiddleware no longer carries `transformers`; a middleware can no longer register the "
        "stream projection that names its own events"
    )
    for transformer in (
        MessagesTransformer,
        UpdatesTransformer,
        CustomTransformer,
        SubgraphTransformer,
    ):
        assert issubclass(transformer, StreamTransformer)
        assert getattr(transformer, "required_stream_modes", None), (
            f"{transformer.__name__} no longer declares required_stream_modes, which is how v3 "
            "decides what the graph must emit"
        )


@pytest.mark.parametrize(
    ("module", "name", "reader"),
    [
        ("deepagents", "RubricMiddleware", "the in-loop answer critic"),
        ("deepagents.middleware.subagents", "SubAgentMiddleware", "agent/team.py"),
        ("deepagents.middleware.subagents", "CompiledSubAgent", "agent/team.py"),
        ("deepagents.middleware.skills", "SkillsMiddleware", "agent/langgraph_agent.py"),
        ("deepagents.backends", "FilesystemBackend", "agent/skill_backend.py"),
        ("deepagents.backends", "CompositeBackend", "agent/langgraph_agent.skills_backend"),
        ("deepagents.backends", "StateBackend", "agent/langgraph_agent.skills_backend"),
    ],
)
def test_the_deepagents_middleware_this_repo_composes_are_importable(
    module: str, name: str, reader: str
) -> None:
    """`create_deep_agent` is deliberately not called; these are imported one at a time.

    D-2026-08-11 declines the bundled harness because its default stack always registers
    `FilesystemMiddleware` — a write/edit/glob/grep surface acquired as a side effect of wanting to
    read a `SKILL.md`. The cost of picking middleware individually is that a 0.x reshuffle can move
    one without moving the others, so each import this repository makes is asserted rather than
    assumed.
    """
    import importlib

    imported = importlib.import_module(module)
    assert hasattr(imported, name), f"{module} no longer exports {name}, which {reader} uses"


def test_the_pinned_versions_are_the_ones_these_assertions_were_measured_against() -> None:
    """A floor, not a ceiling — so a bump is loud once and then accepted deliberately.

    Every assertion above was measured against these versions. The point is not to forbid an
    upgrade; it is that raising this floor is the moment somebody re-reads the file and decides the
    dependencies above are still the right ones. Failing here means "go and look", never "pin it
    back down".
    """
    from importlib.metadata import version

    measured: dict[str, tuple[int, ...]] = {
        "langchain": (1, 3, 14),
        "langgraph": (1, 2, 10),
        "deepagents": (0, 7, 5),
        "langchain-mcp-adapters": (0, 3, 2),
    }
    for package, floor in measured.items():
        found = tuple(int(part) for part in version(package).split(".")[:3])
        assert found >= floor, (
            f"{package} {'.'.join(map(str, found))} is below the {'.'.join(map(str, floor))} "
            "these assertions were measured against"
        )
