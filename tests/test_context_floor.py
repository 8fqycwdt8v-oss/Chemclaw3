"""What a turn costs before the user has said anything, pinned so it can only shrink on purpose.

Every tool, prompt block and skill description is paid on every model call. This file is a
ratchet, not a report: it asserts a ceiling per profile, and a change that grows the floor fails
here naming the part that grew, so the cost is visible in the pull request that creates it. A
ceiling with headroom rather than an equality, so it is edited deliberately; lowering one after a
real reduction is the commit that proves the reduction happened.

The basis is what a turn is actually sent, never a re-derivation: tools are read off the compiled
graph's `ToolNode` (`_bound_tools`) with the connector surface bound (`_connector_tools`), the
system message is captured off the wire (`_observed_prefix`), and everything is counted with the
counter compaction uses (`count_tokens_approximately`). `SERVED_ELSEWHERE` names the bundles whose
servers live in `Chemclaw3-mcp`; their cost is measured across a process boundary in that
repository's interpreter and skipped loudly where there is no checkout.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Collection, Iterable, Iterator
from contextlib import contextmanager
from functools import cache
from pathlib import Path
from typing import Any, cast

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_mcp_adapters.tools import load_mcp_tools
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import SecretStr

from chemclaw.agent.audit import NullAuditSink
from chemclaw.agent.chemclaw_agent import _capability_tools, connector_specs, instructions_for
from chemclaw.agent.langgraph_agent import (
    _labelled,
    _skill_dirs,
    _skills_middleware,
    build_langgraph_agent,
    skills_backend,
)
from chemclaw.agent.profile_discovery import load_profiles
from chemclaw.agent.profiles import get_profile, registered_profile_names
from chemclaw.agent.skill_manifest import MAX_SKILL_DESCRIPTION_CHARS
from chemclaw.connectors.registry import discovered, enabled, server_tools_module
from chemclaw.connectors.transport import _allowed
from chemclaw.core.config import Settings, settings
from tests.siblings import (
    SIBLING_SKIP,
    bundles_declared_here,
    fleet_published_bundles,
    sibling_python,
    sibling_root,
)

# Discovered at import, not in a fixture: `registered_profile_names()` parametrises the test below
# at collection time, before any fixture runs, so a fixture load would collect one profile only.
load_profiles()

#: Per-profile ceilings on the static prefix, in tokens.
#:
#: One number with thin headroom over the widest profile (`default`): enough for ordinary drift,
#: less than one large tool, so a new tool cannot be absorbed unnoticed. A per-profile entry may be
#: added when one earns a tighter bound.
#:
#: Raising it is never free: `PREFIX_BOUND` is this ceiling plus `SERVED_ELSEWHERE_ALLOWANCE`, and
#: `core/config/agent.py` derives both compaction defaults from it, so every token of prefix is a
#: token of thread allowance the policy no longer has (`tests/test_compaction.py` holds the
#: allowances). Narrow first (trim docstrings, move model rationale into `#` comments, or narrow a
#: profile's allow-list); raise only for capability, by the measured delta, in the pull request that
#: adds it. The live figure and headroom come from `_report` in the failure message, never from
#: this comment.
CEILINGS: dict[str, int] = {"__default__": 73_450}

#: How much of the floor one tool may be. A schema above this is not expensive, it is *badly
#: shaped* — the fix is pagination, a narrower argument, or splitting a tool that does two things.
MAX_SINGLE_TOOL_TOKENS = 900

#: The tools already over `MAX_SINGLE_TOOL_TOKENS`, with what they cost when last measured.
#:
#: Recorded rather than hidden by a bigger bound, so the *next* one fails. Each takes a domain
#: document as its argument (a BoFire decision space, the note frontmatter contract, a laboratory
#: procedure), which `convert_to_openai_tool` inlines model by model; a typed procedure or
#: `OptimizationProblem` cannot fit under 900 tokens without deleting the schema, which trades
#: constrained generation for context where a malformed call costs most. A `$defs`/`$ref`
#: conversion does not help: it costs tokens rather than saving them, because a field's
#: `description` no longer overrides the nested model's docstring. Adding to this list is not the
#: way past the test; narrowing the shared model is, since it pays back once per tool that uses it.
KNOWN_OVERSIZED: dict[str, int] = {
    # Not ours to narrow: most of this is upstream's own tool description, arriving through a
    # middleware `_apply_excluded_middleware` refuses to let a profile strip, and `_SCOPE_GUIDANCE`
    # is appended rather than forked so it cannot go stale on a bump. The available lever is the
    # profile allow-list.
    "write_todos": 1_372,
    "suggest_next_experiment": 2_951,
    # Grew when `criterion` folded BoFire's `DoEStrategy` in, far cheaper than a second tool that
    # would have carried a second copy of the `OptimizationProblem` schema. No tool taking that
    # schema can clear the cap.
    "generate_screening_design": 2_673,
    "predict_outcome": 2_201,
    "campaign_progress": 2_087,
    "start_optimization_campaign": 1_532,
    # The docstring tells the model a note is written directly and corrected, not reviewed, so the
    # model does not assert an unreviewed note as established fact.
    "record_knowledge_note": 1_238,
    # These two share the `ExperimentDesign` schema, so a change to it moves both.
    "draft_experiment_protocol": 2_738,
    "structure_experiment_request": 1_095,
    "rank_species": 1_141,
    "rank_species_across_solvents": 1_058,
    "compute_reaction_energy": 1_018,
    "survey_bond_strengths": 1_009,
    "refine_ensemble": 984,
    "profile_rotation": 936,
}

#: How far a `KNOWN_OVERSIZED` figure may drift before it has to be re-recorded.
#:
#: Two-sided: a figure that grew is debt nobody re-priced, one that shrank is a narrowing nobody
#: claimed. A band rather than equality so a clearer sentence passes; every entry is over 900
#: tokens, so the band is never narrower than ~45. Growth hiding between bands is still caught by
#: the ceiling.
OVERSIZED_TOLERANCE = 0.05


def _count(text: str | BaseMessage) -> int:
    """Tokens, by the same counter `agent/compaction.py` budgets the thread with.

    A message counts as itself rather than as its text: `count_tokens_approximately` charges a
    per-message overhead, and production (`context_budget.MeasureRequestPrefix`) counts this way.
    """
    from langchain_core.messages.utils import count_tokens_approximately

    message = text if isinstance(text, BaseMessage) else HumanMessage(text)
    return int(count_tokens_approximately([message]))


def _tool_name(tool: Any) -> str:
    """The name a provider sees, which for this repository's tools is the function's."""
    return str(getattr(tool, "name", None) or getattr(tool, "__name__", tool))


def _tool_schema(tool: Any) -> str:
    """One tool exactly as a provider is sent it, via LangChain's own conversion.

    Must be handed the `BaseTool` the graph binds, not a registry callable: `@tool` is identity, so
    a callable converts to a different (smaller) schema than the one that is sent.
    """
    from langchain_core.utils.function_calling import convert_to_openai_tool

    return json.dumps(convert_to_openai_tool(tool))


#: The endpoint-bearing bundles this repository declares but whose servers live in `Chemclaw3-mcp`.
#:
#: This tree holds their `connector.yaml` and none of their code, so their schemas cannot be
#: measured in this interpreter; the allowance test below measures them across a process boundary,
#: and `chemclaw_connector_tool_schema_tokens` reports them at runtime. Named and asserted because a
#: check that quietly shrinks is worse than one that says what it did not look at.
#:
#: Membership is two predicates: declared in both trees *and* bound by silence. Bundles declared
#: here with `default_enabled: false` are charged to whoever enables them, not to this allowance;
#: flipping one to `default_enabled: true` without raising the allowance fails
#: `test_the_bundles_both_repositories_declare_are_the_ones_charged_to_the_allowance`.
SERVED_ELSEWHERE = frozenset({"chem", "rxnpredict", "safety"})

#: What to allow for `SERVED_ELSEWHERE`'s schemas when a *bound* on the whole prefix is needed.
#:
#: A bound rather than a measurement, for the same reason `CEILINGS` is one; the two are added in
#: `PREFIX_BOUND`. It carries headroom because nothing here fails when one of those servers adds a
#: tool. It is asserted against the sibling's own servers by
#: `test_the_allowance_for_the_bundles_this_ratchet_cannot_serve_is_still_a_bound`, which skips with
#: the reason where there is no checkout (and fails in CI, which sets `CHEMCLAW_SIBLINGS_REQUIRED`).
SERVED_ELSEWHERE_ALLOWANCE = 10_250

#: What the fleet's **whole** published `manifests/` directory costs, as a second, looser bound.
#:
#: `SERVED_ELSEWHERE_ALLOWANCE` covers only bundles this repository also declares, which is the
#: right basis for `PREFIX_BOUND`: the Helm chart binds no fleet-only bundle. The e2e lane
#: (`infra/live/e2e-full-stack/up.sh`) mounts the whole directory and enables everything it
#: discovers, so its prefix exceeds `PREFIX_BOUND`; that excess is charged to spend via
#: `agent_context_prefix_basis` rather than absorbed into the defaults
#: (`tests/test_compaction.py::test_binding_every_published_bundle_costs_spend_not_thread`).
#:
#: What this bounds is the growth of the half nothing else here watches: a bundle the fleet adds to
#: `manifests/` lands in this total and nowhere else in this repository. Raise it by the measured
#: delta plus the same ~11% headroom, and state the per-call cost in the pull request.
FLEET_PUBLISHED_ALLOWANCE = 36_700

#: The whole static prefix a shipped `default` turn may cost, as a bound: this file's ceiling plus
#: the allowance for what it cannot see.
#:
#: `core/config/agent.py` derives both compaction thresholds from this, and
#: `tests/test_compaction.py` asserts the relation through `CLEAR_TRIGGER_THREAD_ALLOWANCE` and
#: `BUDGET_THREAD_ALLOWANCE`; read those for the live allowances. It lives here because the ceiling
#: does: two numbers that must move together belong in one place.
PREFIX_BOUND = CEILINGS["__default__"] + SERVED_ELSEWHERE_ALLOWANCE


@cache
def _served_tools(connector: str) -> tuple[Any, ...]:
    """Every tool one bundle's own MCP server advertises, as the `BaseTool`s a turn would bind.

    The bundle's real `FastMCP` server, over a real MCP session, through `load_mcp_tools` (what
    `HeldConnectorSession._hold` calls); only the transport is in-memory, so this is the
    `tools/list` payload a pod would answer with. Cached per connector because these imports are
    heavy and `_floor` runs once per profile per test.
    """
    module = server_tools_module(connector)
    server = getattr(module, "server", None) if module is not None else None
    if server is None:
        return ()

    async def load() -> list[Any]:
        async with create_connected_server_and_client_session(server) as session:
            return list(await load_mcp_tools(session))

    return tuple(asyncio.run(load()))


def _connector_tools(profile: Any) -> list[Any]:
    """This profile's connector surface, narrowed exactly as a turn narrows it.

    `connector_specs(profile)` and `_allowed` are production's own narrowing; only the transport is
    replaced, so an enabled bundle, a new manifest tool or a widened profile lands in the floor
    without this file being taught about it.
    """
    tools: list[Any] = []
    for spec in connector_specs(profile):
        tools.extend(_allowed(list(_served_tools(spec.name)), spec.allowed_tools))
    return tools


def _bound_tools(graph: Any) -> list[Any]:
    """The tools a compiled graph actually binds — every one, as the object it binds.

    Read off the graph rather than re-derived: a registry callable's schema differs from its
    `BaseTool`'s, and middleware tools (filesystem, `task`, `write_todos`) never appear in the
    registry. Any tool source that binds lands here without this file being taught about it.

    The node's copy can differ slightly from what `bind_tools` receives (`FilesystemMiddleware`
    trims `grep`'s description); the node's is larger, the safe direction, and
    `test_the_ratchet_charges_at_least_what_the_model_is_sent` pins it. The three upstream shapes
    read here — node key `"tools"`, `PregelNode.bound`, `ToolNode._tools_by_name` — are pinned in
    `tests/test_upstream_surface.py`.
    """
    return list(graph.nodes["tools"].bound._tools_by_name.values())


#: What the last `_CapturingModel` was sent and bound. Module level because a `BaseChatModel` is a
#: pydantic model, so a class attribute would become a field with a mutable default.
_RECEIVED: list[Any] = []
_BOUND: list[Any] = []


class _CapturingModel(GenericFakeChatModel):
    """A fake model that keeps what it was actually sent, so the prompt comes off the wire.

    `GenericFakeChatModel.bind_tools` raises `NotImplementedError`, which a turn hits before the
    model receives the request.
    """

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Record the surface and stay unbound — this model has no tool-calling path."""
        _BOUND[:] = list(tools)
        return self

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kw: Any) -> Any:
        """Record the request, then answer as the fake model would."""
        _RECEIVED[:] = list(messages)
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kw)


@contextmanager
def _as_a_deployment_runs() -> Iterator[None]:
    """Build under `session_store="postgres"`, which is what every real deployment sets.

    The suite's default `"memory"` makes `personal_skills_available()` false, so `propose_skill`
    would be stripped here while the shipped chart binds and pays for it. A pure predicate: no
    database is opened, because the store only arrives as an argument.
    """
    original = settings.session_store
    settings.session_store = "postgres"
    try:
        yield
    finally:
        settings.session_store = original


def _observed_prefix(profile: Any) -> tuple[SystemMessage, list[Any], list[Any]]:
    """One real model call: the system message as sent, the tools as bound, the node's own list.

    Driven rather than derived: deepagents middlewares write into the same system message, so
    `instructions_for(profile)` plus a skills listing under-counts. A compiled graph is invoked and
    answers with an empty message so the loop ends after one call; nothing is stubbed between the
    profile and the wire.

    Returns:
        The `SystemMessage` the model received, the tools bound to it, and the tools its `ToolNode`
        holds (the same surface seen from two places).
    """
    with _as_a_deployment_runs():
        graph = build_langgraph_agent(
            model=_CapturingModel(messages=iter([AIMessage(content="")])),
            profile=profile,
            audit_sink=NullAuditSink(),
            connectors=_connector_tools(profile),
        )
    bound = _bound_tools(graph)
    _RECEIVED.clear()
    _BOUND.clear()
    graph.invoke({"messages": [HumanMessage("what does this turn cost?")]})
    system = [message for message in _RECEIVED if isinstance(message, SystemMessage)]
    assert system, (
        "the model was called with no system message, so there is no observed prompt to charge — "
        "check that `build_langgraph_agent` still passes its instructions as `system_message`"
    )
    return system[0], list(_BOUND), bound


def _skills_listing(profile: Any, tools: list[Any], available: Collection[str]) -> str:
    """The skills block exactly as `SkillsMiddleware` publishes it into the system prompt.

    Built through the real middleware (`before_agent` on an empty state is a first turn's load path)
    so this file does not re-implement upstream's formatting. `available` is the bound surface,
    passed as `build_langgraph_agent` passes it, because the capability predicate that decides which
    skills are listed reads it.
    """
    labelled = _labelled(_skill_dirs())
    backend = skills_backend(profile, tools, labelled=labelled, available=available)
    middleware = _skills_middleware(backend, labelled, profile)
    loaded = middleware.before_agent({}, None, None) or {}
    return str(middleware._format_skills_list(loaded.get("skills_metadata", [])))


def _maximal_instructions(profile: Any) -> int:
    """The most expensive instruction text any deployment of this profile can be sent.

    Worst case on both axes: every prompt block (`available=None`), and the longer of the two
    alternative audit-trail blocks (the log-only one), so a non-Postgres deployment is not
    under-charged.
    """
    return max(
        _count(instructions_for(profile, durable_trail=durable)) for durable in (True, False)
    )


def _floor(profile_name: str) -> tuple[int, dict[str, int]]:
    """The static prefix for one profile: its total, and the per-part breakdown behind it.

    The total is observed: the `SystemMessage` the model was handed plus every schema the
    `ToolNode` holds. The prompt lines in the breakdown *split* that total: this repository's
    instructions and skills listing (built as `build_langgraph_agent` builds them, the listing
    narrowed by the bound names), plus a remainder that is the deepagents middleware sections. A
    negative remainder means the split is wrong, not the total.

    Instructions are charged at `_maximal_instructions` rather than observed, because this fixture
    binds no `SERVED_ELSEWHERE` bundle and so observes a prompt missing their blocks; the difference
    is its own named line.
    """
    profile = get_profile(profile_name)
    system, _sent, bound = _observed_prefix(profile)
    observed = _count(instructions_for(profile, {_tool_name(tool) for tool in bound}))
    maximal = _maximal_instructions(profile)
    listing = _count(
        _skills_listing(profile, _capability_tools(profile), {_tool_name(tool) for tool in bound})
    )
    parts = {
        "instructions": observed,
        "instructions:blocks-only-a-served-fleet-binds": maximal - observed,
        "skills-listing": listing,
        "prompt:middleware-sections": _count(system) - observed - listing,
    }
    for tool in bound:
        parts[f"tool:{_tool_name(tool)}"] = _count(_tool_schema(tool))
    return sum(parts.values()), parts


def _report(total: int, parts: dict[str, int], ceiling: int) -> str:
    """The failure message, which is the deliverable.

    Whoever trips this needs to see *what they grew*, sorted, without going and measuring it
    themselves — otherwise the ratchet is an obstacle rather than a tool.
    """
    widest = sorted(parts.items(), key=lambda item: -item[1])[:12]
    lines = [f"  {tokens:>6}  {name}" for name, tokens in widest]
    return (
        f"static prefix is {total} tokens against a ceiling of {ceiling}.\n"
        "The twelve widest contributors:\n" + "\n".join(lines) + "\n"
        "Either make one of these narrower, or raise the ceiling in this file and say in the "
        "pull request why the turn is worth more."
    )


@pytest.mark.parametrize("profile_name", sorted(registered_profile_names()))
def test_the_static_prefix_stays_under_its_ceiling(profile_name: str) -> None:
    """Every profile's turn costs what it costs today, and no more, without somebody saying so."""
    total, parts = _floor(profile_name)
    ceiling = CEILINGS.get(profile_name, CEILINGS["__default__"])
    assert total <= ceiling, _report(total, parts, ceiling)


def test_the_ratchet_charges_at_least_what_the_model_is_sent() -> None:
    """The basis may over-count what a turn costs; it may never under-count it.

    A ceiling bounds spend only while the number under it is at least the bill. So the `ToolNode`
    surface (what the graph runs) is compared with what `bind_tools` receives (what the model is
    told about); today the node's copy is slightly larger, and the day the sign flips this file
    starts under-counting.
    """
    system, sent, bound = _observed_prefix(get_profile("default"))
    charged = {_tool_name(tool): _count(_tool_schema(tool)) for tool in bound}
    on_the_wire = {_tool_name(tool): _count(_tool_schema(tool)) for tool in sent}

    uncharged = sorted(set(on_the_wire) - set(charged))
    assert not uncharged, (
        f"{uncharged} are bound to the model and are not in the surface this file charges, so the "
        "ratchet does not bound what a turn costs. `_bound_tools` reads the graph's ToolNode; "
        "whatever now puts a tool on the wire without putting it there has to be counted too."
    )
    prompt = _count(system)
    total_charged = prompt + sum(charged.values())
    total_sent = prompt + sum(on_the_wire.values())
    differing = {
        name: (charged[name], size) for name, size in on_the_wire.items() if charged[name] != size
    }
    assert total_charged >= total_sent, (
        f"the ratchet charges {total_charged} tokens against {total_sent} the model is actually "
        f"sent, so it under-counts by {total_sent - total_charged}. The tools the two bases "
        f"disagree about, as (charged, sent): {differing}. Charge the surface `bind_tools` "
        "receives instead, and move the upstream-surface pins that name `_bound_tools` with it."
    )


def test_no_single_tool_schema_dominates_the_floor() -> None:
    """A tool wider than `MAX_SINGLE_TOOL_TOKENS` is badly shaped, not merely expensive.

    Pagination, filtering and sensible defaults shrink the schema as well as the result; a 900-token
    argument description is a sign the tool is doing two jobs.
    """
    _, parts = _floor("default")
    oversized = {
        name.removeprefix("tool:"): tokens
        for name, tokens in parts.items()
        if name.startswith("tool:") and tokens > MAX_SINGLE_TOOL_TOKENS
    }
    unexpected = {name: tokens for name, tokens in oversized.items() if name not in KNOWN_OVERSIZED}
    assert not unexpected, (
        f"these tool schemas are over {MAX_SINGLE_TOOL_TOKENS} tokens each: {unexpected}. "
        "Narrow the arguments or paginate the result; do not add them to KNOWN_OVERSIZED to make "
        "this pass — that list is debt already taken on, not a place to put more."
    )
    fixed = sorted(set(KNOWN_OVERSIZED) - set(oversized))
    assert not fixed, (
        f"{fixed} no longer exceed {MAX_SINGLE_TOOL_TOKENS} tokens — delete them from "
        "KNOWN_OVERSIZED. A debt list that outlives the debt reads as live state."
    )


def test_the_recorded_cost_of_a_known_oversized_tool_is_still_true() -> None:
    """`KNOWN_OVERSIZED`'s numbers are a measurement, and a measurement nobody repeats is prose.

    The membership test above leaves the figures unasserted; this holds each within
    `OVERSIZED_TOLERANCE`. Whoever trips it re-records the number in the commit that moved it; the
    message carries the value to paste.
    """
    _, parts = _floor("default")
    drifted = {}
    for name, recorded in KNOWN_OVERSIZED.items():
        live = parts.get(f"tool:{name}")
        if live is None:
            continue  # No longer bound at all; the membership test above is what reports that.
        if abs(live - recorded) > recorded * OVERSIZED_TOLERANCE:
            drifted[name] = (recorded, live)
    assert not drifted, (
        "these KNOWN_OVERSIZED figures are no longer what the tool costs, by more than "
        f"{OVERSIZED_TOLERANCE:.0%}: "
        + ", ".join(
            f"{name} recorded {rec} but measures {live} ({live - rec:+})"
            for name, (rec, live) in sorted(drifted.items())
        )
        + ". Re-record them in the same commit that moved them, and say in the pull request what "
        "moved them — a figure nobody re-derives is a claim about the afternoon it was taken."
    )


def _nested_descriptions(node: Any, path: str = "") -> list[tuple[int, str, str]]:
    """Every `description` below a tool's own, as (tokens, path, text).

    The top-level description is the tool's prompt and is excluded. Everything under it is a field
    or model description, and a model's is published once per use — the multiplier the test below
    bounds.
    """
    found: list[tuple[int, str, str]] = []
    if isinstance(node, dict):
        text = node.get("description")
        if isinstance(text, str):
            found.append((_count(text), path, text))
        for key, value in node.items():
            found.extend(_nested_descriptions(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_nested_descriptions(value, f"{path}[{index}]"))
    return found


#: How long one *nested* schema description may be, in tokens.
#:
#: A different bound from `MAX_SINGLE_TOOL_TOKENS`: it asks whether a *model* carries developer
#: prose, which is invisible per tool because it is spread across every tool referencing the model.
#: The widest legitimate field description (`record_knowledge_note`'s `relations`) sits well under
#: it. The remedy is never shortening to fit: keep the model-facing half in the docstring and move
#: the rationale into a `#` comment beside the fields.
MAX_NESTED_DESCRIPTION_TOKENS = 250


def test_no_nested_schema_description_carries_a_design_note() -> None:
    """A model docstring is a prompt once per *use*, so a long one is paid for several times over.

    Pydantic publishes a class docstring as the JSON-schema `description` and
    `convert_to_openai_tool` inlines rather than `$ref`s, so prose written for a module reader is
    sent inside every tool that names the model.
    """
    from langchain_core.utils.function_calling import convert_to_openai_tool

    _, _, bound = _observed_prefix(get_profile("default"))
    over: dict[str, tuple[int, str]] = {}
    for tool in bound:
        function = convert_to_openai_tool(tool)["function"]
        for tokens, path, text in _nested_descriptions(function.get("parameters", {})):
            if tokens > MAX_NESTED_DESCRIPTION_TOKENS:
                over[f"{function['name']}{path}"] = (tokens, text[:80])
    assert bound and not over, (
        f"these schema descriptions are over {MAX_NESTED_DESCRIPTION_TOKENS} tokens: {over}. "
        "A description below a tool's own is a field or a model explanation, and a model's ships "
        "once per tool that references it — move the design rationale into a `#` comment beside "
        "the fields and leave the sentence a caller needs."
    )


def test_the_floor_measures_the_connector_surface_a_turn_actually_binds() -> None:
    """The ratchet's basis includes the endpoint tools a turn binds.

    Derived from the manifests, not transcribed: every tool the enabled, locally served bundles
    declare must be in the bound set. A new bundle, a new manifest tool, or `connectors=` dropped
    from the fixture all fail here instead of shrinking the number silently.
    """
    _, _, bound_tools = _observed_prefix(get_profile("default"))
    bound = {_tool_name(tool) for tool in bound_tools}
    declared = {
        tool
        for manifest in enabled()
        if manifest.endpoint is not None and manifest.name not in SERVED_ELSEWHERE
        for tool in manifest.endpoint.tools
    }
    assert declared, "no in-repo connector declares an endpoint tool; this test now checks nothing"
    assert declared <= bound, (
        f"these declared connector tools are not in the floor's basis: {sorted(declared - bound)}. "
        "The ratchet is measuring a turn with fewer tools than a deployment binds, which is the "
        "exact defect `_observed_prefix`'s `connectors=` argument exists to prevent."
    )


def test_the_bundles_this_floor_cannot_measure_are_exactly_the_ones_it_names() -> None:
    """`SERVED_ELSEWHERE` is a claim about which schemas are out of reach, so it is checked.

    Both directions fail: a bundle whose server moved into this tree would stay excluded from the
    ceiling, and a new bundle served elsewhere would widen the unmeasured half silently.
    """
    endpoint_bundles = {m.name for m in enabled() if m.endpoint is not None}
    unmeasurable = {name for name in endpoint_bundles if not _served_tools(name)}
    assert unmeasurable == SERVED_ELSEWHERE & endpoint_bundles, (
        f"this file can measure the tool schemas of {sorted(endpoint_bundles - unmeasurable)} and "
        f"not of {sorted(unmeasurable)}, but SERVED_ELSEWHERE names {sorted(SERVED_ELSEWHERE)}. "
        "Update it and the ceiling comment's share-of-the-prefix figure in the same commit."
    )


# --------------------------------------------------------------------------------------------
# The half this ratchet cannot serve, measured rather than quoted.
#
# `PREFIX_BOUND` includes `SERVED_ELSEWHERE_ALLOWANCE`, which bounds servers built in
# `Chemclaw3-mcp` and moves on that repository's merges. They are run in the *sibling's*
# interpreter, since their closure is not importable here; `tools/list` crosses the boundary as
# JSON, and conversion and counting happen here through the same functions as every other figure.
# --------------------------------------------------------------------------------------------

#: The program run inside the sibling checkout's interpreter. Written here rather than committed
#: there because it is *this* file's measurement: the sibling owes the fleet a `tools/list`, not a
#: token count in this repository's estimator.
_SIBLING_DUMP = """
import asyncio, importlib, json, sys
from mcp.shared.memory import create_connected_server_and_client_session


async def dump(name):
    server = importlib.import_module("chemclaw_mcp_%s.tools" % name).server
    async with create_connected_server_and_client_session(server) as session:
        listed = await session.list_tools()
        return [t.model_dump(mode="json", exclude_none=True) for t in listed.tools]


print(json.dumps({name: asyncio.run(dump(name)) for name in sys.argv[1:]}))
"""


def _sibling_python() -> tuple[Path | None, str]:
    """The sibling checkout's own interpreter, or `None` and the reason there is not one.

    The search is `infra/live/siblings.sh`'s, asked rather than reimplemented (see
    `tests/siblings.py`). `CHEMCLAW_MCP_REPO` and `CHEMCLAW_MCP_CHECKOUT` both override it.
    """
    return sibling_python("CHEMCLAW_MCP_REPO", "Chemclaw3-mcp")


def test_the_ratchet_finds_the_checkout_the_live_lane_finds() -> None:
    """This file's sibling search and `infra/live/siblings.sh`'s must resolve the same tree.

    Otherwise the allowance tests skip on a machine where the live lane finds the checkout, and that
    skip is indistinguishable from a genuine no-checkout skip. A skip with no checkout is honest; a
    skip beside a live-lane hit is the defect, and the only case this fails on.
    """
    root, live_reason = sibling_root("CHEMCLAW_MCP_REPO", "Chemclaw3-mcp")
    if root is None:
        pytest.skip(f"{SIBLING_SKIP} neither search has one to find: {live_reason}")
    interpreter, ratchet_reason = _sibling_python()
    assert interpreter is not None or ".venv" in ratchet_reason, (
        f"`infra/live/siblings.sh` resolves Chemclaw3-mcp to {root} and this file does not: "
        f"{ratchet_reason}. Two searches for one checkout is what that script exists to have "
        "ended; the consequence here is not a wrong answer but a control that skips on a machine "
        "which could have run it."
    )


def test_the_bundles_both_repositories_declare_are_the_ones_charged_to_the_allowance() -> None:
    """`SERVED_ELSEWHERE` is a claim about the *sibling's* tree, so the sibling's tree answers it.

    The neighbouring completeness test reads only this tree's `connectors/`, so a bundle whose
    manifest lives next door is invisible to it; this half reads the other tree.

    It deliberately does not widen the allowance to the fleet-only bundles (`pyexec`) or to the
    `default_enabled: false` bundles declared here: no chart deployment binds them, so charging them
    to `PREFIX_BOUND` would tighten both compaction defaults everywhere. `FLEET_PUBLISHED_ALLOWANCE`
    bounds them instead. Needs only a checkout, not a built `.venv`.
    """
    root, reason = sibling_root("CHEMCLAW_MCP_REPO", "Chemclaw3-mcp")
    if root is None:
        pytest.skip(
            f"{SIBLING_SKIP} the fleet's published manifests were NOT read: {reason}. Whether "
            f"SERVED_ELSEWHERE ({', '.join(sorted(SERVED_ELSEWHERE))}) is still what both "
            "repositories declare is unchecked in this run."
        )
    published = set(fleet_published_bundles(root))
    bound_by_silence = {m.name for _, m in discovered().values() if m.default_enabled}
    charged = published & set(bundles_declared_here()) & bound_by_silence
    assert charged == SERVED_ELSEWHERE, (
        f"the fleet publishes {sorted(published)}, this repository declares "
        f"{sorted(set(bundles_declared_here()))} and binds {sorted(bound_by_silence)} by silence; "
        f"the names in all three are {sorted(charged)} where SERVED_ELSEWHERE says "
        f"{sorted(SERVED_ELSEWHERE)}. A name in both trees that an empty `connectors_enabled` "
        "still binds is a bundle this repository declares, does not serve, and pays for on every "
        "model call — so its schemas are charged to SERVED_ELSEWHERE_ALLOWANCE and through it to "
        "PREFIX_BOUND and both compaction defaults. A bundle declaring `default_enabled: false` "
        "is declared and not charged; flipping one to true means raising the allowance in the "
        "same commit."
    )


def _one_sibling_dump(interpreter: Path, name: str) -> tuple[list[dict[str, Any]] | None, str]:
    """`tools/list` for one bundle, or `None` and the reason that bundle could not be measured.

    One subprocess per bundle, so one bundle that fails to import cannot take the measurement of the
    others with it.
    """
    import subprocess

    try:
        completed = subprocess.run(
            [str(interpreter), "-c", _SIBLING_DUMP, name],
            capture_output=True,
            text=True,
            # Per bundle, where it used to bound the whole batch — so the helper's own worst case
            # is now this times the bundle count. Inert rather than dangerous: pytest's global
            # `timeout = 180` bites first, and a dump that takes even 30 s is a finding.
            timeout=60,
            cwd=str(interpreter.parents[2]),
        )
    except (OSError, subprocess.SubprocessError) as error:  # pragma: no cover - environment
        return None, f"could not run the sibling's interpreter: {error}"
    if completed.returncode != 0:
        return None, f"its tools/list dump failed: {completed.stderr.strip()[-400:]}"
    try:
        listed = json.loads(completed.stdout)
    except ValueError as error:  # pragma: no cover - environment
        return None, f"its dump was not JSON: {error}"
    tools = listed.get(name)
    if not isinstance(tools, list):  # pragma: no cover - the program above prints one key
        return None, f"the dump printed {sorted(listed)} rather than {name!r}"
    return [dict(tool) for tool in tools], ""


def _sibling_tool_tokens(
    names: Iterable[str],
) -> tuple[dict[str, tuple[int, int]], dict[str, str]]:
    """Per-bundle `(tools, tokens)`, and per-bundle reasons for the ones that went unmeasured.

    Never raises: a missing or broken sibling is a fact about somebody's checkout, not a regression,
    so a failed dump is returned as a reason string and the caller decides between skipping and
    failing. Partitioned per bundle (one spawn each, ~0.7 s) so a check never quietly shrinks
    because a *different* bundle grew a dependency.
    """
    interpreter, reason = _sibling_python()
    wanted = sorted(names)
    if interpreter is None:
        return {}, dict.fromkeys(wanted, reason)

    from langchain_core.tools import StructuredTool

    def _unused(**kwargs: Any) -> None:
        """A body these tools never get: only their published schema is measured."""

    measured: dict[str, tuple[int, int]] = {}
    unmeasured: dict[str, str] = {}
    for name in wanted:
        tools, why = _one_sibling_dump(interpreter, name)
        if tools is None:
            unmeasured[name] = why
            continue
        total = 0
        for tool in tools:
            built = StructuredTool(
                name=str(tool["name"]),
                description=str(tool.get("description") or ""),
                args_schema=tool["inputSchema"],
                func=_unused,
            )
            total += _count(_tool_schema(built))
        measured[name] = (len(tools), total)
    return measured, unmeasured


def test_the_allowance_for_the_bundles_this_ratchet_cannot_serve_is_still_a_bound() -> None:
    """`SERVED_ELSEWHERE_ALLOWANCE` has to be checked against the servers it stands in for.

    Unlike the ceiling, nothing in this repository's history moves this allowance; another
    repository's merges do, and `PREFIX_BOUND` (and both compaction defaults) rest on it. It cannot
    be a hard requirement of this suite, so it skips loudly, naming which bundles went unmeasured
    and why; `tests/conftest.py::_report_sibling_skips` counts the skip.
    """
    measured, unmeasured = _sibling_tool_tokens(SERVED_ELSEWHERE)
    total = sum(tokens for _tools, tokens in measured.values())
    breakdown = ", ".join(
        f"{name} {tokens} / {tools}" for name, (tools, tokens) in sorted(measured.items())
    )
    # Asserted before the skip: a partial total is a lower bound on the real one, so a bundle that
    # grew past the allowance on its own still fails even when another could not be measured.
    assert total <= SERVED_ELSEWHERE_ALLOWANCE, (
        f"the bundles this ratchet cannot serve now cost {total} tokens ({breakdown}) against an "
        f"allowance of {SERVED_ELSEWHERE_ALLOWANCE}. That allowance is half of PREFIX_BOUND "
        f"({PREFIX_BOUND}), which `core/config/agent.py` derives `agent_tool_result_clear_trigger` "
        "and `agent_context_token_budget` from — so raising it is a change to both defaults and to "
        "what every request may cost, not a bump. Raise all three together, or narrow a schema in "
        "Chemclaw3-mcp."
    )
    if unmeasured:
        pytest.skip(
            f"{SIBLING_SKIP} {len(unmeasured)} of the {len(SERVED_ELSEWHERE)} bundles served from "
            "Chemclaw3-mcp were NOT measured — "
            + "; ".join(f"{name}: {why}" for name, why in sorted(unmeasured.items()))
            + f". The {len(measured)} that were cost {total} tokens ({breakdown or 'none'}), "
            f"which is a lower bound. SERVED_ELSEWHERE_ALLOWANCE ({SERVED_ELSEWHERE_ALLOWANCE}) "
            f"and therefore PREFIX_BOUND ({PREFIX_BOUND}) are unchecked in this run, and both "
            "compaction defaults are derived from them."
        )


def test_one_unmeasurable_bundle_does_not_take_the_measurement_of_the_others() -> None:
    """The partition the two tests above rest on, driven rather than read off the helper.

    A name no module answers to reproduces a bundle whose import raises inside the dump, without
    depending on what the sibling's tree happens to be missing today.
    """
    interpreter, reason = _sibling_python()
    if interpreter is None:
        pytest.skip(f"{SIBLING_SKIP} {reason}, so the partition cannot be driven")

    alone, _ = _sibling_tool_tokens(SERVED_ELSEWHERE)
    beside, unmeasured = _sibling_tool_tokens([*SERVED_ELSEWHERE, "notabundle"])

    # The property is "a broken name costs its own measurement and no other", stated against what
    # this checkout could measure rather than against `SERVED_ELSEWHERE`, so a real bundle that is
    # independently unmeasurable neither reds this nor reds it with the wrong diagnosis.
    assert set(beside) == set(alone), (
        f"measuring {sorted(SERVED_ELSEWHERE)} beside a bundle that cannot be imported returned "
        f"{sorted(beside)} where measuring them without it returned {sorted(alone)}: the broken "
        "name took another bundle down with it, which is the all-or-nothing behaviour this "
        "partition replaced"
    )
    assert "notabundle" in unmeasured, "the unimportable bundle was reported as measured"
    assert all(tokens > 0 for _tools, tokens in beside.values()), (
        "a bundle measured at zero tokens is a dump that returned nothing, which would satisfy "
        "the allowance bound by measuring nothing at all"
    )
    assert "notabundle" in unmeasured["notabundle"], (
        "the reason must name the bundle it is about, or a partial skip says less than the "
        "all-or-nothing one it replaced"
    )


def test_the_whole_directory_the_e2e_lane_mounts_is_bounded_too() -> None:
    """`FLEET_PUBLISHED_ALLOWANCE` bounds every bundle the fleet publishes, not only the shared.

    The test above bounds what `PREFIX_BOUND` is built from. This bounds the fleet's whole
    `manifests/` directory, the price of the e2e lane's fleet half, which nothing else here watches.
    It can only skip or fail; it never sets a default here, which is why it is a second constant.
    """
    root, reason = sibling_root("CHEMCLAW_MCP_REPO", "Chemclaw3-mcp")
    published = sorted(fleet_published_bundles(root)) if root is not None else []
    measured, unmeasured = (
        _sibling_tool_tokens(published)
        if published
        # `reason` is empty when the checkout resolved but publishes nothing this can read — a
        # manifests-only clone whose symlinks do not resolve. Saying so beats printing a skip whose
        # reason is a full stop, which is what naming `reason` unconditionally produced.
        else ({}, {"the fleet's published bundles": reason or "the checkout publishes none"})
    )
    # A bundle whose dump returns an empty tool list lands in `measured` at zero tokens and raises
    # no reason, so the allowance below would pass having measured nothing. The sibling test above
    # asserts this for `SERVED_ELSEWHERE`; this is the same guard over the wider set.
    hollow = sorted(name for name, (tools, _tokens) in measured.items() if not tools)
    assert not hollow, (
        f"{hollow} published no tools at all, so the allowance below would be satisfied by a dump "
        "that returned nothing rather than by a bundle that is small"
    )
    total = sum(tokens for _tools, tokens in measured.values())
    breakdown = ", ".join(
        f"{name} {tokens} / {tools}" for name, (tools, tokens) in sorted(measured.items())
    )
    assert total <= FLEET_PUBLISHED_ALLOWANCE, (
        f"the fleet's published manifests now cost {total} tokens ({breakdown}) against an "
        f"allowance of {FLEET_PUBLISHED_ALLOWANCE}. A deployment that points "
        "CHEMCLAW_CONNECTORS_DIR at that directory and enables its bundles pays this on every "
        "model call, on top of what this file's own ceiling bounds."
    )
    # A partial total compared against the *whole* allowance is the second hazard of the old
    # all-or-nothing helper, in the direction that reassures: the assertion above still holds
    # honestly on a lower bound, and this is what stops the run reading it as a verdict.
    if unmeasured:
        pytest.skip(
            f"{SIBLING_SKIP} {len(unmeasured)} of the fleet's published bundles were NOT "
            "measured — "
            + "; ".join(f"{name}: {why}" for name, why in sorted(unmeasured.items()))
            + f". The {len(measured)} that were cost {total} tokens ({breakdown or 'none'}), so "
            f"FLEET_PUBLISHED_ALLOWANCE ({FLEET_PUBLISHED_ALLOWANCE}) is unchecked in this run and "
            "nothing here is evidence about what the fleet's directory costs a deployment."
        )


# --------------------------------------------------------------------------------------------
# The only cache the shipped gateway has: a prefix whose bytes repeat.
#
# An OpenAI-compatible endpoint has no `cache_control`, so prompt caching depends on the serving
# stack recognising a repeated prefix (e.g. vLLM's prefix caching). This repository's half is that
# the bytes are worth caching, and only this module can obtain the request prefix to test it.
# --------------------------------------------------------------------------------------------


def sent_prefix(actor: str, correlation_id: str) -> str:
    """The bytes a model call sends before the conversation, for one actor and correlation id.

    Public because the cross-process test below drives it in a subprocess. Built on
    `_CapturingModel` so it is what the graph assembles, not a second implementation; tool schemas
    are serialised in bind order, because order is part of the bytes.
    """
    from langchain_core.utils.function_calling import convert_to_openai_tool

    _RECEIVED.clear()
    _BOUND.clear()
    with _as_a_deployment_runs():
        graph = build_langgraph_agent(
            model=_CapturingModel(messages=iter([AIMessage(content="done")])),
            profile="default",
            actor=actor,
            correlation_id=correlation_id,
            audit_sink=NullAuditSink(),
            connectors=_connector_tools(get_profile("default")),
        )
    asyncio.run(
        graph.ainvoke(
            {"messages": [HumanMessage(content="hello")]},
            {"configurable": {"thread_id": uuid.uuid4().hex}},
        )
    )
    return json.dumps(
        {
            "tools": [convert_to_openai_tool(tool) for tool in _BOUND],
            "system": [
                message.content for message in _RECEIVED if isinstance(message, SystemMessage)
            ],
        }
    )


def test_the_prefix_two_sessions_are_sent_is_the_same_bytes() -> None:
    """A prefix cache can only hit on bytes that repeat, so the prefix must not carry a turn in it.

    Prefix caching is byte-exact: a timestamp, correlation id, session id or reshuffled tool order
    anywhere in the prefix turns every cache hit into a full prefill, silently. Two turns for
    different actors, correlation ids and threads must be byte-identical.
    """
    first = sent_prefix("alice@example.com", "corr-a")
    second = sent_prefix("bob@example.com", "corr-b")
    at = next(
        (i for i, (a, b) in enumerate(zip(first, second, strict=False)) if a != b),
        min(len(first), len(second)),
    )
    assert first == second, (
        "the prefix two sessions are sent differs, so no server-side prefix cache can hit across "
        f"them and every model call pays a full prefill. First difference at character {at}:\n"
        f"  {first[at : at + 120]!r}\n  {second[at : at + 120]!r}"
    )


#: What a child process prints: its envelope tag, and the prefix hashed with the nonce masked out.
#:
#: The nonce appears twice — the envelope tag and `framing.SYSTEM_SPEECH_MARK` — and both are
#: masked, so anything else per-process still fails.
_CHILD = """
import hashlib, json, sys
sys.path.insert(0, "tests")
from chemclaw.agent.framing import ENVELOPE_TAG, SYSTEM_SPEECH_MARK
from chemclaw.core.config import settings
from test_context_floor import sent_prefix
prefix = sent_prefix("alice@example.com", "corr-a")
prefix = prefix.replace(SYSTEM_SPEECH_MARK, "<MARK>").replace(ENVELOPE_TAG, "<TAG>")
print("RESULT " + json.dumps(
    {"tag": ENVELOPE_TAG, "masked": hashlib.sha256(prefix.encode()).hexdigest()}
))
"""


def _child_prefix(**env: str) -> dict[str, str]:
    """Build the request prefix in a *fresh* process and report its tag and masked hash."""
    import os
    import subprocess
    import sys

    completed = subprocess.run(
        [sys.executable, "-c", _CHILD],
        capture_output=True,
        text=True,
        timeout=900,
        env={**os.environ, **env},
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert completed.returncode == 0, f"child failed:\n{completed.stderr[-3000:]}"
    line = next(
        (ln for ln in completed.stdout.splitlines() if ln.startswith("RESULT ")),
        None,
    )
    assert line is not None, f"child printed no result:\n{completed.stdout[-3000:]}"
    return dict(json.loads(line.removeprefix("RESULT ")))


def test_two_processes_send_the_same_prefix_but_for_the_envelope_nonce() -> None:
    """Across processes the prefix varies in exactly one place, and config decides whether it does.

    `agent/framing.py::_envelope_nonce` falls back to a random token when `framing_envelope_secret`
    is unset, and it is written into the system prompt, so every replica and restart sends a
    different prefix and a byte-keyed prefix cache holds one entry per pod-process. That is a second
    reason (beside correctness) to set `CHEMCLAW_FRAMING_ENVELOPE_SECRET`.

    Both halves are asserted: the masked comparison catches anything *else* per-process (the seeds
    differ deliberately), and the tag comparison catches the nonce losing its configured
    determinism.
    """
    configured = _child_prefix(CHEMCLAW_FRAMING_ENVELOPE_SECRET="probe-secret", PYTHONHASHSEED="0")
    random = _child_prefix(CHEMCLAW_FRAMING_ENVELOPE_SECRET="", PYTHONHASHSEED="999")

    assert configured["masked"] == random["masked"], (
        "two processes send different prefixes for a reason other than the envelope nonce, so no "
        "server-side prefix cache can hit across replicas or across a restart and every model "
        "call on every pod pays a full prefill of the whole static prefix"
    )
    assert configured["tag"] != random["tag"], (
        "the envelope tag did not vary with `framing_envelope_secret` — either the fallback "
        "stopped being per-process or the secret stopped reaching it; `agent/framing.py`"
    )
    from chemclaw.agent.framing import _envelope_nonce

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "chemclaw.agent.framing.settings",
            Settings(  # type: ignore[call-arg]
                _env_file=None, framing_envelope_secret=SecretStr("probe-secret")
            ),
        )
        assert configured["tag"].endswith(_envelope_nonce()), (
            "a configured envelope secret must give every process the same tag; that determinism "
            "is what makes one prefix-cache entry serve the whole fleet"
        )


def test_a_narrowing_profile_is_actually_cheaper_than_the_default() -> None:
    """A profile that narrows the surface but not the bill is not narrowing anything.

    Narrowing a profile is partly for safety and partly for cost; a floor matching the default's
    means it has stopped delivering the second half.
    """
    default_total, _ = _floor("default")
    cheaper = {name: _floor(name)[0] for name in registered_profile_names() if name != "default"}
    not_narrowing = {name: total for name, total in cheaper.items() if total >= default_total}
    assert not not_narrowing, (
        f"the default profile's prefix is {default_total} tokens and these are not below it: "
        f"{not_narrowing}. A profile that advertises fewer tools should cost fewer tokens."
    )


def test_a_helpers_prefix_is_bounded_by_the_one_this_file_already_ratchets() -> None:
    """A helper is a second graph with a second prefix, bounded by its caller's.

    A helper's surface is a strict subset of its caller's
    (`tests/test_subagents.py::test_a_helper_holds_no_tool_its_caller_does_not`) and its prompt is
    the caller's plus `HELPER_BRIEF` minus the harness block, so the caller's ceiling bounds it as
    an inequality rather than a second transcribed number. This fails if a helper prompt outgrows
    what the harness block pays for, or a tool source reaches a helper without reaching its caller.
    """
    import inspect

    profile = get_profile("default")
    connectors = _connector_tools(profile)
    caller = build_langgraph_agent(
        model=_CapturingModel(messages=iter([AIMessage(content="") for _ in range(8)])),
        profile=profile,
        audit_sink=NullAuditSink(),
        connectors=connectors,
    )
    task = caller.nodes["tools"].bound.tools_by_name["task"]
    body = cast(Any, getattr(task, "coroutine", None) or getattr(task, "func", None))
    helper = inspect.getclosurevars(body).nonlocals["subagent_graphs"]["general-purpose"]

    def prefix(graph: Any) -> int:
        _RECEIVED.clear()
        _BOUND.clear()
        graph.invoke({"messages": [HumanMessage("what does this turn cost?")]})
        system = [message for message in _RECEIVED if isinstance(message, SystemMessage)][0]
        content = system.content if isinstance(system.content, str) else str(system.content)
        return _count(content) + sum(_count(_tool_schema(tool)) for tool in _BOUND)

    caller_prefix, helper_prefix = prefix(caller), prefix(helper)
    assert helper_prefix < caller_prefix, (
        f"a helper's static prefix is {helper_prefix} tokens against its caller's {caller_prefix}, "
        "so the ceilings in this file no longer bound it — and a fan-out pays that prefix once per "
        "helper. Either the helper's prompt has outgrown the harness block it is built without, or "
        "something now binds a tool to a helper that it does not bind to its caller"
    )


#: What a chemist's own skills may add to the prefix of every one of their model calls, in tokens.
#:
#: Outside `CEILINGS` because that bounds what this repository ships to everyone, while this tier is
#: per actor, usually empty and authored by a chemist; folding its worst case into `PREFIX_BOUND`
#: would cost every deployment thread allowance. The runtime still charges the real prefix
#: (`agent/context_budget.prefix_tokens`). Derived from `agent_local_skills_max` rows at the maximal
#: description length, then measured, plus small headroom.
LOCAL_SKILLS_ALLOWANCE = 5_700

#: What the organisation's skills may add to the prefix of every model call **every chemist** makes.
#:
#: Separate from `LOCAL_SKILLS_ALLOWANCE` because it bounds a different population: everybody's
#: prefix, and every helper's. Outside `CEILINGS` for the same reason; it is bounded tightly instead
#: (`agent_org_skills_max`). Measured on this tier's own mount (`/org/`), since the listing's
#: scaffolding differs from `/mine/`, plus the same small headroom.
ORG_SKILLS_ALLOWANCE = 3_450


def test_a_chemists_own_skills_cost_no_more_prefix_than_their_cap_allows() -> None:
    """The one part of the prefix a *person* writes, bounded and measured rather than assumed.

    `_observed_prefix` passes no `store=`, so the ratchet itself never mounts a personal tier. This
    fails on a raised row cap, a longer permitted description or a grown listing format — each a
    real change in what a chemist's own judgment costs them on every turn.
    """
    import asyncio

    from langgraph.store.memory import InMemoryStore

    from chemclaw.agent.local_skills import save_local_skill
    from chemclaw.core.config import settings
    from chemclaw.core.identity_context import reset_current_identity, set_current_identity

    profile = get_profile("default")
    connectors = _connector_tools(profile)

    def prefix(store: Any) -> int:
        tokens = set_current_identity("a-chemist", frozenset())
        try:
            graph = build_langgraph_agent(
                model=_CapturingModel(messages=iter([AIMessage(content="")])),
                profile=profile,
                audit_sink=NullAuditSink(),
                connectors=connectors,
                store=store,
            )
            _RECEIVED.clear()
            graph.invoke({"messages": [HumanMessage("what does this turn cost?")]})
            system = [message for message in _RECEIVED if isinstance(message, SystemMessage)][0]
            return _count(
                system.content if isinstance(system.content, str) else str(system.content)
            )
        finally:
            reset_current_identity(tokens)

    # Maximal by the tier's own two bounds: every row the cap permits, each with the longest
    # description deepagents will publish rather than truncate.
    filled = InMemoryStore()
    description = "x" * MAX_SKILL_DESCRIPTION_CHARS
    for index in range(settings.agent_local_skills_max):
        name = f"local-skill-{index:03d}"
        asyncio.run(
            save_local_skill(
                filled,
                "a-chemist",
                name,
                f"---\nname: {name}\ndescription: {description}\n---\n\nbody\n",
            )
        )

    empty, full = prefix(InMemoryStore()), prefix(filled)
    cost = full - empty

    assert cost <= LOCAL_SKILLS_ALLOWANCE, (
        f"a full personal skills tier adds {cost} tokens to every one of that chemist's model "
        f"calls, over the {LOCAL_SKILLS_ALLOWANCE} this file allows it. Either "
        "`agent_local_skills_max` rose, the permitted description grew, or upstream's listing "
        "format did — each is a real change in what a person's own judgment costs them per turn"
    )
    assert cost > 0, (
        "a full personal tier costs nothing, which means it is not reaching the system message at "
        "all — the feature is mounted and invisible to the model, so this asserts nothing"
    )


def test_the_organisations_skills_cost_no_more_prefix_than_the_cap_allows() -> None:
    """The part of the prefix an *administrator* writes, bounded and measured rather than assumed.

    The twin of `test_a_chemists_own_skills_cost_no_more_prefix_than_their_cap_allows`, separate
    because this tier is paid by everybody (see `ORG_SKILLS_ALLOWANCE`). It fails on a raised row
    cap, a longer permitted description or a grown listing format.
    """
    import asyncio

    from langgraph.store.memory import InMemoryStore

    from chemclaw.agent.org_skills import save_org_skill

    profile = get_profile("default")
    connectors = _connector_tools(profile)

    def prefix(store: Any) -> int:
        # No ambient identity: the organisation's tier needs none, and measuring it without one is
        # also what keeps this figure free of the personal tier, which needs an actor to mount.
        with _as_a_deployment_runs():
            graph = build_langgraph_agent(
                model=_CapturingModel(messages=iter([AIMessage(content="")])),
                profile=profile,
                audit_sink=NullAuditSink(),
                connectors=connectors,
                store=store,
            )
        _RECEIVED.clear()
        graph.invoke({"messages": [HumanMessage("what does this turn cost?")]})
        system = [message for message in _RECEIVED if isinstance(message, SystemMessage)][0]
        return _count(system.content if isinstance(system.content, str) else str(system.content))

    # Maximal by the tier's own two bounds: every row the cap permits, each with the longest
    # description deepagents will publish rather than truncate.
    filled = InMemoryStore()
    description = "x" * MAX_SKILL_DESCRIPTION_CHARS
    for index in range(settings.agent_org_skills_max):
        name = f"org-skill-{index:03d}"
        asyncio.run(
            save_org_skill(
                filled,
                name,
                f"---\nname: {name}\ndescription: {description}\n---\n\nbody\n",
                activated_by="an-admin",
            )
        )

    empty, full = prefix(InMemoryStore()), prefix(filled)
    cost = full - empty

    assert cost <= ORG_SKILLS_ALLOWANCE, (
        f"a full organisation skills tier adds {cost} tokens to every model call every chemist in "
        f"this deployment makes, over the {ORG_SKILLS_ALLOWANCE} this file allows it. Either "
        "`agent_org_skills_max` rose, the permitted description grew, or upstream's listing format "
        "did — and this one is paid by everybody, and again by every helper a turn spawns"
    )
    assert cost > 0, (
        "a full organisation tier costs nothing, which means it is not reaching the system message "
        "at all — the tier is mounted and invisible to the model, so this asserts nothing"
    )
