"""The LangGraph engine builds and runs a turn.

Claims under test:

1. The graph compiles and completes a tool-using turn, driven by a scripted fake model, so the
   assertion is about wiring, not model behaviour.
2. The in-process capability surface reaches the graph unchanged: `core/tool_registry` holds plain
   callables, so no tool needs a LangGraph twin.
3. The middleware chain is reached, in order: each test asserts what the model is handed, since an
   inert chain looks like an absent one. The decisions are pinned in `test_tool_authz.py`,
   `test_repeat_guard.py` and `test_audit.py`.
4. Skills are narrowed, and re-narrowed every turn; the gate itself is in `test_skill_backend.py`.
"""

import asyncio
import re
from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver

from chemclaw.agent.audit import AuditEvent, NullAuditSink
from chemclaw.agent.authz import side_effecting_tools
from chemclaw.agent.chemclaw_agent import (
    _advertised_names,
    _capability_tools,
    available_tool_names,
    harness_tool_names,
    subagent_tool_names,
    withheld_tool_names,
)
from chemclaw.agent.framing import ENVELOPE_TAG, SYSTEM_SPEECH_MARK
from chemclaw.agent.langgraph_agent import _labelled, build_langgraph_agent, skills_backend
from chemclaw.agent.local_skills import PERSONAL_TIER_TOOLS, personal_skills_available
from chemclaw.agent.loop_cap import loop_capped
from chemclaw.agent.plan_gate import PLAN_GATE_REASON, harness_enabled_for, plan_approval_refusal
from chemclaw.agent.profile_discovery import load_profiles
from chemclaw.agent.profiles import AgentProfile, get_profile
from chemclaw.agent.repeat_guard import begin_call_watch, end_call_watch
from chemclaw.agent.scratchpad import MEMORY_ROOT, SCRATCH_ROOT, scratchpad_tools
from chemclaw.agent.skill_access import skill_permits
from chemclaw.agent.skill_backend import REFUSED
from chemclaw.agent.skill_manifest import declared_tools, required_tools
from chemclaw.agent.state import turn_config, turn_input
from chemclaw.agent.tool_authz import denial_result, dry_run_refusal
from chemclaw.api.events import ToolFailedEvent
from chemclaw.api.graph_stream import _signal_event, graph_events
from chemclaw.api.runner_trace import ToolCallTrace
from chemclaw.connectors.registry import skills_dirs as _bundle_skills_dirs
from chemclaw.core.config import settings
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from chemclaw.core.session_context import reset_current_session_id, set_current_session_id
from chemclaw.core.tool_registry import registered_tool_names
from chemclaw.core.turn_flags import reset_dry_run, set_dry_run
from chemclaw.core.turn_signals import _KEY as _SIGNAL_KEY
from chemclaw.core.turn_signals import Signal, ToolFailureSignal
from chemclaw.kg.note import NoteError
from tests.fakes import ScriptedModel
from tests.fakes_langgraph import ScriptedChatModel


def _listed_skills(prompt: str) -> set[str]:
    """The skill names a rendered system prompt advertises.

    Raises if it parses nothing, so a broken parser cannot make a caller's assertion vacuous.
    """
    names = set(re.findall(r"\*\*([a-z0-9][a-z0-9-]*)\*\*:", prompt))
    assert names, "the skills list could not be parsed from the prompt — the helper is broken"
    return names


class _Recording(ScriptedModel):
    """A scripted model that also records the system prompt each call was given.

    The skills listing reaches the model only through its system prompt, which is the only place it
    is observable.
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
    return ScriptedModel(
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

    `ask_clarifying_question` is in-process and side-effect free, so this exercises the loop. Driven
    through `ainvoke` because every capability tool is `async def` with no sync path.
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

    Compared against `_capability_tools`, the function the surface is built from, rather than a
    written list.
    """
    graph = build_langgraph_agent(model=_scripted("ask_clarifying_question", {"question": "x"}))

    advertised = _advertised(graph)
    # The registry plus what the backend, the subagent middleware and the harness bring. `task` is
    # unconditional (`SubAgentMiddleware` is required by `create_deep_agent`); `write_todos` depends
    # on the harness and is unioned through the same predicate the graph builds on.
    ambient = set(scratchpad_tools()) | subagent_tool_names()
    if harness_enabled_for(get_profile("default")):
        ambient |= harness_tool_names()
    # Minus what this deployment cannot deliver: `propose_skill` needs the personal tier that
    # `personal_skills_available()` gates, so it is withheld through the same predicate the graph
    # uses.
    withheld = set() if personal_skills_available() else set(PERSONAL_TIER_TOOLS)
    # The registry only grows, so a template launcher an earlier build in this process registered
    # under another configuration can be held while this deployment withholds it.
    withheld |= withheld_tool_names()
    assert advertised == ({tool.__name__ for tool in _capability_tools()} - withheld) | ambient
    assert advertised == (set(registered_tool_names()) - withheld) | ambient


def test_a_profile_narrows_the_graph_surface() -> None:
    """A profile narrows the graph surface.

    Built from an explicit `AgentProfile` rather than a discovered one, so the property is about the
    dial, not the shipped set: `tool_names` narrows, strictly. `ask_clarifying_question` is the
    in-process tool named because the shipped profile's other tools live in connectors.
    """
    kept = "ask_clarifying_question"
    full = build_langgraph_agent(model=_scripted(kept, {"question": "x"}))
    narrowed = build_langgraph_agent(
        model=_scripted(kept, {"question": "x"}),
        profile=AgentProfile(name="narrow", tool_names=frozenset({kept})),
    )

    # `read_file`, `task` and `write_todos` survive every profile because none confers authority of
    # its
    # own: reads go through the already-narrowed backend, a `task` helper is built from this same
    # profile (attenuation is proven in `tests/test_subagents.py`), and a plan is bounded by the
    # tools the profile holds.
    harness = harness_tool_names() if harness_enabled_for(AgentProfile(name="narrow")) else set()
    assert _advertised(narrowed) == {
        kept,
        *scratchpad_tools(),
        *subagent_tool_names(),
        *harness,
    }
    assert _advertised(narrowed) < _advertised(full), "a profile must attenuate, never widen"


# --- the middleware chain ---
#
# These prove the wiring: each drives a real turn and asserts what the model is handed back. The
# decisions themselves live in `test_tool_authz.py`, `test_repeat_guard.py` and `test_audit.py`.


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

    Signals go to the graph's custom stream, which exists only while streaming; this reads the final
    state and the custom payloads, as `api/graph_stream` does.
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


def _as_the_model_sees_it(denial: str) -> str:
    """`denial_result`'s sentence plus the mark `_refusal_message` appends to an access decision.

    Composed separately because `_refusal_message` defangs what it is handed, which would escape a
    mark composed upstream.
    """
    return f"{denial} {SYSTEM_SPEECH_MARK}"


def test_a_denied_call_reaches_the_model_as_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A denied call reaches the model as a refusal.

    `enforce_tool_authz` raising helps only if `surface_authorization_denials` turns it into
    something the model can act on; a generic tool error would be retried.
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
    """`dry_run` refuses a side-effecting tool.

    The expected text comes from `dry_run_refusal` and the tool from the live side-effecting set, so
    neither is a second copy.
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
        expected = dry_run_refusal(write_tool, {})
    finally:
        reset_dry_run(token)

    assert expected is not None
    assert content == _as_the_model_sees_it(denial_result(expected))


def test_a_repeated_call_is_refused_on_this_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """The turn's repeat counter is reached on this engine and its refusal relayed.

    At a limit of 1 a single turn shows both halves: the first call runs, the second is refused.
    """
    monkeypatch.setattr(settings, "entra_required", False)
    monkeypatch.setattr(settings, "max_identical_tool_calls", 1)
    args = {"question": "which solvent?"}
    graph = build_langgraph_agent(
        model=ScriptedModel(
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
    """A tool call lands in the audit trail with its name, arguments and result."""
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
    """A failing tool is announced, recorded and relayed to the model.

    `announce_tool_failures` is innermost so it sees the raw exception. The tool is substituted into
    the compiled `ToolNode` because `core.tool_registry` is process-global.
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


def _a_skill_this_deployment_lists() -> str:
    """The alphabetically first shipped skill the default surface actually offers.

    Skills whose tools ship with an opt-in bundle are hidden by default, so a role-gate fixture must
    start from a listed skill. Answered by `skill_permits` itself, minus the role gate about to be
    installed, rather than re-derived.
    """
    directories = [*settings.skills_dirs]
    permits = skill_permits(
        enabled=settings.skills_enabled_list,
        declared=declared_tools(directories),
        required=required_tools(directories),
        available=_advertised_names(get_profile(None), _capability_tools()),
        gates={},
    )
    return sorted(name for name in declared_tools(directories) if permits.filed(name))[0]


def test_the_skills_middleware_is_attached_and_narrows_by_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A role-gated skill is invisible and unreadable to a caller lacking the role.

    `SkillsMiddleware` publishes skill paths in the prompt, so hiding the listing while leaving the
    file readable would be a gate a model walks around. The gated skill comes from the shipped tree.
    """
    gated = _a_skill_this_deployment_lists()
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


def test_the_backend_narrows_skills_by_the_shared_predicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A role-gated skill is hidden from a caller without the role, on the shipped corpus.

    The backend narrows by `skill_permits`, the predicate every caller asks. Every basis
    (`available=`, `required=`) is passed to both sides: omitting `available=` would test a path
    production no longer takes, and omitting `required=` would make `skill_permits` a weaker
    predicate.
    """
    gated = _a_skill_this_deployment_lists()
    monkeypatch.setattr(settings, "skill_role_gates", {gated: ["process-chemist"]})
    profile, tools = get_profile(None), _capability_tools()
    bound = {fn.__name__ for fn in tools}

    token = set_current_identity("u-1", frozenset({"reader"}))
    try:
        from chemclaw.connectors.registry import skills_dirs

        every_dir = [*settings.skills_dirs, *skills_dirs()]
        permitted = {
            name
            for name in declared_tools(every_dir)
            if skill_permits(
                enabled=settings.skills_enabled_list,
                declared=declared_tools(every_dir),
                required=required_tools(every_dir),
                available=bound,
                gates=settings.skill_role_gates,
            ).filed(name)
        }
        graph = _skill_names(skills_backend(profile, tools, available=bound))
    finally:
        reset_current_identity(token)

    assert graph == permitted, "the backend and the shared predicate disagreed about visibility"
    assert gated not in graph, "the fixture must gate something for this to mean anything"


def test_two_skills_trees_cannot_be_labelled_the_same_thing() -> None:
    """Two skills trees cannot be labelled the same thing.

    The label is a route prefix, and `skills_backend` builds a dict over the pairs, so a duplicate
    would silently drop a tree while its path stayed advertised.
    """
    labelled = _labelled(["a/skills", "a-1/skills", "b/a/skills"])

    labels = [label for label, _directory in labelled]
    assert len(set(labels)) == len(labelled), f"two trees share a label: {labels}"
    # The comprehension `skills_backend` uses, so the assertion is about what the backend routes
    # rather than about the labels alone.
    routes = {f"/{label}/": directory for label, directory in labelled}
    assert len(routes) == len(labelled), "a skills tree was dropped from the backend's routes"


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
    """A role change mid-session re-narrows the skill listing.

    `SkillsMiddleware.before_agent` skips loading when `skills_metadata` is in state, and the
    checkpointer keeps state across turns. Turn two must show exactly the ungated remainder, since
    an empty list would also lack the gated skill.
    """
    gated = _a_skill_this_deployment_lists()
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
    """A state-changing call is refused without an approved plan.

    A fresh session has no approved plan, so the first state-changing call is refused. Proves the
    gate is reached and relayed; the predicate is `test_plan_gate.py`'s.
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

    # The whole refusal, built from the function the gate raises. Consumers key on the
    # discriminator, asserted on the wire in the test below, not on a phrase of the prose.
    assert content == _as_the_model_sees_it(denial_result(plan_approval_refusal(write_tool)))


def test_a_plan_refusal_reaches_the_stream_with_its_own_discriminator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plan refusal reaches the stream with its own discriminator.

    A refusal and an outage share the `tool_failed` event type, so the event says why rather than
    leaving readers to parse prose. Driven through `graph_events`, because the stamp must survive
    the gate, `announce_tool_failures` and the translator.
    """
    monkeypatch.setattr(settings, "entra_required", False)
    monkeypatch.setattr(settings, "harness_enabled", True)
    monkeypatch.setattr(settings, "harness_autonomy", "plan_only")
    write_tool = sorted(set(side_effecting_tools()) & {t.__name__ for t in _capability_tools()})[0]
    # `ScriptedChatModel`, because `graph_events` reads `stream_mode="messages"` and the plain fake
    # yields no chunks for a tool-call turn.
    graph = build_langgraph_agent(
        model=ScriptedChatModel([{"name": write_tool, "args": {}}, "done"]),
        audit_sink=NullAuditSink(),
    )

    class _Usage:
        def add(self, _usage: Any) -> None:
            """The ledger's shape; this test does not assert on tokens."""

    async def _drive() -> list[Any]:
        return [
            event
            async for event in graph_events(
                graph,
                "help",
                config={"configurable": {"thread_id": "t-plan-gate"}},
                trace=ToolCallTrace(),
                on_signal=lambda _s: None,
                usage=_Usage(),
            )
        ]

    session = set_current_session_id("session-discriminator")
    try:
        events = asyncio.run(_drive())
    finally:
        reset_current_session_id(session)

    failures = [e for e in events if isinstance(e, ToolFailedEvent)]
    assert [(f.tool, f.reason) for f in failures] == [(write_tool, PLAN_GATE_REASON)]
    # And the negative half, so the field is a *classification* rather than a constant: an ordinary
    # tool fault carries no reason at all, which is what every failure emitted before this field
    # existed still means to a surface that reads it.
    fault = _signal_event(ToolFailureSignal(tool="x", message="ConnectionError: down"))
    assert isinstance(fault, ToolFailedEvent) and fault.reason is None


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

    # Expressed against the refusal the gate would have produced, not against a phrase of it: the
    # claim is "this call was not gated", and a substring check made that claim depend on wording
    # nobody promised to keep.
    assert content != _as_the_model_sees_it(
        denial_result(plan_approval_refusal("ask_clarifying_question"))
    )


def test_the_gate_is_absent_when_the_deployment_did_not_ask_for_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plan gate is absent when the deployment did not enable the harness.

    The same tool, session and turn: refused with the harness on, allowed with it off, so the
    positive test cannot pass vacuously.
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

    # Same negative, same reason as above — the exact refusal this tool would have earned, rather
    # than a sentence fragment that only happens to appear in it.
    assert content != _as_the_model_sees_it(denial_result(plan_approval_refusal(write_tool)))


def test_the_harness_adds_its_plan_tool_only_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The harness adds `write_todos` only when enabled.

    `harness_tool_names()` stays in `available_tool_names()` either way, so validators recognise
    `write_todos` whatever this process has configured.
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

    At a limit of exactly 1 an inference from the loop's last stop decision would see nothing; the
    recorded count does.
    """
    monkeypatch.setattr(settings, "entra_required", False)
    monkeypatch.setattr(settings, "harness_enabled", True)
    monkeypatch.setattr(settings, "harness_max_loop_iterations", 1)

    def _graph(script: list[AIMessage]) -> Any:
        return build_langgraph_agent(
            model=ScriptedModel(messages=iter(script)),
            audit_sink=NullAuditSink(),
            checkpointer=InMemorySaver(),
        )

    # A model that keeps calling tools, or the loop never re-enters and the cap is never consulted.
    calling = AIMessage(
        content="",
        tool_calls=[{"name": "list_skills", "args": {}, "id": "c1", "type": "tool_call"}],
    )
    capped = _graph([calling] * 4)
    config = {"configurable": {"thread_id": "capped-1"}}
    # The value `ainvoke` returns, not `get_state(config).values`: `loop_capped` is an
    # `UntrackedValue` channel, never checkpointed, so `get_state` would read a silent `False`.
    state = asyncio.run(capped.ainvoke(turn_input("go"), config=config))

    assert loop_capped(state), "a looping turn at a cap of 1 was not capped"
    assert "loop_capped" not in capped.get_state(config).values, (
        "the cap flag was checkpointed; it is per turn and the thread outlives the turn"
    )

    # And the other side, which is the half the count could never express: a turn that spends its
    # last allowed call and then answers is *not* capped, even though it ends at exactly the cap.
    # Reporting that one as capped marks a complete answer partial.
    finished = _graph([AIMessage(content="done")])
    done_config = {"configurable": {"thread_id": "capped-2"}}
    done = asyncio.run(finished.ainvoke(turn_input("go"), config=done_config))

    assert not loop_capped(done), "a turn that answered within the cap was reported as capped"


def test_the_loop_cap_holds_with_the_harness_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The loop cap holds with the harness off.

    The cap belongs to the model/tool loop; without it a runaway turn's only bound is
    `agent_recursion_limit`, whose `GraphRecursionError` discards the turn. The script loops harder
    than the cap allows, so an ungated cap is what stops it.
    """
    monkeypatch.setattr(settings, "entra_required", False)
    monkeypatch.setattr(settings, "harness_enabled", False)
    monkeypatch.setattr(settings, "harness_max_loop_iterations", 1)

    calling = AIMessage(
        content="",
        tool_calls=[{"name": "list_skills", "args": {}, "id": "c1", "type": "tool_call"}],
    )
    graph = build_langgraph_agent(
        model=ScriptedModel(messages=iter([calling] * 4)),
        audit_sink=NullAuditSink(),
        checkpointer=InMemorySaver(),
    )
    state = asyncio.run(
        graph.ainvoke(turn_input("go"), config={"configurable": {"thread_id": "classic-cap"}})
    )

    assert loop_capped(state), "the classic agent ran past the cap; the graceful stop is gone"


def test_a_parallel_tool_batch_is_bounded_by_turn_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """A parallel tool batch is bounded by `turn_config`.

    `ToolNode` gathers every call in a message with no limit, so one message could take a whole
    pool's connections. Driven through the production graph and config builder, since the bound is a
    property of the invocation config.
    """
    monkeypatch.setattr(settings, "entra_required", False)
    monkeypatch.setattr(settings, "agent_max_parallel_tool_calls", 2)

    peak = 0
    current = 0

    async def probe(n: int) -> str:
        nonlocal peak, current
        current += 1
        peak = max(peak, current)
        await asyncio.sleep(0.05)
        current -= 1
        return f"done {n}"

    slow = StructuredTool.from_function(
        coroutine=probe, name="slow_probe", description="Sleep briefly and report."
    )
    calls = [
        {"name": "slow_probe", "args": {"n": i}, "id": f"c{i}", "type": "tool_call"}
        for i in range(6)
    ]
    graph = build_langgraph_agent(
        model=ScriptedModel(
            messages=iter([AIMessage(content="", tool_calls=calls), AIMessage(content="done")])
        ),
        audit_sink=NullAuditSink(),
        connectors=[slow],
    )
    asyncio.run(graph.ainvoke(turn_input("go"), config=turn_config()))

    assert peak == 2, f"a 6-call batch ran {peak} bodies at once against a bound of 2"

    # And the escape hatch: 0 means a deployment chose unbounded, spelled by the key's absence
    # because that absence *is* upstream's unbounded default.
    monkeypatch.setattr(settings, "agent_max_parallel_tool_calls", 0)
    assert "max_concurrency" not in turn_config()


def test_the_loop_cap_counts_the_turn_and_not_the_session() -> None:
    """The loop cap counts the turn, not the session, because of the channel type.

    A checkpointed counter keyed by session would accumulate across turns until every turn ended
    before the model was called. Under an untracked channel (`state.TurnTotal`) there is no stored
    count to restore. The input is bare `{"messages": ...}` with nothing zeroing anything; four
    turns at a cap of 3 on one thread, so the fourth would fail.
    """
    from langgraph.checkpoint.memory import InMemorySaver

    async def _scenario() -> list[str]:
        saver = InMemorySaver()
        config = {"configurable": {"thread_id": "sess-cap-per-turn"}}
        answers: list[str] = []
        for turn in range(4):
            graph = build_langgraph_agent(
                model=ScriptedModel(messages=iter([AIMessage(content=f"answer {turn}")])),
                audit_sink=NullAuditSink(),
                checkpointer=saver,
            )
            state = await graph.ainvoke({"messages": [("user", f"question {turn}")]}, config=config)
            answers.append(str(state["messages"][-1].content))
        return answers

    original = (settings.harness_enabled, settings.harness_max_loop_iterations)
    settings.harness_enabled, settings.harness_max_loop_iterations = True, 3
    try:
        answered = asyncio.run(_scenario())
    finally:
        settings.harness_enabled, settings.harness_max_loop_iterations = original

    assert answered == [f"answer {turn}" for turn in range(4)], (
        "a turn answered with something other than the model's reply — the cap counted the session"
    )


def test_a_turn_runs_under_a_chosen_step_ceiling_not_the_frameworks_9999() -> None:
    """A turn runs under a chosen step ceiling, not the framework's 9999.

    `create_agent` and `create_deep_agent` both bake `recursion_limit=9999`; config passed at
    `ainvoke` wins. The ceiling is the backstop under the loop cap, which is the graceful stop that
    still sends the partial answer. Asserted against the derivation from the two settings. The
    superstep cost per iteration was measured by binary search as `6*N + 8` over several N;
    re-measure when the middleware stack changes.
    """
    config = turn_config("session-1")

    assert config["recursion_limit"] == settings.agent_recursion_limit
    assert config["recursion_limit"] != 9999
    # `+ 1`: the tool-less wrap-up call a capped graph is owed (`agent/loop_cap.py`).
    assert config["recursion_limit"] == (
        (settings.harness_max_loop_iterations + 1) * settings.agent_supersteps_per_model_call + 8
    )
    assert config["configurable"] == {"thread_id": "session-1"}

    # A template step has no thread — one bounded turn, no conversation before or after it — and
    # still gets the ceiling. That path runs with the harness off, so the loop cap is not
    # attached and this is its only bound.
    assert "configurable" not in turn_config()
    assert turn_config()["recursion_limit"] == settings.agent_recursion_limit


def _observed_prompt(**kwargs: Any) -> tuple[str, set[str]]:
    """The whole system message one built graph sends, and the tools it bound, off the wire.

    Every middleware writes into the same system message, so claims about the prompt are checked on
    what the model actually received, against the surface the graph runs.
    """
    model = _Recording(messages=iter([AIMessage(content="done")]))
    graph = build_langgraph_agent(model=model, audit_sink=NullAuditSink(), **kwargs)
    asyncio.run(graph.ainvoke({"messages": [("user", "hi")]}))
    assert model.prompts, "the model was never called, so no prompt was observed"
    return model.prompts[0], _advertised(graph)


def _system_prompt(**kwargs: Any) -> str:
    """Just the system message, for a caller with nothing to ask about the surface."""
    prompt, _bound = _observed_prompt(**kwargs)
    return prompt


@pytest.mark.parametrize(
    ("claim", "why"),
    [
        ("Executing Skill Scripts", "`scratchpad_tools()` withholds `execute`"),
        ("Skills may contain Python scripts", "there is no verb here that runs one"),
        ("web-research", "no such skill exists and the posture declines the capability"),
        ("Use any helper scripts", "the same instruction in the example workflow"),
        ("shared across all agent tools on this machine", "no machine-wide skills tree exists"),
    ],
)
def test_the_prompt_does_not_tell_the_model_to_run_a_skills_script(claim: str, why: str) -> None:
    """The prompt does not tell the model to run a skill's scripts.

    Upstream's skills prompt says skills may contain executable scripts and names a web-research
    skill, neither of which applies here (`execute` is withheld; no egress). `_skills_prompt`
    removes those passages from upstream's template, raising if upstream rewords one; the passages
    are pinned in `tests/test_upstream_surface.py`.
    """
    assert claim not in _system_prompt(), (
        f"the system message still tells the model {claim!r}, and {why}"
    )


def test_a_listed_skill_always_has_at_least_one_tool_this_turn_binds() -> None:
    """A listed skill always has at least one tool this turn binds.

    `ToolScopedSkills` measures absence against the tools the graph binds, not what manifests
    advertise, so a skill whose server is unreachable is not listed.
    """
    prompt, bound = _observed_prompt()
    listed = _listed_skills(prompt)
    declared = declared_tools([*settings.skills_dirs, *_bundle_skills_dirs()])
    orphaned = {
        name: sorted(declared[name])
        for name in listed
        if declared.get(name) and not (declared[name] & bound)
    }
    assert not orphaned, (
        f"skills listed with no bound tool at all: {orphaned}. The listing must be narrowed by "
        "what this turn binds, not by what a manifest advertises."
    )


def test_the_prompt_says_where_a_turn_may_write_and_where_it_may_not() -> None:
    """The prompt says where a turn may write and where it may not.

    `write_file`, `edit_file`, `ls`, `glob` and `grep` are bound and `filesystem_permissions()`
    refuses writes outside `/scratch` and `/memories`, so the prompt must state that rule.
    """
    load_profiles()
    # The default prose and a profile that replaces it: `FilesystemMiddleware` is composed for every
    # agent, so the block is in both `_INSTRUCTION_BLOCKS` and `_SAFETY_BLOCKS`.
    for profile in (None, "safety"):
        prompt = _system_prompt() if profile is None else _system_prompt(profile=profile)
        for token in ("write_file", SCRATCH_ROOT, MEMORY_ROOT):
            assert token in prompt, (
                f"the {profile or 'default'} prompt never mentions {token!r}, which every "
                "turn binds"
            )


def test_a_narrow_profiles_system_message_drops_the_floor_sentence_it_cannot_act_on() -> None:
    """A narrow profile's system message drops the floor sentence it cannot act on.

    The `record_knowledge_note` sentence goes only to an agent binding the tool. Both directions:
    `property-lookup` must not be told to record findings, `reporting` must still be. The security
    floor is asserted in the same capture.
    """
    load_profiles()
    sentence = "goes through record_knowledge_note"

    narrow = _system_prompt(profile="property-lookup")
    assert sentence not in narrow, "a profile with no write tool is still told to record findings"
    assert ENVELOPE_TAG in narrow, "the narrowing took the envelope rule with it"
    assert "'Refused:'" in narrow, "the narrowing took the refusal semantics with it"

    assert sentence in _system_prompt(profile="reporting"), (
        "the profile built around record_knowledge_note lost the sentence about it"
    )


def test_a_denial_drops_off_the_wire_when_the_fleet_binds_the_tool_that_refutes_it() -> None:
    """A denial drops off the wire when the fleet binds the tool that refutes it.

    `absent_unless` acts on what the graph binds. The bundle serving `screen_genotoxic_alerts` lives
    in `Chemclaw3-mcp`, so a tool of that name is passed as a connector, the shape
    `open_connector_specs` returns. `tests/test_prose_contract.py` covers the same rule through
    `instructions_for`.
    """
    denial = "genotoxicity (ICH M7)"
    served = StructuredTool.from_function(
        func=lambda smiles: "no alert",
        name="screen_genotoxic_alerts",
        description="Screen a structure for DNA-reactive structural alerts.",
    )

    assert denial in _system_prompt(), "the limit is not stated on a deployment without the tool"
    assert denial not in _system_prompt(connectors=[served]), (
        "the tool is bound and the prompt still denies the capability it provides"
    )


def test_a_connector_tool_cannot_take_a_first_party_name_through_the_connectors_argument() -> None:
    """A connector tool cannot take a first-party name through the `connectors=` argument.

    `ToolNode` keys by name with connectors appended second, so a collision would silently replace
    the first-party tool while `authz.side_effecting_tools()`, the plan gate and the audit trail
    still act on its name. The control is the same graph without the collision, so a check refusing
    every `connectors=` argument fails.
    """
    from chemclaw.connectors.registry import ConnectorError

    shadow = StructuredTool.from_function(
        func=lambda: "the connector's body ran",
        name="record_knowledge_note",
        description="a connector tool claiming a first-party name",
    )
    assert "record_knowledge_note" in side_effecting_tools(), (
        "the precondition is a name the authorization layer classifies; without it the refusal "
        "would be about tidiness rather than about a gate"
    )
    with pytest.raises(ConnectorError, match="record_knowledge_note"):
        build_langgraph_agent(
            ScriptedChatModel(["done"]), connectors=[shadow], audit_sink=NullAuditSink()
        )

    innocent = StructuredTool.from_function(
        func=lambda: "served elsewhere",
        name="screen_genotoxic_alerts",
        description="a connector tool claiming no first-party name",
    )
    graph = build_langgraph_agent(
        ScriptedChatModel(["done"]), connectors=[innocent], audit_sink=NullAuditSink()
    )
    assert "screen_genotoxic_alerts" in graph.nodes["tools"].bound.tools_by_name, (
        "the check refused a connector tool that collides with nothing"
    )
