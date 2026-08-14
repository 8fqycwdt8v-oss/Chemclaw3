"""The four invariants of `D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor` (M9).

A team is the one part of this migration that adds capability rather than porting it, so it is the
one part where a test suite that only proves "it works" would be negligent. Each invariant below is
a *security* property, and each test is written to fail if the property is removed rather than to
pass because the machinery ran.

Invariant 2 — the actor reaching a subagent — is the one the ADR asked to verify **before**
building, because deepagents issue #569 questioned whether `runtime.config` propagates at all. The
answer turned out not to depend on that: Chemclaw's actor never travels through graph state or
through `RunnableConfig`. It is a contextvar bound around the whole turn, and every hop into a
subagent is spawned with `copy_context()`. So the question "does `_EXCLUDED_STATE_KEYS` filter our
identity" is moot — there is nothing identity-shaped in state to filter — and the real question is
whether execution ever leaves the turn's context. `test_the_human_actor_reaches_a_specialist`
answers that one, which is the question that actually decides whether `require_actor` holds.
"""

import asyncio
from typing import Any

import pytest
from langchain_core.tools import tool

from chemclaw.agent.audit import NullAuditSink
from chemclaw.agent.chemclaw_agent import advertised_tool_names
from chemclaw.agent.langgraph_agent import build_langgraph_agent, skills_backend
from chemclaw.agent.profiles import AgentProfile, get_profile
from chemclaw.agent.team import (
    _SUPERVISOR_PROMPT,
    _TASK_TOOL_DESCRIPTION,
    REQUIRED_SPECIALIST,
    SPECIALISTS,
    TeamError,
    _description,
    build_team_middleware,
    reject_widening,
    running_specialist,
    specialist_profiles,
)
from chemclaw.core.config import settings
from chemclaw.core.identity_context import (
    get_current_actor,
    reset_current_identity,
    set_current_identity,
)
from tests.fakes_langgraph import ScriptedChatModel


@pytest.fixture(autouse=True)
def _discovered() -> None:
    """Register the shipped profiles, as `create_app` does at startup.

    Autouse and idempotent (`load_profiles` skips a name already registered), because every test
    here resolves a specialist by name and a suite that ran them against an empty registry would be
    asserting about profiles that do not exist.
    """
    from chemclaw.agent.profile_discovery import load_profiles

    load_profiles()


def _default() -> AgentProfile:
    """The supervisor's profile — the unnarrowed agent every specialist is checked against."""
    return get_profile(None)


# --- invariant 1: attenuation only ---------------------------------------------------------------


@pytest.mark.parametrize("name", SPECIALISTS)
def test_every_shipped_specialist_is_an_attenuation_of_the_default_agent(name: str) -> None:
    """No specialist in `data/profiles/` advertises a tool the unnarrowed agent does not.

    Parametrized over `SPECIALISTS` rather than a written list so a sixth specialist is covered on
    the day it is added — the same reason `test_connector_transport.py` parametrizes over
    discovery.
    """
    reject_widening(_default(), get_profile(name))
    assert advertised_tool_names(get_profile(name)) <= advertised_tool_names(_default())


def test_a_specialist_that_would_widen_its_caller_fails_the_build() -> None:
    """The invariant, proven by violating it — otherwise the test above proves only today's data.

    A narrowed supervisor holding two tools, and a "specialist" holding one it does not: exactly
    the shape that would turn delegation into a privilege escalation, and exactly the shape
    `_reject_unknown_tool_names` accepts, because every name here is real and the deployment does
    provide them all.
    """
    supervisor = AgentProfile(name="narrow-boss", tool_names=frozenset({"predict_pka"}))
    specialist = AgentProfile(
        name="greedy", tool_names=frozenset({"predict_pka", "start_optimization_campaign"})
    )
    with pytest.raises(TeamError, match="would widen"):
        reject_widening(supervisor, specialist)


def test_the_check_that_already_existed_would_not_have_caught_it() -> None:
    """Why invariant 1 needed new code, stated as a test rather than as a comment.

    `_reject_unknown_tool_names` asks whether a profile names a tool the *deployment* provides. The
    escalating specialist above passes that check cleanly, because `start_optimization_campaign` is
    a real tool — it is just not one its caller holds. A test that did not pin this would let
    someone delete `reject_widening` believing the older check covered it.
    """
    from chemclaw.agent.chemclaw_agent import _reject_unknown_tool_names

    greedy = AgentProfile(
        name="greedy", tool_names=frozenset({"predict_pka", "start_optimization_campaign"})
    )
    _reject_unknown_tool_names(greedy)  # passes: both names exist deployment-wide


# --- the safety gate -----------------------------------------------------------------------------


def test_a_team_without_the_safety_specialist_is_refused() -> None:
    """`safety` is a gate, not one capability among five, so it is not attenuable away.

    Every other narrowing in this system is permitted because attenuation can only reduce what an
    agent may do. This one is refused because reducing it removes the check a chemist runs *before*
    deciding whether to approve work — and a hazard screen nobody ran is not a smaller answer, it
    is a missing one.
    """
    with pytest.raises(TeamError, match="must include"):
        specialist_profiles(("evidence", "computation"))


def test_the_full_team_resolves_and_includes_the_gate() -> None:
    """The ordinary path, asserted so the refusal above cannot be passing for the wrong reason."""
    profiles = specialist_profiles()
    assert [profile.name for profile in profiles] == list(SPECIALISTS)
    assert REQUIRED_SPECIALIST in {profile.name for profile in profiles}


# --- invariant 2: identity reaches a specialist --------------------------------------------------


def test_the_human_actor_reaches_a_specialist() -> None:
    """`require_actor` must hold inside a subagent, and this is what decides whether it does.

    The actor is read ambiently by every tool that attributes work to a person. If execution inside
    a specialist ran in a context not derived from the turn's, `get_current_actor()` would return
    `None` there — and under `entra_required` that is a loud refusal, but in dev it silently
    degrades to the service identity and the audit trail loses its attribution. That is precisely
    the failure D-040 found in MAF's `mode_set`.

    Driven through the real wrapper the team uses, in a real event loop, so what is proven is the
    execution path rather than the reasoning about it.
    """
    seen: list[str | None] = []

    class _Specialist:
        """A stand-in specialist that reports the ambient actor it was invoked under."""

        async def ainvoke(self, state: Any, config: Any = None, **kwargs: Any) -> Any:
            seen.append(get_current_actor())
            return {"messages": []}

    from chemclaw.agent.team import _AttributedSpecialist

    async def _turn() -> None:
        token = set_current_identity("oid-of-a-real-chemist", frozenset({"chemist"}))
        try:
            await _AttributedSpecialist("evidence", _Specialist()).ainvoke({})
        finally:
            reset_current_identity(token)

    asyncio.run(_turn())
    assert seen == ["oid-of-a-real-chemist"]


def test_a_specialist_cannot_leak_an_identity_change_back_to_its_caller() -> None:
    """Propagation is strictly downward, which is the polarity that makes the contextvar safe.

    `copy_context()` snapshots at spawn, so a subagent that rebound the actor would affect only its
    own context. Asserted because the alternative — a specialist able to change who the *parent*
    thinks it is acting for — would be far worse than losing the actor.
    """
    outer = set_current_identity("oid-parent", frozenset())
    try:

        async def _inner() -> str | None:
            inner = set_current_identity("oid-impostor", frozenset())
            try:
                return get_current_actor()
            finally:
                reset_current_identity(inner)

        assert asyncio.run(_inner()) == "oid-impostor"
        assert get_current_actor() == "oid-parent"
    finally:
        reset_current_identity(outer)


# --- invariant 3: the trail names the specialist beside the human --------------------------------


def test_the_running_specialist_is_stamped_and_always_unstamped() -> None:
    """The name must not outlive the specialist's invocation.

    A leaked stamp would attribute the *supervisor's* next tool call to a specialist that had
    already returned — a wrong row in the one table that must not be wrong. Asserted on the raising
    path too, because that is the path a `finally` exists for and the one nobody exercises by hand.
    """
    from chemclaw.core.identity_context import get_current_specialist

    assert get_current_specialist() == ""
    with running_specialist("safety"):
        assert get_current_specialist() == "safety"
    assert get_current_specialist() == ""

    with pytest.raises(RuntimeError), running_specialist("evidence"):
        assert get_current_specialist() == "evidence"
        raise RuntimeError("the specialist failed")
    assert get_current_specialist() == ""


# --- invariant 4: skills do not inherit ----------------------------------------------------------


def test_a_specialist_sees_only_the_skills_its_own_surface_earns() -> None:
    """Skills are scoped by the specialist's tools, not by its caller's.

    A skill declares the tools its judgment is about, and `skill_permits` drops one whose tools the
    agent cannot call. So `safety` — six tools — must not be offered the calculation-selection or
    experiment-design skills, which are judgment about capabilities it does not hold. This is the
    capability scope working one level down rather than a fifth rule.
    """
    everything = _listed(_default())
    narrowed = _listed(get_profile("safety"))
    assert everything, "the supervisor sees no skills at all — the helper is broken"
    assert narrowed < everything, "the safety specialist sees every skill the supervisor does"
    assert not any("experiment-design" in entry for entry in narrowed)
    assert not any("calculation-selection" in entry for entry in narrowed)


def _listed(profile: AgentProfile) -> set[str]:
    """Every skill path one profile's backend will show, across all its skill trees.

    Listed per *route* rather than from `/`, because the composite's default route is a
    `StateBackend` that refuses to read outside a graph execution — an unrouted path is meant to
    find nothing, and asking it to is a test artefact rather than the behaviour under test.
    """
    from chemclaw.agent.langgraph_agent import _labelled, _skill_dirs

    backend = skills_backend(profile, [])
    found: set[str] = set()
    for label, _directory in _labelled(_skill_dirs()):
        found.update(str(entry["path"]) for entry in backend.ls(f"/{label}/").entries or [])
    return found


# --- the team as built ---------------------------------------------------------------------------


def test_the_team_middleware_carries_one_attributed_specialist_per_name() -> None:
    """The assembled team: five specialists, each compiled and each stamping its own name."""
    built: list[str] = []

    def _build(profile: AgentProfile, **_kwargs: Any) -> Any:
        built.append(profile.name)
        return build_langgraph_agent(
            ScriptedChatModel(["ok"]), profile=profile, audit_sink=NullAuditSink()
        )

    middleware = build_team_middleware(_default(), build=_build)
    assert built == list(SPECIALISTS)
    assert middleware.subagent_names == frozenset(SPECIALISTS)


def test_a_specialist_does_not_get_a_team_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """The recursion guard, which is a rule rather than a depth limit.

    `build_team_middleware` builds each specialist through `build_langgraph_agent`, so a specialist
    whose own build attached a team would build five more, each of which would build five more. The
    guard is "a specialist does not have a team", expressed as membership rather than as a counter —
    a counter would only bound how badly the rule was broken.
    """
    monkeypatch.setattr(settings, "agent_teams_enabled", True)
    graph = build_langgraph_agent(
        ScriptedChatModel(["ok"]), profile=get_profile("safety"), audit_sink=NullAuditSink()
    )
    assert "task" not in {tool.name for tool in graph.nodes["tools"].bound.tools_by_name.values()}


def test_binding_a_config_keeps_the_specialists_name() -> None:
    """`with_config` must re-wrap, or the attribution silently disappears at the last moment.

    `SubAgentMiddleware` binds each subagent's config and invokes *the result*. If that call
    returned the bare inner runnable, every specialist's tool calls would land in the audit trail
    attributed to the supervisor — nothing would fail, nothing would be logged, and the audit record
    would simply be wrong. There is no observable symptom, so there has to be a test.
    """
    from chemclaw.agent.team import _AttributedSpecialist
    from chemclaw.core.identity_context import get_current_specialist

    seen: list[str] = []

    class _Inner:
        """A runnable whose `with_config` returns a *different* object, as LangChain's does."""

        def with_config(self, *_args: Any, **_kwargs: Any) -> "_Inner":
            return _Inner()

        async def ainvoke(self, _state: Any, _config: Any = None, **_kwargs: Any) -> Any:
            seen.append(get_current_specialist())
            return {"messages": []}

    bound = _AttributedSpecialist("safety", _Inner()).with_config({"tags": ["x"]})
    asyncio.run(bound.ainvoke({}))
    assert seen == ["safety"]


def test_the_team_is_off_by_default() -> None:
    """A capability M12 has not yet shown to help is not what a deployment gets by accident."""
    assert settings.agent_teams_enabled is False


# --- the handoff the trace shows -----------------------------------------------------------------


def _turn_events(graph: Any) -> list[Any]:
    """Drive one turn through the front door's translator and collect what it emitted."""
    from chemclaw.api.graph_stream import graph_events
    from chemclaw.api.runner_trace import ToolCallTrace

    class _Usage:
        def add(self, _usage: Any) -> None:
            """The ledger's shape; these tests do not assert on tokens."""

    async def _run() -> list[Any]:
        return [
            event
            async for event in graph_events(
                graph,
                "is palladium residue a problem here?",
                config={"configurable": {"thread_id": "t-handoff"}},
                trace=ToolCallTrace(),
                on_signal=lambda _s: None,
                usage=_Usage(),
            )
        ]

    return asyncio.run(_run())


def _delegating_turn(monkeypatch: pytest.MonkeyPatch, answer: str) -> list[Any]:
    """A supervisor that delegates once to `safety`, with every specialist on a scripted model.

    `build_chat_model` is patched rather than the specialists being injected, because
    `_team_middleware` deliberately does not forward the supervisor's model — so the seam that
    exists for "assemblable and testable without live credentials" is the one to use, and using it
    means this runs the *production* wiring rather than a hand-assembled stand-in.
    """
    monkeypatch.setattr(settings, "agent_teams_enabled", True)
    monkeypatch.setattr(
        "chemclaw.agent.langgraph_agent.build_chat_model",
        lambda *_args, **_kwargs: ScriptedChatModel([answer]),
    )
    graph = build_langgraph_agent(
        ScriptedChatModel(
            [
                {
                    "name": "task",
                    "args": {
                        "description": "check palladium residue for hazards",
                        "subagent_type": "safety",
                    },
                },
                "done",
            ]
        ),
        audit_sink=NullAuditSink(),
    )
    return _turn_events(graph)


def test_a_delegated_turn_announces_the_handoff_and_the_hand_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gap this closes: `HandoffEvent` was in the contract and nothing produced one.

    Driven through the whole path — supervisor, `task` tool, `_AttributedSpecialist`, the writer,
    the translator — rather than by calling the emitter directly, because the part that was in
    doubt is whether a handoff raised *inside a tool call* reaches the turn's stream at all. A test
    of the mapping alone would have passed against the shipped code, which is the whole problem.

    `reason` is the supervisor's own `description`, which is what makes the event answer "why" and
    not merely "who": a trace that records a route without its justification is the gap M9's
    argument for a supervisor was about.
    """
    from chemclaw.api.events import HandoffEvent

    events = _delegating_turn(monkeypatch, "no genotoxic alert matched")
    assert [(e.to, e.reason) for e in events if isinstance(e, HandoffEvent)] == [
        ("safety", "check palladium residue for hazards"),
        ("", ""),
    ]


def test_the_specialists_own_output_falls_between_its_handoff_and_its_hand_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pair is a span, not two announcements, and only the order makes it one.

    Without this a handoff emitted at the wrong moment — after the specialist returned, say — would
    still produce both events and still pass the test above, while a surface rendering the trace as
    a timeline would attribute the specialist's work to the supervisor. That is the same
    misattribution invariant 3 exists to prevent in the audit trail, one layer up.
    """
    from chemclaw.api.events import HandoffEvent

    events = _delegating_turn(monkeypatch, "no genotoxic alert matched")
    kinds = [
        event.type if not isinstance(event, HandoffEvent) else f"handoff:{event.to or 'back'}"
        for event in events
    ]
    enter, back = kinds.index("handoff:safety"), kinds.index("handoff:back")
    specialist_output = [i for i, k in enumerate(kinds) if k == "token"]
    assert enter < back
    assert any(enter < i < back for i in specialist_output), kinds
    # The tool result closes the delegation, so it must land after the hand back — the supervisor
    # is only back in control once the `task` call has returned.
    assert back < kinds.index("tool_result"), kinds


def test_a_specialists_prose_is_streamed_but_is_not_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A specialist's tokens reach the surface marked, and reach the answer not at all.

    Both halves, because each without the other is a defect that already had a candidate fix. Drop
    a specialist's tokens and the delegation goes silent for the longest stretch of the turn, and
    the test above — which pins the specialist's output *inside* its handoff span — has nothing to
    find. Stream them unattributed and `api/runner` concatenates them into `answer_parts`, which is
    both the text a chemist reads and the durable transcript: one agent's working prose interleaved
    with the supervisor's, in whatever order the two produced it.

    Asserted through the real stream rather than on `TokenEvent`'s default, because the field being
    *declared* was never in doubt — what was in doubt is whether the producer sets it.
    """
    from chemclaw.api.events import TokenEvent

    specialist_answer = "no genotoxic alert matched"
    events = _delegating_turn(monkeypatch, specialist_answer)
    tokens = [e for e in events if isinstance(e, TokenEvent)]

    attributed = "".join(e.text for e in tokens if e.agent)
    assert specialist_answer in attributed, (
        f"the specialist's prose never reached the stream: {[(e.agent, e.text) for e in tokens]}"
    )
    answer = "".join(e.text for e in tokens if not e.agent)
    assert specialist_answer not in answer, (
        f"the specialist's prose was spliced into the answer the runner builds: {answer!r}"
    )


def test_a_specialists_events_are_attributed_to_the_specialist_not_to_the_tool_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M9's `agent` field must carry the specialist's name — it carried `"tools"` on every turn.

    The attribution was derived from the subgraph namespace, on the assumption that a specialist's
    updates arrive under `("<specialist>:<task-id>",)`. They arrive under `("tools:<uuid>",)`:
    `SubAgentMiddleware` invokes the compiled specialist as an ordinary runnable *inside* the
    `task` tool, so the parent's tool node is the only frame and the specialist's name is not on
    the path at all. Every event a specialist raised was therefore attributed to an agent called
    `"tools"`.

    Found on the live lane, not here: a sonnet-5 routing arm reported its single delegation as
    `expected evidence → tools`, scoring as a supervisor mis-route what was the harness reading a
    field that could never hold the right value. The unit test that should have caught it passed
    against hand-written namespaces the engine never emits.

    So the assertion is driven end to end and reads the specialist's *tool call*, which is the
    event a routing measurement actually scores.
    """
    from chemclaw.api.events import ToolCallEvent

    monkeypatch.setattr(settings, "agent_teams_enabled", True)
    monkeypatch.setattr(
        "chemclaw.agent.langgraph_agent.build_chat_model",
        lambda *_a, **_k: ScriptedChatModel(
            [{"name": "screen_hazards", "args": {"smiles": "CCO"}}, "no alert matched"]
        ),
    )
    graph = build_langgraph_agent(
        ScriptedChatModel(
            [
                {
                    "name": "task",
                    "args": {"description": "check it", "subagent_type": "safety"},
                },
                "done",
            ]
        ),
        audit_sink=NullAuditSink(),
    )
    calls = {
        event.tool: event.agent for event in _turn_events(graph) if isinstance(event, ToolCallEvent)
    }
    assert calls.get("screen_hazards") == "safety", calls
    # The supervisor's own delegation is not the specialist's work, so it stays unattributed.
    assert calls.get("task") == ""


def test_a_specialist_that_raises_still_hands_control_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `finally` the audit stamp already relied on now closes the trace's span too.

    A handoff that never closes leaves a surface showing a turn stuck inside a specialist it left,
    and leaves the audit record implying the specialist authored everything that followed. Exercised
    on the raising path for the reason the unstamp assertion is: nobody reaches it by hand.
    """
    from chemclaw.core import turn_signals

    published: list[Any] = []
    monkeypatch.setattr(turn_signals, "get_stream_writer", lambda: published.append)

    with pytest.raises(RuntimeError), running_specialist("evidence", "look it up"):
        raise RuntimeError("the specialist failed")

    assert [
        (signal.to, signal.reason)
        for signal in (payload[turn_signals._KEY] for payload in published)
    ] == [("evidence", "look it up"), ("", "")]


def test_a_supervisor_with_connector_tools_can_build_its_team(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enabling the team must not brick every turn — measured live, it did.

    **The whole team suite could not express this, which is why it shipped.** Every other test here
    builds the supervisor with no connectors, and `_narrowed_connectors` returns `[]` before it
    reaches the widening assertion (`if not connectors: return []`), so the assertion was
    unreachable in all 22 of them.

    The defect: `build_langgraph_agent` composes the model's surface as
    `[*tools, *connectors, skill_read_tool]` but handed `_team_middleware` only `tools` as the
    supervisor's names. The assertion in `_narrowed_connectors` compares *connector* tool names
    against that set, so every connector tool a specialist legitimately kept read as a widening.
    Measured against the live stack with `CHEMCLAW_AGENT_TEAMS_ENABLED=true`: 15 of 15 probe turns
    raised `TeamError: specialist 'evidence' would reach connector tool(s) [...] that its supervisor
    cannot` at graph construction, before the model was called — each leaving a `turn_costs` row
    with `completed=false` and zero tokens.

    The guard itself is right and stays. What was wrong is the half of the surface it was given.

    `agent_teams_enabled` is set here because it is off by default, so without it `_team_middleware`
    returns `[]` and this test passes against the defect — which it did, on the first run, and is
    why the mutation check is worth more than the assertion.
    """
    monkeypatch.setattr(settings, "agent_teams_enabled", True)
    # Each specialist is compiled through `build_langgraph_agent` with no model of its own, so
    # without this the build reaches for a real Anthropic client and fails on the credential rather
    # than on the thing under test.
    monkeypatch.setattr(
        "chemclaw.agent.langgraph_agent.build_chat_model",
        lambda *_a, **_k: ScriptedChatModel(["ok"]),
    )

    @tool
    def similar_molecules(smiles: str) -> str:
        """A connector-provided read tool, in the shape `open_connector_specs` hands down."""
        return "[]"

    graph = build_langgraph_agent(
        ScriptedChatModel(["ok"]),
        profile=_default(),
        audit_sink=NullAuditSink(),
        connectors=[similar_molecules],
    )
    assert graph is not None


def test_the_routing_menu_tells_the_supervisor_what_each_specialist_does() -> None:
    """The supervisor's only per-specialist text must carry capability, not identity.

    **This is what a 1-of-15 delegation rate turned out to be.** `_description` took
    `instructions.split(". ")[0]`, and every shipped profile opens with the same identity sentence
    shape — "You are Chemclaw's `<name>` specialist" — so the menu upstream renders as
    `- {name}: {description}` printed the name twice and the capability never. The supervisor was
    picking between five descriptions that carried no information it did not already have from the
    name, which is not a routing decision it can get right.

    The assertion is deliberately about *information*, not about wording: a description may not be
    reconstructible from the specialist's name alone. Stripping the name and demanding the
    remainder still differ pairwise is what makes this fail against the old implementation — under
    it, all five collapsed to "you are chemclaw's specialist".
    """
    menu = {name: _description(get_profile(name)) for name in SPECIALISTS}

    for name, description in menu.items():
        assert not description.lower().startswith("you are chemclaw's"), (
            f"{name}'s description is the identity sentence the menu line already prints: "
            f"{description!r}"
        )

    stripped = {name: menu[name].lower().replace(name.lower(), "") for name in SPECIALISTS}
    assert len(set(stripped.values())) == len(SPECIALISTS), (
        "two specialists are indistinguishable once their own name is removed, so the supervisor "
        f"cannot route between them: {stripped}"
    )


def test_a_profile_that_does_not_announce_itself_keeps_its_first_sentence() -> None:
    """Dropping the identity sentence must not eat a profile whose first sentence is the work.

    The narrowing is "drop a sentence that only says which specialist this is", and an out-of-tree
    profile that never writes one must come through unchanged rather than losing its opening line
    to a rule written for the five in this repo.
    """
    profile = AgentProfile(
        name="kinetics",
        instructions="Fit rate constants from time-course data. Report the residuals.",
    )

    assert _description(profile) == "Fit rate constants from time-course data."


def test_an_opening_sentence_that_says_something_is_not_read_as_identity() -> None:
    """Opening with "You are" is not the test — naming yourself and nothing else is.

    A profile that opens "You are given a reaction and must return its hazards" begins with the
    same two words as an identity sentence and is not one, so the name has to appear before the
    sentence is dropped.
    """
    profile = AgentProfile(
        name="hazards",
        instructions="You are given a reaction and must return its hazards. Cite every alert.",
    )

    assert _description(profile) == "You are given a reaction and must return its hazards."


def test_the_task_tool_and_the_supervisor_prompt_agree() -> None:
    """The two texts the supervisor reads must describe one mechanism, not two.

    This is the defect `D-2026-08-12-a-supervisor-that-holds-every-tool-has-no-reason-to-delegate`
    found: the system prompt said "route by surface" and the tool description said "for complex,
    multi-step work", and on a one-tool question those disagree. That ADR settled the disagreement
    toward the capability partition and measured 2 of 15.

    **It has since been settled the other way**, and this test moved with it: the reason to spawn is
    isolation, parallelism or an independent look — upstream's reason, which does not depend on the
    supervisor lacking a tool — while the five names are the *surfaces* a helper runs on. The
    property under test is unchanged and is the one that actually matters: whatever the framing, the
    two texts must not contradict each other, the menu must be interpolated (a description without
    `{available_agents}` lists no surfaces at all), and every buildable surface must be named
    somewhere the supervisor reads.
    """
    assert "{available_agents}" in _TASK_TOOL_DESCRIPTION
    # Both texts must offer the same grounds for spawning. Asserted as a shared vocabulary rather
    # than by string equality, which would just be a copy of the prompt in the test.
    for ground in ("isolat", "at once"):
        assert ground in _SUPERVISOR_PROMPT.lower()
        assert ground in _TASK_TOOL_DESCRIPTION.lower()
    for specialist in SPECIALISTS:
        assert specialist in _SUPERVISOR_PROMPT, (
            f"{specialist} is buildable but the supervisor is never told when to use it"
        )


# --- the delegation tally the challenge gate reads ------------------------------------------------


def test_a_specialist_invocation_is_counted() -> None:
    """Every work delegation advances the tally the challenge gate reads.

    The count is what lets `agent/challenge_gate.py` tell a team from a solo turn without asking a
    model, and it rides `_AttributedSpecialist` so that a specialist which ran is a specialist which
    was stamped, announced *and* counted, in one place.
    """
    from chemclaw.agent.team import (
        _AttributedSpecialist,
        begin_delegation_tally,
        delegations,
        end_delegation_tally,
    )

    class _Runnable:
        async def ainvoke(self, _state: Any, _config: Any = None, **_kw: Any) -> str:
            return "done"

    async def _run() -> int:
        token = begin_delegation_tally()
        try:
            wrapped = _AttributedSpecialist("evidence", _Runnable())
            await wrapped.ainvoke({"messages": []})
            await wrapped.ainvoke({"messages": []})
            return delegations()
        finally:
            end_delegation_tally(token)

    assert asyncio.run(_run()) == 2


def test_the_tally_is_zero_outside_a_turn() -> None:
    """No tally means no delegations, so the gate's trigger is safe to evaluate anywhere.

    A template step and a CLI call never start one, and both must read 0 rather than raise — the
    same "safe to ask unconditionally" property `loop_cap.loop_hit_cap` has.
    """
    from chemclaw.agent.team import delegations

    assert delegations() == 0


def test_a_challenger_is_attributed_but_not_counted_as_a_delegation() -> None:
    """The panel's own members must not make the gate mistake its review for a team.

    `running_specialist` brackets a challenger for the audit trail exactly as it brackets a
    specialist — a challenger's tool calls have to be attributable — but counting them would mean a
    turn that delegated once got challenged *unconditionally* on the revision pass, because the
    first panel had inflated the count past the team threshold.
    """
    from chemclaw.agent.team import begin_delegation_tally, delegations, end_delegation_tally

    token = begin_delegation_tally()
    try:
        with running_specialist("challenger:grounding", "check the citations"):
            pass
        assert delegations() == 0
    finally:
        end_delegation_tally(token)
