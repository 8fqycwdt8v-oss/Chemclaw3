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

import asyncio
from typing import Any, get_type_hints

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


def test_a_todo_still_carries_a_status_and_still_spells_the_live_one_in_progress() -> None:
    """`agent/plan_link.py` reads `todo["status"] == "in_progress"`, and both halves are upstream's.

    The pin beside it covers the channel; this covers the *item*. `plan_link_from_todos` picks the
    step a durable job is stamped with by finding the first todo whose `status` is the literal
    `"in_progress"` — a key and a value `Todo` declares and nothing promises. A rename either side
    fails **silently and permanently**: the generator matches nothing, every job stamps
    `plan_step=""`, and `job_records` fills with runs that name no step while every first-party
    test still passes, because they all construct their own todo dicts rather than asking upstream
    what one looks like. That is the same fail-silent shape the `todos` pin above exists for, one
    level down.
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


def test_the_filesystem_middleware_still_takes_its_permissions_under_a_private_name() -> None:
    """The deny-rules reach enforcement through `_permissions=`, which upstream marks private.

    `create_deep_agent(permissions=…)` hands the rules only to the `FilesystemMiddleware` *it*
    builds, and `agent/langgraph_agent._middleware` substitutes an instance of its own under the
    same `.name` to withhold `execute`/`delete` — a replacement inherits nothing, so that instance
    has to be handed the rules itself, and the only keyword that takes them is underscored.

    That is exactly the coupling this file exists for: a rename upstream is not a build error, it
    is `TypeError: unexpected keyword`, and the version before it was renamed disarmed the rules in
    silence — measured, `_permissions == []` and a scripted `write_file("/outside/evil.md")`
    succeeded while `tests/test_scratchpad.py` asserted the rule list and stayed green. The
    *behaviour* is asserted there, on a compiled graph; what is pinned here is the name.
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
        "the filesystem tool surface changed; agent/langgraph_agent._middleware allow-lists it by "
        "name (via agent/scratchpad.scratchpad_tools) and chemclaw_agent.available_tool_names must "
        "answer for every verb"
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


def test_a_filesystem_permission_still_has_the_two_modes_the_rules_use() -> None:
    """`deny` is what keeps the scratchpad out of the skills tree.

    The permission rules are the second half of the narrowing the allow-list starts: the model may
    write, but not under `/skills/`, because a turn that can rewrite judgment decides what the next
    turn is able to load. `agent/scratchpad.filesystem_permissions` spells exactly two modes —
    `allow` under the two writable roots, then a blanket `deny` behind them.

    **`interrupt` is deliberately not asserted, and this test used to assert it.** Its name said
    "the three modes the rules use" and its docstring said `interrupt` "is what Phase 4's approvals
    ride on" — but no rule in `src/` has ever declared one, and nothing in this tree emits an
    interrupt at all (`docs/planning/BACKLOG.md` carries `graph_stream._from_update` dropping
    `__interrupt__` as *latent*, precisely because there is no producer). A third mode named by a
    test and by nothing else is a claim that a capability is wired up. Pinning what the rules
    actually declare is what makes this file's failure message true when it fires.
    """
    from deepagents import FilesystemPermission

    mode = FilesystemPermission.__annotations__["mode"]
    for expected in ("allow", "deny"):
        assert expected in repr(mode), (
            f"FilesystemPermission no longer supports mode={expected!r}; "
            "agent/scratchpad.filesystem_permissions declares rules in both"
        )


def test_the_interrupt_on_predicate_is_still_synchronous() -> None:
    """The **restart condition** for `HumanInTheLoopMiddleware`, asserted as the absence it is.

    Nothing in `src/` imports any of this: plan approval stays a first-party `wrap_tool_call`
    (`agent/plan_gate.enforce_plan_approval`), declined on four measurements by
    `D-2026-08-15-the-plan-gate-stays-a-refusal-because-an-interrupt-cannot-ask-the-question`. That
    ADR is explicit that the declination is not permanent and names exactly one thing that would
    lift its first finding: **an async `when`**. The gate's own predicates
    (`gate_applies`, `plan_gate._plan_behind`) read the durable approval store, which is `await`.

    So the assertion is that `when` is still declared as a *plain* `Callable[..., bool]` — an
    absence, in the shape `test_the_mcp_adapter_still_calls_a_tool_with_no_read_timeout` uses, so
    that upstream lifting the constraint turns this red and the deferral gets re-read instead of
    outliving its reason. Asserting merely that `when` *exists* would be the opposite: green
    forever, and describing an adoption that never happened.

    The other three findings in that ADR stand regardless, so a red build here is a prompt to
    re-measure, never an instruction to migrate.
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


def test_both_filesystem_write_verbs_still_take_the_path_as_file_path() -> None:
    """The argument name a write gate now reads, and the direction its absence fails in.

    `authz.writes_durable_memory` tells a durable `/memories/` write from a turn-local `/scratch/`
    one by reading `file_path` off the call, because the tool *name* cannot: one verb serves both
    roots. If upstream renamed the parameter, the lookup would miss, every path would read as
    unknown, and — because an unreadable path is treated as durable — the gate would fail *closed*,
    refusing every scratchpad write on a dry run. Loud rather than silent, but still wrong, and
    this is what says so.
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


def test_a_pydantic_tool_return_still_reaches_the_model_as_repr() -> None:
    """`_stringify` prefers JSON and falls back to `str()`, so a `BaseModel` arrives as its repr.

    **This covers the in-process half of the tool surface and only that half**, which the sentence
    here used to overstate: it said "every structured tool in this repository" and named
    `EvidenceSweep`, `NoteView` and `FingerprintSearch`, all three of which are in-process. A
    *connector* result never meets `_stringify` at all — `langchain_mcp_adapters` builds the
    message from the server's own content blocks, and FastMCP has already serialized the model to
    JSON on the far side. Measured through a compiled graph over a live connector, and pinned one
    test down (`test_an_mcp_tool_result_still_arrives_as_content_blocks_carrying_the_servers_json`)
    because `agent/tool_framing.py` rewrites those blocks in place.

    **This is asserted because a fix upstream would silently change every in-process tool's
    payload**, and because one design decision in this tree was already made against the wrong
    belief about it: `Condensation.rows` carried `Field(exclude=True)` and a measurement taken with
    `model_dump_json()`, neither of which described the wire. `agent/protocol_tools` now renders a
    string at the tool boundary rather than depending on this behaviour — the assertion is here so
    that if upstream starts serializing models properly, whoever reads this knows the repr
    assumption is gone and can drop the workarounds it justified rather than leave them
    unexplained.

    **A blanket move to JSON payloads was measured and declined**
    (`D-2026-08-27-a-tool-result-crosses-a-boundary-and-must-say-so`), and the number that would be
    the obvious reason to decline says the opposite: compact JSON of a realistic `EvidenceSweep` is
    0.4–0.8% *shorter* than its repr, so context cost is not the argument. What decided it is that
    a `wrap_tool_call` middleware is handed an already-stringified `ToolMessage`, so the change
    cannot be made in one place — it is an edit to every tool's return, for a payload the model
    reads equally well either way.
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
    """A connector result is a *list* of blocks, not a string, and `agent/tool_framing.py` knows it.

    Two shapes exist on this repository's tool surface and the framing middleware has to rewrite
    both: an in-process tool's `ToolMessage.content` is a `str` (the pin above), and a connector's
    is `list[str | dict]` — one `{"type": "text", "text": …}` block per content item the server
    returned. `_rewritten` frames the `text` and copies every other key, which is what keeps the
    block list, its ids and the artifact beside it intact; if upstream ever joined the blocks into
    a string before the message is built, the list arm would go dead and the rewrite would still
    pass, silently.

    Asserted on the *annotation* rather than by opening a session, because a live connector turn is
    what `tests/test_tool_framing.py` already does and this file is for the shapes upstream does
    not promise. `content` is typed as the union; a narrowing to `str` is the change that matters.
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
    """`connectors/server.py` reaches into `FastMCP`'s tool manager, and this is that coupling.

    The third upstream-internal read in that file, beside the two `ToolManager.call_tool` patches
    the identity binder and the error sanitizer install. `_publish_tool_results` walks
    `server._tool_manager.list_tools()` and reassigns each entry's `fn`, because the publish hook
    routes on the *model* a tool returns and `Tool.fn` is the last point at which the result still
    is one — by the time `call_tool` returns, `convert_result` has turned it into content blocks.

    Four things are read and `mcp` promises none of them: the private `_tool_manager`, that
    `list_tools()` hands back the live `Tool` objects rather than copies, that `fn` is a writable
    attribute, and that `is_async` (decided at registration, dispatched on by
    `call_fn_with_arg_validation`) is what says whether an async wrapper is legal. A copy or a
    frozen model would make the wrapper install cleanly and publish nothing — the
    `audit_events.agent` shape, a hook that reads as installed and is not.
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
    """A tool step reads `structuredContent` out of upstream's artifact, by key, and by shape.

    `template_activities._structured` is what makes `${steps.<id>.result.<field>}` work on a `tool`
    step at all: the content blocks are joined into a string by the time `_mcp_text` is done, so the
    only route to a walkable value is the artifact `langchain_mcp_adapters` attaches. Three things
    it depends on and upstream promises none of: that tools are built with
    `response_format="content_and_artifact"`, that the artifact is a `dict` (an `MCPToolArtifact`
    `TypedDict`, so `.get` works), and that the server's structured payload sits under
    `structured_content`.

    Pinned here rather than trusted because the failure is silent and expensive: if any of the three
    changes, `_structured` returns `None`, the step falls back to the joined string, and four
    shipped templates go back to raising `UnresolvedReference` *after* the launch — which is exactly
    how they shipped in the first place.
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
    """`tests/test_context_floor.py` renders the skills block through upstream's own formatter.

    The alternative was re-deriving the block from `SKILL.md` frontmatter, which would be a second
    implementation of upstream's formatting to keep in step — and the floor has to be the number
    the model is actually sent. So the coupling is deliberate; what was missing is it being
    *recorded*, which is this file's whole job.

    `_format_skills_list` is private, so a rename is a patch-release away and the symptom would be
    an `AttributeError` inside a token-counting helper, several layers from the cause.
    """
    from deepagents.middleware.skills import SkillsMiddleware

    assert hasattr(SkillsMiddleware, "_format_skills_list"), (
        "deepagents' SkillsMiddleware no longer exposes `_format_skills_list`. "
        "tests/test_context_floor.py::_skills_listing calls it to render the skills block the "
        "static-prefix ratchet measures."
    )


def test_before_agent_still_accepts_the_three_argument_call_the_floor_uses() -> None:
    """The arity `test_context_floor.py` invokes `before_agent` with, pinned because arity moved.

    This is the one dependency in this file with previous form. `ReloadingSkillsMiddleware` was
    rewritten onto a single `UntrackedValue` channel specifically to *stop* depending on the number
    of arguments LangChain invokes a hook with
    (`D-2026-08-14-the-coupling-is-the-cost-not-the-line-count`), and production no longer does.
    The floor helper calls the hook directly, so it depends on the signature again — narrowly, in a
    test, and now visibly rather than silently.

    Asserted against the signature rather than by calling it, so this stays a shape assertion and
    does not need a loaded skills backend.
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
    """`agent/compaction.py::_cleared_calls` identifies cleared results by upstream's stamp.

    The alternative — matching the placeholder text — is first-party and would break silently the
    moment the string is reworded, so the metadata key is the dependency worth pinning. If it
    changes, the repeat guard is told nothing was cleared and goes on refusing calls whose answers
    the model no longer holds.
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
    """`core/mcp_session.cancel_on_timeout` cancels by id, and the id comes from `_request_id`.

    The MCP SDK generates a request id *inside* `send_request` and publishes it nowhere, so a
    caller that wants to send `notifications/cancelled` for a request it just gave up on has to
    read the counter beforehand. That is a private attribute and therefore a real coupling: if it
    is renamed, a timed-out call goes back to being abandoned silently while the server runs the
    tool to completion — which is the defect that function exists to fix, restored with every test
    green.
    """
    from mcp.shared.session import BaseSession

    assert "_request_id" in getattr(BaseSession, "__annotations__", {}) or hasattr(
        BaseSession, "_request_id"
    ), (
        "BaseSession no longer declares `_request_id`; core/mcp_session.py::cancel_on_timeout "
        "reads it to learn which request id to cancel when a call outlives its read bound"
    )


def test_a_read_bound_timeout_arrives_as_a_408_mcp_error() -> None:
    """And `cancel_on_timeout` tells that timeout apart from a server error by exactly that code.

    408 is the SDK's own invention around its `anyio.fail_after` — no server sends it — which is
    what makes it usable as the signal. A change here does not fail open in the dangerous
    direction (a missed timeout means no cancellation, i.e. today's behaviour), but it does mean
    the fix silently stops working, so it is pinned rather than trusted.
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
    """Two modules decide "is this a tool result" with `isinstance`, and the subclass is why.

    `agent/audit.returned_failure` and `api/graph_stream._from_update` both test the message with
    `isinstance(x, ToolMessage)` rather than by class name, because `ToolMessageChunk` — what
    upstream emits for a tool result on a *streaming* run — is a real subclass. Narrowing either to
    `type(x) is ToolMessage` passed every test that touches them, and `ToolMessageChunk` appears
    nowhere in this suite: whether a chunk arrives is a property of upstream's streaming mode, not
    of this repository, which is exactly what belongs here.

    Both consequences are silent. The audit trail records a failed connector call as `outcome='ok'`
    with the error text in `detail`, and the stream traces no result at all: no `result_ref`, no
    `tool_result` event, and the grounding check scoring an answer against evidence it never saw.
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
    """A crash mid-turn must not brick the session, and upstream's healer is what prevents it.

    deepagents' `PatchToolCallsMiddleware` answers a dangling call, and this repository
    deliberately declined to write a second healer.

    The checkpointer commits per superstep, so a process killed between the model superstep and
    the tool superstep leaves the thread's newest message an `AIMessage` whose `tool_calls`
    nothing answered — and the provider rejects any thread replaying it ("tool_use ids were found
    without tool_result blocks"), on every later turn, permanently. `create_deep_agent` composes
    `PatchToolCallsMiddleware`, whose `before_agent` answers each dangling call (and each
    `invalid_tool_call`) with a synthesized `ToolMessage`, which is the only reason that crash
    window is survivable. Nothing in `src/` duplicates it, exactly as
    `agent/message_pairing.py`'s docstring says — so if upstream drops or renames the middleware,
    this is the assertion that turns red instead of sessions quietly bricking in production.

    Driven through the *production builder* rather than upstream's class directly, because what
    matters is that the graph a chemist's turn runs on carries the healer — a middleware upstream
    ships but this build accidentally displaced would pass a class-level test.
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
    """The front door's largest unpromised-shape coupling, pinned where the ADR said it was.

    `api/graph_stream.py` unpacks `async for namespace, mode, payload in graph.astream(...,
    stream_mode=list, subgraphs=True)` — an arity upstream never promised, verified against
    `langgraph.pregel.main._output` and kept deliberately when the v3 move was reverted
    (`D-2026-08-14` §6: v3 books 0 tokens for a turn abandoned mid-message, a free budget bypass).
    That ADR says `tests/test_upstream_surface.py` keeps the coupling from rotting silently, and
    until this test the file pinned only the v3 restart-condition seam — an upstream arity change
    would have surfaced as a bare unpack `ValueError` deep in an unrelated stream test rather than
    as a named diagnostic. Driven on a compiled graph because the arity is a property of the
    *running* pregel loop, not of any signature.
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


def test_the_after_model_call_cap_is_still_the_one_upstream_shape_this_repo_declines() -> None:
    """The runaway cap is first-party, and `CLAUDE.md` must not say otherwise.

    `D-2026-08-15-an-after-model-counter-is-a-counter-that-can-be-skipped` reverted the move onto
    `ModelCallLimitMiddleware` and left a general rule behind: it is unsafe to compose with any
    middleware that jumps from `after_model`, because upstream *increments* there — measured, a cap
    of 2 ran 4 model calls. `CLAUDE.md` carried the pre-revert sentence sixty lines above the
    paragraph describing the revert, so the primary document asserted a mechanism it then called
    unsafe, and pointed every reader at the composition the ADR forbids.

    Three assertions, because the claim has three halves and each can go stale on its own:

    1. **Upstream still counts in `after_model`.** If it ever moves the increment into
       `before_model`, the reason for the revert is gone and this goes red so the decision gets
       taken again rather than inherited — the absence pattern this file's header describes.
    2. **Nothing in `src/` imports it.** A subclass needs an import, so import-absence is the whole
       check.
    3. **`CLAUDE.md`'s cap sentence names `agent/loop_cap.py`**, and does not name the upstream
       class before it. Prose is the half that was wrong; leaving it ungated is how it got wrong.
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

    claim = (root / "CLAUDE.md").read_text(encoding="utf-8").split("the runaway cap is", 1)
    assert len(claim) == 2, "CLAUDE.md no longer describes the runaway cap at all"
    mechanism = claim[1].split("(`agent/loop_cap.py`)", 1)
    assert len(mechanism) == 2, "CLAUDE.md's runaway-cap sentence no longer points at loop_cap.py"
    assert "ModelCallLimitMiddleware" not in mechanism[0], (
        "CLAUDE.md describes the runaway cap as upstream's middleware; it is a first-party "
        "`before_model` counter, and the same file's M14 paragraph calls that composition unsafe"
    )
    assert "before_model" in mechanism[0], (
        "CLAUDE.md's runaway-cap sentence no longer names the hook the cap actually counts in"
    )


def test_tool_node_still_stores_a_prebuilt_tool_object_instead_of_rebuilding_it() -> None:
    """`ToolNode` converts a plain callable and passes a `BaseTool` straight through.

    `agent/tool_schema.py` reads this shape. It derives each in-process capability tool's
    `BaseTool` once per process and hands `build_langgraph_agent`'s tool list the result, because
    `ToolNode.__init__` otherwise calls `langchain_core.tools.tool` on every plain callable it is
    given — a pydantic model built from the signature and docstring, on every per-turn compile.
    Profiled at 108 conversions per build and about four fifths of the compile;
    `tests/test_langgraph_connectors.py` carries the before/after.

    Two halves, and the second is the one that makes the first worth anything: a callable is
    converted, and a `BaseTool` is stored **as the same object**. If upstream ever started copying
    or re-deriving what it is handed, the cache would still be correct and would silently stop
    being a saving — which is the failure mode this file exists to turn red.
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
