"""The LangGraph engine builds and runs — layer 1's rebuild, phase M1 (D-2026-08-10).

These prove the claims `agent/langgraph_agent.py` makes and nothing it does not yet make. The
engine landed phase by phase behind a config switch, and this file grew with it;
asserting here on a gate M5 has not built would be a test of a plan rather than of the code.

The claims under test, in the order the phases landed:

1. **The graph compiles and completes a tool-using turn** (M1). Driven by a scripted fake model, so
   the assertion is about the wiring — the model is offered Chemclaw's tools, its tool call is
   executed, its result comes back — and not about model behaviour. The same bargain
   `tests/test_agent.py` strikes for the MAF path.
2. **The in-process capability surface transfers unchanged** (M1). `core/tool_registry` holds plain
   callables, so the same functions the MAF agent advertises reach the model here with no adapter
   and no second declaration. If that stopped being true every tool would need a LangGraph twin,
   which is the cost the D-118/R2 seam was built to avoid.
3. **The middleware chain is reached, in order** (M3). Each test drives a real turn and asserts what
   the *model* is handed, because a chain that is attached but inert looks identical from outside to
   one that is absent. The decisions themselves stay pinned in `test_tool_authz.py`,
   `test_repeat_guard.py` and `test_audit.py`, against the functions both engines share.
4. **Skills narrow identically to MAF, and re-narrow every turn** (M4). The gate itself lives in
   `test_skill_backend.py`; what is asserted here is that this engine reaches it.
"""

import asyncio
import re
from typing import Any, cast

import pytest
from agent_framework import SkillsSourceContext
from agent_framework._agents import SupportsAgentRun
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver

from chemclaw.agent.audit import AuditEvent, NullAuditSink
from chemclaw.agent.authz import side_effecting_tools
from chemclaw.agent.chemclaw_agent import (
    _capability_tools,
    available_tool_names,
    harness_tool_names,
    skills_source,
)
from chemclaw.agent.langgraph_agent import build_langgraph_agent, skills_backend
from chemclaw.agent.loop_cap import loop_capped
from chemclaw.agent.plan_gate import plan_approval_refusal, plan_identity
from chemclaw.agent.profiles import AgentProfile, get_profile
from chemclaw.agent.repeat_guard import begin_call_watch, end_call_watch
from chemclaw.agent.skill_backend import REFUSED, SKILL_READ_TOOL
from chemclaw.agent.skill_manifest import declared_tools
from chemclaw.agent.tool_authz import denial_result, dry_run_refusal
from chemclaw.agent.turn_flags import reset_dry_run, set_dry_run
from chemclaw.core.config import settings
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from chemclaw.core.session_context import reset_current_session_id, set_current_session_id
from chemclaw.core.tool_registry import registered_tool_names
from chemclaw.core.turn_signals import _KEY as _SIGNAL_KEY
from chemclaw.core.turn_signals import Signal
from chemclaw.kg.note import NoteError


class _ScriptedModel(GenericFakeChatModel):
    """A model that replays a fixed script, and accepts tool binding without honouring it.

    Subclassed because `create_agent`'s model node calls `.bind_tools(...)` on every request and
    `GenericFakeChatModel.bind_tools` raises `NotImplementedError` — measured, not assumed. Binding
    returns `self` here: the script already contains the tool call under test, so the point of the
    override is that the graph gets a model it can bind, not that the fake reasons about tools.

    What that costs is worth naming. This proves the *loop* — that a tool call is dispatched, run
    and fed back — and cannot prove that the tool schemas Chemclaw hands over are ones a real model
    can call. `test_every_in_process_tool_reaches_the_graph_unchanged` covers the surface, and only
    a live run covers the schemas; M12's re-validation is where that happens.
    """

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Accept the binding and keep replaying the script."""
        return self


def _listed_skills(prompt: str) -> set[str]:
    """The skill names a rendered system prompt advertises.

    Self-checking, because the first version of this helper silently matched nothing and made its
    caller's assertion vacuous: `set() == set() - {gated}` holds, so a fix that deleted every skill
    passed a test written to catch exactly that. A parser that returns empty is now a failure of
    the parser rather than a quiet pass for whatever it was meant to measure.
    """
    names = set(re.findall(r"\*\*([a-z0-9][a-z0-9-]*)\*\*:", prompt))
    assert names, "the skills list could not be parsed from the prompt — the helper is broken"
    return names


class _Recording(_ScriptedModel):
    """A scripted model that also records the system prompt each call was given.

    The skills listing reaches the model in its system prompt, and `create_agent`'s *output* schema
    carries only `messages` — so what the model was told is both the thing under test and the only
    place it is observable.
    """

    prompts: list[str] = []

    def _generate(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
        self.prompts.append("\n".join(str(m.content) for m in messages))
        return super()._generate(messages, *args, **kwargs)


def _advertised(graph: Any) -> set[str]:
    """The tool names a compiled graph offers the model."""
    return {t.name for t in graph.nodes["tools"].bound.tools_by_name.values()}


def _scripted(tool_name: str, tool_args: dict[str, Any]) -> Any:
    """A fake model that calls `tool_name` once and then produces a final answer."""
    return _ScriptedModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[{"name": tool_name, "args": tool_args, "id": "call-1"}],
                ),
                AIMessage(content="done"),
            ]
        )
    )


def test_the_graph_runs_a_tool_call_to_a_final_answer() -> None:
    """A scripted tool call is executed and its result rejoins the conversation.

    `ask_clarifying_question` is the tool under test because it is in-process, side-effect free and
    needs no database, connector or credential — so this exercises the loop rather than a
    capability. What is asserted is the shape of a completed turn: the model's tool call, a
    `ToolMessage` carrying the result, and a final answer after it.

    Driven through `ainvoke`, and that is a property of the surface rather than a test style:
    every Chemclaw capability tool is `async def`, so LangChain wraps each as a `StructuredTool`
    with no sync path and `graph.invoke` raises. The production path is async on both engines, so
    the async-only surface is the honest one to exercise.
    """
    graph = build_langgraph_agent(
        model=_scripted("ask_clarifying_question", {"question": "which solvent?"})
    )

    result = asyncio.run(graph.ainvoke({"messages": [("user", "help")]}))

    messages = result["messages"]
    assert any(isinstance(m, ToolMessage) for m in messages), (
        f"the tool call was never executed; got {[type(m).__name__ for m in messages]}"
    )
    assert messages[-1].content == "done"


def test_every_in_process_tool_reaches_the_graph_unchanged() -> None:
    """The graph advertises exactly the registry, so no tool needs a LangGraph-specific twin.

    Compared against `_capability_tools` rather than against a written list, because that function
    is what the MAF agent advertises: the property worth pinning is that the two engines offer the
    *same* surface, not that this one offers some particular set. A tool that reached one engine
    and not the other would be a capability that appears or vanishes with a config flag.
    """
    graph = build_langgraph_agent(model=_scripted("ask_clarifying_question", {"question": "x"}))

    advertised = _advertised(graph)
    # `read_file` only: the harness (and with it `write_todos`) is off by default, which the
    # test below asserts separately rather than folding into this one.
    assert advertised == {tool.__name__ for tool in _capability_tools()} | {SKILL_READ_TOOL}
    assert advertised == set(registered_tool_names()) | {SKILL_READ_TOOL}


def test_a_profile_narrows_the_graph_surface() -> None:
    """A profile attenuates this engine exactly as it attenuates the MAF one.

    Built from an explicit `AgentProfile` rather than a discovered name, because discovery reads
    `data/profiles/` plus every enabled connector bundle and would make this a test of what the
    repository currently ships. The property is about the dial, not about the shipped set:
    `tool_names` narrows, and narrowing is strict.

    `ask_clarifying_question` is the one in-process tool named here because the shipped
    `property-lookup` profile's other four live in the `calc` connector, and connector tools do not
    reach this engine until M7.
    """
    kept = "ask_clarifying_question"
    full = build_langgraph_agent(model=_scripted(kept, {"question": "x"}))
    narrowed = build_langgraph_agent(
        model=_scripted(kept, {"question": "x"}),
        profile=AgentProfile(name="narrow", tool_names=frozenset({kept})),
    )

    # `read_file` survives every profile, and it must: it carries no authority of its own — every
    # read goes through the backend the three narrowings already bound — so taking it away would
    # not attenuate anything, it would only make the profile's remaining skills unreadable.
    assert _advertised(narrowed) == {kept, SKILL_READ_TOOL}
    assert _advertised(narrowed) < _advertised(full), "a profile must attenuate, never widen"


# --- the middleware chain (M3) -------------------------------------------------------------------
#
# The three tests above would pass with no middleware attached at all, so these are the ones that
# prove the chain. Each drives a real turn through the compiled graph and asserts on what the
# *model* is handed back, because that is where a middleware's behaviour is visible: a chain that
# is attached but inert looks identical from the outside to one that is absent.
#
# What they deliberately do **not** re-assert is the decisions themselves — that a `reader` lacks a
# gated tool, that the dry-run wording says "DRY RUN", that the third identical call is the one
# refused. Those live in `test_tool_authz.py`, `test_repeat_guard.py` and `test_audit.py` against
# the shared functions both engines call, and restating them here would create the second copy the
# extraction exists to prevent. These prove the *wiring*: that this engine reaches those decisions,
# in the right order, and relays what they return.


class _CollectingSink:
    """An `AuditSink` that keeps every event, to assert what reaches the trail."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> None:
        self.events.append(event)


def _run(graph: Any) -> Any:
    """Drive one turn to completion."""
    return asyncio.run(graph.ainvoke({"messages": [("user", "help")]}))


def _run_collecting_signals(graph: Any) -> tuple[Any, list[Any]]:
    """Drive one turn and also collect what its tools announced out of band.

    `ainvoke` above cannot see them: a signal is published to the graph's *custom* stream, so it
    only exists while something is streaming. This is the same pair `api/graph_stream` reads — the
    final state and the custom payloads — asked for directly, without the event translation.
    """

    async def _drive() -> tuple[Any, list[Any]]:
        state: Any = None
        signals: list[Any] = []
        async for mode, payload in graph.astream(
            {"messages": [("user", "help")]}, stream_mode=["values", "custom"]
        ):
            if mode == "values":
                state = payload
            elif isinstance(payload, dict) and isinstance(payload.get(_SIGNAL_KEY), Signal):
                signals.append(payload[_SIGNAL_KEY])
        return state, signals

    return asyncio.run(_drive())


def _tool_result(result: Any) -> str:
    """The content of the single `ToolMessage` in a completed turn."""
    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1, f"expected one tool result, got {len(tool_messages)}"
    return str(tool_messages[0].content)


def test_a_denied_call_reaches_the_model_as_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate blocks the body and the converter hands the model the reason, not a bare failure.

    Two middlewares in one assertion on purpose, because either alone is useless:
    `lg_enforce_tool_authz` raising only helps if `lg_surface_authorization_denials` turns it into
    something the model can act on, and a denial the model reads as a generic tool error is one it
    will retry.
    """
    monkeypatch.setattr(settings, "entra_required", True)
    monkeypatch.setattr(settings, "tool_role_gates", {"ask_clarifying_question": ["chemist"]})
    graph = build_langgraph_agent(
        model=_scripted("ask_clarifying_question", {"question": "x"}),
        audit_sink=NullAuditSink(),
    )

    token = set_current_identity("u-1", frozenset({"reader"}))
    try:
        content = _tool_result(_run(graph))
    finally:
        reset_current_identity(token)

    assert content.startswith("Refused: ")
    assert "ask_clarifying_question" in content


def test_a_dry_run_refuses_a_side_effecting_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """`dry_run` stops a write at the boundary every tool passes through.

    The expected text comes from `dry_run_refusal` itself rather than being spelled out, so this
    cannot become a second copy of the sentence — which is the drift the extraction prevents. The
    tool is picked from the live intersection of the side-effecting set and the registry for the
    same reason: naming one here would pin a set that is meant to grow.
    """
    monkeypatch.setattr(settings, "entra_required", False)
    # `_capability_tools()` first, because it is what registers the generated connector-job and
    # template launchers: most of the side-effecting surface does not exist in the registry until
    # something has assembled the toolset once.
    advertised = {t.__name__ for t in _capability_tools()}
    write_tool = sorted(set(side_effecting_tools()) & advertised)[0]
    graph = build_langgraph_agent(model=_scripted(write_tool, {}), audit_sink=NullAuditSink())

    token = set_dry_run(True)
    try:
        content = _tool_result(_run(graph))
        # Inside the dry run, because `dry_run_refusal` reads the ambient flag — asking it outside
        # returns `None`, which is the correct answer to a different question.
        expected = dry_run_refusal(write_tool)
    finally:
        reset_dry_run(token)

    assert expected is not None
    assert content == denial_result(expected)


def test_a_repeated_call_is_refused_on_this_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """The turn's repeat counter is reached from this engine and its refusal relayed.

    Driven at a limit of 1 so a single turn shows both halves: the first call runs, the second is
    refused. What is asserted is that the graph consulted the counter at all — the threshold's
    value and its wording belong to `test_repeat_guard.py`.
    """
    monkeypatch.setattr(settings, "entra_required", False)
    monkeypatch.setattr(settings, "max_identical_tool_calls", 1)
    args = {"question": "which solvent?"}
    graph = build_langgraph_agent(
        model=_ScriptedModel(
            messages=iter(
                [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {"name": "ask_clarifying_question", "args": args, "id": "call-1"},
                            {"name": "ask_clarifying_question", "args": args, "id": "call-2"},
                        ],
                    ),
                    AIMessage(content="done"),
                ]
            )
        ),
        audit_sink=NullAuditSink(),
    )

    token = begin_call_watch()
    try:
        contents = [str(m.content) for m in _run(graph)["messages"] if isinstance(m, ToolMessage)]
    finally:
        end_call_watch(token)

    assert len(contents) == 2
    assert sum(c.startswith("Error: ") and "already called" in c for c in contents) == 1


def test_the_audit_trail_records_a_call_on_this_engine() -> None:
    """A tool call lands in the GxP trail with its identity, outcome and result.

    `make_langgraph_audit_middleware` shares `_recording` with the MAF middleware, so what this
    pins is that the adapter reaches it with the three fields only the engine knows: the tool's
    name, its arguments, and its result as the `ok` detail.
    """
    sink = _CollectingSink()
    graph = build_langgraph_agent(
        model=_scripted("ask_clarifying_question", {"question": "which solvent?"}),
        actor="tester",
        correlation_id="cid-1",
        audit_sink=sink,
    )

    _run(graph)

    assert len(sink.events) == 1
    event = sink.events[0]
    assert (event.tool, event.outcome) == ("ask_clarifying_question", "ok")
    assert (event.actor, event.correlation_id) == ("tester", "cid-1")
    assert "which solvent?" in event.arguments
    assert event.detail, "the tool's result is the ok detail, and it was empty"


def test_a_failing_tool_is_announced_and_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raising tool reaches the chemist's stream, the trail, and the model — all three.

    `lg_announce_tool_failures` is innermost precisely so it sees the raw exception even when a
    converter turns it into a result, so one turn asserts every layer: the transcript gets a
    failure signal, the trail gets an `error` row, and the model still gets a readable message
    rather than a bare tool error.

    The tool is substituted into the compiled `ToolNode` rather than registered, because
    `core.tool_registry` is process-global module state and a test that writes to it would leak a
    fake tool into every later test's advertised surface.
    """
    monkeypatch.setattr(settings, "entra_required", False)
    sink = _CollectingSink()

    async def _raises() -> str:
        raise NoteError("no note with id 'nope'")

    graph = build_langgraph_agent(model=_scripted("ask_clarifying_question", {}), audit_sink=sink)
    monkeypatch.setitem(
        graph.nodes["tools"].bound.tools_by_name,
        "ask_clarifying_question",
        StructuredTool.from_function(
            coroutine=_raises, name="ask_clarifying_question", description="raises"
        ),
    )

    state, signals = _run_collecting_signals(graph)

    assert _tool_result(state) == "Error: no note with id 'nope'"
    # The failure signal rides the graph's own custom stream — there is no buffer beside the turn
    # to drain any more, so the turn has to be *streamed* for it to exist at all.
    assert [type(s).__name__ for s in signals] == ["ToolFailureSignal"]
    assert [e.outcome for e in sink.events] == ["error"]


# --- skills (M4) ---------------------------------------------------------------------------------


def test_the_skills_middleware_is_attached_and_narrows_by_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A role-gated skill is invisible *and* unreadable to a caller lacking the role.

    Both halves in one assertion, because under this engine they are two different mechanisms and
    only having both is a gate: `SkillsMiddleware` publishes skill paths into the system prompt, so
    hiding a skill from the listing while leaving `/deep-research/SKILL.md` readable would be a
    gate a model walks around by guessing a path it has seen the shape of.

    The gated skill is chosen from the shipped tree rather than named, so this keeps testing the
    real corpus as it grows.
    """
    gated = sorted(declared_tools([*settings.skills_dirs]))[0]
    monkeypatch.setattr(settings, "skill_role_gates", {gated: ["process-chemist"]})
    backend = skills_backend(get_profile(None), _capability_tools())

    denied = set_current_identity("u-1", frozenset({"reader"}))
    try:
        listed = _skill_names(backend)
        refused = backend.read(f"/skills/{gated}/SKILL.md")
    finally:
        reset_current_identity(denied)

    assert gated not in listed
    assert refused.error == REFUSED

    allowed = set_current_identity("u-2", frozenset({"process-chemist"}))
    try:
        assert gated in _skill_names(backend)
        assert backend.read(f"/skills/{gated}/SKILL.md").error is None
    finally:
        reset_current_identity(allowed)


def test_both_engines_narrow_skills_identically(monkeypatch: pytest.MonkeyPatch) -> None:
    """The two engines answer "which skills are visible" the same way, by construction.

    The property that matters is not which set either produces but that they produce the *same*
    one: a skill hidden under `maf` and offered under `langgraph` is not a gate. Asserted against
    `skills_source`'s own chain rather than against a written list, so the shipped corpus and the
    shipped gates are what is compared.
    """
    gated = sorted(declared_tools([*settings.skills_dirs]))[0]
    monkeypatch.setattr(settings, "skill_role_gates", {gated: ["process-chemist"]})
    profile, tools = get_profile(None), _capability_tools()

    token = set_current_identity("u-1", frozenset({"reader"}))
    try:
        maf = {
            skill.frontmatter.name
            for skill in asyncio.run(
                skills_source(profile, tools).get_skills(
                    SkillsSourceContext(agent=cast(SupportsAgentRun, None))
                )
            )
        }
        graph = _skill_names(skills_backend(profile, tools))
    finally:
        reset_current_identity(token)

    assert graph == maf
    assert gated not in maf, "the fixture must actually gate something for this to mean anything"


def _skill_names(backend: Any) -> set[str]:
    """The skill names a backend lists, across every routed skills tree."""
    names: set[str] = set()
    for prefix in getattr(backend, "routes", {"/": backend}):
        for entry in backend.ls(prefix).entries or []:
            path = str(entry.get("path", "")).strip("/")
            if entry.get("is_dir") and path:
                names.add(path.rsplit("/", 1)[-1])
    return names


def test_a_role_change_mid_session_renarrows_the_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cached skill listing does not outlive the roles it was computed for.

    `SkillsMiddleware.before_agent` skips its load whenever `skills_metadata` is already in state —
    "from a prior turn or checkpointed session". That is a fine cache for a fixed skills tree and
    wrong for a narrowed one: with M6's checkpointer, state *does* survive the turn, so a listing
    computed for one caller would be served to the next.

    **Both halves are asserted, and the second half is the one that matters.** The first version of
    this test only checked that the gated skill was gone from turn two, and it passed against a fix
    that removed *every* skill — 28 listed on turn one, 0 on turn two — because an empty list also
    contains no gated skill. A staleness fix that silently deletes the whole skills layer after the
    first turn is worse than the staleness. So turn two must show exactly the ungated remainder.
    """
    gated = sorted(declared_tools([*settings.skills_dirs]))[0]
    monkeypatch.setattr(settings, "skill_role_gates", {gated: ["process-chemist"]})
    monkeypatch.setattr(settings, "entra_required", False)
    model = _Recording(messages=iter([AIMessage(content="done")] * 2))
    prompts = model.prompts
    graph = build_langgraph_agent(
        model=model,
        audit_sink=NullAuditSink(),
        checkpointer=InMemorySaver(),
    )
    session = {"configurable": {"thread_id": "s-1"}}

    holder = set_current_identity("u-1", frozenset({"process-chemist"}))
    try:
        asyncio.run(graph.ainvoke({"messages": [("user", "hi")]}, config=session))
    finally:
        reset_current_identity(holder)
    assert gated in prompts[0], "the fixture must show the gated skill to a role-holder"

    # The same session — same `thread_id`, so the checkpointer restores the state the first turn
    # left, including its cached listing — continued by a caller without the role.
    reader = set_current_identity("u-2", frozenset({"reader"}))
    try:
        asyncio.run(graph.ainvoke({"messages": [("user", "again")]}, config=session))
    finally:
        reset_current_identity(reader)

    assert gated not in prompts[1], "a cached listing outlived the roles it was computed for"
    # The half that catches "fixed it by deleting everything": every other skill is still offered.
    still_listed = _listed_skills(prompts[1])
    assert still_listed == _listed_skills(prompts[0]) - {gated}, (
        f"turn two re-narrowed to {len(still_listed)} skills, expected only {gated} to drop"
    )


# --- the plan gate (M5) --------------------------------------------------------------------------


def test_a_state_changing_call_is_refused_without_an_approved_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-execution GxP gate holds on this engine: propose before you act.

    A fresh session has no plan, so it has no *approved* plan, so the first state-changing call is
    refused — the documented behaviour rather than an edge case. Asserted through a real turn, so
    what is proven is that the gate is reached and its refusal relayed, not that the predicate
    behind it works (that is `test_plan_gate.py`'s job, against the functions both engines share).
    """
    monkeypatch.setattr(settings, "entra_required", False)
    monkeypatch.setattr(settings, "harness_enabled", True)
    monkeypatch.setattr(settings, "harness_autonomy", "plan_only")
    write_tool = sorted(set(side_effecting_tools()) & {t.__name__ for t in _capability_tools()})[0]
    graph = build_langgraph_agent(model=_scripted(write_tool, {}), audit_sink=NullAuditSink())

    session = set_current_session_id("session-1")
    try:
        content = _tool_result(_run(graph))
    finally:
        reset_current_session_id(session)

    assert content == denial_result(plan_approval_refusal(write_tool))
    assert "has not been approved yet" in content


def test_a_read_only_call_is_untouched_by_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate governs state-changing tools only — a question is not a plan step."""
    monkeypatch.setattr(settings, "entra_required", False)
    monkeypatch.setattr(settings, "harness_enabled", True)
    graph = build_langgraph_agent(
        model=_scripted("ask_clarifying_question", {"question": "x"}), audit_sink=NullAuditSink()
    )

    session = set_current_session_id("session-2")
    try:
        content = _tool_result(_run(graph))
    finally:
        reset_current_session_id(session)

    assert "has not been approved yet" not in content


def test_both_engines_hash_a_plan_to_the_same_identity() -> None:
    """An approval is a durable row, so the two engines must agree on what it identifies.

    This is the one place a divergence would be *retroactive*: a hash computed differently would
    silently invalidate every decision a chemist has already recorded, rather than merely behaving
    oddly from now on. `plan_identity` is the single definition; what is pinned here is that the
    LangGraph state shape (`todos[i]["content"]`) feeds it the same items MAF's `todo_plan_items`
    does, and that the empty plan is nobody's plan under either.
    """
    titles = ["gather the evidence", "compute the barrier", "propose the note"]
    todos = [{"content": title, "status": "pending"} for title in titles]

    assert plan_identity([todo["content"] for todo in todos]) == plan_identity(titles)
    assert plan_identity([]) is None, "the empty plan is a constant every session shares"


def test_the_gate_is_absent_when_the_deployment_did_not_ask_for_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No harness means no plan, so imposing an approval-first posture would refuse every write.

    The conditional half of `gate_applies`, and the reason the test above cannot pass vacuously:
    the same tool, the same session, the same turn — refused with the harness on and allowed with
    it off. A gate that fired unconditionally would look identical in the positive test alone.
    """
    monkeypatch.setattr(settings, "entra_required", False)
    monkeypatch.setattr(settings, "harness_enabled", False)
    write_tool = sorted(set(side_effecting_tools()) & {t.__name__ for t in _capability_tools()})[0]
    graph = build_langgraph_agent(model=_scripted(write_tool, {}), audit_sink=NullAuditSink())

    session = set_current_session_id("session-3")
    try:
        content = _tool_result(_run(graph))
    finally:
        reset_current_session_id(session)

    assert "has not been approved yet" not in content


def test_the_harness_adds_its_plan_tool_only_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """`write_todos` is the harness's, so it appears exactly when the harness does.

    Matching MAF, where the classic agent has no todo list at all. Attaching it unconditionally
    would be a difference between the engines while both are live — a harmless-looking one, which
    is the kind that survives review.

    `harness_tool_names()` is a deployment-wide question and therefore stays in
    `available_tool_names()` either way: a validator must recognise `write_todos` whatever this
    process happens to have configured.
    """
    monkeypatch.setattr(settings, "harness_enabled", False)
    assert not harness_tool_names() & _advertised(
        build_langgraph_agent(model=_scripted("ask_clarifying_question", {"question": "x"}))
    )

    monkeypatch.setattr(settings, "harness_enabled", True)
    assert harness_tool_names() <= _advertised(
        build_langgraph_agent(model=_scripted("ask_clarifying_question", {"question": "x"}))
    )
    assert harness_tool_names() <= available_tool_names()


def test_a_capped_loop_is_a_recorded_fact_not_an_inference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`loop_capped` reads the count the cap keeps, so a cap of 1 is visible.

    This is the improvement over MAF rather than a port of it. There the cap lived inside
    `create_harness_agent` with no hook, so its firing had to be inferred from the loop's last stop
    decision — and at `harness_max_loop_iterations == 1` the predicate is never consulted, so a
    capped turn recorded nothing and reported no cap. A limit of exactly 1 is therefore the case
    worth testing: it is the one the inference could not see.
    """
    monkeypatch.setattr(settings, "entra_required", False)
    monkeypatch.setattr(settings, "harness_enabled", True)
    monkeypatch.setattr(settings, "harness_max_loop_iterations", 1)
    graph = build_langgraph_agent(
        model=_ScriptedModel(messages=iter([AIMessage(content="done")] * 3)),
        audit_sink=NullAuditSink(),
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "capped-1"}}

    asyncio.run(graph.ainvoke({"messages": [("user", "go")]}, config=config))

    assert loop_capped(graph.get_state(config).values)
