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
    """Why `connectors/registry.py` has to bound a tool call at the *session*, not the call.

    `langchain_mcp_adapters` calls `session.call_tool` with no `read_timeout_seconds` of its own, so
    a connector that never answers blocks the turn forever — measured: a 4 s tool still blocked at
    25 s. The bound now exists, and it is the only shape available: `_session_kwargs` sets the
    `ClientSession`'s default so `mcp.shared.session.send_request` has a deadline to expire, because
    the adapter offers no per-call one to pass. **That is the absence this pins, and it is a
    different mechanism from the session default — so the assertion is unchanged now that the row it
    was written beside is closed.** The day upstream names a call timeout, this test fails and the
    session-wide default should be re-examined: one number per connection is a coarser instrument
    than one per call, and it was chosen only because it was the one on offer. A test that pins an
    upstream absence is how a workaround gets revisited instead of outliving its reason.
    """
    import inspect

    from langchain_mcp_adapters import tools

    source = inspect.getsource(tools)
    assert "read_timeout_seconds" not in source, (
        "langchain-mcp-adapters now names a call timeout — re-examine the session-wide default "
        "`_session_kwargs` sets in `connectors/registry.py`, chosen for want of a per-call one"
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


def test_create_deep_agent_still_takes_the_parameters_the_harness_is_assembled_from() -> None:
    """`build_langgraph_agent` passes every one of these by keyword.

    The harness swap (D-2026-08-15) stopped hand-assembling a middleware list and handed the
    assembly to `create_deep_agent`, which means this signature *is* the seam. A parameter that
    disappears upstream is a build error here rather than a silent behaviour change, but a
    parameter that is silently *renamed* would be neither — `**kwargs` does not exist on this
    function today and this assertion is what notices if it ever does.
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


def test_the_filesystem_tool_surface_is_still_the_eight_names_the_gate_answers_for() -> None:
    """Every filesystem verb has to be answered for, so a new one must not arrive quietly.

    `FilesystemMiddleware` is the scratchpad, and each name it registers is gated by
    `tool_role_gates`, validated by `make prose-validate` and listed by
    `chemclaw_agent.available_tool_names`. Upstream adding a ninth verb would hand the model a
    capability no gate names — which is exactly what happened once already when deepagents 0.7 added
    `delete` and `agent/skill_backend.py` had no refusal for it
    (`docs/decisions/D-2026-08-12-the-cap-was-right-and-what-it-was-holding-back.md`).

    Asserted as equality rather than containment for that reason: a superset is the failure mode.
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
        "the filesystem tool surface changed; agent/langgraph_agent._filesystem_middleware "
        "allow-lists it by name and chemclaw_agent.available_tool_names must answer for every verb"
    )


def test_the_filesystem_middleware_still_offloads_oversized_tool_results() -> None:
    """The capability the scratchpad exists for, and the reason `compaction.py` shrank.

    `FilesystemMiddleware.wrap_tool_call` writes a result past `tool_token_limit_before_evict` to
    the backend and leaves the model a path plus a preview. That is strictly better than
    `ClearToolUsesEdit`'s placeholder — the evidence stays *readable* instead of being dropped —
    and it is what makes a multi-source research turn possible at all. If the parameter goes, the
    offloading goes with it and the context policy is back to discarding.
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


def test_a_filesystem_permission_still_has_the_three_modes_the_rules_use() -> None:
    """`deny` is what keeps the scratchpad out of the skills tree.

    The permission rules are the second half of the narrowing the allow-list starts: the model may
    write, but not under `/skills/`, because a turn that can rewrite judgment decides what the next
    turn is able to load. `interrupt` is the third mode and is what Phase 4's approvals ride on.
    """
    from deepagents import FilesystemPermission

    mode = FilesystemPermission.__annotations__["mode"]
    for expected in ("allow", "deny", "interrupt"):
        assert expected in repr(mode), (
            f"FilesystemPermission no longer supports mode={expected!r}; "
            "agent/langgraph_agent._filesystem_permissions declares rules in all three"
        )


def test_the_interrupt_config_still_carries_a_when_predicate() -> None:
    """The shape the plan gate became.

    `enforce_plan_approval` was a first-party `wrap_tool_call` that asked "does this call need an
    approved plan, and is the plan behind it the one that was approved". `InterruptOnConfig.when`
    is that question as an upstream predicate, which is why the gate could stop being first-party.
    Lose it and the approval either fires on every tool or on none.
    """
    from langchain.agents.middleware.human_in_the_loop import InterruptOnConfig

    assert "when" in InterruptOnConfig.__annotations__, (
        "InterruptOnConfig lost its `when` predicate; the plan approval gate is expressed as one"
    )


def test_the_rubric_middleware_still_bounds_its_revision_loop() -> None:
    """An unbounded critic is a runaway turn, and the bound has to be upstream's.

    `RubricMiddleware` re-enters the *same* run to revise, so its iterations are counted by
    `CappedModelCallLimit`'s `run_limit` — the cap and the critic share a budget. `max_iterations`
    is what keeps the critic's share of it finite; without it the two guards fight and the cap wins
    by truncating an answer mid-revision.
    """
    import inspect

    from deepagents import RubricMiddleware

    assert "max_iterations" in inspect.signature(RubricMiddleware.__init__).parameters, (
        "RubricMiddleware no longer bounds its revision loop; agent/verifier.py delegates to it "
        "and agent/loop_cap.py counts its iterations against the turn"
    )


def test_the_store_backend_still_takes_a_namespace_factory() -> None:
    """The namespace is the erasure key, which is why this parameter is load-bearing.

    `D-2026-08-10-basestore-is-not-where-this-systems-memory-lives` rejected `BaseStore` partly
    because `store` has no actor column, so `tests/test_leaver.py`'s derived check would report a
    departing person's memories as absent while they remained — a safety net returning a false
    green. The answer is to put the actor *in the namespace*, so erasure is a `list_namespaces` and
    a `delete`. That only works while the namespace is ours to choose.
    """
    import inspect

    from deepagents.backends import StoreBackend

    assert "namespace" in inspect.signature(StoreBackend.__init__).parameters, (
        "StoreBackend no longer takes a namespace factory; agent/scratchpad.py keys it by actor "
        "oid so that erasure can find a departing person's memories"
    )


def test_a_checkpointer_can_delete_a_thread_without_naming_its_tables() -> None:
    """What replaced the hand-maintained table tuple in retention and erasure.

    `CHECKPOINT_TABLES` was the sole route to the checkpoint rows for both `durable/retention.py`
    and `agent/leaver.py`, and it is hand-maintained against tables `AsyncPostgresSaver.setup()`
    creates — so a library upgrade adding a fourth table was invisible to every gate that reads it.
    """
    from langgraph.checkpoint.base import BaseCheckpointSaver

    assert hasattr(BaseCheckpointSaver, "adelete_thread"), (
        "BaseCheckpointSaver lost adelete_thread; durable/retention.py and agent/leaver.py would "
        "have to go back to naming the checkpoint tables by hand"
    )


def test_the_store_still_names_its_two_version_ledgers_the_way_the_grant_file_does() -> None:
    """The two table names in this database that are derivable from nothing.

    Every other table LangGraph creates appears in a `CREATE TABLE IF NOT EXISTS` inside one of the
    `MIGRATIONS` lists, so `tests/test_database_privileges.py` reads it off the installed package.
    These two do not: `AsyncPostgresStore.setup()` passes them to `_get_version(cur, table=...)` and
    interpolates them into a `CREATE TABLE` template, so the names exist only as string literals in
    upstream's source. The grant file has to spell them, which makes a rename a silent un-granting —
    the store would create `store_schema_version`, the reconciliation would grant nothing on it, and
    the failure would be an `InsufficientPrivilege` on a schema step during a rolling deploy.

    Asserted against the source text rather than a symbol because upstream exports neither.
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
    """The splice rule the whole composition rests on, and the reason `execute` stays off.

    `create_deep_agent` composes a `FilesystemMiddleware` registering all eight verbs whether or not
    a caller wants them. This repository withholds `execute` (a shell — deepagents 0.7 ships one
    concrete sandbox, declined here on egress grounds, and `LocalShellBackend` is documented as
    unrestricted) and `delete` (D-2026-08-12's argument) by passing its own instance under the
    *same* `.name`, which `_apply_custom_middleware` swaps in place of upstream's.

    A private function, asserted here for exactly that reason. If the rule changed to "append", the
    two middlewares would coexist and upstream's would put the withheld verbs back — with every
    other test still green, because the narrowed one is still present and still narrow.

    The alternative mechanism, `HarnessProfile.excluded_tools`, is *not* used and this is why: a
    profile is resolved by the model's self-reported `provider:identifier` and is silently skipped
    on a key miss (measured during the swap — a registration under `"anthropic"` never reached a
    model whose resolved provider differed, logging one warning). A narrowing that fails open on a
    model swap is not a narrowing.
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
    """Why `agent/subagents.py` exists at all, pinned as the constraint rather than the workaround.

    `SubAgentMiddleware` is in `create_deep_agent`'s required-scaffolding set, and
    `_apply_excluded_middleware` *raises* rather than let a `HarnessProfile` strip it. So the `task`
    tool ships on every agent this deployment builds and "no subagents" was never on the menu — the
    only decision available is whether what `task` reaches is governed.

    Asserted as a *presence* in the required set rather than by provoking the raise, because the
    property that matters is the membership: if this middleware ever became strippable, the honest
    move would be to reconsider whether the roster should exist, not to keep the workaround.
    """
    from deepagents.graph import _REQUIRED_MIDDLEWARE_NAMES

    assert "SubAgentMiddleware" in _REQUIRED_MIDDLEWARE_NAMES


def test_the_general_purpose_subagent_is_still_displaced_by_claiming_its_name() -> None:
    """The suppression this repository relies on, pinned as the exact string it turns on.

    Left alone, `create_deep_agent` inserts a `general-purpose` subagent holding every tool the
    parent holds and none of this repository's middleware. It skips that insertion when a supplied
    spec already claims `GENERAL_PURPOSE_SUBAGENT["name"]` — a plain comparison with nothing to
    mismatch, unlike the harness-profile route. `agent/subagents.py` hard-codes the winning string,
    so an upstream rename would silently restore the ungoverned subagent beside ours.
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
        ("deepagents", "RubricMiddleware", "the in-loop answer critic"),
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
    """Every deepagents name `src/` spells, asserted rather than assumed, on a 0.x dependency.

    This docstring used to say `create_deep_agent` was deliberately not called. It is called now
    (`D-2026-08-15-the-harness-is-adopted-whole-or-its-defaults-are-inherited-silently`), and the
    list below is no longer "middleware picked one at a time" — half of these are reached *through*
    `create_deep_agent` rather than composed beside it. What has not changed is the reason to assert
    them: a 0.x minor can move a symbol between modules without a deprecation, and an import that
    breaks at construction time breaks every turn at once.
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
