"""Peer-to-peer handoff: the tools that move a turn from one agent to another.

Delegation (`task`) returns control to the caller; a handoff does not. The receiving agent answers
the chemist directly and keeps the turn until it hands on or answers. A handoff is a tool rather
than a routing node so it crosses `@wrap_tool_call` like every other call: it is audited,
authorized, refused under dry-run (`is_handoff_tool_name` is what `authz.changes_the_conversation`
asks, because `active_agent` is checkpointed) and counted by `repeat_guard`.

`Command(goto=…, graph=Command.PARENT)` navigates in the enclosing graph, `agent/turn_graph.py`,
which computes each peer's surface as the root surface ∩ the peer's profile; these tools name a
`goto` and nothing else, so a handoff redistributes the turn's authority and cannot extend it
(D-2026-09-19-a-handoff-redistributes-the-turns-authority-it-cannot-extend-it). Only the turn graph
passes them to `build_langgraph_agent(handoffs=…)`, so a `task` helper never holds one.

The schema is one `reason` string plus two injected arguments. `tool_call_id` lets the handoff
answer its own call (an unanswered `tool_calls` entry is rejected on the next request). `state` lets
the inner message list travel up with the command: `Command(graph=PARENT)` terminates the inner
agent without merging its state, so without it the `AIMessage` carrying the call never reaches the
parent thread. `add_messages` keys on id, so re-sending is not duplication.
"""

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Annotated, Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command

from chemclaw.agent.profiles import AgentProfile
from chemclaw.agent.subagents import bounded_tool_list
from chemclaw.core.errors import ChemclawError
from chemclaw.core.model_prose import ModelProse
from chemclaw.core.turn_signals import record_handoff

logger = logging.getLogger(__name__)

# The prefix every handoff tool's name carries; the factory, the turn graph and the tests compare
# against it.
HANDOFF_PREFIX = "transfer_to_"

# What the receiving agent is told, appended to its instructions by `agent/turn_graph.py`; kept
# beside the tool description so the two texts cannot drift apart. The handover sentence is
# conditional because the root is a node of the mesh too and may be handed control back.
PEER_BRIEF = ModelProse("""

**You are one of several Chemclaw agents on this conversation.** If another agent handed control
to you, the conversation shows the work it did and the reason it gave — you are continuing it, not
starting again. Answer the chemist directly and in your own voice; there is no supervisor waiting
for a report, and nothing you say is relayed through anybody.

When the work in front of you is squarely another agent's, hand it on with that agent's
`transfer_to_…` tool and say in `reason` what you established and what you are asking it to do. The
chemist sees that you handed over, so do not also narrate it at length. Hand over when the *work*
changes, not when a single question is hard: a handoff costs a model call and loses nothing but
your attention, and a chain that bounces between two agents helps nobody.

**Everything you hold, the agent that opened this turn also held.** A handoff moves the
conversation; it does not widen what this system may do, and there is nothing you can reach by
asking another agent for it that you could not have reached yourself. If a tool you need is absent
from your own surface, say so — do not hand over in the hope that somebody else has it.""")


# Characters a minted handoff tool name may carry; every other one is folded to `_`. An allow-list,
# because profile names are unvalidated file stems. Two names that fold together are a collision,
# which `handoff_tools` refuses.
_TOOL_NAME_CHARS = re.compile(r"[^0-9A-Za-z_]")


def handoff_tool_name(peer: str) -> str:
    """The tool name that hands control to `peer`, e.g. `transfer_to_property_lookup`.

    The one place the spelling is derived; characters outside `_TOOL_NAME_CHARS` fold to `_`.
    """
    return f"{HANDOFF_PREFIX}{_TOOL_NAME_CHARS.sub('_', peer)}"


def is_handoff_tool_name(name: str) -> bool:
    """Whether `name` has the shape `handoff_tool_name` mints.

    A shape rather than a set because `authz.changes_the_conversation` asks it per call with no peer
    roster in hand. The whole shape, not just the prefix: refusals interpolate the name unreduced,
    so a model-invented `transfer_to_x | …` must not match.
    """
    suffix = name.removeprefix(HANDOFF_PREFIX)
    return (
        name.startswith(HANDOFF_PREFIX) and bool(suffix) and _TOOL_NAME_CHARS.search(suffix) is None
    )


def describe_peer(profile: AgentProfile, bound: Iterable[str], menu_tools: int) -> str:
    """What the handing model reads when deciding whether to transfer to `profile`.

    The purpose is the profile's `description`; the capability half is derived from the tools that
    peer actually bound (at most `menu_tools` names), so two peers never read identically. Separate
    from `describe_helper` because a peer takes over the conversation rather than reporting back;
    only the derivation (`subagents.bounded_tool_list`) is shared.
    """
    purpose = (profile.description or "").strip()
    held = bounded_tool_list(bound, menu_tools)
    return (
        f"Hand this conversation to the {profile.name} agent, which then answers the chemist "
        f"directly and keeps control until it answers or hands on. {purpose} It holds: {held}. "
        f"Say in `reason` what you established and what you are asking it to take on — the chemist "
        f"sees that the handover happened, and the agent you hand to reads your reason."
    )


def handoff_tools(
    peers: Sequence[tuple[AgentProfile, frozenset[str]]],
    *,
    current: str,
    menu_tools: int,
    max_handoffs: int,
) -> list[Any]:
    """One `transfer_to_<peer>` tool for every peer `current` may hand to.

    `peers` pairs each profile with the tool names its graph bound (not what the profile names).
    `current` is excluded: a self-handoff is a no-op `goto` that burns model calls. Order follows
    `peers` so the prefix stays stable across processes. `max_handoffs` is passed in so the cap
    reaches the tool from resolved configuration.

    Raises:
        ChemclawError: Two peers share a name, or two names fold to one tool name.
    """
    names = [profile.name for profile, _ in peers]
    if len(set(names)) != len(names):
        raise ChemclawError(
            f"peer roster names a profile twice: {sorted(names)} — one `goto` would name two "
            "nodes, and which one runs would depend on insertion order"
        )
    # The minted names can collide when the profile names do not (`property-lookup` and
    # `property_lookup`): one peer would be unreachable and the provider would reject two functions
    # of one name.
    minted = [handoff_tool_name(name) for name in names]
    if len(set(minted)) != len(minted):
        collided = sorted({name for name in minted if minted.count(name) > 1})
        raise ChemclawError(
            f"peer roster mints one handoff tool for two profiles: {collided} from {sorted(names)} "
            "— `-` and `_` fold together in a tool name, so one peer would be unreachable and the "
            "model would be sent two functions of one name"
        )
    # What this node actually binds, tool name → peer. Arbitration is over these calls only, so an
    # unbound or hallucinated name of the minted shape cannot win and refuse the valid handoff.
    bound_handoffs = {
        handoff_tool_name(profile.name): profile.name
        for profile, _ in peers
        if profile.name != current
    }
    return [
        _one_handoff_tool(profile, bound, menu_tools, max_handoffs, current, bound_handoffs)
        for profile, bound in peers
        if profile.name != current
    ]


def _one_handoff_tool(
    profile: AgentProfile,
    bound: frozenset[str],
    menu_tools: int,
    max_handoffs: int,
    handing: str,
    bound_handoffs: Mapping[str, str],
) -> Any:
    """The tool that hands control to one peer.

    `peer` is closed over rather than an argument, so no call can name a node the turn graph did not
    compile. `bound_handoffs` (tool name → peer) is shared by sibling tools so two calls in one
    message are arbitrated over what exists here.
    """
    peer = profile.name
    description = describe_peer(profile, bound, menu_tools)

    @tool(handoff_tool_name(peer), description=description)
    def _transfer(
        reason: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
        state: Annotated[dict[str, Any], InjectedState],
    ) -> Command[Any] | str:
        # The cap is checked here rather than in a middleware so the hop is declined while the agent
        # keeps control: a refused tool result the model can act on, not an ended turn.
        refusal = refuse_a_handoff_past_the_cap(state, max_handoffs) or refuse_a_later_handoff(
            state, tool_call_id, bound_handoffs
        )
        if refusal:
            return refusal

        # The `ToolMessage` answers the call (an unanswered one is rejected on the next request),
        # and the whole inner message list travels with it because `Command(graph=PARENT)` does not
        # merge the inner state. `graph=Command.PARENT` puts the `goto` in the turn graph, where the
        # node names live.

        # Announced here rather than reconstructed by the stream: the carried message list would
        # replay every earlier hop, and a tool name cannot recover a peer name containing `-`.
        record_handoff(from_agent=handing, to_agent=peer, reason=reason)

        handed = ToolMessage(
            content=f"Handed to {peer}. Reason given: {reason}",
            tool_call_id=tool_call_id,
            name=handoff_tool_name(peer),
        )
        # The winner answers the later handoffs in this message, because ToolNode drops the losers'
        # own results and PatchToolCalls would otherwise fill them with a false "cancelled" message.
        refused = [
            ToolMessage(
                content=later_handoff_refusal(bound_handoffs[call["name"]]),
                tool_call_id=call["id"],
                name=call["name"],
            )
            for call in _bound_handoff_calls(state, tool_call_id, bound_handoffs)[1:]
        ]
        return Command(
            goto=peer,
            graph=Command.PARENT,
            update={
                "messages": [*state.get("messages", []), handed, *refused],
                "active_agent": peer,
                # Per-turn count so a chain can be bounded. Written as the running total, not `1`:
                # `TurnTotal` folds each writer's advance, so a constant delta would count every hop
                # after the first as zero.
                "handoffs": int(state.get("handoffs", 0) or 0) + 1,
            },
        )

    return _transfer


def _bound_handoff_calls(
    state: Any, tool_call_id: str, bound_handoffs: Mapping[str, str]
) -> list[dict[str, Any]]:
    """The bound handoff calls in the assistant message carrying `tool_call_id`, in order.

    Only names in `bound_handoffs` count; returns `[]` when that message has no such call.
    """
    for message in reversed(state.get("messages", [])):
        calls = getattr(message, "tool_calls", None)
        if not calls:
            continue
        handoffs = [call for call in calls if call.get("name") in bound_handoffs]
        if any(call.get("id") == tool_call_id for call in handoffs):
            return handoffs
        return []
    return []


def later_handoff_refusal(peer: str) -> str:
    """The refusal text for a handoff to `peer` that was not the first in its message.

    Shared by the losing tool body and the winner, which answers the losers' calls.
    """
    return (
        f"Only the first handoff in one message is taken, so {peer} was not reached. Hand to "
        "one agent at a time; the agent you handed to can hand on if the work needs it."
    )


def refuse_a_later_handoff(state: Any, tool_call_id: str, bound_handoffs: Mapping[str, str]) -> str:
    """The refusal for a handoff that is not the first bound one in its assistant message, or `""`.

    ToolNode runs every call but applies only the first `Command(graph=PARENT)`, so without this
    both bodies would announce a handoff and only one would happen. The first is kept. Returns `""`
    when no assistant message carrying the call is found.
    """
    calls = _bound_handoff_calls(state, tool_call_id, bound_handoffs)
    if not calls or calls[0].get("id") == tool_call_id:
        return ""
    this = next(call for call in calls if call.get("id") == tool_call_id)
    return later_handoff_refusal(bound_handoffs[this["name"]])


def refuse_a_handoff_past_the_cap(state: Any, limit: int) -> str:
    """The refusal text when the turn has handed over `limit` times (0 disables), or `""`.

    A refusal rather than a jump: the agent keeps control and its other tools, and the text names
    the cap and what to do instead (`agent/refusal_route.py`).
    """
    if limit <= 0:
        return ""
    so_far = int(state.get("handoffs", 0) or 0)
    if so_far < limit:
        return ""
    return (
        f"This turn has already handed between agents {so_far} times, which is the limit "
        f"({limit}). Answer the chemist yourself with what you have, and say which agent you "
        "would have handed to and what you wanted from it — a handoff cannot reach anything you "
        "could not reach here."
    )


def log_the_roster(peers: Sequence[tuple[AgentProfile, frozenset[str]]]) -> None:
    """Log which peers a turn compiled, at INFO, once per turn graph.

    A peer whose intersected surface is empty is dropped; this line says which.
    """
    logger.info(
        "turn graph: %d peer(s) compiled — %s",
        len(peers),
        ", ".join(f"{profile.name} ({len(bound)} tools)" for profile, bound in peers),
    )
