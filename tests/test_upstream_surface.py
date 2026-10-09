"""Every upstream shape this repository depends on, asserted in one place.

Layer 1 depends on some things `langchain`, `langgraph` and `deepagents` never promised: a state
key's name, a tool's name, a private constant, a baked default. Each is asserted here once, with
the first-party code that would break named in the failure message, so a dependency bump is one
review rather than scattered surprises. Upstream behaviour is asserted where it is used, against
a compiled graph, not here.

When one fails, do not just update the value: read the named module, decide whether the
dependency is still right, and record a changed decision in an ADR. How many couplings there are
is this file's length, not a number in a sentence.
"""

import asyncio
import inspect
from types import SimpleNamespace
from typing import Any, get_type_hints

import pytest

# Module-level, unlike the lazy `build_langgraph_agent` imports below, because a `parametrize`
# decorator is evaluated at collection: the point of importing it is that this file cannot hold
# its own copy of the tuple, and a copy is what a lazy import would force back.
from chemclaw.agent.langgraph_agent import _SKILLS_PROMPT_REMOVALS


def test_the_todo_middleware_still_names_the_plan_channel_todos() -> None:
    """`todos` is the plan channel, and three first-party readers spell it by hand.

    `agent/plan_state.py`, `agent/plan_gate.py` and `ChemclawState` all rely on it; a rename would
    make the gate fail open, bound to a plan nobody can find.
    """
    from langchain.agents.middleware.todo import PlanningState

    assert "todos" in get_type_hints(PlanningState, include_extras=True), (
        "TodoListMiddleware no longer declares `todos`; agent/plan_state.py and agent/plan_gate.py "
        "both read that key by name"
    )


def test_a_todo_still_carries_a_status_and_still_spells_the_live_one_in_progress() -> None:
    """A todo still carries `status`, and the live one is still spelled `"in_progress"`.

    `agent/plan_link.py` stamps a durable job with the first todo in that state; a rename either
    side would silently stamp every job `plan_step=""`, and first-party tests build their own todos.
    """
    from typing import get_args

    from langchain.agents.middleware.todo import Todo

    hints = get_type_hints(Todo, include_extras=True)
    assert "status" in hints, "a Todo no longer carries `status`; agent/plan_link.py reads it"
    assert "in_progress" in get_args(hints["status"]), (
        "`in_progress` is no longer one of Todo.status's values; agent/plan_link.py matches that "
        "literal to decide which plan step a launched job is stamped with"
    )


def test_the_plan_is_written_by_a_tool_called_write_todos() -> None:
    """The plan is written by a tool called `write_todos`.

    `agent/plan_gate.py` spells it as a literal so a rename fails loudly rather than letting a gated
    call beside a plan rewrite through.
    """
    from langchain.agents.middleware import TodoListMiddleware

    names = {tool.name for tool in TodoListMiddleware().tools}
    assert "write_todos" in names, (
        "TodoListMiddleware renamed its tool; agent/plan_gate._PLAN_WRITE_TOOL and "
        "agent/chemclaw_agent.harness_tool_names both depend on `write_todos`"
    )


def test_the_todo_middleware_still_lets_a_subclass_replace_its_tool_and_read_its_prompts() -> None:
    """The todo middleware lets a subclass replace its tool and read its prompts.

    `agent/plan_scope.py` subclasses `TodoListMiddleware`, reads `self.system_prompt` and
    `self.tool_description`, appends to both and replaces `self.tools`, so each plan step declares
    its tools. None of the three is published API, and a change would leave the subclass internally
    consistent while every plan declared nothing.
    """
    from langchain.agents.middleware import TodoListMiddleware

    upstream = TodoListMiddleware()
    assert isinstance(getattr(upstream, "system_prompt", None), str), (
        "TodoListMiddleware no longer exposes `system_prompt`; agent/plan_scope.py appends to it"
    )
    assert isinstance(getattr(upstream, "tool_description", None), str), (
        "TodoListMiddleware no longer exposes `tool_description`; agent/plan_scope.py appends to it"
    )
    assert isinstance(upstream.tools, list) and len(upstream.tools) == 1, (
        "TodoListMiddleware no longer publishes exactly one tool in a plain list; "
        "agent/plan_scope.ScopedTodoListMiddleware replaces that list wholesale"
    )


def test_a_todo_still_carries_only_content_and_status_upstream() -> None:
    """A todo still carries only `content` and `status` upstream.

    `ScopedTodo` restates those two keys and adds a third; an extra upstream key would be silently
    refused by the plan tool.
    """
    from langchain.agents.middleware.todo import Todo

    from chemclaw.agent.plan_scope import ScopedTodo

    upstream = set(get_type_hints(Todo, include_extras=True))
    ours = set(get_type_hints(ScopedTodo, include_extras=True))
    assert upstream <= ours, f"agent/plan_scope.ScopedTodo is missing {sorted(upstream - ours)}"
    assert ours - upstream == {"tools"}, (
        f"agent/plan_scope.ScopedTodo and upstream's Todo now differ by {sorted(ours - upstream)}; "
        "the copy is only safe while `tools` is the single first-party addition"
    )


def test_a_subagent_still_cannot_see_the_parent_s_todos() -> None:
    """A subagent still cannot see the parent's `todos`.

    `plan_gate._plan_behind` falls back to `session_todos()` only because of this exclusion; if it
    ends, the fallback is dead code and should be deleted.
    """
    from deepagents.middleware.subagents import _EXCLUDED_STATE_KEYS

    assert "todos" in _EXCLUDED_STATE_KEYS, (
        "subagents now inherit `todos`; agent/plan_gate._plan_behind's session fallback is "
        "no longer needed and should be removed rather than left in place"
    )


def test_a_subagents_files_still_cross_into_its_callers_state() -> None:
    """A subagent's `files` still cross into its caller's state.

    `_EXCLUDED_STATE_KEYS` does not include `files`, so a helper's scratch file reaches its caller.
    Asserted as an absence so upstream adding it turns this red. A red here means correcting the
    prose in `_subagents`, `HELPER_BRIEF` and `frame_connector_results`; the framing branch is still
    needed for a caller's own writes.
    """
    from deepagents.middleware.subagents import _EXCLUDED_STATE_KEYS

    assert "files" not in _EXCLUDED_STATE_KEYS, (
        "subagents no longer pass `files` back to their caller. agent/langgraph_agent._subagents, "
        "agent/subagents.HELPER_BRIEF and agent/tool_framing.frame_connector_results all describe "
        "a helper's file crossing back and staying; re-read "
        "docs/decisions/D-2026-09-04-a-helpers-file-crosses-back-and-stays.md before editing them"
    )


def test_read_file_still_has_no_video_route_this_deployment_could_reach() -> None:
    """`read_file` still has no video route this deployment could reach.

    On a video, `read_file` returns a `Command` with a synthetic `HumanMessage` whose header
    interpolates the caller's path, and `rewritten_tool_messages` rewrites only `ToolMessage`s.
    `video_dependencies_available()` needs `av` and `PIL.Image`; Pillow already arrives via RDKit,
    so one transitive dependency would enable it. The predicate is imported from its definition site
    `deepagents.middleware._video`. On a red, decide first whether a video route is wanted; the fix
    would be in `agent/tool_result_shape.py`.
    """
    from deepagents.middleware._video import video_dependencies_available

    assert not video_dependencies_available(), (
        "`av` and Pillow are both installed, so deepagents' read_file now has a video route whose "
        "synthetic HumanMessage carries the caller's path in a text block. "
        "agent/tool_result_shape.rewritten_tool_messages visits ToolMessages only, so "
        "agent/tool_framing.frame_connector_results does not defang it — read "
        "docs/decisions/D-2026-09-04-a-helpers-file-crosses-back-and-stays.md and decide whether "
        "this deployment wants that route before installing the dependency"
    )


def test_the_skills_middleware_still_caches_under_skills_metadata() -> None:
    """The skills middleware still caches under `skills_metadata`.

    `ReloadingSkillsMiddleware` redeclares that channel as an `UntrackedValue` so the listing
    reloads every turn; a rename would leave a caller who lost a role still offered its skills.
    """
    from deepagents.middleware.skills import SkillsState

    hints = get_type_hints(SkillsState, include_extras=True)
    assert "skills_metadata" in hints, (
        "SkillsMiddleware renamed its cache channel; "
        "agent/langgraph_agent.ReloadingSkillsState redeclares `skills_metadata` by name"
    )
    # The annotation as well as the name: the redeclaration must reproduce `PrivateStateAttr`, or
    # the role-narrowed listing enters the graph's input schema where a caller could replace it.
    # `tests/test_state_channels.py` asserts our side.
    assert "OmitFromSchema" in repr(hints["skills_metadata"]), (
        "SkillsMiddleware no longer marks `skills_metadata` private; "
        "agent/langgraph_agent.ReloadingSkillsState copies that marker and should stop"
    )


def test_private_state_attr_is_still_where_the_skills_state_reaches_for_it() -> None:
    """`PrivateStateAttr` is still where `ReloadingSkillsState` imports it from.

    It is not in `langchain.agents.middleware.__all__`, so `agent/langgraph_agent.py` reaches into
    `langchain.agents.middleware.types`; dropping the marker is not an option because it is a
    security property.
    """
    from langchain.agents.middleware import types

    assert hasattr(types, "PrivateStateAttr"), (
        "PrivateStateAttr moved; agent/langgraph_agent.ReloadingSkillsState imports it from "
        "langchain.agents.middleware.types because it is not re-exported by the package"
    )


def test_create_agent_still_bakes_a_recursion_limit_this_repo_overrides() -> None:
    """`create_agent` still bakes a recursion limit this repository overrides.

    Upstream's 9999 supersteps is effectively no ceiling, and hitting it raises
    `GraphRecursionError`, discarding the partial answer `agent/loop_cap.py` lets out. Read off the
    compiled graph rather than grepped from source, which a comment could satisfy.
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
    """The MCP adapter still calls a tool with no read timeout.

    So `connectors/registry.py` bounds tool calls with the `ClientSession` default in
    `_session_kwargs`. Pinned as an absence: if upstream adds a per-call timeout, revisit the
    coarser session-wide default.
    """
    import inspect

    from langchain_mcp_adapters import tools

    source = inspect.getsource(tools)
    assert "read_timeout_seconds" not in source, (
        "langchain-mcp-adapters now names a call timeout — re-examine the session-wide default "
        "`_session_kwargs` sets in `connectors/registry.py`, chosen for want of a per-call one"
    )


def test_the_v3_stream_transformer_extension_point_is_present() -> None:
    """The v3 stream-transformer extension point is present.

    It is the restart condition for a deferred migration, not a live dependency.

    Nothing in `src/` imports it. v3 reports token usage only at `message-finish`, so an abandoned
    turn would book nothing. If this seam disappears, the deferred backlog row should be closed.
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


def test_create_deep_agent_still_takes_the_parameters_the_harness_is_assembled_from() -> None:
    """`create_deep_agent` still takes the parameters the harness is assembled from.

    `build_langgraph_agent` passes each by keyword, so this signature is the seam; a rename into
    `**kwargs` would otherwise be silent.
    """
    import inspect

    from deepagents import create_deep_agent

    parameters = set(inspect.signature(create_deep_agent).parameters)
    required = {
        "model",
        "tools",
        "system_prompt",
        "middleware",
        "subagents",
        "skills",
        "permissions",
        "backend",
        "interrupt_on",
        "state_schema",
        "checkpointer",
        "store",
    }
    assert required <= parameters, (
        f"create_deep_agent no longer accepts {sorted(required - parameters)}; "
        "agent/langgraph_agent.build_langgraph_agent passes each of these by keyword"
    )


def test_the_filesystem_middleware_still_takes_its_permissions_under_a_private_name() -> None:
    """The filesystem middleware still takes its permissions under the private `_permissions=`.

    `agent/langgraph_agent._middleware` substitutes its own `FilesystemMiddleware` to withhold
    `execute`/`delete`, and the replacement must be handed the deny rules itself. The behaviour is
    asserted in `tests/test_scratchpad.py`; the name is pinned here.
    """
    import inspect

    from deepagents.middleware.filesystem import FilesystemMiddleware

    parameters = inspect.signature(FilesystemMiddleware.__init__).parameters
    assert "_permissions" in parameters, (
        "FilesystemMiddleware no longer takes `_permissions`; agent/langgraph_agent._middleware "
        "passes the deny-rules to its replacement instance under that name, and without it the "
        "rules reach nothing"
    )
    assert "permissions" not in parameters, (
        "FilesystemMiddleware now takes a *public* `permissions` — the underscored keyword this "
        "repository reaches for has been promoted, so agent/langgraph_agent._middleware should "
        "stop reaching past the API"
    )


def test_the_filesystem_tool_surface_is_still_the_eight_names_the_gate_answers_for() -> None:
    """The filesystem tool surface is still exactly the eight names the gate answers for.

    Each verb is gated, validated and listed; a new upstream verb would be a capability no gate
    names. Equality, because a superset is the failure mode.
    """
    from deepagents.backends import StateBackend
    from deepagents.middleware.filesystem import FilesystemMiddleware

    surface = {tool.name for tool in FilesystemMiddleware(backend=StateBackend()).tools}
    assert surface == {
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "delete",
        "glob",
        "grep",
        "execute",
    }, (
        "the filesystem tool surface changed; agent/langgraph_agent._middleware allow-lists it by "
        "name (via agent/scratchpad.scratchpad_tools) and chemclaw_agent.available_tool_names must "
        "answer for every verb"
    )


def test_the_tool_node_still_holds_its_bound_tools_under_tools_by_name() -> None:
    """The tool node still holds its bound tools under `_tools_by_name`.

    `tests/test_context_floor.py` must measure the tools the graph binds, and `ToolNode` has no
    public accessor. Asserted on a real instance, since the attribute is set in `__init__`.
    """
    from langchain_core.tools import tool as create_tool
    from langgraph.prebuilt.tool_node import ToolNode

    @create_tool
    def probe(value: str) -> str:
        """A tool whose only job is to be held."""
        return value

    held = ToolNode([probe])._tools_by_name
    assert held == {"probe": probe}, (
        "ToolNode no longer holds its bound tools under `_tools_by_name`; "
        "tests/test_context_floor.py::_bound_tools reads the static prefix off exactly that "
        "attribute and would silently measure nothing"
    )


def test_a_compiled_agent_still_reaches_its_tool_node_at_nodes_tools_dot_bound() -> None:
    """A compiled agent still reaches its tool node at `nodes["tools"].bound`.

    `_bound_tools` relies on the node key, the `PregelNode.bound` accessor and the private mapping
    above. Asserted over upstream's own `create_agent`, so a first-party regression cannot look like
    a dependency bump.
    """
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.tools import tool as create_tool
    from langgraph.prebuilt.tool_node import ToolNode

    @create_tool
    def probe(value: str) -> str:
        """A tool whose only job is to be bound."""
        return value

    graph = create_agent(
        model=GenericFakeChatModel(messages=iter([AIMessage(content="")])), tools=[probe]
    )

    assert "tools" in graph.nodes, (
        f"a compiled agent no longer names its tool node `tools` (it has {sorted(graph.nodes)}); "
        "tests/test_context_floor.py::_bound_tools looks the node up by that literal key and "
        "would raise KeyError instead of measuring the static prefix"
    )
    node = graph.nodes["tools"]
    assert hasattr(node, "bound"), (
        "a compiled agent's PregelNode no longer exposes the node it wraps as `.bound`; "
        "tests/test_context_floor.py::_bound_tools unwraps the ToolNode through exactly that "
        "attribute"
    )
    assert isinstance(node.bound, ToolNode), (
        f"`nodes['tools'].bound` is now {type(node.bound).__name__} rather than a ToolNode; "
        "tests/test_context_floor.py::_bound_tools reads `_tools_by_name` off it and the "
        "assertion above this one no longer says anything about what it will find"
    )
    assert node.bound._tools_by_name == {"probe": probe}, (
        "the full path tests/test_context_floor.py::_bound_tools walks — "
        "`nodes['tools'].bound._tools_by_name` — no longer yields the tools the graph was built "
        "with, so the context ratchet would measure the wrong surface"
    )


def test_the_filesystem_middleware_still_offloads_oversized_tool_results() -> None:
    """The filesystem middleware still offloads oversized tool results.

    A result past `tool_token_limit_before_evict` is written to the backend and the model gets a
    path plus a preview, so evidence stays readable rather than dropped.
    """
    import inspect

    from deepagents.middleware.filesystem import FilesystemMiddleware

    parameters = inspect.signature(FilesystemMiddleware.__init__).parameters
    assert "tool_token_limit_before_evict" in parameters, (
        "FilesystemMiddleware no longer offloads oversized tool results; agent/compaction.py "
        "was narrowed on the assumption that it does"
    )
    assert "tools" in parameters, (
        "FilesystemMiddleware lost its tool allow-list; agent/langgraph_agent uses it to withhold "
        "`execute` and `delete`, which is a security narrowing rather than a preference"
    )


def test_a_filesystem_permission_still_has_the_two_modes_the_rules_use() -> None:
    """A filesystem permission still has the two modes the rules use.

    `agent/scratchpad.filesystem_permissions` uses `allow` under the two writable roots and a
    blanket `deny` behind them, which keeps the scratchpad out of the skills tree. `interrupt` is
    not
    asserted: no rule declares one.
    """
    from deepagents import FilesystemPermission

    mode = FilesystemPermission.__annotations__["mode"]
    for expected in ("allow", "deny"):
        assert expected in repr(mode), (
            f"FilesystemPermission no longer supports mode={expected!r}; "
            "agent/scratchpad.filesystem_permissions declares rules in both"
        )


def test_the_interrupt_on_predicate_is_still_synchronous() -> None:
    """The `interrupt_on` predicate is still synchronous.

    It is the restart condition for adopting `HumanInTheLoopMiddleware`.

    Nothing in `src/` imports it; plan approval is a first-party `wrap_tool_call`. The gate's
    predicates `await` the approval store, so an async `when` would lift one of the reasons for
    that. Asserted as an absence, so a red is a prompt to re-measure, not to migrate.
    """
    from langchain.agents.middleware.human_in_the_loop import InterruptOnConfig

    when = repr(InterruptOnConfig.__annotations__["when"])
    assert "when" in InterruptOnConfig.__annotations__, (
        "InterruptOnConfig lost its `when` predicate entirely; "
        "D-2026-08-15-the-plan-gate-stays-a-refusal names it as the restart condition's subject"
    )
    assert "Awaitable" not in when and "Coroutine" not in when, (
        f"InterruptOnConfig.when is now {when} — an async predicate lifts the first of the four "
        "findings that declined HumanInTheLoopMiddleware for plan approval. Re-read "
        "docs/decisions/D-2026-08-15-the-plan-gate-stays-a-refusal-because-an-interrupt-cannot-"
        "ask-the-question.md; the other three still stand."
    )


def test_the_store_backend_still_takes_a_namespace_factory() -> None:
    """The store backend still takes a namespace factory.

    The actor is put in the namespace so erasure is a `list_namespaces` and a `delete`; that only
    works while the namespace is ours to choose.
    """
    import inspect

    from deepagents.backends import StoreBackend

    assert "namespace" in inspect.signature(StoreBackend.__init__).parameters, (
        "StoreBackend no longer takes a namespace factory; agent/scratchpad.py keys it by actor "
        "oid so that erasure can find a departing person's memories"
    )


def test_a_checkpointer_can_delete_a_thread_without_naming_its_tables() -> None:
    """A checkpointer can delete a thread without naming its tables.

    Retention and erasure use this instead of a hand-maintained table list that a library upgrade
    could outdate.
    """
    from langgraph.checkpoint.base import BaseCheckpointSaver

    assert hasattr(BaseCheckpointSaver, "adelete_thread"), (
        "BaseCheckpointSaver lost adelete_thread; durable/retention.py and agent/leaver.py would "
        "have to go back to naming the checkpoint tables by hand"
    )


def test_both_filesystem_write_verbs_still_take_the_path_as_file_path() -> None:
    """Both filesystem write verbs still take the path as `file_path`.

    `authz.writes_durable_memory` reads it to tell `/memories/` from `/scratch/`; a rename would
    make every path unknown and, treated as durable, refuse every scratch write on a dry run.
    """
    from deepagents.backends import StateBackend
    from deepagents.middleware.filesystem import FilesystemMiddleware

    args = {
        tool.name: set(tool.args)
        for tool in FilesystemMiddleware(backend=StateBackend()).tools
        if tool.name in {"write_file", "edit_file"}
    }
    assert set(args) == {"write_file", "edit_file"}, (
        "a filesystem write verb disappeared; agent/authz.py gates the pair by name"
    )
    for name, parameters in args.items():
        assert "file_path" in parameters, (
            f"{name} no longer takes `file_path`; agent/authz.writes_durable_memory reads it to "
            "tell a durable memory write from a turn-local scratchpad one"
        )


def test_the_store_still_names_its_two_version_ledgers_the_way_the_grant_file_does() -> None:
    """The store still names its two version-ledger tables the way the grant file does.

    They exist only as string literals in `AsyncPostgresStore.setup()`, so a rename would silently
    un-grant them and fail a rolling deploy with `InsufficientPrivilege`. Asserted against source
    text because upstream exports neither name.
    """
    import inspect
    import re

    from langgraph.store.postgres import aio

    source = re.sub(r"\s+", " ", inspect.getsource(aio))
    for table in ("store_migrations", "vector_migrations"):
        assert re.search(rf'table\s*=\s*"{table}"', source) or f"INTO {table} " in source, (
            f"the postgres store no longer names its version ledger {table!r}; "
            "infra/sql/grants/app_privileges.sql grants INSERT on that name by hand"
        )


def test_custom_middleware_still_replaces_an_upstream_entry_by_name() -> None:
    """Custom middleware still replaces an upstream entry by name.

    The repository withholds `execute` and `delete` by passing its own `FilesystemMiddleware` under
    the same `.name`, which `_apply_custom_middleware` swaps in. If it appended instead, upstream's
    would restore the withheld verbs. `HarnessProfile.excluded_tools` is not used: it resolves by
    the model's reported provider and is silently skipped on a miss.
    """
    from typing import Any

    from deepagents.graph import _apply_custom_middleware
    from langchain.agents.middleware import AgentMiddleware, TodoListMiddleware

    class Impostor(TodoListMiddleware):
        """A stand-in for `FilesystemMiddleware`, sharing a name it does not own by class."""

        @property
        def name(self) -> str:
            return "TodoListMiddleware"

    base: list[AgentMiddleware[Any, Any, Any]] = [TodoListMiddleware()]
    mine = Impostor()
    assert _apply_custom_middleware(base, [mine]) == [mine], (
        "custom middleware no longer replaces a same-named upstream entry in place; "
        "agent/langgraph_agent._middleware relies on this to withhold `execute` and `delete`"
    )


def test_the_subagent_middleware_still_cannot_be_excluded() -> None:
    """The subagent middleware still cannot be excluded.

    `SubAgentMiddleware` is required scaffolding and `_apply_excluded_middleware` raises rather than
    strip it, so `task` ships on every agent and the only decision is what it reaches. If it became
    strippable, reconsider whether the roster should exist.
    """
    from deepagents.graph import _REQUIRED_MIDDLEWARE_NAMES

    assert "SubAgentMiddleware" in _REQUIRED_MIDDLEWARE_NAMES


def test_the_general_purpose_subagent_is_still_displaced_by_claiming_its_name() -> None:
    """The general-purpose subagent is still displaced by claiming its name.

    `create_deep_agent` skips inserting an ungoverned `general-purpose` subagent when a spec already
    claims `GENERAL_PURPOSE_SUBAGENT["name"]`; `agent/subagents.py` hard-codes that string.
    """
    from deepagents.middleware.subagents import GENERAL_PURPOSE_SUBAGENT

    from chemclaw.agent.subagents import general_purpose_helper

    assert general_purpose_helper(runnable=None)["name"] == GENERAL_PURPOSE_SUBAGENT["name"], (
        "upstream renamed its default general-purpose subagent, so the spec in agent/subagents.py "
        "no longer displaces it — the `task` roster now carries an ungoverned copy of the surface"
    )


@pytest.mark.parametrize(
    ("module", "name", "reader"),
    [
        ("deepagents", "create_deep_agent", "agent/langgraph_agent.build_langgraph_agent"),
        ("deepagents.middleware.subagents", "SubAgentMiddleware", "the subagent seam"),
        ("deepagents.middleware.subagents", "CompiledSubAgent", "the subagent seam"),
        ("deepagents.middleware.skills", "SkillsMiddleware", "agent/langgraph_agent.py"),
        ("deepagents.backends", "FilesystemBackend", "agent/skill_backend.py"),
        ("deepagents.backends", "CompositeBackend", "agent/langgraph_agent.skills_backend"),
        ("deepagents.backends", "StateBackend", "agent/langgraph_agent.skills_backend"),
    ],
)
def test_the_deepagents_symbols_this_repo_names_are_importable(
    module: str, name: str, reader: str
) -> None:
    """Every deepagents name `src/` spells is importable.

    A 0.x minor can move a symbol without deprecation, and an import failing at construction breaks
    every turn at once.
    """
    import importlib

    imported = importlib.import_module(module)
    assert hasattr(imported, name), f"{module} no longer exports {name}, which {reader} uses"


def test_the_gateway_client_still_publishes_cache_tokens_under_the_two_flat_keys() -> None:
    """The gateway client still publishes cache tokens under two flat keys, with no per-TTL keys.

    `agent/turn_usage.graph_usage_tokens` subtracts `cache_read` and `cache_creation` from
    `input_tokens` and books `cache_creation` as the write; a rename would price every cached token
    as full input. The per-TTL keys are asserted absent: if `langchain_openai` starts emitting them,
    `turn_usage` needs a reader for them.
    """
    from langchain_openai.chat_models.base import _create_usage_metadata

    usage = _create_usage_metadata(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "total_tokens": 1200,
            "prompt_tokens_details": {"cached_tokens": 700, "cache_write_tokens": 100},
        }
    )
    details = usage["input_token_details"]
    assert details.get("cache_read") == 700, (
        "langchain_openai renamed the cache-read key — agent/turn_usage.graph_usage_tokens "
        "subtracts it from input_tokens and would now double-count every cached token"
    )
    assert details.get("cache_creation") == 100, (
        "langchain_openai renamed the cache-write key — agent/turn_usage.graph_usage_tokens "
        "reads it, and turn_costs.cache_write_tokens is what it fills"
    )
    # `prompt_tokens` includes the cached share, which is the whole reason `input` is a residual.
    assert usage["input_tokens"] == 1000
    assert not [key for key in details if key.startswith("ephemeral_")], (
        "langchain_openai now publishes a per-TTL cache-write breakdown; it zeroes the flat key "
        "when it does, so agent/turn_usage needs the `_cache_creation` helper back"
    )

    # With a service tier taken from the response body, the same function prefixes both keys
    # (`priority_cache_read`), so a gateway alone can trigger it. `graph_usage_tokens` reads by
    # suffix; this pins that the prefix is joined by one underscore and the names after it are
    # unchanged.
    tiered = _create_usage_metadata(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "total_tokens": 1200,
            "prompt_tokens_details": {"cached_tokens": 700, "cache_write_tokens": 100},
        },
        "flex",
    )
    tiered_details = tiered["input_token_details"]
    assert "flex_cache_read" in tiered_details and "flex_cache_creation" in tiered_details, (
        "langchain_openai changed how service_tier prefixes the cache keys; "
        "agent/turn_usage._cache_detail matches `<tier>_cache_read`/`<tier>_cache_creation` by "
        "suffix, so a tiered deployment's cache reads would be billed as fresh input again"
    )
    from langchain_openai import ChatOpenAI

    assert ChatOpenAI(model="x", api_key="k").service_tier is None, (  # type: ignore[arg-type]
        "ChatOpenAI now defaults service_tier to a value; that is not itself a defect any more "
        "(the keys are read by suffix), but it changes what an untiered deployment is sent"
    )


def test_a_failed_model_call_still_carries_the_gateways_own_usage_block() -> None:
    """A failed model call still carries the gateway's own usage block.

    `turn_usage.error_result_usage` reads `on_llm_error`'s `response=` kwarg and
    `response_metadata["body"]`. With `method="json_schema"` a malformed reply raises inside the SDK
    and `on_llm_end` never fires, so this is the only source of the measured usage. Asserted against
    the private helper that builds the shape.
    """
    from langchain_core.language_models.chat_models import _generate_response_from_error

    error = ValueError("bad reply")
    error.response = SimpleNamespace(  # type: ignore[attr-defined]
        json=lambda: {"usage": {"prompt_tokens": 5000, "completion_tokens": 500}},
        status_code=200,
    )
    generations = _generate_response_from_error(error)
    assert generations, "langchain_core no longer builds a generation from a failed call"
    body = generations[0].message.response_metadata.get("body")
    assert isinstance(body, dict) and body.get("usage"), (
        "langchain_core stopped putting the failing call's raw HTTP body on the error result — "
        "agent/turn_usage.error_result_usage reads it, and the verifier's degrade path would book "
        "zero against a request the gateway billed in full"
    )


def test_a_message_still_flattens_its_content_blocks_through_a_text_property() -> None:
    """`BaseMessage.text` is still a property, which `evals/live_judge.py` relies on.

    If it became a method again, `str(response.text)` would be a bound-method repr and every probe
    would grade `ungraded`. Asserted on the class, since an instance's `.text` is a string either
    way.
    """
    from langchain_core.messages import BaseMessage

    assert isinstance(BaseMessage.text, property), (
        "BaseMessage.text is no longer a property; evals/live_judge.py reads `response.text` as "
        "one and would grade every probe `ungraded` off the repr of a bound method"
    )


def test_the_sse_decoder_is_still_private_and_still_flushes_on_an_empty_line() -> None:
    """The SSE decoder is still private and still flushes on an empty line.

    `evals/live.decoded_events` drives `httpx_sse`'s parser directly: `SSEDecoder` must stay
    importable under its two class names, and `decode("")` must return the pending event, because
    `aiter_sse` drops the final event of a cut-off stream. If it becomes public, update the layering
    row and the import. If upstream flushes itself,
    `tests/test_live_probes.py::test_the_final_event_of_a_stream_that_ends_without_a_blank_line_still_arrives`
    says when the workaround may go.
    """
    import httpx_sse
    from httpx_sse._decoders import SSEDecoder, SSELineDecoder

    assert "SSEDecoder" not in httpx_sse.__all__, (
        "httpx_sse now publishes SSEDecoder; chemclaw/evals/live.py should import it from the "
        "package top level and lose its row in tests/test_third_party_layering.py"
    )

    lines = SSELineDecoder()
    assert lines.decode('data: {"a": 1}\n') == ['data: {"a": 1}']
    assert lines.decode('data: {"b": ') == []
    assert lines.flush() == ['data: {"b": ']

    events = SSEDecoder()
    assert events.decode('data: {"a": 1}') is None, (
        "SSEDecoder no longer buffers a `data:` line; evals/live.decoded_events drives it line "
        "by line and reads the event off the blank line that follows"
    )
    flushed = events.decode("")
    assert flushed is not None and flushed.data == '{"a": 1}', (
        "SSEDecoder no longer emits the pending event on an empty line; that is the flush "
        "evals/live.decoded_events supplies at end-of-stream so a truncated turn keeps its answer"
    )


def test_the_pinned_versions_are_the_ones_these_assertions_were_measured_against() -> None:
    """The pinned versions are the ones these assertions were measured against.

    A floor, not a ceiling: raising it is the moment to re-read this file. Failing here means "go
    and look", never "pin it back down".
    """
    from importlib.metadata import version

    measured: dict[str, tuple[int, ...]] = {
        "langchain": (1, 3, 14),
        "langgraph": (1, 2, 10),
        "deepagents": (0, 7, 5),
        "langchain-mcp-adapters": (0, 3, 2),
        # The two clients the gateway seam actually reads shapes off: `langchain-openai` for the
        # usage keys above, `langchain-core` for `BaseMessage.text` being a property.
        "langchain-openai": (1, 6, 0),
        "langchain-core": (1, 6, 0),
        # Not a layer-1 dependency at all — the live harnesses' SSE parser, whose *private*
        # decoder `evals/live.py` drives by hand. It is here because that is the coupling most
        # likely to be moved by a patch release, and the row above is the one that says so.
        "httpx-sse": (0, 4, 3),
    }
    for package, floor in measured.items():
        found = tuple(int(part) for part in version(package).split(".")[:3])
        assert found >= floor, (
            f"{package} {'.'.join(map(str, found))} is below the {'.'.join(map(str, floor))} "
            "these assertions were measured against"
        )


def test_a_pydantic_tool_return_still_reaches_the_model_as_repr() -> None:
    """A pydantic tool return still reaches the model as its repr.

    `_stringify` prefers JSON and falls back to `str()`. This covers in-process tools only;
    connector results arrive as content blocks (next test). If upstream starts serializing models,
    the repr assumption and the workarounds it justified can go. A blanket move to JSON payloads was
    declined, since a `wrap_tool_call` middleware receives an already-stringified message.
    """
    from langchain_core.tools.base import _stringify
    from pydantic import BaseModel, Field

    class _Probe(BaseModel):
        kept: str = "x"
        hidden: str = Field(default="y", exclude=True)

    rendered = _stringify(_Probe())

    assert not rendered.startswith("{"), (
        "`_stringify` now serializes a pydantic model as JSON. Every tool's payload just changed "
        "shape, and `Field(exclude=True)` now takes effect where it previously did not — re-check "
        "agent/condense.Condensation and agent/protocol_tools' string rendering."
    )
    assert "hidden=" in rendered, (
        "`exclude=True` now survives tool-result stringification; the comment on "
        "`Condensation.rows` saying it does not is stale"
    )


def test_an_mcp_tool_result_still_arrives_as_content_blocks_carrying_the_servers_json() -> None:
    """An MCP tool result still arrives as content blocks carrying the server's JSON.

    A connector's `ToolMessage.content` is `list[str | dict]`, and `agent/tool_framing.py` rewrites
    each block's `text` while copying other keys. Asserted on the annotation: a narrowing to `str`
    is the change that would leave the list arm dead.
    """
    from langchain_core.messages import ToolMessage

    hints = get_type_hints(ToolMessage)
    rendered = str(hints["content"])
    assert "list" in rendered, (
        "`ToolMessage.content` is no longer a union with a list arm; "
        "`chemclaw.agent.tool_framing._rewritten` handles content blocks that can no longer arrive"
    )
    assert "str" in rendered, (
        "`ToolMessage.content` no longer admits a plain string; "
        "`chemclaw.agent.tool_framing._rewritten` frames an in-process tool's result on that arm"
    )


def test_a_fastmcp_tool_is_still_a_mutable_object_the_manager_will_hand_over() -> None:
    """A FastMCP tool is still a mutable object the tool manager hands over.

    `connectors/server.py`'s `_publish_tool_results` reassigns each tool's `fn` via
    `server._tool_manager.list_tools()`, relying on live objects, a writable `fn` and `is_async`.
    Copies or frozen models would install cleanly and publish nothing.
    """
    import inspect

    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.tools.base import Tool

    server = FastMCP("upstream-surface-probe")

    @server.tool()
    async def probe(value: str) -> str:
        """A tool whose only job is to be found by the manager."""
        return value

    manager = getattr(server, "_tool_manager", None)
    assert manager is not None, (
        "FastMCP no longer exposes `_tool_manager`; chemclaw.connectors.server patches "
        "`call_tool` on it twice and walks `list_tools()` on it once"
    )
    listed = manager.list_tools()
    assert [tool.name for tool in listed] == ["probe"], listed
    tool = listed[0]
    assert tool.is_async, (
        "`Tool.is_async` no longer reports an async tool as async; "
        "chemclaw.connectors.server._publish_tool_results skips a tool it reads as synchronous"
    )

    sentinel = object()
    tool.fn = sentinel
    assert manager.list_tools()[0].fn is sentinel, (
        "`ToolManager.list_tools` no longer hands back the live `Tool` objects, so "
        "chemclaw.connectors.server._publish_tool_results wraps a copy and publishes nothing"
    )
    assert "fn" in inspect.signature(Tool).parameters or "fn" in Tool.model_fields, (
        "`Tool.fn` is gone; the publish hook has no place left to wrap the tool's own body"
    )


def test_the_mcp_adapter_still_puts_structured_content_under_that_artifact_key() -> None:
    """The MCP adapter still puts structured content under that artifact key.

    `template_activities._structured` needs `response_format="content_and_artifact"`, a `dict`
    artifact and the payload under `structured_content`; otherwise template steps that read a field
    of a tool result raise `UnresolvedReference` after launch.
    """
    import inspect

    import langchain_mcp_adapters.tools as adapter

    source = inspect.getsource(adapter)
    assert 'response_format="content_and_artifact"' in source, (
        "the adapter no longer builds tools with an artifact; "
        "chemclaw.durable.template_activities._structured has nothing to read"
    )
    assert "structured_content" in adapter.MCPToolArtifact.__annotations__, (
        "MCPToolArtifact no longer carries `structured_content`; "
        "chemclaw.durable.template_activities._structured reads that key by name"
    )
    assert issubclass(adapter.MCPToolArtifact, dict), (
        "MCPToolArtifact is no longer a TypedDict; "
        "_structured's `isinstance(artifact, dict)` guard would reject every real artifact"
    )


def test_the_skills_middleware_still_formats_its_listing_under_a_private_name() -> None:
    """The skills middleware still formats its listing under the private `_format_skills_list`.

    `tests/test_context_floor.py` renders the skills block with it, so the floor counts what the
    model is sent rather than a second implementation.
    """
    from deepagents.middleware.skills import SkillsMiddleware

    assert hasattr(SkillsMiddleware, "_format_skills_list"), (
        "deepagents' SkillsMiddleware no longer exposes `_format_skills_list`. "
        "tests/test_context_floor.py::_skills_listing calls it to render the skills block the "
        "static-prefix ratchet measures."
    )


def test_before_agent_still_accepts_the_three_argument_call_the_floor_uses() -> None:
    """`before_agent` still accepts the three-argument call `test_context_floor.py` makes.

    Production no longer depends on the hook's arity, but the floor helper calls it directly.
    Asserted against the signature, so no skills backend is needed.
    """
    import inspect

    from deepagents.middleware.skills import SkillsMiddleware

    params = list(inspect.signature(SkillsMiddleware.before_agent).parameters)
    assert len(params) >= 4, (
        f"deepagents' SkillsMiddleware.before_agent now takes {params}; "
        "tests/test_context_floor.py::_skills_listing calls it as "
        "`before_agent({}, None, None)` (three arguments after self) to load the skills the "
        "static-prefix ratchet measures. Adjust that call, or move the helper onto whatever "
        "load path upstream now publishes."
    )


def test_a_cleared_tool_result_is_still_marked_in_response_metadata() -> None:
    """A cleared tool result is still marked in `response_metadata`.

    `agent/compaction.py::_cleared_calls` relies on that stamp rather than the placeholder text;
    without it the repeat guard keeps refusing calls whose answers the model no longer holds.
    """
    from langchain.agents.middleware import ClearToolUsesEdit
    from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
    from langchain_core.messages.utils import count_tokens_approximately

    messages: list[AnyMessage] = [
        HumanMessage("go"),
        AIMessage("", tool_calls=[{"name": "t", "args": {}, "id": "c0"}]),
        ToolMessage("x " * 6000, tool_call_id="c0"),
        AIMessage("", tool_calls=[{"name": "t", "args": {"n": 1}, "id": "c1"}]),
        ToolMessage("x " * 6000, tool_call_id="c1"),
    ]
    ClearToolUsesEdit(trigger=1, keep=1, placeholder="[cleared]").apply(
        messages, count_tokens=count_tokens_approximately
    )
    assert messages[2].response_metadata.get("context_editing", {}).get("cleared") is True, (
        "ClearToolUsesEdit no longer stamps response_metadata['context_editing']['cleared']. "
        "agent/compaction.py::_cleared_calls reads it to tell agent/repeat_guard.py which calls "
        "lost their answers."
    )


def test_a_client_session_exposes_the_id_its_next_request_will_claim() -> None:
    """A client session exposes the id its next request will claim, as `_request_id`.

    `core/mcp_session.cancel_on_timeout` reads it to send `notifications/cancelled`; a rename would
    silently leave timed-out tools running on the server.
    """
    from mcp.shared.session import BaseSession

    assert "_request_id" in getattr(BaseSession, "__annotations__", {}) or hasattr(
        BaseSession, "_request_id"
    ), (
        "BaseSession no longer declares `_request_id`; core/mcp_session.py::cancel_on_timeout "
        "reads it to learn which request id to cancel when a call outlives its read bound"
    )


def test_a_read_bound_timeout_arrives_as_a_408_mcp_error() -> None:
    """A read-bound timeout arrives as a 408 MCP error.

    408 is the SDK's own code around `anyio.fail_after`, which `cancel_on_timeout` uses to tell a
    timeout from a server error.
    """
    import inspect

    import httpx
    from mcp.shared import session as mcp_session

    source = inspect.getsource(mcp_session.BaseSession.send_request)
    assert "REQUEST_TIMEOUT" in source, (
        "mcp.shared.session.send_request no longer raises its read-bound timeout as "
        f"httpx.codes.REQUEST_TIMEOUT ({int(httpx.codes.REQUEST_TIMEOUT)}); "
        "core/mcp_session.py::cancel_on_timeout matches on that code to decide when to send "
        "notifications/cancelled"
    )


def test_a_streamed_tool_result_is_still_a_subclass_of_tool_message() -> None:
    """A streamed tool result is still a subclass of `ToolMessage`.

    `audit.returned_failure` and `graph_stream._from_update` use `isinstance` because streaming runs
    emit `ToolMessageChunk`; narrowing to `type(x) is ToolMessage` would silently mis-audit failures
    and drop result traces.
    """
    from langchain_core.messages import ToolMessage, ToolMessageChunk

    assert issubclass(ToolMessageChunk, ToolMessage), (
        "ToolMessageChunk is no longer a ToolMessage; agent/audit.returned_failure and "
        "api/graph_stream._from_update both recognise a streamed tool result by isinstance"
    )
    chunk = ToolMessageChunk(content="Error: the instrument is offline", tool_call_id="c-1")
    assert (chunk.status, chunk.tool_call_id) == ("success", "c-1"), (
        "a chunk no longer carries the two fields both readers take off it"
    )


def test_a_dangling_tool_call_is_still_healed_before_the_model_reads_it() -> None:
    """A dangling tool call is still healed before the model reads it.

    A process killed between the model and tool supersteps leaves an unanswered `tool_calls`, which
    the provider then rejects on every later turn. deepagents' `PatchToolCallsMiddleware` answers it
    and nothing in `src/` duplicates it. Driven through the production builder, so a displaced
    middleware would fail.
    """
    from langchain_core.messages import AIMessage, HumanMessage

    from chemclaw.agent.langgraph_agent import build_langgraph_agent
    from chemclaw.agent.message_pairing import unmatched_call_ids
    from tests.fakes import ScriptedModel

    seen: list[list[Any]] = []

    class _Recording(ScriptedModel):
        def _generate(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
            seen.append(list(messages))
            return super()._generate(messages, *args, **kwargs)

    orphan = AIMessage(
        content="",
        tool_calls=[{"name": "predict_pka", "args": {"smiles": "CCO"}, "id": "c-crashed"}],
    )
    graph = build_langgraph_agent(model=_Recording(messages=iter([AIMessage(content="done")])))
    asyncio.run(
        graph.ainvoke({"messages": [HumanMessage("compute"), orphan, HumanMessage("and now?")]})
    )

    assert seen, "the model was never called"
    assert unmatched_call_ids(seen[0]) == set(), (
        "an orphaned tool_use reached the model request; deepagents' PatchToolCallsMiddleware "
        "no longer heals dangling calls, and without a replacement every session that crashes "
        "between two supersteps is permanently bricked"
    )


def test_astream_with_a_mode_list_and_subgraphs_still_yields_three_tuples() -> None:
    """`astream` with a mode list and `subgraphs=True` still yields three-tuples.

    `api/graph_stream.py` unpacks `namespace, mode, payload`, an arity upstream never promised.
    Driven on a compiled graph, since the arity belongs to the running pregel loop.
    """
    from langchain_core.messages import AIMessage

    from chemclaw.agent.langgraph_agent import build_langgraph_agent
    from tests.fakes import ScriptedModel

    graph = build_langgraph_agent(model=ScriptedModel(messages=iter([AIMessage(content="ok")])))

    async def _drive() -> list[Any]:
        chunks = []
        async for chunk in graph.astream(
            {"messages": [("user", "hi")]},
            {"recursion_limit": 50},
            stream_mode=["messages", "updates", "custom"],
            subgraphs=True,
        ):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(_drive())
    assert chunks, "the stream yielded nothing; the driver below would too"
    assert all(isinstance(chunk, tuple) and len(chunk) == 3 for chunk in chunks), (
        "graph.astream(stream_mode=list, subgraphs=True) no longer yields "
        "(namespace, mode, payload) 3-tuples; api/graph_stream.py unpacks exactly that shape "
        "and every turn would die on the first chunk"
    )
    modes = {chunk[1] for chunk in chunks}
    assert modes <= {"messages", "updates", "custom"}, (
        f"the middle element is no longer the mode name: {modes}"
    )


def test_a_streamed_message_still_names_the_node_it_ran_in_and_tools_run_in_tools() -> None:
    """A streamed message names its node, and `create_agent` still names the tool node `tools`.

    `api/graph_stream._TOOL_NODE` withholds `messages` chunks from `langgraph_node == "tools"`, so a
    model call inside a tool body does not leak into the answer. Behaviour is asserted in
    `tests/test_langgraph_stream.py`.
    """
    from langchain.agents import create_agent
    from langchain_core.messages import AIMessage

    from chemclaw.api.graph_stream import _TOOL_NODE
    from tests.fakes import ScriptedModel

    # `Any`: the overloads of `astream` select on a `version` literal this call does not pass.
    graph: Any = create_agent(
        model=ScriptedModel(messages=iter([AIMessage(content="ok")])), tools=[]
    )
    assert _TOOL_NODE == "tools"

    async def _drive() -> list[dict[str, Any]]:
        return [
            payload[1]
            async for _ns, mode, payload in graph.astream(
                {"messages": [("user", "hi")]}, stream_mode=["messages"], subgraphs=True
            )
            if mode == "messages"
        ]

    metadata = asyncio.run(_drive())
    assert metadata and all("langgraph_node" in item for item in metadata), (
        "a `messages` chunk no longer carries `langgraph_node`; api/graph_stream.py reads it to "
        "keep a tool's own model call out of the answer"
    )

    from langchain_core.tools import tool

    @tool
    def noop() -> str:
        """Do nothing."""
        return ""

    with_tools = create_agent(model=ScriptedModel(messages=iter([])), tools=[noop])
    assert _TOOL_NODE in with_tools.get_graph().nodes, (
        f"create_agent no longer names its tool node {_TOOL_NODE!r}; api/graph_stream.py withholds "
        "a tool's own model calls by that name"
    )


def test_the_after_model_call_cap_is_still_the_one_upstream_shape_this_repo_declines() -> None:
    """The runaway cap is first-party; upstream's `ModelCallLimitMiddleware` is unsafe here.

    Upstream increments in `after_model`, so a middleware jumping from `after_model` skips the
    count. Red if upstream moves the increment (re-take the decision) or if anything in `src/`
    imports it.
    """
    import ast
    import inspect
    from pathlib import Path

    from langchain.agents.middleware import ModelCallLimitMiddleware

    counters = {"thread_model_call_count", "run_model_call_count"}
    after = inspect.getsource(ModelCallLimitMiddleware.after_model)
    before = inspect.getsource(ModelCallLimitMiddleware.before_model)
    assert all(f'"{name}", 0) + 1' in after for name in counters), (
        "ModelCallLimitMiddleware no longer increments in `after_model`; the reason "
        "D-2026-08-15 reverted it may no longer hold — re-take the decision, do not edit this"
    )
    assert not any("+ 1" in line for line in before.splitlines()), (
        "ModelCallLimitMiddleware now counts in `before_model` too; see above"
    )

    root = Path(__file__).resolve().parents[1]
    importers = []
    for path in sorted((root / "src").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import | ast.ImportFrom)
                else []
            )
            if "ModelCallLimitMiddleware" in names:
                importers.append(str(path.relative_to(root)))
    assert importers == [], (
        f"{importers} import ModelCallLimitMiddleware; the cap is `agent/loop_cap.py`'s "
        "`before_model` counter, and composing upstream's with a middleware that jumps from "
        "`after_model` skips it (D-2026-08-15)"
    )


def test_tool_node_still_stores_a_prebuilt_tool_object_instead_of_rebuilding_it() -> None:
    """`ToolNode` converts a plain callable and stores a prebuilt `BaseTool` as the same object.

    `agent/tool_schema.py` derives each in-process tool once per process to avoid per-compile
    conversion; if upstream copied what it is handed, the cache would silently stop saving anything.
    """
    from langchain_core.tools import BaseTool
    from langchain_core.tools import tool as create_tool
    from langgraph.prebuilt.tool_node import ToolNode

    def probe_upstream_surface_tool(x: int) -> int:
        """Double one integer.

        Args:
            x: The integer to double.
        """
        return x * 2

    converted = create_tool(probe_upstream_surface_tool)
    assert isinstance(converted, BaseTool)

    from_callable = ToolNode([probe_upstream_surface_tool]).tools_by_name
    from_object = ToolNode([converted]).tools_by_name

    assert list(from_callable) == ["probe_upstream_surface_tool"], (
        "ToolNode no longer keys a converted callable by the function's name; "
        "`agent/tool_schema.py` assumes the conversion it performs is the one ToolNode would"
    )
    assert from_object["probe_upstream_surface_tool"] is converted, (
        "ToolNode no longer stores a prebuilt BaseTool as the same object — it copies or "
        "re-derives it, so `agent/tool_schema.py`'s per-process cache buys nothing and the "
        "measurement in tests/test_langgraph_connectors.py no longer holds"
    )


def test_the_task_tool_returns_a_dict_shaped_command_update() -> None:
    """The `task` tool returns a dict-shaped `Command.update`.

    `agent/tool_result_shape.py` rewrites only the dict form; the other forms `Command` accepts
    would let a helper's report reach the caller without defanging or the size ceiling.
    """
    import deepagents.middleware.subagents as upstream_subagents
    from langchain_core.messages import ToolMessage
    from langgraph.types import Command

    from chemclaw.agent.tool_result_shape import rewritten_tool_messages

    source = inspect.getsource(upstream_subagents._build_task_tool)
    assert "Command(" in source, (
        "deepagents' task tool no longer returns a Command; `agent/tool_result_shape.py` "
        "dispatches on that type, and a plain return would take the ToolMessage branch instead"
    )
    assert "update={" in source, (
        "deepagents' task tool no longer builds a dict-shaped Command.update, so "
        "`agent/tool_result_shape.py` rewrites nothing in a helper's report — it reaches the "
        "caller's thread undefanged and unbounded, which is what that module exists to prevent"
    )

    marked = ToolMessage(content="report", tool_call_id="probe")
    rewritten = rewritten_tool_messages(
        Command(update={"messages": [marked], "model_calls": 1}),
        lambda message: message.model_copy(update={"content": "rewritten"}),
    )
    assert rewritten.update["messages"][0].content == "rewritten"
    assert rewritten.update["model_calls"] == 1, (
        "rewriting a helper's report dropped another update key; those keys are how a fan-out's "
        "spend reaches the single budget it shares"
    )


def test_a_file_a_helper_hands_back_is_a_mapping_carrying_its_text_under_content() -> None:
    """A file a helper hands back is a mapping carrying its text under `content`.

    `rewritten_command_files` rewrites that text in place (rebuilding the file would restamp
    `created_at`); if the shape moved, the bound would go quiet on every file.
    """
    from deepagents.backends.utils import create_file_data
    from langgraph.types import Command

    from chemclaw.agent.tool_result_shape import rewritten_command_files

    data = create_file_data("the helper's notes")
    assert isinstance(data, dict), (
        "deepagents no longer represents a file as a mapping, so "
        "`agent/tool_result_shape.rewritten_command_files` reads nothing and the bound on what a "
        "helper writes into its caller's checkpointed state is silently off"
    )
    assert data.get("content") == "the helper's notes", (
        "a file's text is no longer under `content`, so the same bound is silently off"
    )

    bounded = rewritten_command_files(
        Command(update={"files": {"/scratch/n.md": data}, "model_calls": 1}),
        lambda content, sharing: content[:3],
    )
    assert bounded.update["files"]["/scratch/n.md"]["content"] == "the"
    assert bounded.update["files"]["/scratch/n.md"]["created_at"] == data["created_at"], (
        "bounding a file restamped it, so a helper's file arrives looking newer than it is"
    )
    assert bounded.update["model_calls"] == 1, (
        "bounding a helper's files dropped another update key; those keys are how a fan-out's "
        "spend reaches the single budget it shares"
    )


async def test_a_pipeline_block_on_an_autocommit_connection_is_still_one_transaction() -> None:
    """A pipeline block on an autocommit connection is still one transaction.

    Inside `conn.pipeline()` psycopg does not commit per statement (`txid_current()` matches), which
    makes `AsyncPostgresSaver`'s `aput`, `aput_writes` and `adelete_thread` atomic.
    `agent/checkpointer.py` and `durable/retention.py` reason from this.
    """
    import psycopg

    from chemclaw.core.config import settings
    from tests.pg import migrated_db_or_skip

    await migrated_db_or_skip()
    conn = await psycopg.AsyncConnection.connect(settings.postgres_dsn, autocommit=True)
    try:
        async with conn.pipeline():
            async with conn.cursor() as cur:
                await cur.execute("SELECT txid_current()")
                first = await cur.fetchone()
                await cur.execute("SELECT txid_current()")
                second = await cur.fetchone()
        async with conn.cursor() as cur:
            await cur.execute("SELECT txid_current()")
            outside_first = await cur.fetchone()
            await cur.execute("SELECT txid_current()")
            outside_second = await cur.fetchone()
    finally:
        await conn.close()

    assert first == second, (
        "a pipeline block on an autocommit connection is no longer one transaction; "
        "agent/checkpointer.py and durable/retention.py both reason from it being one"
    )
    assert outside_first != outside_second, (
        "the control arm failed: autocommit outside a pipeline should commit per statement, "
        "so this test would pass for the wrong reason"
    )


def test_aput_still_writes_its_blobs_and_its_checkpoint_row_in_one_transaction() -> None:
    """`aput` still writes its blobs and its checkpoint row in one transaction.

    The test above pins psycopg's pipeline; this pins that `aput` still opens one. Measured through
    `xmin`: two `checkpoint_blobs` rows and one `checkpoints` row must share one xid. Two channels,
    so per-table commits would also fail.
    """
    import asyncio

    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from chemclaw.core.config import settings
    from chemclaw.core.db import connect
    from tests.pg import create_checkpoint_tables, migrated_db_or_skip

    thread = "upstream-aput-one-transaction"

    async def _run() -> tuple[set[str], int]:
        await migrated_db_or_skip()
        await create_checkpoint_tables()
        async with await connect(settings.postgres_dsn) as conn:
            await conn.execute("DELETE FROM checkpoints WHERE thread_id = %s", (thread,))
            await conn.execute("DELETE FROM checkpoint_blobs WHERE thread_id = %s", (thread,))
            await conn.commit()

        async with await connect(settings.postgres_dsn) as writer:
            # Autocommit, because that is what `agent/checkpointer.py` gives the saver and the whole
            # question is what a pipeline does to a connection in that mode.
            await writer.set_autocommit(True)
            saver = AsyncPostgresSaver(writer)  # type: ignore[arg-type]
            checkpoint = empty_checkpoint()
            checkpoint["channel_values"] = {"messages": ["x" * 2048], "other": ["y" * 2048]}
            await saver.aput(
                {"configurable": {"thread_id": thread, "checkpoint_ns": ""}},
                checkpoint,
                {"source": "update", "step": 1},
                {"messages": "1", "other": "1"},
            )

        async with await connect(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT xmin::text FROM checkpoint_blobs WHERE thread_id = %s", (thread,)
                )
                blobs = [str(row[0]) for row in await cur.fetchall()]
                await cur.execute(
                    "SELECT xmin::text FROM checkpoints WHERE thread_id = %s", (thread,)
                )
                rows = [str(row[0]) for row in await cur.fetchall()]
        return set(blobs) | set(rows), len(blobs)

    xids, blob_rows = asyncio.run(_run())

    assert blob_rows >= 2, (
        f"the fixture wrote {blob_rows} blob row(s); with fewer than two this cannot tell a "
        "per-table commit from an atomic one, so it would pass for the wrong reason"
    )
    assert len(xids) == 1, (
        f"aput wrote its blobs and its checkpoint row in {len(xids)} transactions "
        f"({sorted(xids)}); durable/retention.py's three comments and agent/checkpointer.py's "
        "header all state that it is one, and the residual they retracted — a turn whose blobs "
        "commit before a sweep's snapshot and whose row commits after it — is real again"
    )


def test_the_editing_middleware_hands_its_edited_request_to_its_handler() -> None:
    """The editing middleware hands its edited request to its handler.

    `agent/compaction.OffLoopContextEditing` runs upstream's synchronous `wrap_model_call` in a
    worker thread with a handler that captures the prepared request, so the CPU-heavy edits leave
    the event loop without copying upstream's method body. That needs the handler called once with
    the request.
    """
    from typing import Any, cast

    from langchain.agents.middleware import ClearToolUsesEdit, ContextEditingMiddleware
    from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage

    messages: list[AnyMessage] = [
        HumanMessage("go"),
        AIMessage("", tool_calls=[{"name": "t", "args": {}, "id": "c0"}]),
        ToolMessage("x " * 6000, tool_call_id="c0"),
        AIMessage("", tool_calls=[{"name": "t", "args": {"n": 1}, "id": "c1"}]),
        ToolMessage("x " * 6000, tool_call_id="c1"),
    ]

    class _Request:
        def __init__(self, messages: list[AnyMessage]) -> None:
            self.messages = messages

        def override(self, **updates: Any) -> "_Request":
            return _Request(updates.get("messages", self.messages))

    seen: list[_Request] = []

    def capture(request: _Request) -> None:
        seen.append(request)

    middleware = ContextEditingMiddleware(
        edits=[ClearToolUsesEdit(trigger=1, keep=1, placeholder="[cleared]")]
    )
    middleware.wrap_model_call(cast(Any, _Request(messages)), cast(Any, capture))

    assert len(seen) == 1, (
        "ContextEditingMiddleware.wrap_model_call no longer calls its handler exactly once. "
        "agent/compaction.OffLoopContextEditing runs that method in a worker thread with a "
        "capturing handler, to move the per-model-call deepcopy and the context edits off the "
        "event loop without duplicating upstream's method body. Adjust it, or duplicate the body "
        "deliberately."
    )
    assert seen[0].messages[2].content == "[cleared]", (
        "ContextEditingMiddleware.wrap_model_call no longer hands the *edited* message list to its "
        "handler. agent/compaction.OffLoopContextEditing takes the request from that call, so a "
        "handler that receives the unedited one means compaction has silently stopped running."
    )


@pytest.mark.parametrize(("removal", "why"), _SKILLS_PROMPT_REMOVALS)
def test_the_skills_prompt_still_contains_every_sentence_this_deployment_removes(
    removal: str, why: str
) -> None:
    """Each substring `_skills_prompt` cuts from upstream's template is still present.

    `agent/langgraph_agent._skills_prompt` derives the skills prompt from `SKILLS_SYSTEM_PROMPT`
    minus passages false on this deployment, by substring, and raises if one is missing.
    Parametrized over the production tuple, so a bump names the passage that moved.
    """
    from deepagents.middleware.skills import SKILLS_SYSTEM_PROMPT

    assert removal in SKILLS_SYSTEM_PROMPT, (
        f"deepagents' SKILLS_SYSTEM_PROMPT no longer contains a passage this deployment removes "
        f"({why}); re-read upstream's template and update `_SKILLS_PROMPT_REMOVALS` in "
        "agent/langgraph_agent.py"
    )


def test_a_store_search_still_pages_by_offset_and_still_dates_every_item() -> None:
    """A store search still pages by offset and still dates every item.

    `agent/scratchpad._evict_past_the_cap` pages with `offset=len(seen)` and orders by
    `Item.updated_at` itself. The search's own ordering is not assumed: `InMemoryStore` returns
    insertion order where `AsyncPostgresStore` returns most-recent first, asserted below.
    """
    from langgraph.store.memory import InMemoryStore

    store = InMemoryStore()
    namespace = ("upstream-order-probe",)
    for key in ("c", "a", "d", "b"):
        store.put(namespace, key, {"v": key})

    everything = store.search(namespace, limit=10)
    assert len(everything) == 4
    assert all(item.updated_at is not None for item in everything), (
        "a store item no longer carries `updated_at`; "
        "`agent/scratchpad.py::BoundedStoreBackend._evict_past_the_cap` orders the eviction by it "
        "and would now evict in an arbitrary order"
    )
    assert [item.key for item in store.search(namespace, limit=2, offset=2)] == [
        item.key for item in everything[2:]
    ], (
        "a store search no longer skips `offset` items; "
        "`agent/scratchpad.py::BoundedStoreBackend._evict_past_the_cap` pages a namespace with it "
        "and would loop on its first page"
    )
    assert [item.key for item in everything] == ["c", "a", "d", "b"], (
        "`InMemoryStore` no longer answers a query-less search in insertion order — the "
        "disagreement with `AsyncPostgresStore` that `_evict_past_the_cap` declines to rely on"
    )


def test_the_task_tool_still_closes_over_its_roster_as_subagent_graphs() -> None:
    """The `task` tool still closes over its roster as `subagent_graphs`.

    `SubAgentMiddleware` exposes no accessor for a compiled caller's roster, so
    `tests/test_subagents.py::_helper_of` walks the closure to check what a helper holds. Pins that
    the body is reachable as `coroutine`/`func` and the nonlocal is `subagent_graphs` keyed by name.
    If an accessor appears, delete the walk.
    """
    import inspect

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    from chemclaw.agent.langgraph_agent import build_langgraph_agent
    from chemclaw.agent.profiles import AgentProfile

    caller = build_langgraph_agent(
        model=GenericFakeChatModel(messages=iter([AIMessage(content="")])),
        profile=AgentProfile(name="default"),
    )
    task = caller.nodes["tools"].bound.tools_by_name["task"]
    body = getattr(task, "coroutine", None) or getattr(task, "func", None)
    assert body is not None, (
        "the `task` tool no longer carries its body on `coroutine` or `func`; "
        "tests/test_subagents.py::_helper_of reaches the roster through exactly those two names"
    )
    nonlocals = inspect.getclosurevars(body).nonlocals
    assert "subagent_graphs" in nonlocals, (
        f"the `task` tool no longer closes over `subagent_graphs` (it closes over "
        f"{sorted(nonlocals)}); tests/test_subagents.py::_helper_of reads the compiled roster "
        "from that name, and without it the helper's connector narrowing has no observed basis"
    )
    assert "general-purpose" in nonlocals["subagent_graphs"], (
        "the roster is no longer keyed by subagent name, so `_helper_of` cannot name the one "
        "helper `agent/subagents.py` compiles"
    )


def test_an_oversized_skill_description_is_still_truncated_rather_than_refused() -> None:
    """An oversized skill description is still truncated rather than refused upstream.

    So `agent/skill_manifest.py` turns `MAX_SKILL_NAME_LENGTH` and `MAX_SKILL_DESCRIPTION_LENGTH`
    into pydantic bounds, refusing what upstream would silently truncate. If upstream switched to
    refusing, the skill would silently vanish instead; either is workable, not knowing is not.
    """
    from deepagents.middleware.skills import (
        MAX_SKILL_DESCRIPTION_LENGTH,
        MAX_SKILL_NAME_LENGTH,
        _parse_skill_metadata,
    )

    from chemclaw.agent.skill_manifest import (
        MAX_SKILL_DESCRIPTION_CHARS,
        MAX_SKILL_NAME_CHARS,
    )

    assert (MAX_SKILL_NAME_CHARS, MAX_SKILL_DESCRIPTION_CHARS) == (
        MAX_SKILL_NAME_LENGTH,
        MAX_SKILL_DESCRIPTION_LENGTH,
    ), "this repository's bounds are imported from upstream's, so they cannot disagree"

    over = "d" * (MAX_SKILL_DESCRIPTION_LENGTH + 500)
    parsed = _parse_skill_metadata(
        f"---\nname: over-long\ndescription: {over}\n---\n\nbody\n", "/x/SKILL.md", "over-long"
    )

    assert parsed is not None, (
        "upstream now refuses an over-long description rather than truncating it. A skill over the "
        "limit would vanish from the listing with no error, so `SkillManifest`'s bounds are the "
        "only refusal a person ever sees — keep them, and say so here"
    )
    assert len(parsed["description"]) == MAX_SKILL_DESCRIPTION_LENGTH, (
        "upstream no longer truncates to the spec limit, so the number `SkillManifest` bounds at "
        "is no longer the number the model is served"
    )


def test_pyjwt_still_fetches_its_key_set_through_fetch_data() -> None:
    """PyJWT still fetches its key set through `fetch_data`.

    `api/auth._HttpxJwkClient` overrides that one method so the JWKS fetch does not use `urlopen`,
    which follows an ambient proxy. If upstream fetched elsewhere the auth tests would stay green
    while the proxy posture lapsed, so a subclass overriding only `fetch_data` must serve a whole
    lookup with no network.
    """
    from jwt import PyJWKClient

    assert "fetch_data" in vars(PyJWKClient), (
        "`PyJWKClient.fetch_data` is no longer defined on the class; "
        "api/auth.py::_HttpxJwkClient overrides exactly that name to keep the JWKS fetch off the "
        "ambient proxy"
    )

    calls = 0

    class _Offline(PyJWKClient):
        def fetch_data(self) -> Any:
            nonlocal calls
            calls += 1
            # Typed `Any`: `JWKSetCache.put` is annotated `PyJWKSet` but upstream's `fetch_data`
            # passes the parsed dict, and `api/auth.py` copies the runtime contract.
            data: Any = {
                "keys": [
                    {
                        "kty": "oct",
                        "kid": "kid-a",
                        "use": "sig",
                        "k": "c2VjcmV0LWtleS1tYXRlcmlhbA",
                    }
                ]
            }
            if self.jwk_set_cache is not None:
                self.jwk_set_cache.put(data)
            return data

    client = _Offline("https://tenant.invalid/discovery/v2.0/keys")
    assert [key.key_id for key in client.get_signing_keys()] == ["kid-a"], (
        "a `PyJWKClient` no longer serves its signing keys from `fetch_data`'s return value; "
        "api/auth.py::_HttpxJwkClient's override is the only thing keeping the tenant fetch on "
        "httpx with `trust_env=False`"
    )
    assert calls == 1, (
        f"resolving one key set called `fetch_data` {calls} times; api/auth.py replaces that "
        "method, so a fetch upstream makes by another route is a fetch through `urlopen` and the "
        "ambient proxy"
    )


def test_a_pyjwt_client_still_fills_its_key_set_cache_from_fetch_data() -> None:
    """A PyJWT client still fills its key-set cache from `fetch_data`.

    `get_jwk_set` reads the cache before calling `fetch_data`, so the override must fill it or every
    token validation becomes an outbound request. The second lookup must cost no fetch.
    """
    from jwt import PyJWKClient

    calls = 0

    class _Counting(PyJWKClient):
        def fetch_data(self) -> Any:
            nonlocal calls
            calls += 1
            data: Any = {"keys": [{"kty": "oct", "kid": "kid-a", "use": "sig", "k": "c2VjcmV0"}]}
            # Exactly what `api/auth._HttpxJwkClient.fetch_data` does with its response.
            if self.jwk_set_cache is not None:
                self.jwk_set_cache.put(data)
            return data

    client = _Counting("https://tenant.invalid/discovery/v2.0/keys")
    assert client.jwk_set_cache is not None, (
        "a `PyJWKClient` no longer caches its key set by default; api/auth.py's override writes "
        "`jwk_set_cache` by hand and would be filling something nothing reads"
    )
    client.get_signing_keys()
    client.get_signing_keys()
    assert calls == 1, (
        f"two key-set lookups cost {calls} fetches; `jwk_set_cache.put` in "
        "api/auth.py::_HttpxJwkClient.fetch_data is no longer what makes the second one free, so "
        "every token validation is an outbound request to the tenant"
    )


def test_a_subgraph_compiled_without_a_checkpointer_inherits_its_parents() -> None:
    """A subgraph compiled without a checkpointer inherits its parent's.

    `None` means "inherit" to LangGraph, which is why helpers and the fan-out pass
    `checkpointer=False`. If `None` ever meant "none", those would become silent no-ops.
    """
    import inspect

    from langgraph import types as lg_types
    from langgraph.pregel import Pregel

    # Read the source rather than `__doc__`: `Checkpointer` is a type alias, so the string under it
    # is a module-level literal that never becomes an attribute. Asserting `__doc__` here passed
    # vacuously on the union's own docstring until this comment was written.
    doc = inspect.getsource(lg_types)
    assert "inherits checkpointer from the parent graph" in doc, (
        "upstream no longer documents `None` as inheriting the parent's checkpointer, so the "
        "`checkpointer=False` call sites in this tree may now be saying something else"
    )
    assert "disables checkpointing, even if the parent graph has a checkpointer" in doc, (
        "upstream no longer documents `False` as the opt-out; `agent/langgraph_agent.py` and "
        "`retrieval/fanout.py` both rely on it"
    )
    assert "if self.checkpointer is False" in inspect.getsource(Pregel._defaults), (
        "`Pregel._defaults` no longer short-circuits on `checkpointer is False` before reading "
        "the parent's saver out of the run config, which is where the opt-out is resolved"
    )


def test_every_compiled_graph_in_this_tree_names_its_checkpointer() -> None:
    """Every compiled graph in this tree names its checkpointer.

    A bare `.compile()` silently adopts the caller's saver. Syntactic on purpose: the hazard is the
    default, so every compile site must state its choice.
    """
    import ast
    from pathlib import Path

    # The regular-expression engines, whose `.compile` has nothing to do with graphs. A new entry
    # should be a new engine, not an exception added to make this green.
    _PATTERN_ENGINES = {"re", "regex"}

    root = Path(__file__).resolve().parent.parent / "src"
    bare: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "compile":
                continue
            if isinstance(node.func.value, ast.Name) and node.func.value.id in _PATTERN_ENGINES:
                continue
            if any(keyword.arg == "checkpointer" for keyword in node.keywords):
                continue
            bare.append(f"{path.relative_to(root)}:{node.lineno}")

    assert not bare, (
        f"{len(bare)} graph compile site(s) name no checkpointer: {bare}. `None` means *inherit*, "
        "so a graph invoked inside a turn adopts the chemist's Postgres saver and checkpoints "
        "whatever it carries under its own namespace. Pass `checkpointer=False` for a graph "
        "nothing resumes, or name the saver."
    )


def test_only_the_subagent_middleware_returns_a_command_carrying_the_files_channel() -> None:
    """Only the subagent middleware returns a `Command` carrying the `files` channel.

    `agent/tool_result_size.py` divides the helper file budget by the batch's `task` calls, which is
    sound only while `task` is the sole producer. The scan matches `Command(update=...)` dict
    literals with a spread or a literal `"files"` key and names every site: the subagent producer
    and the filesystem relays that rebuild a wrapped tool's result. A new entry is the thing to look
    at. Write verbs reach `files` via `StateBackend` and a plain `ToolMessage`, outside this
    population.
    """
    import ast
    import importlib
    import inspect
    import pkgutil

    import deepagents

    producers: list[str] = []
    for found in pkgutil.walk_packages(deepagents.__path__, deepagents.__name__ + "."):
        try:
            module = importlib.import_module(found.name)
            source = inspect.getsource(module)
        except Exception:
            # A module that will not import, or has no readable source, cannot be a producer.
            continue
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "Command":
                continue
            for keyword in node.keywords:
                if keyword.arg != "update" or not isinstance(keyword.value, ast.Dict):
                    continue
                # A literal `"files"` key, or a `**spread` of state this module did not filter —
                # the second is how the real producer does it and the first is how a new one would.
                spreads = any(key is None for key in keyword.value.keys)
                literal = any(
                    isinstance(key, ast.Constant) and key.value == "files"
                    for key in keyword.value.keys
                )
                if spreads or literal:
                    producers.append(f"{found.name}:{node.lineno}")

    assert producers == [
        # The two relays: a wrapped tool's own update, rebuilt with new messages.
        "deepagents.middleware.filesystem:3422",
        "deepagents.middleware.filesystem:3463",
        # The producer: `**state_update`, every key the subagent held that is not excluded.
        "deepagents.middleware.subagents:507",
    ], (
        f"the sites handing a caller a `Command` that can carry `files` are {producers}, and "
        "`agent/tool_result_size.py::batch_siblings` divides the helper file budget by the "
        "batch's calls naming ONE tool on the grounds that exactly one of them originates such a "
        "command. A second originator divides by its own count and the two together exceed the "
        "budget. So: read the new or moved site. If it relays a wrapped tool's update, add it "
        "here with that noted; if it builds one of its own, the divisor's argument no longer "
        "holds and `batch_siblings` has to count the union of the producing tools."
    )


def test_rdkits_fragment_catalogue_still_carries_what_this_module_assumes() -> None:
    """RDKit's fragment catalogue still carries what `core/chem.standardize` assumes.

    Whether a hydrate or solvate collapses depends on `FragmentRemover`'s catalogue, and identity
    tests would move with it. Two directions: water must be present, and tetrafluoroborate absent,
    which is why the charge clause in `standardize` leads.
    """
    from rdkit import Chem
    from rdkit.Chem.MolStandardize import rdMolStandardize

    remover = rdMolStandardize.FragmentRemover()

    def stripped_from(organic: str, spectator: str) -> bool:
        """Whether the catalogue removes `spectator` when it sits beside an organic fragment."""
        pair = Chem.MolFromSmiles(f"{organic}.{spectator}")
        assert pair is not None, spectator
        remaining = {
            Chem.MolToSmiles(f) for f in Chem.GetMolFrags(remover.remove(pair), asMols=True)
        }
        return Chem.MolToSmiles(Chem.MolFromSmiles(spectator)) not in remaining

    carried = {"O": "water", "Cl": "hydrogen chloride", "[Na+]": "sodium"}
    for spectator, name in carried.items():
        assert stripped_from("CCN", spectator), (
            f"RDKit's fragment catalogue no longer carries {name}. `core/chem.standardize` "
            "discards a neutral spectator only if this catalogue knows it, so every hydrate and "
            "solvate "
            "silently stops collapsing — re-read that branch before touching anything else"
        )
    omitted = {"F[B-](F)(F)F": "tetrafluoroborate"}
    for spectator, name in omitted.items():
        assert not stripped_from("CC[NH3+]", spectator), (
            f"RDKit's fragment catalogue now carries {name}. That is the omission the charge "
            "clause in `core/chem.standardize` was measured against — it leads because the list "
            "alone "
            "regressed TBTU — so the argument for its order is worth re-reading, not the code"
        )


def test_pyjwt_refetches_an_unknown_kid_at_once_when_its_own_cooldown_is_off() -> None:
    """PyJWT refetches an unknown `kid` at once when its own cooldown is off.

    `api/auth.py` passes `cooldown_duration=0` and owns the cooldown via
    `entra_jwks_refresh_cooldown_seconds`; if upstream stopped honouring `0`, a rotated key would be
    refused for upstream's window.
    """
    from jwt import PyJWKClient
    from jwt.exceptions import PyJWKClientError

    fetches = 0

    class _Offline(PyJWKClient):
        def fetch_data(self) -> Any:
            nonlocal fetches
            fetches += 1
            data: Any = {"keys": [{"kty": "oct", "kid": "kid-a", "k": "c2VjcmV0"}]}
            if self.jwk_set_cache is not None:
                self.jwk_set_cache.put(data)
            return data

    client = _Offline("https://tenant.invalid/keys", cooldown_duration=0)
    client.get_signing_keys()
    for _ in range(2):
        with pytest.raises(PyJWKClientError):
            client.get_signing_key("kid-b")
    assert fetches == 3, (
        f"two lookups of an unknown kid made {fetches - 1} refetches, not 2; PyJWT's own cooldown "
        "is holding despite `cooldown_duration=0`, so api/auth.py's configured cooldown is not "
        "the one deciding rotation latency"
    )


async def _absorbs_a_cancellation(hops: int) -> bool:
    """Whether psycopg's async connect wait swallowed one cancel that landed `hops` turns late.

    A fake connection waits on a socketpair; the connect and a second reader wake in the same loop
    iteration, and the reader cancels the task after `hops` further `call_soon` turns, sweeping the
    window where a `wait_for`-based wait would drop the cancellation.
    """
    import socket

    from psycopg import waiting

    loop = asyncio.get_running_loop()
    conn_r, conn_w = socket.socketpair()
    trig_r, trig_w = socket.socketpair()
    conn_r.setblocking(False)
    trig_r.setblocking(False)

    def connecting() -> Any:
        yield conn_r.fileno(), waiting.WAIT_R
        return "connected"

    task = asyncio.create_task(waiting.wait_conn_async(connecting(), interval=0.1))
    await asyncio.sleep(0)  # the task is now parked on the connection socket
    requested: list[bool] = []

    def cancel_after(remaining: int) -> None:
        if remaining:
            loop.call_soon(cancel_after, remaining - 1)
        else:
            # False once the task has already returned: a cancel that arrived too late to cancel
            # anything is not one the wait absorbed.
            requested.append(task.cancel())

    def fired() -> None:
        loop.remove_reader(trig_r.fileno())
        cancel_after(hops)

    loop.add_reader(trig_r.fileno(), fired)
    conn_w.send(b"x")
    trig_w.send(b"x")
    try:
        await task
    except asyncio.CancelledError:
        return False
    finally:
        loop.remove_reader(trig_r.fileno())
        for sock in (conn_r, conn_w, trig_r, trig_w):
            sock.close()
    return any(requested)


async def test_a_cancelled_connect_is_not_absorbed_by_psycopg() -> None:
    """A `task.cancel()` landing as the connect's socket wakes still cancels the task.

    `asyncio.wait_for` on Python 3.11 can drop a coinciding cancellation; psycopg 3.3.6 awaits a
    plain future instead, and `pyproject.toml` floors it there. A swallowed cancel would let a
    stopped turn wait on the model forever.
    """
    absorbed = {
        hops: sum([await _absorbs_a_cancellation(hops) for _ in range(10)]) for hops in range(6)
    }
    assert not any(absorbed.values()), (
        f"psycopg's async connect wait swallowed cancellations {absorbed} (by how many loop turns "
        "late the cancel landed); a cancelled turn can then run on past a database read — see "
        "pyproject.toml's psycopg floor"
    )


async def test_a_cancelled_pool_checkout_is_not_absorbed() -> None:
    """A task cancelled while it checks a connection out of a pool built with `check=` is cancelled.

    Every pool here sets `check=`. `psycopg-pool` before 3.3.3 caught the `CancelledError` of the
    check, returned the connection and looped to hand out another, so the cancellation vanished and
    the caller (a database read in a middleware, a checkpoint write) went on after the turn was
    stopped. `pyproject.toml` floors the version.
    """
    from psycopg_pool import AsyncConnectionPool

    from chemclaw.core.config import settings
    from tests.pg import cancels_absorbed, migrated_db_or_skip

    await migrated_db_or_skip()
    pool = AsyncConnectionPool(
        settings.postgres_dsn,
        kwargs={"autocommit": True},
        min_size=0,
        max_size=4,
        check=AsyncConnectionPool.check_connection,
        open=False,
    )
    await pool.open()

    async def checkout() -> None:
        async with pool.connection() as conn:
            await conn.execute("SELECT pg_sleep(0.004)")

    try:
        absorbed = await cancels_absorbed(checkout, 500)
    finally:
        await pool.close()
    assert absorbed == 0, (
        f"{absorbed} of 500 cancelled checkouts returned as if nothing had happened; see the "
        "psycopg-pool floor in pyproject.toml"
    )


def test_the_durability_a_turn_asks_for_is_spelt_and_resolved_the_way_the_guard_relies_on() -> None:
    """`astream(durability=...)` exists, and upstream resolves it per graph in `_defaults`.

    `api/graph_stream.TURN_DURABILITY` passes `"sync"`; `core/graph_durability.py` overrides
    `_defaults`, whose checkpointer is the fifth item and whose durability is the last. Upstream
    leaves `"sync"` as asked for a graph with no checkpointer, which is what the guard corrects.
    """
    from typing import get_args

    from langchain_core.runnables.config import ensure_config
    from langgraph.graph import END, START, StateGraph
    from langgraph.pregel import Pregel
    from langgraph.types import Durability
    from typing_extensions import TypedDict

    assert {"sync", "async"} <= set(get_args(Durability))
    assert "durability" in inspect.signature(Pregel.astream).parameters
    assert "durability" in inspect.signature(Pregel.ainvoke).parameters

    class _State(TypedDict):
        n: int

    graph = StateGraph(_State)
    graph.add_node("bump", lambda state: {"n": state["n"] + 1})
    graph.add_edge(START, "bump")
    graph.add_edge("bump", END)
    resolved = graph.compile(checkpointer=False)._defaults(
        ensure_config({"configurable": {"thread_id": "t"}}),
        stream_mode="values",
        print_mode=(),
        output_keys=None,
        interrupt_before=None,
        interrupt_after=None,
        durability="sync",
    )
    assert resolved[4] is None and resolved[-1] == "sync", resolved
