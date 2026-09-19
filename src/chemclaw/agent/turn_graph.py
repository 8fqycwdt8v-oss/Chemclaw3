"""The turn graph: several peer agents over one conversation, with control moving between them.

**What this adds, in one sentence.** `build_langgraph_agent` compiles *one* agent; this compiles a
`StateGraph` whose nodes are several of them, so a `transfer_to_<peer>` tool can move the
conversation from one to another with `Command(goto=…, graph=Command.PARENT)` and the receiving
agent answers the chemist in its own voice. `agent/handoff.py` is the tool half; this is the graph
the `goto` names.

**It is `None` unless a deployment asks for it, and that is the shipped configuration.**
`agent_peer_roster` is empty by default, `build_turn_graph` returns `None`, and every caller falls
back to the single agent it built before — so the default path is not merely equivalent, it is the
same object built by the same call. Three independent reasons, and the first alone would be enough:

1. `D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor` requires a multi-agent arrangement
   to ship disabled until hand-off accuracy is measured against the single-agent baseline. That
   measurement does not exist — `evals/delegation.py` has still never run against a model — and
   this feature does not create it. **Nothing here is evidence that handing over pays.**
2. A mis-routing mesh is worse than the single agent it replaces, which is the same sentence that
   ADR wrote about a mis-routing supervisor and is not weakened by the topology changing.
3. An empty roster binds no handoff tool, so `tests/test_context_floor.py`'s prefix is untouched
   for every existing deployment. A first-party tool's schema is charged against `CEILINGS`
   directly with no allowance to absorb it, so turning this on is a cost a deployment opts into
   with its eyes open rather than one that arrives in a release.

## The invariant, and why it is arithmetic

`D-2026-08-10` invariant 1 says a subagent's surface is an attenuation of **its caller's**. Read
literally that forbids this feature outright: a peer worth handing to holds something its
counterpart does not, so every interesting handoff is a widening relative to the handing agent.

What replaces it is a bound one frame further out, and it is strictly stronger than a chain of
pairwise narrowings:

> Every peer's surface is `root_surface ∩ peer_profile.tool_names`, where `root_surface` is what
> the agent that **opened the turn** binds. No peer holds a name the root did not.

So a chain `A → B → C` of any length is bounded by the root, because each element was intersected
against the root before any of them was compiled — `_peer_surface` below is the only place a peer's
surface is decided and it takes `root` as an argument, never the handing agent. A pairwise rule
does *not* give this: `C ⊆ B ⊆ A` is what it promises, and it says nothing about a fourth hop that
re-widens back toward `A` after two narrowings, which is a shape a mesh can reach and a tree
cannot. The chemist authorized the root surface when the turn opened; a handoff redistributes it.

**This is a narrowing of authority, not a source of it.** Every peer is compiled by
`build_langgraph_agent`, so each carries the whole middleware chain — the audit row, the
authorization gate, the dry-run refusal, the plan gate, the spend cap. The per-call authorization
gate reads the *actor's* entitlements and is untouched by any of this:
`agent/profiles.py`'s rule that a profile "attenuates, it never authorizes" is exactly why
redistributing a profile-shaped surface cannot escalate anything. A peer that could be reached with
a tool the root lacks would be a new actor, which is the thing that ADR's title forbids and which
`_peer_surface` cannot express.

## What a peer is not

It is not a helper. A helper (`task`, `agent/subagents.py`) reads in its own context, returns one
report, and is subtracted down to tools that cannot act — because it works on a brief the chemist
never saw. A peer keeps the conversation: the chemist reads it directly, so it keeps the acting
tools the root held, and the audit trail names which peer made each call
(`build_langgraph_agent(peer=…)`). The two compose — a peer may spawn a helper — and a helper may
not hand over, because `_subagents` passes no `handoffs=` and there is therefore no set anybody
could forget to subtract from.

## Three things that were measured rather than assumed

Each of these was a design decision that a reading of the documentation would have got wrong. The
probe scripts are not kept — the findings are here and the assertions are in
`tests/test_turn_graph.py`, and a script whose finding is asserted is a second copy of it:

- **A tool's `Command(graph=PARENT)` does reach an outer node**, and it terminates the inner agent
  *without merging its state* — so the handoff tool has to carry the inner message list up
  explicitly or the parent thread holds an orphan `ToolMessage`. `agent/handoff.py` has the two
  message lists.
- **Peers share the turn's caps.** `model_calls` is an `UntrackedValue` subclass that crosses the
  node boundary, measured at 2 over two peers rather than 1 each — so a mesh cannot buy itself a
  fresh allowance by handing over, which would have made `agent/loop_cap.py` and
  `agent/spend_cap.py` advisory.
- **`checkpointer=False` and `checkpointer=None` on a peer are observationally identical here** —
  same channels checkpointed (`active_agent`, `messages`), same restore, same message count. So
  `False` is taken, because `D-2026-09-18-a-checkpointer-of-none-is-the-callers-checkpointer`
  measured what `None` costs when it silently inherits, and identical behaviour at lower cost is
  not a close call. The outer graph's checkpointer holds the thread, which is the whole of what
  must survive a turn.
"""

import logging
from functools import partial
from typing import Any

from langgraph.graph import END, START, StateGraph

from chemclaw.agent.chemclaw_agent import _capability_tools
from chemclaw.agent.handoff import handoff_tools, log_the_roster
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.profile_discovery import ProfileError, load_profiles
from chemclaw.agent.profiles import AgentProfile, get_profile, registered_profile_names
from chemclaw.agent.state import PEER_DEPTH_ATTR, ChemclawState
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError

logger = logging.getLogger(__name__)


def peer_roster() -> list[str]:
    """The profile names this deployment wants as peers, in declaration order.

    Order is kept rather than sorted because it is part of the prefix: each peer's handoff tools
    are built in roster order, so two processes on one deployment send byte-identical tool lists —
    which `tests/test_context_floor.py::test_the_prefix_two_sessions_are_sent_is_the_same_bytes`
    asserts of the whole system message and which a set would quietly break.

    Returns:
        The configured peer names, empty when the feature is off (the shipped default).
    """
    return settings.peer_roster


def refuse_an_unknown_peer_roster(known: list[str]) -> None:
    """Raise at startup if the peer roster names a profile that does not exist.

    The loud half of the same split `subagents.refuse_an_unknown_roster` makes, and for the same
    reason: `build_turn_graph` skips an unknown name per turn with a warning, because losing every
    turn to one bad config token is worse than serving a smaller mesh — but a deployment that
    misspells a peer should learn at boot rather than from a mesh that is quietly one agent short.

    Args:
        known: `registered_profile_names()`.

    Raises:
        ChemclawError: The roster names a profile nothing registers.
    """
    unknown = sorted(set(peer_roster()) - set(known))
    if unknown:
        raise ChemclawError(
            f"peer roster names profile(s) that do not exist: {unknown} — known profiles are "
            f"{sorted(known)}. Fix CHEMCLAW_AGENT_PEER_ROSTER or add the profile under "
            "data/profiles/."
        )


def root_surface(profile: AgentProfile, connectors: list[Any] | None) -> frozenset[str]:
    """Every tool name the agent that opens the turn can reach — the bound every peer is cut to.

    **Both halves, because a peer's profile narrows both.** `tool_names` governs the in-process
    half through `_capability_tools`; a connector tool arrives already open as a `BaseTool` and
    `tool_names` never sees it, so its name has to be unioned in here or the intersection below
    would silently drop every connector tool from every peer. That is the same two-halves argument
    `helper_connectors` exists for, and getting it wrong fails in the safe direction (a peer with
    no connectors) which is exactly why it would not have been noticed.

    The in-process names come from `_capability_tools(profile)` rather than from the registry,
    because only that call has seen `_register_generated_tools()` run — read the registry directly
    and a deployment's `run_*` launchers and template launchers are simply absent.

    Args:
        profile: The root profile — the agent a chemist talks to when a turn opens.
        connectors: This turn's already-open connector tools, or `None`.

    Returns:
        The union of the two halves.
    """
    in_process = frozenset(fn.__name__ for fn in _capability_tools(profile))
    reachable = frozenset(tool.name for tool in (connectors or []))
    return in_process | reachable


def _peer_surface(root: frozenset[str], peer: AgentProfile) -> frozenset[str]:
    """What one peer may reach: the root's surface intersected with what its profile names.

    **`&` and never `|`, and a profile naming nothing narrows to nothing.** `tool_names is None`
    means "this profile does not narrow", which is the right reading for a *session* profile and
    the wrong one for a roster entry — there it would hand a named peer the root's entire surface
    under a name promising something specific. `helper_profile` takes the same position for the
    same reason, and the two agreeing is not a coincidence: it is the one rule that makes a roster
    entry's name mean anything.

    This function takes `root` and not the handing agent, which is the whole invariant. See the
    module docstring for why a pairwise rule is weaker rather than equivalent.

    Args:
        root: `root_surface(...)` for this turn.
        peer: The rostered profile.

    Returns:
        The intersection — possibly empty, which the caller reads as "do not offer this peer".
    """
    named = peer.tool_names if peer.tool_names is not None else frozenset()
    return root & named


def build_turn_graph(
    model: Any | None = None,
    *,
    profile: str | AgentProfile | None = None,
    actor: str = "",
    correlation_id: str | None = None,
    audit_sink: Any | None = None,
    checkpointer: Any | None = None,
    connectors: list[Any] | None = None,
    store: Any | None = None,
) -> Any | None:
    """Compile the turn graph for this turn, or `None` when no peer roster is configured.

    **`None` rather than a one-node graph, deliberately.** A turn graph with a single peer would be
    a wrapper that changes the stream's namespace depth, the checkpointed channel set and the
    answer-attribution predicate, in exchange for nothing — and every one of those is a thing that
    breaks quietly. Returning `None` means a deployment that has not asked for peers runs the
    *same* compiled object it ran before this module existed, which is a stronger claim than
    "equivalent" and is what makes this feature's default safe to reason about.

    Args:
        model: As `build_langgraph_agent`. One model is resolved per peer, through each peer's own
            profile, so a peer naming a `model_route` gets it.
        profile: The root profile — the agent that opens the turn, and a peer like any other.
        actor: Fallback audit actor, passed to every peer unchanged. A handoff does not change who
            the human is: `D-2026-08-10` invariant 2 holds across the hop, and it holds because
            every peer is handed the same `actor` rather than because anything re-derives one.
        correlation_id: Fallback correlation id, likewise shared — one turn is one correlation
            however many agents it passes through, which is what makes the trail joinable.
        audit_sink: The durable trail, shared by every peer. Each peer's rows carry its own name in
            `agent`, so one turn's rows say which agent made each call while naming one human.
        checkpointer: Where the *turn graph's* state is persisted. Peers get `checkpointer=False`;
            see the module docstring for what was measured.
        connectors: This turn's already-open connector tools. Shared by every peer, at zero extra
            sockets, for `D-2026-09-15-a-helper-shares-the-session-its-caller-already-opened`'s
            reason — the sessions are already open when this runs, and a peer is the same actor,
            session and correlation id, so the connector's own log is right without anything being
            re-bound.
        store: This process's memory store, passed to peers. Unlike a helper, a peer keeps it: a
            peer is talking to the chemist, so a preference it records is one the chemist asked for.

    Returns:
        The compiled turn graph, or `None` when `agent_peer_roster` is empty — which is the shipped
        default and means "use `build_langgraph_agent` as before".
    """
    roster = peer_roster()
    if not roster:
        return None

    root_profile = profile if isinstance(profile, AgentProfile) else get_profile(profile)
    # Same fail-soft reload `_subagents` does, and for the same reason: the profile files are
    # globbed lazily, so a deployment whose roster names a file-defined profile has to be able to
    # find it on the first turn rather than on the second.
    try:
        if not set(roster) <= set(registered_profile_names()):
            load_profiles()
    except ProfileError:
        logger.warning(
            "peer roster: the profile files could not be loaded, so no turn graph is built; "
            "this turn runs as a single agent",
            exc_info=True,
        )
        return None

    root = root_surface(root_profile, connectors)
    # The root is a peer like any other — it holds the whole root surface by definition, and being
    # in the roster is what lets another peer hand *back* to it. Without this the mesh is one-way
    # and a conversation that moves to the safety agent can never return to the generalist, which
    # is not a mesh so much as a trapdoor.
    peers: list[tuple[AgentProfile, frozenset[str]]] = [(root_profile, root)]
    for name in roster:
        if name == root_profile.name:
            logger.warning(
                "peer roster: %r is the root profile and is already a peer; the repeat is ignored",
                name,
            )
            continue
        if any(name == p.name for p, _ in peers):
            logger.warning("peer roster: %r is named twice; the repeat is ignored", name)
            continue
        try:
            rostered = get_profile(name)
        except ValueError:
            logger.warning(
                "peer roster: no profile named %r, so it is not a peer on this turn; known: %s",
                name,
                sorted(registered_profile_names()),
            )
            continue
        surface = _peer_surface(root, rostered)
        if not surface:
            # INFO rather than WARNING, and it names both causes: this is a legitimate state for a
            # deployment whose connector bundles are narrower than its profiles, exactly as it is
            # for the helper roster.
            logger.info(
                "peer roster: %r is not offered on this turn — nothing survives the intersection "
                "of the root's surface (%s) with what that profile names. Either its connector "
                "bundle is disabled, or the root profile is too narrow for it",
                name,
                root_profile.name,
            )
            continue
        peers.append((rostered, surface))

    if len(peers) < 2:
        # One peer is no mesh. Falling back to the single agent is better than compiling a wrapper
        # whose only effect is to change three quiet things (see this function's docstring), and
        # the WARNING is what stops a deployment believing it turned something on that it did not.
        logger.warning(
            "peer roster: %s named, but nothing survived beside the root, so this turn runs as a "
            "single agent",
            roster,
        )
        return None

    log_the_roster(peers)
    graph: StateGraph[Any, Any, Any, Any] = StateGraph(ChemclawState)
    for peer_profile, surface in peers:
        graph.add_node(
            peer_profile.name,
            build_langgraph_agent(
                model=model,
                # The peer's own instructions and model route, over the root-bounded surface. A
                # `model_copy` rather than a constructor, so a field added to `AgentProfile` next
                # year travels here without this line being remembered.
                profile=peer_profile.model_copy(update={"tool_names": surface}),
                actor=actor,
                correlation_id=correlation_id,
                audit_sink=audit_sink,
                connectors=connectors,
                store=store,
                # Measured identical to `None` and cheaper — see the module docstring. The turn
                # graph's own checkpointer below is what holds the thread.
                checkpointer=False,
                handoffs=handoff_tools(
                    peers,
                    current=peer_profile.name,
                    menu_tools=settings.agent_helper_menu_tools,
                    max_handoffs=settings.agent_max_handoffs,
                ),
                peer=peer_profile.name,
            ),
        )
        # Every peer may end the turn. A peer that hands on never reaches this edge — the
        # `Command(goto=…)` jumps first — so the edge is what makes "answer the chemist" the
        # default and handing over the deliberate act, rather than the other way round.
        graph.add_edge(peer_profile.name, END)

    # The root is `names[0]` by construction — it is appended before the roster loop — which is
    # what `entry_peer_or_root` falls back to for a name no longer in the mesh.
    names = [p.name for p, _ in peers]
    graph.add_conditional_edges(START, partial(entry_peer_or_root, peers=names), names)
    compiled = graph.compile(checkpointer=checkpointer)
    # **The stream needs to know a peer is not a subagent, and only this function knows.** Every
    # event a peer produces arrives one namespace frame down, which `api/graph_stream.py`'s
    # root/non-root test would otherwise read as "below the root" — marking the agent the chemist
    # is talking to as a helper, and answering the turn empty because the runner builds the answer
    # from *unattributed* tokens. The stamp is deliberate rather than derived: `root_depth`'s
    # docstring records that the obvious derivation (an `active_agent` channel) is true of every
    # compiled agent in this tree and measured 1 for a single agent.
    setattr(compiled, PEER_DEPTH_ATTR, 1)
    return compiled


def entry_peer_or_root(state: Any, peers: list[str]) -> str:
    """`_entry_peer` with the fallback resolved — the form the conditional edge actually needs.

    Separate from `_entry_peer` because a conditional edge's function must return a node name that
    exists, and "the root" is knowable only from the peer list. Kept as a named function rather
    than a lambda so `tests/test_turn_graph.py` can drive the fallback directly: an unknown name is
    the case that must not raise, and a lambda inside a builder is a branch no test can reach
    without compiling a whole graph.

    Args:
        state: The turn graph's state.
        peers: Node names, root first.

    Returns:
        `active_agent` when it names a compiled peer, else the root.
    """
    active = str(state.get("active_agent") or "")
    return active if active in peers else peers[0]


def build_turn_agent(
    model: Any | None = None,
    *,
    profile: str | AgentProfile | None = None,
    actor: str = "",
    correlation_id: str | None = None,
    audit_sink: Any | None = None,
    checkpointer: Any | None = None,
    connectors: list[Any] | None = None,
    store: Any | None = None,
) -> Any:
    """What a turn actually runs on: the mesh when one is configured, the single agent otherwise.

    **One entry point rather than a branch at each call site**, and there are four of them. A
    caller that had to ask "is the peer roster set?" is a caller that can forget, and three of the
    four would have had no reason to remember — `cli/chat.py` and
    `durable/template_activities.py` predate this feature entirely. Here the question is asked in
    the one place that knows the answer, and the fallback is not an approximation of the old
    behaviour but literally the old call.

    This is the default `graph_factory` in `api/runner.py`. A test passing its own factory is
    unaffected, which is why the injection point is kept rather than replaced.

    Args:
        model: As `build_langgraph_agent`.
        profile: The root profile — the agent a chemist talks to when a turn opens, and the widest
            surface in any mesh built from it.
        actor: The human this turn belongs to, unchanged across every peer.
        correlation_id: One turn is one correlation however many agents it passes through.
        audit_sink: The durable trail, shared; each peer's rows carry its own name.
        checkpointer: Where the thread persists — the turn graph's own, or the single agent's.
        connectors: This turn's already-open connector tools.
        store: This process's memory store.

    Returns:
        A compiled graph. Construction only, exactly as `build_langgraph_agent` promises.
    """
    mesh = build_turn_graph(
        model,
        profile=profile,
        actor=actor,
        correlation_id=correlation_id,
        audit_sink=audit_sink,
        checkpointer=checkpointer,
        connectors=connectors,
        store=store,
    )
    if mesh is not None:
        return mesh
    return build_langgraph_agent(
        model,
        profile=profile,
        actor=actor,
        correlation_id=correlation_id,
        audit_sink=audit_sink,
        checkpointer=checkpointer,
        connectors=connectors,
        store=store,
    )
