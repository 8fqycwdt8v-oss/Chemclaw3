"""The turn graph: peers, the handoff that moves between them, and the bounds on both.

**What this file is for, above any individual assertion.** Peer handoff is the one arrangement in
this tree where an agent's surface is decided by something other than its immediate caller, so the
assertions that matter are the ones about *arithmetic*: that a peer's surface is a subset of the
root's, whatever the roster says, and that a chain of any length is still bounded by the root. A
number would rot; the inequalities cannot.

The multi-hop test is the centre of it. Everything else here is a bound on a way that could fail.
"""

import asyncio
import os
from typing import Any

import pytest

from chemclaw.agent.audit import NullAuditSink
from chemclaw.agent.handoff import (
    HANDOFF_PREFIX,
    handoff_tool_name,
    handoff_tools,
    refuse_a_handoff_past_the_cap,
)
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.profiles import AgentProfile
from chemclaw.agent.state import answer_text, turn_config, turn_input
from chemclaw.agent.turn_graph import (
    _peer_surface,
    build_turn_graph,
    entry_peer_or_root,
    root_surface,
)
from chemclaw.core.errors import ChemclawError
from tests.fakes_langgraph import ScriptedChatModel


def _profile(name: str, tools: set[str] | None) -> AgentProfile:
    """A rostered profile with a description, which the peer menu requires."""
    return AgentProfile(
        name=name,
        description=f"The {name} agent.",
        tool_names=None if tools is None else frozenset(tools),
    )


# --------------------------------------------------------------------------------------------
# The invariant: a peer's surface is the root's, intersected. Never a widening.
# --------------------------------------------------------------------------------------------


def test_a_peer_cannot_reach_a_tool_the_root_does_not_hold() -> None:
    """The invariant that replaces `D-2026-08-10`'s "attenuation of its caller", as arithmetic.

    A profile naming a tool the root lacks does not get it. This is the whole safety argument for
    peer handoff and it is one line of set algebra, which is the point: there is no code path that
    *adds* a name, so no review has to check that nobody added one.
    """
    root = frozenset({"find_notes", "screen_hazards"})
    greedy = _profile("greedy", {"find_notes", "start_optimization_campaign", "record_answer"})

    surface = _peer_surface(root, greedy)

    assert surface == frozenset({"find_notes"})
    assert surface <= root, "a peer's surface must be a subset of the root's, always"


def test_a_profile_that_narrows_nothing_narrows_to_nothing_as_a_peer() -> None:
    """`tool_names is None` means "does not narrow" for a session profile and nothing for a peer.

    The alternative reading hands a peer the root's entire surface under a name that promises
    something specific, which is `D-2026-08-12`'s identical-menu defect with worse consequences:
    the menu would differ while the surfaces did not.
    """
    assert _peer_surface(frozenset({"a", "b"}), _profile("vague", None)) == frozenset()


def test_the_root_surface_is_both_halves_or_a_peer_loses_every_connector() -> None:
    """`tool_names` never sees a connector tool, so the connector half has to be unioned in.

    Getting this wrong fails in the safe direction — a peer with no connector tools at all — which
    is exactly why it would have shipped unnoticed.
    """

    class _Connector:
        name = "similar_reactions"

    surface = root_surface(AgentProfile(name="default"), [_Connector()])

    assert "similar_reactions" in surface
    assert "find_notes" in surface, "the in-process half must still be there"


# --------------------------------------------------------------------------------------------
# The handoff tools themselves
# --------------------------------------------------------------------------------------------


def test_a_peer_is_never_offered_a_tool_that_hands_to_itself() -> None:
    """A self-goto re-enters the running node: a plausible no-op that burns a model call."""
    peers = [
        (_profile("evidence", {"a"}), frozenset({"a"})),
        (_profile("safety", {"b"}), frozenset({"b"})),
    ]

    names = {
        t.name for t in handoff_tools(peers, current="evidence", menu_tools=12, max_handoffs=3)
    }

    assert names == {handoff_tool_name("safety")}


def test_a_peer_name_with_a_hyphen_becomes_a_callable_tool_name() -> None:
    """`property-lookup` is a real shipped profile name and is not a valid OpenAI tool name."""
    assert handoff_tool_name("property-lookup") == f"{HANDOFF_PREFIX}property_lookup"


def test_the_menu_entry_names_what_that_peer_actually_binds() -> None:
    """The capability half is derived, which is what makes the identical-menu defect unrepeatable.

    Two peers with different surfaces must read differently to the deciding model, or a roster is
    a list of names with no basis on which to choose one.
    """
    peers = [
        (_profile("root", None), frozenset({"x"})),
        (_profile("evidence", {"find_notes"}), frozenset({"find_notes"})),
        (_profile("safety", {"screen_hazards"}), frozenset({"screen_hazards"})),
    ]

    built = handoff_tools(peers, current="root", menu_tools=12, max_handoffs=3)
    descriptions = {t.name: t.description for t in built}

    assert "find_notes" in descriptions[handoff_tool_name("evidence")]
    assert "screen_hazards" in descriptions[handoff_tool_name("safety")]
    assert descriptions[handoff_tool_name("evidence")] != descriptions[handoff_tool_name("safety")]


def test_two_peers_of_one_name_are_refused_at_build_time() -> None:
    """One `goto` naming two nodes would resolve by insertion order, which is not a design."""
    peers = [
        (_profile("same", {"a"}), frozenset({"a"})),
        (_profile("same", {"b"}), frozenset({"b"})),
    ]

    with pytest.raises(ChemclawError, match="names a profile twice"):
        handoff_tools(peers, current="other", menu_tools=12, max_handoffs=3)


# --------------------------------------------------------------------------------------------
# The cap
# --------------------------------------------------------------------------------------------


def test_the_cap_refuses_rather_than_ending_the_turn() -> None:
    """A refusal leaves the agent holding control with everything else it had.

    Ending the run would discard what the conversation produced, which is the position
    `agent/loop_cap.py` takes and the reason this is a returned string rather than a jump.
    """
    assert refuse_a_handoff_past_the_cap({"handoffs": 3}, 3)
    assert not refuse_a_handoff_past_the_cap({"handoffs": 2}, 3)
    assert not refuse_a_handoff_past_the_cap({"handoffs": 99}, 0), "0 removes the bound"


def test_the_refusal_names_what_to_do_instead() -> None:
    """A refusal names what to do instead, not only the wall.

    `agent/refusal_route.py`'s one-shape rule: prose naming only the wall supports exactly the two
    moves that do not help — call the identical tool again, or report the wall and stop.
    """
    refusal = refuse_a_handoff_past_the_cap({"handoffs": 3}, 3)

    assert "Answer the chemist yourself" in refusal
    assert "could not reach here" in refusal


# --------------------------------------------------------------------------------------------
# Entry: the property that makes this a swarm rather than a supervisor
# --------------------------------------------------------------------------------------------


def test_a_turn_resumes_on_whoever_held_the_conversation() -> None:
    """A chemist handed to safety who asks a follow-up is still talking to safety."""
    assert entry_peer_or_root({"active_agent": "safety"}, ["root", "safety"]) == "safety"


def test_an_unknown_active_agent_falls_back_to_the_root_rather_than_raising() -> None:
    """An unknown name routes to the root rather than raising.

    `active_agent` is a string in a checkpoint, so "what if it says something unexpected" must have
    a boring answer. The root is the widest surface in the mesh and reaches every other peer.
    """
    assert entry_peer_or_root({"active_agent": "deleted-peer"}, ["root", "safety"]) == "root"
    assert entry_peer_or_root({}, ["root", "safety"]) == "root"


# --------------------------------------------------------------------------------------------
# The default: nothing changes unless a deployment asks
# --------------------------------------------------------------------------------------------


def test_no_roster_builds_no_turn_graph() -> None:
    """The shipped default runs the same object it ran before this module existed."""
    assert build_turn_graph(ScriptedChatModel(["hi"]), audit_sink=NullAuditSink()) is None


def test_a_roster_that_survives_to_one_peer_builds_no_mesh(monkeypatch: Any) -> None:
    """A roster that survives to one peer builds no mesh.

    One peer is no mesh, and a wrapper whose only effect is to change three quiet things — the
    namespace depth, the checkpointed channel set and the answer predicate — is worse than nothing.
    """
    monkeypatch.setattr("chemclaw.core.config.settings.agent_peer_roster", "no-such-profile")

    assert build_turn_graph(ScriptedChatModel(["hi"]), audit_sink=NullAuditSink()) is None


def test_a_helper_holds_no_handoff_tool() -> None:
    """A `task` helper cannot hand the conversation anywhere, by construction.

    `_subagents` passes no `handoffs=`, so there is no set to subtract from and no name anybody can
    forget — which is the difference between this and the `SPEAKS_TO_THE_CHEMIST` shape. Driven on
    a compiled graph rather than asserted about the source, because what is bound is the question.
    """
    helper = build_langgraph_agent(
        ScriptedChatModel(["done"]),
        audit_sink=NullAuditSink(),
        connectors=[],
        helper=True,
        handoffs=None,
    )
    bound = set(helper.nodes["tools"].bound.tools_by_name)

    assert not [name for name in bound if name.startswith(HANDOFF_PREFIX)]


# --------------------------------------------------------------------------------------------
# The multi-hop scenario — the centre of this file
# --------------------------------------------------------------------------------------------


def _mesh(monkeypatch: Any, scripts: dict[str, list[Any]]) -> Any:
    """A compiled turn graph whose peers replay the given scripts, keyed by peer name."""
    from chemclaw.agent import profiles as profiles_module

    # Registration is process-global and `register_profile` refuses a duplicate, so this is
    # guarded rather than repeated — two tests in this file build a mesh.
    for name in ("evidence-peer", "safety-peer"):
        if name not in profiles_module.registered_profile_names():
            profiles_module.register_profile(
                AgentProfile(
                    name=name,
                    description=f"The {name}.",
                    tool_names=frozenset({"find_notes", "expand_note"}),
                )
            )
    monkeypatch.setattr(
        "chemclaw.core.config.settings.agent_peer_roster",
        "evidence-peer" + os.pathsep + "safety-peer",
    )

    built: dict[str, Any] = {}
    real = build_langgraph_agent

    def _spy(*args: Any, **kwargs: Any) -> Any:
        peer = kwargs.get("peer", "")
        if peer in scripts:
            kwargs["model"] = ScriptedChatModel(scripts[peer])
        graph = real(*args, **kwargs)
        built[peer] = graph
        return graph

    monkeypatch.setattr("chemclaw.agent.turn_graph.build_langgraph_agent", _spy)
    graph = build_turn_graph(
        ScriptedChatModel(["unused"]), audit_sink=NullAuditSink(), connectors=[]
    )
    assert graph is not None, "the roster should have produced a mesh"
    return graph


def test_a_turn_hands_twice_and_the_thread_stays_well_formed(monkeypatch: Any) -> None:
    """**The multi-hop scenario.** default → evidence-peer → safety-peer, in one turn.

    Three things are asserted together because they fail together:

    1. **Control actually moves twice.** Each peer runs, in order, and the last one answers.
    2. **Every tool call has its answer.** A `Command(graph=PARENT)` terminates the inner agent
       without merging its state, so a handoff that returned only its `ToolMessage` would leave the
       parent holding an orphan whose `tool_call_id` matches nothing — and an OpenAI-compatible
       endpoint rejects that thread on the *next* request, which surfaces as a bug in whichever
       agent happens to be holding the conversation by then. Measured both ways before the fix
       existed; `agent/handoff.py` has the two message lists.
    3. **The turn's answer is the last peer's**, not a relayed report. That is the difference
       between a handoff and `task`, and `answer_text` is what a caller reads.
    """
    graph = _mesh(
        monkeypatch,
        {
            "default": [{"name": handoff_tool_name("evidence-peer"), "args": {"reason": "lookup"}}],
            "evidence-peer": [
                {"name": handoff_tool_name("safety-peer"), "args": {"reason": "hazard check"}}
            ],
            "safety-peer": ["No genotoxic alerts on that scaffold."],
        },
    )

    result = asyncio.run(graph.ainvoke(turn_input("is it safe?"), turn_config("multi-hop")))

    messages = result["messages"]
    answered = {call["id"] for m in messages for call in (getattr(m, "tool_calls", None) or [])}
    responded = {m.tool_call_id for m in messages if type(m).__name__ == "ToolMessage"}

    assert result.get("handoffs") == 2, f"expected two hops, got {result.get('handoffs')}"
    assert result.get("active_agent") == "safety-peer"
    assert answered == responded, (
        "every tool call must have its ToolMessage and vice versa — an orphan on either side is a "
        f"thread an OpenAI-compatible endpoint rejects: calls={answered} responses={responded}"
    )
    assert answer_text(result) == "No genotoxic alerts on that scaffold."


def test_the_second_hop_is_bounded_by_the_root_not_by_the_first(monkeypatch: Any) -> None:
    """A chain re-widening after a narrowing is the shape a mesh reaches and a tree cannot.

    `C ⊆ B ⊆ A` is what a pairwise rule promises and it says nothing about a fourth hop. This
    asserts the stronger thing directly: every compiled peer's bound surface is a subset of the
    root's, so the chain is bounded by its first frame however long it gets.
    """
    graph = _mesh(
        monkeypatch,
        {
            "default": [{"name": handoff_tool_name("evidence-peer"), "args": {"reason": "x"}}],
            "evidence-peer": ["done"],
            "safety-peer": ["done"],
        },
    )

    surfaces = {
        name: set(node.bound.nodes["tools"].bound.tools_by_name)
        for name, node in graph.nodes.items()
        if hasattr(node.bound, "nodes")
    }
    root = surfaces["default"]

    for name, surface in surfaces.items():
        capability = {t for t in surface if not t.startswith(HANDOFF_PREFIX)}
        assert capability <= root, (
            f"peer {name!r} binds {sorted(capability - root)}, which the root does not hold — "
            "the root-bound invariant is broken and a handoff has become a widening"
        )


# --------------------------------------------------------------------------------------------
# The stream: a peer is the agent the chemist is talking to, not a subagent
# --------------------------------------------------------------------------------------------


def test_a_peers_answer_reaches_the_chemist_unattributed(monkeypatch: Any) -> None:
    """The regression a wrapper graph creates, which nothing else in this file covers.

    `api/graph_stream` attributes by namespace, and every event a peer produces arrives one frame
    below the stream's root because a peer *is* a node of the turn graph. Under the predicate this
    module shipped with — `bool(namespace)` — a peer's tokens arrive marked `"subagent"`, and
    `api/runner` builds the turn's answer by concatenating the **unattributed** ones. So the mesh
    would have answered every turn with the empty string and the runner would have classified it
    `empty_answer`: a total failure that no assertion about surfaces, handoffs or caps can see.

    Asserted on tokens rather than on `root_depth` directly, because the number is a mechanism and
    the answer reaching the chemist is the property.
    """
    from chemclaw.api.events import TokenEvent
    from chemclaw.api.graph_stream import graph_events, root_depth
    from chemclaw.api.runner_trace import ToolCallTrace

    graph = _mesh(
        monkeypatch,
        {
            "default": [{"name": handoff_tool_name("safety-peer"), "args": {"reason": "hazards"}}],
            "evidence-peer": ["unused"],
            "safety-peer": ["No alerts."],
        },
    )
    assert root_depth(graph) == 1, "a turn graph must declare that its peers are not subagents"

    class _Usage:
        def add(self, *_: Any) -> None:
            return None

    async def _drive() -> list[Any]:
        return [
            event
            async for event in graph_events(
                graph,
                "is it safe?",
                config=turn_config("stream-attribution"),
                trace=ToolCallTrace(),
                on_signal=lambda _s: None,
                usage=_Usage(),
            )
        ]

    events = asyncio.run(_drive())
    answer = "".join(e.text for e in events if isinstance(e, TokenEvent) and not e.agent)

    assert "No alerts." in answer, (
        "the peer holding the conversation answered the chemist, but its tokens arrived attributed "
        "to a subagent, so the runner would build an empty answer from this turn: "
        f"{[(e.agent, e.text) for e in events if isinstance(e, TokenEvent)]}"
    )


def test_a_single_agent_is_not_mistaken_for_a_mesh() -> None:
    """The failure the derived marker actually had, pinned so it cannot return.

    The first version of `root_depth` asked whether the graph declared an `active_agent` channel.
    Every compiled agent in this tree declares it — `ChemclawState` does — so a plain single agent
    measured 1, which marks every token of every shipped turn as a subagent's and answers every
    turn empty. This is the assertion that says the marker is about what the builder built.
    """
    from chemclaw.api.graph_stream import root_depth

    single = build_langgraph_agent(
        ScriptedChatModel(["hi"]), audit_sink=NullAuditSink(), connectors=[]
    )

    assert "active_agent" in single.channels, "the premise: the channel is on every agent"
    assert root_depth(single) == 0, "a single agent is the root of its own stream"


def test_a_bouncing_turn_hits_the_cap_and_still_answers(monkeypatch: Any) -> None:
    """The chain bound, driven on a mesh rather than on the function that computes it.

    `refuse_a_handoff_past_the_cap` is unit-tested above, which proves the arithmetic and nothing
    about whether the tool consults it — and the counter feeding it was wrong in exactly that gap:
    it wrote a constant `1` into a `TurnTotal`, which folds absolute totals, so a two-hop turn
    counted 1 and the cap could never be reached however far a turn bounced. A test that called the
    helper directly passed throughout.

    So this drives a turn that tries to hand over more times than it may, and asserts the two
    things that matter together: the count is the real chain length, and the turn **still answers**.
    Refusing the tool rather than ending the run is `agent/loop_cap.py`'s position — a chemist is
    entitled to the work the turn managed — and a cap that stopped the turn would be a worse
    failure than the bouncing it prevents.
    """
    monkeypatch.setattr("chemclaw.core.config.settings.agent_max_handoffs", 1)
    graph = _mesh(
        monkeypatch,
        {
            # Two hops asked for, one allowed. The second peer's transfer is refused, and it is
            # given another turn to answer with what it has.
            "default": [{"name": handoff_tool_name("evidence-peer"), "args": {"reason": "one"}}],
            "evidence-peer": [
                {"name": handoff_tool_name("safety-peer"), "args": {"reason": "two"}},
                "Answered here instead.",
            ],
            "safety-peer": ["should never run"],
        },
    )

    result = asyncio.run(graph.ainvoke(turn_input("go"), turn_config("capped")))

    assert result.get("handoffs") == 1, (
        "the cap let a second hop through, or the counter is not the chain length: "
        f"{result.get('handoffs')}"
    )
    assert result.get("active_agent") == "evidence-peer", "control must not have moved again"
    assert answer_text(result) == "Answered here instead.", (
        "a capped turn must still answer — ending the run discards the work the turn managed, "
        "which is the failure the refusal exists to avoid"
    )


def test_a_two_hop_turn_announces_each_handoff_exactly_once(monkeypatch: Any) -> None:
    """One event per hop, not one per hop still visible in the thread.

    **This is the hazard the fix for the orphan `ToolMessage` creates**, and the two have to be
    held together. A handoff carries its agent's *whole* message list up, because a
    `Command(graph=PARENT)` otherwise drops the `AIMessage` holding the call — so on the second hop
    the update the stream sees contains the **first** hop's tool call as well. A producer that
    scans an update's messages for transfer calls therefore sees hop one twice, and a reader gets a
    trace claiming the conversation went somewhere it had already been.

    Asserted as a multiset over `(from, to)` rather than a count, so a duplicate is named in the
    failure rather than reported as an arithmetic mismatch.
    """
    from chemclaw.api.events import HandoffEvent
    from chemclaw.api.graph_stream import graph_events
    from chemclaw.api.runner_trace import ToolCallTrace

    graph = _mesh(
        monkeypatch,
        {
            "default": [{"name": handoff_tool_name("evidence-peer"), "args": {"reason": "one"}}],
            "evidence-peer": [
                {"name": handoff_tool_name("safety-peer"), "args": {"reason": "two"}}
            ],
            "safety-peer": ["done"],
        },
    )

    class _Usage:
        def add(self, *_: Any) -> None:
            return None

    async def _drive() -> list[Any]:
        return [
            event
            async for event in graph_events(
                graph,
                "go",
                config=turn_config("handoff-events"),
                trace=ToolCallTrace(),
                on_signal=lambda _s: None,
                usage=_Usage(),
            )
        ]

    hops = [
        (e.from_agent, e.to_agent) for e in asyncio.run(_drive()) if isinstance(e, HandoffEvent)
    ]

    assert hops == [("default", "evidence-peer"), ("evidence-peer", "safety-peer")], (
        "each hop must be announced once, in order — a repeat means the producer is re-reading "
        f"calls that the handoff carried up with the thread: {hops}"
    )


def test_the_root_peer_binds_what_it_would_have_bound_alone(monkeypatch: Any) -> None:
    """Turning the mesh on does not change what the agent the chemist already had can do.

    The root is a peer like any other, and it is compiled through the same builder with its
    `tool_names` set to the surface it was measured to hold — which is a *narrowing* operation
    applied to a profile that may not have been narrowing at all. `AgentProfile.tool_names is None`
    means "does not narrow"; replacing it with an explicit set equal to the current surface is
    identical for this turn and would not be if the set were computed even slightly wrong.

    So this compares the two compiled surfaces directly rather than trusting that argument. The
    handoff tools are the only permitted difference, and they are subtracted by name: they are what
    the mesh *adds*, and everything else must be untouched.
    """
    graph = _mesh(
        monkeypatch,
        {"default": ["done"], "evidence-peer": ["done"], "safety-peer": ["done"]},
    )
    alone = build_langgraph_agent(
        ScriptedChatModel(["done"]),
        audit_sink=NullAuditSink(),
        connectors=[],
    )

    in_mesh = {
        name
        for name in graph.nodes["default"].bound.nodes["tools"].bound.tools_by_name
        if not name.startswith(HANDOFF_PREFIX)
    }
    alone_binds = set(alone.nodes["tools"].bound.tools_by_name)

    assert in_mesh == alone_binds, (
        "the root agent's own surface moved when the mesh was turned on — added "
        f"{sorted(in_mesh - alone_binds)}, lost {sorted(alone_binds - in_mesh)}"
    )
