"""The LangGraph engine builds and runs — layer 1's rebuild, phase M1 (D-2026-08-10).

These prove the claims `agent/langgraph_agent.py` makes and nothing it does not yet make. The
engine is selected by `settings.agent_engine`, lands phase by phase, and this file grows with it;
asserting here on middleware, skills or gates that M3–M5 have not built would be a test of a plan
rather than of the code.

The claims under test:

1. **The graph compiles and completes a tool-using turn.** Driven by a scripted fake model rather
   than a live one, so the assertion is about the wiring — the model is offered Chemclaw's tools,
   its tool call is executed, and its result comes back into the conversation — and not about model
   behaviour. That is the same bargain `tests/test_agent.py` strikes for the MAF path.
2. **The in-process capability surface transfers unchanged.** `core/tool_registry` holds plain
   callables, so the same functions the MAF agent advertises reach the model here with no adapter
   and no second declaration. If that ever stops being true, every tool would need a LangGraph
   twin, which is the cost the D-118/R2 seam was built to avoid — so it is worth a test rather
   than an assumption.
"""

import asyncio
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
from chemclaw.agent.chemclaw_agent import _capability_tools, skills_source
from chemclaw.agent.langgraph_agent import build_langgraph_agent, skills_backend
from chemclaw.agent.profiles import AgentProfile, get_profile
from chemclaw.agent.repeat_guard import begin_call_watch, end_call_watch
from chemclaw.agent.skill_backend import REFUSED, SKILL_READ_TOOL
from chemclaw.agent.skill_manifest import declared_tools
from chemclaw.agent.tool_authz import denial_result, dry_run_refusal
from chemclaw.agent.turn_flags import reset_dry_run, set_dry_run
from chemclaw.core.config import settings
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from chemclaw.core.tool_registry import registered_tool_names
from chemclaw.core.turn_signals import begin_turn, drain, end_turn
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

    turn = begin_turn()
    try:
        content = _tool_result(_run(graph))
        signals = drain()
    finally:
        end_turn(turn)

    assert content == "Error: no note with id 'nope'"
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
    computed for one caller would be served to the next. `reload_skills_each_turn` clears it, which
    is what makes this engine match MAF, where the role gate is consulted on every `get_skills`.

    Driven by handing the second turn the first turn's state, which is exactly what a checkpointer
    will do — so this fails today if the hook is removed, rather than only once M6 lands.
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
