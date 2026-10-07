"""The turn graph: several peer agents over one conversation, with control moving between them.

Compiles a `StateGraph` whose nodes are `build_langgraph_agent` graphs; a `transfer_to_<peer>`
tool (`agent/handoff.py`) moves the conversation with `Command(goto=…, graph=Command.PARENT)` and
the receiving peer answers the chemist directly. Off by default: with an empty
`agent_peer_roster`, `build_turn_graph` returns `None` and the caller runs the single agent it
always did. It ships off because no measurement shows handing over pays, a mis-routing mesh is
worse than one agent, and the handoff tools' schemas would be charged to every request's prefix.

Invariant (`D-2026-09-19-a-handoff-redistributes-the-turns-authority-it-cannot-extend-it`): every
peer's surface is `root_surface ∩ peer_profile.tool_names`, where the root is the agent that opened
the turn. Bounding each peer by the root, not by the handing agent, bounds a chain of any length.
Every peer carries the full middleware chain (audit, authorization, dry-run, plan gate, caps), and
authorization reads the actor's entitlements, so redistribution cannot escalate.

A peer is not a helper: it keeps the conversation and the acting tools, and its audit rows carry
its name. A peer may spawn a helper; a helper cannot hand over.

Upstream behaviours this relies on, asserted in `tests/test_turn_graph.py`: a tool's
`Command(graph=PARENT)` ends the inner agent without merging its state (so the handoff carries the
messages up); `model_calls` crosses node boundaries, so peers share the turn's caps; and peers use
`checkpointer=False`, the outer graph's checkpointer holding the thread.
"""

import logging
from functools import partial
from typing import Any

from langgraph.graph import END, START, StateGraph

from chemclaw.agent.handoff import handoff_tools, log_the_roster
from chemclaw.agent.langgraph_agent import bindable_capability_tools, build_langgraph_agent
from chemclaw.agent.profile_discovery import ProfileError, load_profiles
from chemclaw.agent.profiles import AgentProfile, get_profile, registered_profile_names
from chemclaw.agent.state import PEER_DEPTH_ATTR, ChemclawState
from chemclaw.agent.stored_skill_tools import StoredSkillTools
from chemclaw.agent.subagents import roster_names
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError

logger = logging.getLogger(__name__)


def peer_roster() -> list[str]:
    """The profile names this deployment wants as peers, in declaration order.

    Order is kept because handoff tools are built in roster order and the prefix must be
    byte-identical
    across processes.

    Returns:
        The configured peer names, empty when the feature is off (the shipped default).
    """
    return settings.peer_roster


def refuse_an_unknown_peer_roster(known: list[str]) -> None:
    """Raise at startup if the peer roster names a profile that does not exist.

    `build_turn_graph` skips an unknown name per turn with a warning; this makes a misspelling fail
    at
    boot instead.

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

    Unions both halves: in-process names via `bindable_capability_tools(profile)` (which sees
    generated launchers and applies the builder's filters, so it predicts what the root binds) and
    the
    open connector tools' names, without which every peer would silently lose its connector tools.

    Args:
        profile: The root profile — the agent a chemist talks to when a turn opens.
        connectors: This turn's already-open connector tools, or `None`.

    Returns:
        The union of the two halves.
    """
    in_process = frozenset(fn.__name__ for fn in bindable_capability_tools(profile))
    reachable = frozenset(tool.name for tool in (connectors or []))
    return in_process | reachable


def _peer_surface(root: frozenset[str], peer: AgentProfile) -> frozenset[str]:
    """What one peer may reach: the root's surface intersected with what its profile names.

    A roster profile with `tool_names is None` narrows to nothing, not to everything, so a peer's
    name
    means something (as for `helper_profile`). Takes `root`, never the handing agent: that is the
    invariant.

    Args:
        root: `root_surface(...)` for this turn.
        peer: The rostered profile.

    Returns:
        The intersection — possibly empty, which the caller reads as "do not offer this peer".
    """
    return root & roster_names(peer)


# The `AgentProfile` fields a peer brings with it, because none of them carries authority; every
# other field comes from the root. Defined by exclusion so a new field added to `AgentProfile`
# defaults to root-derived. `tests/test_turn_graph.py` checks the set against the model's fields and
# that a peer cannot change whether the plan gate applies.
PEER_OWNED_FIELDS: frozenset[str] = frozenset({"name", "instructions", "effort", "model_route"})


def _narrowed(root: frozenset[str] | None, peer: frozenset[str] | None) -> frozenset[str] | None:
    """One allow-list narrowed by another, where `None` means "does not narrow".

    For dimensions where a peer may hold less than the root but never more. An empty set survives.

    Args:
        root: The root profile's allow-list for this dimension.
        peer: The rostered profile's.

    Returns:
        `None` only when neither narrows; otherwise the tightest of the two.
    """
    if root is None:
        return peer
    if peer is None:
        return root
    return root & peer


def _peer_connectors(connectors: list[Any] | None, surface: frozenset[str]) -> list[Any] | None:
    """The turn's open connector tools, narrowed to the ones this peer's surface names.

    The connector half of the invariant: connector tools are already-open `BaseTool`s that
    `tool_names` never sees, so without this a peer would bind every tool the root opened. Uses the
    same
    `surface` that bounds the in-process tools and `refuse_undeclared_writes`, so the halves cannot
    drift.

    Args:
        connectors: The open connector tools for this turn, as the root opened them.
        surface: `_peer_surface(...)` — the root-bounded surface this peer may reach.

    Returns:
        `None` when the caller passed none, else only the tools `surface` names.
    """
    if connectors is None:
        return None
    return [tool for tool in connectors if getattr(tool, "name", "") in surface]


def _peer_profile(root: AgentProfile, peer: AgentProfile, surface: frozenset[str]) -> AgentProfile:
    """The profile a peer is compiled with: the root's authority, the peer's brief.

    Built from the root, overriding only `PEER_OWNED_FIELDS`. Allow-lists (`mcp_server_names`,
    `skill_names`) are narrowed against the root's; `harness_enabled`/`harness_autonomy` are the
    root's
    outright, because they decide whether the plan gate attaches and `api/runner` reads the root's
    answer for the whole turn — a peer must not turn the gate off, nor on under an ungated root.

    Args:
        root: The profile the turn's root agent runs under.
        peer: The rostered profile, for its brief and its model route.
        surface: `_peer_surface(...)` — the root-bounded in-process tool surface.

    Returns:
        A profile carrying the peer's identity and the root's authority.
    """
    brief = {field: getattr(peer, field) for field in PEER_OWNED_FIELDS}
    return root.model_copy(
        update={
            **brief,
            "tool_names": surface,
            "mcp_server_names": _narrowed(root.mcp_server_names, peer.mcp_server_names),
            "skill_names": _narrowed(root.skill_names, peer.skill_names),
        }
    )


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
    stored_skills: StoredSkillTools | None = None,
) -> Any | None:
    """Compile the turn graph for this turn, or `None` when no peer roster is configured.

    `None` rather than a one-node graph, which would change the stream's namespace depth, the
    checkpointed channels and answer attribution for nothing; without peers a deployment runs the
    same
    object as before.

    Args:
        model: As `build_langgraph_agent`. Each peer resolves its own model through its profile.
        profile: The root profile — the agent that opens the turn, and a peer like any other.
        actor: Fallback audit actor, the same for every peer: a handoff does not change the human.
        correlation_id: Fallback correlation id, shared: one turn is one correlation.
        audit_sink: The durable trail, shared; each peer's rows carry its own name in `agent`.
        checkpointer: Where the turn graph's state is persisted. Peers get `checkpointer=False`.
        connectors: This turn's already-open connector tools, shared (each peer gets its narrowed
            subset) without opening new sessions.
        store: This process's memory store. A peer keeps it, since it talks to the chemist.
        stored_skills: What the stored skills tiers declare about tools, as `build_langgraph_agent`
            takes it.

    Returns:
        The compiled turn graph, or `None` when `agent_peer_roster` is empty (the shipped default).
    """
    roster = peer_roster()
    if not roster:
        return None

    root_profile = profile if isinstance(profile, AgentProfile) else get_profile(profile)
    # Fail-soft reload, as `_subagents` does: profile files are globbed lazily and a rostered
    # file-defined profile must be found on the first turn.
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
    # The root is a peer too, so other peers can hand back to it.
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
            # INFO: legitimate when connector bundles are narrower than profiles.
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
        # One peer is no mesh; fall back to the single agent, and warn so the deployment knows the
        # roster
        # did not take effect.
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
                # The peer's brief over the root's authority — see `_peer_profile`.
                profile=_peer_profile(root_profile, peer_profile, surface),
                actor=actor,
                correlation_id=correlation_id,
                audit_sink=audit_sink,
                # Narrowed to this peer's surface; the root's own entry keeps every tool it opened.
                connectors=_peer_connectors(connectors, surface),
                store=store,
                stored_skills=stored_skills,
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
        # Every peer may end the turn; handing over jumps before this edge, so answering is the
        # default.
        graph.add_edge(peer_profile.name, END)

    # The root is `names[0]` by construction — it is appended before the roster loop — which is
    # what `entry_peer_or_root` falls back to for a name no longer in the mesh.
    names = [p.name for p, _ in peers]
    graph.add_conditional_edges(START, partial(entry_peer_or_root, peers=names), names)
    compiled = graph.compile(checkpointer=checkpointer)
    # Tells `api/graph_stream.py` that peers sit one namespace frame down and are not helpers;
    # otherwise the active peer would be treated as a subagent and the answer would come out empty.
    # Stamped here because it cannot be derived from the compiled graph.
    setattr(compiled, PEER_DEPTH_ATTR, 1)
    return compiled


def entry_peer_or_root(state: Any, peers: list[str]) -> str:
    """The peer a turn enters on: whoever holds the conversation, else the root.

    A named function so tests can drive the fallback: an unknown name must not raise.

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
    stored_skills: StoredSkillTools | None = None,
) -> Any:
    """What a turn actually runs on: the mesh when one is configured, the single agent otherwise.

    The one entry point every driver calls (the default `graph_factory` in `api/runner.py`), so none
    has to remember to ask whether peers are configured. The fallback is the old call itself.

    Args:
        model: As `build_langgraph_agent`.
        profile: The root profile — the agent a chemist talks to when a turn opens.
        actor: The human this turn belongs to, unchanged across every peer.
        correlation_id: One turn is one correlation however many agents it passes through.
        audit_sink: The durable trail, shared; each peer's rows carry its own name.
        checkpointer: Where the thread persists — the turn graph's own, or the single agent's.
        connectors: This turn's already-open connector tools.
        store: This process's memory store.
        stored_skills: As `build_langgraph_agent` takes it, forwarded either way.

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
        stored_skills=stored_skills,
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
        stored_skills=stored_skills,
    )
