"""Peer-to-peer handoff: the tools that move a turn from one agent to another.

**What this is, against what the tree already had.** Delegation here has always been
*hierarchical*: `task` invokes a compiled helper inline and the caller regains control because a
tool call is a tool call — measured, a helper reading ~9.8 kB leaves its caller a 57-character
thread. That shape is right for isolation and wrong for a *transfer*: a helper cannot answer the
chemist, cannot continue the conversation, and its caller pays a model call to relay whatever it
found. A handoff is the other thing — control moves, the receiving agent speaks to the chemist
directly, and the turn continues in its voice until it hands on or answers.

**Why a tool rather than a routing node.** The model already decides what to call; a handoff that
is a tool is a decision made in the one place every other decision is made, and — this is the part
that answers `D-2026-08-10`'s stated objection to a swarm — it crosses `@wrap_tool_call` like
everything else. A handoff therefore lands in the audit trail as a row, passes the authorization
gate, is refused under dry-run, and is counted by `repeat_guard`. The objection that ADR raised was
that a swarm loses the central routing node where "every delegation decision is visible in the
trace". It is answered rather than dismissed: the decision is a tool call, tool calls are the
trace, and `reason` below is the deciding agent's own account of why — recorded in the same row.

**Three of those four claims were free and the dry-run refusal was not**, and this sentence made it
for a year before it was true. `tool_authz.dry_run_refusal` asks `authz.side_effecting_call`, which
is a set membership plus a `file_path` test — and `transfer_to_<peer>` is minted *here*, per peer,
so it was in neither half. Measured with `set_dry_run(True)`: refused nothing. That is not cosmetic,
because `active_agent` is a **checkpointed** channel: a turn the chemist marked "do nothing" moved
every later turn onto a different agent, while the refusal text on that same turn said "Nothing was
started". `is_handoff_tool_name` below is what `authz.changes_the_conversation` asks now — beside
that predicate rather than inside it, so the plan gate, which also reads `side_effecting_call`,
does not put every handoff behind a human-approved plan — and
`tests/test_turn_graph.py` drives a whole dry-run turn against a saver rather than asserting the
predicate, because the durable half is the harm.

**The `Command(goto=…, graph=Command.PARENT)` mechanism, and why it forces an outer graph.**
`create_deep_agent` compiles one agent: a model node and a tool node, with no second agent to move
to. `Command.PARENT` navigates in the graph *enclosing* the one the tool ran in, so a handoff needs
that enclosing graph to exist and to have a node per peer. That graph is `agent/turn_graph.py`, and
it is the whole structural cost of this feature — one `StateGraph` whose nodes are compiled
`build_langgraph_agent` graphs.

**What a handoff may never do, and why it cannot.** `agent/turn_graph.py` computes each peer's
surface as *the turn's root surface ∩ that peer's profile*, so no peer holds a name the root did
not. The tools built here name a `goto` and nothing else — they carry no surface, grant nothing,
and cannot be pointed at a graph the turn graph did not compile, because `peers` is the set of
nodes that graph declared. A handoff therefore **redistributes** authority the chemist's turn
already opened with; there is no arrangement of them that extends it. That is the invariant
`D-2026-09-19-a-handoff-redistributes-the-turns-authority-it-cannot-extend-it` replaces
`D-2026-08-10` invariant 1 with, and the reason it is arithmetic rather than a review rule.

**A helper never holds one.** These tools are passed explicitly to `build_langgraph_agent(
handoffs=…)` by the turn graph and by nothing else; `_subagents` does not pass them, so a `task`
helper cannot reach one by construction rather than by subtraction. That distinction matters: the
`SPEAKS_TO_THE_CHEMIST` shape is a name removed from a set, which works only as long as somebody
remembers the name. Here there is no set to forget — a helper is compiled without the argument.

**The tool announces the handoff itself, and that is the third thing measurement moved.** The
event a surface renders (`api/events.HandoffEvent`) is raised from inside `_transfer` through
`core/turn_signals.record_handoff`, not reconstructed by the stream from a completed node's
`tool_calls`. Reconstructing looked like the careful choice — structured data rather than the
`ToolMessage`'s prose — and is wrong twice: the message list this tool carries up (see below) makes
every later update replay every earlier hop, measured at **seven** announcements for a two-hop
turn, and a peer's name is not recoverable from a tool's name, because a tool name cannot hold a
`-` and `transfer_to_evidence_peer` reads back as `evidence_peer` for a profile called
`evidence-peer`. From here both are gone by construction: once per call, with the names the factory
closed over. `record_handoff` is the function `D-2026-08-26` deleted for having no caller, back
with one.

**The schema is deliberately thin.** Every token of a bound tool's schema is a token of prefix, and
`tests/test_context_floor.py` charges first-party tools against `CEILINGS` directly with no
allowance to absorb them. One required `reason` string, two injected arguments the model never
sees, and a description derived from the peer's own profile — nothing else.

**Both injected arguments are load-bearing, and the second was found by driving the thing rather
than by reading upstream's example.** `tool_call_id` is `InjectedToolCallId` because a handoff must
still answer the call it was made by: a `tool_calls` entry with no matching `ToolMessage` is a
malformed thread, and an OpenAI-compatible endpoint rejects the *next* request rather than this
one, which is the failure that looks like a model bug in the *receiving* agent.

`state` is `InjectedState` for a reason that is invisible until measured, and the obvious
implementation gets it wrong. When a tool returns `Command(graph=Command.PARENT)`, LangGraph routes
the update to the enclosing graph and the inner agent terminates **without merging its own
accumulated state**. So the `AIMessage` carrying the very `tool_calls` entry this handoff answers
never reaches the parent thread. Driven on a compiled graph, both ways, in one probe: returning
only the `ToolMessage` produced a four-message turn with **three** messages —
`Human → Tool → AI` — an orphan `ToolMessage` whose `tool_call_id` matches nothing in the thread,
which is precisely the malformed shape the paragraph above says to avoid, arrived at from the other
side. Carrying `state["messages"]` up with the command produced `Human → AI(tool_calls) → Tool →
AI(answer)`, well-formed. Wrapping the compiled agent in a plain function instead of adding it as a
node does **not** fix it; the message list has to travel explicitly. Passing the whole list is safe
rather than duplicative because `messages` reduces with `add_messages`, which keys on message id.
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
from chemclaw.core.turn_signals import record_handoff

logger = logging.getLogger(__name__)

#: The prefix every handoff tool's name carries. One definition, because three places compare
#: against it — the factory that mints the names, the turn graph that must not offer a peer a tool
#: pointing at itself, and `tests/test_turn_graph.py`, which asserts a helper holds none of them.
HANDOFF_PREFIX = "transfer_to_"

#: What the receiving agent is told, appended to its own instructions by `agent/turn_graph.py`.
#: It is here rather than there for the reason `HELPER_BRIEF` sits beside `general_purpose_helper`:
#: `D-2026-08-13` found a supervisor prompt and a `task` description describing two different
#: mechanisms and recorded that the *disagreement* was the defect, so the text a peer reads and the
#: text its counterpart reads are maintained one import apart.
#:
#: **The handover sentence is conditional, because the root is a node of the mesh too.** It opens
#: the conversation, and it was told on turn one that it was "continuing" work an earlier agent had
#: done — a reason to look for, or invent, context nobody produced. It can also be *handed back*
#: control, so a separate brief for non-root nodes would be false the other way round; one sentence
#: that is true of every node costs no parameter and cannot be mis-assigned.
PEER_BRIEF = """

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
from your own surface, say so — do not hand over in the hope that somebody else has it."""


#: Every character a minted handoff tool name may carry, beyond which one is folded to `_`.
#:
#: **An allow-list rather than the one separator this repository happens to use**, which is the fix
#: for a fold that named `-` alone. A profile is a file stem and `agent/profile_discovery.py`
#: validates no charset on it, so `data/profiles/property lookup.yaml` is a legal deployment file
#: and minted `transfer_to_property lookup`; `property.lookup` minted `transfer_to_property.lookup`.
#: Both are invalid tool names by the very argument that folds `-`, and nothing refused either — the
#: provider does, on the request, which surfaces as a model failure on a turn nobody changed.
#:
#: Folding more characters cannot fail open: two names that fold together are a *collision*, and
#: `handoff_tools` refuses a roster that mints one tool for two profiles before anything is bound.
_TOOL_NAME_CHARS = re.compile(r"[^0-9A-Za-z_]")


def handoff_tool_name(peer: str) -> str:
    """The tool name that hands control to `peer`.

    One function so the spelling is derived in every place that needs it rather than formatted
    twice — the turn graph mints the tools, the context ratchet looks for them by name, and the
    helper test asserts their absence. The peer name reaches a model as part of a tool name, so the
    characters a tool name may carry are the constraint, and every character outside
    `_TOOL_NAME_CHARS` is folded to `_` here and nowhere else — see that constant for why it is an
    allow-list and not the single separator this repository's own profile files use.

    Args:
        peer: The profile name of the agent to hand to.

    Returns:
        The tool name, e.g. `transfer_to_property_lookup` for the `property-lookup` profile.
    """
    return f"{HANDOFF_PREFIX}{_TOOL_NAME_CHARS.sub('_', peer)}"


def is_handoff_tool_name(name: str) -> bool:
    """Whether `name` is one of these tools, by the shape `handoff_tool_name` mints.

    **A shape rather than a set, because the set is not knowable where it is asked.**
    `authz.changes_the_conversation` is what must recognise a handoff — a handoff writes the
    checkpointed `active_agent`, so it changes something that outlives the turn — and it is called
    per tool call from inside the middleware chain, with no peer roster in hand. The alternative
    was for `side_effecting_tools()` to enumerate `handoff_tool_name(p)` over the configured peers,
    and that set is `@cache`d for the process's life while `agent_peer_roster` is a setting a test
    and a deployment both rewrite — so it would answer for the roster that happened to be loaded
    first.

    **The whole shape, not the prefix alone**: the suffix must be what `handoff_tool_name` can
    mint (`_TOOL_NAME_CHARS`' complement). The refusals reached past this predicate interpolate the
    name unreduced on the premise that it is one this repository owns, and ToolNode runs the
    middleware chain for an *unregistered* name too — so a bare prefix test let a model-invented
    `transfer_to_x | sanctioned path: …` write a forged routing footer into the refusal it reads
    and into the audit row. A name of the minted shape that no peer carries still reaches the same
    refusal, which is the correct answer to a name that does not exist.
    """
    suffix = name.removeprefix(HANDOFF_PREFIX)
    return (
        name.startswith(HANDOFF_PREFIX) and bool(suffix) and _TOOL_NAME_CHARS.search(suffix) is None
    )


def describe_peer(profile: AgentProfile, bound: Iterable[str], menu_tools: int) -> str:
    """What the *handing* model reads when deciding whether to transfer to `profile`.

    **Two halves, and only one of them is written by a person.** The purpose comes from the
    profile's own `description`; the capability half is derived from the surface that peer's graph
    actually *bound* on this turn. That derivation is `describe_helper`'s and the reason is
    `D-2026-08-12`'s identical-menu defect — five roster entries whose descriptions were
    indistinguishable, so the model had no basis on which to pick one and the measurement that
    followed was about nothing.

    It is a separate function from `describe_helper` rather than a shared one, and that is a
    deliberate refusal of the obvious DRY move. The two texts describe *different acts*: a helper
    entry says "this thing will read and report back to you", a peer entry says "this thing will
    take over the conversation". A shared function would have to take a flag naming which sentence
    to emit, which is two functions wearing one name — and the wording is exactly where
    `D-2026-08-13`'s defect lived. What is shared is the derivation (`subagents.bounded_tool_list`),
    not the prose.

    Args:
        profile: The peer's profile — `description` is the written half.
        bound: The tool names that peer's compiled graph binds on this turn.
        menu_tools: How many names to enumerate before saying "and N more"
            (`settings.agent_helper_menu_tools`).

    Returns:
        One paragraph for the handing model's tool description.
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

    **`current` is excluded, and that is not politeness.** A tool handing an agent to itself is a
    `goto` onto the node already running, which LangGraph executes — so the model gets a plausible
    no-op that burns a model call, and two of them in a row is a turn that never terminates except
    through the loop cap. The exclusion is here rather than in the prompt because a prompt is a
    request and this is arithmetic.

    Args:
        peers: `(profile, bound_tool_names)` for every peer the turn graph compiled, including
            `current`. The bound names are what `describe_peer` derives the capability half from,
            so they must be the surface that peer's graph *bound* rather than what its profile
            names — the two differ whenever a bundle is disabled.
        current: The profile name of the agent these tools are being built for.
        menu_tools: How many tool names each description enumerates.
        max_handoffs: `settings.agent_max_handoffs` — how many hops one turn may make before
            these tools refuse. Passed in rather than read inside the closure so a test can drive
            the cap without patching settings, and so the number reaches the tool from the one
            place configuration is resolved.

    Returns:
        The tools to pass to `build_langgraph_agent(handoffs=…)`, one per peer other than
        `current`, in the order `peers` gives — which the turn graph keeps stable so that two
        processes send the same prefix (`tests/test_context_floor.py` asserts that of the whole
        system message, and an unsorted roster would break it for no gain).

    Raises:
        ChemclawError: Two peers share a name, so one `goto` would name two nodes. The turn graph
            cannot produce this and a caller assembling `peers` by hand can, which is exactly the
            case worth refusing at build time rather than debugging as a routing bug.
    """
    names = [profile.name for profile, _ in peers]
    if len(set(names)) != len(names):
        raise ChemclawError(
            f"peer roster names a profile twice: {sorted(names)} — one `goto` would name two "
            "nodes, and which one runs would depend on insertion order"
        )
    # **And the same question one fold later, because the *minted* names can collide when the
    # profile names do not.** `handoff_tool_name` folds `-` to `_` (a tool name cannot carry `-`,
    # and `data/profiles/property-lookup.yaml` uses one), so `property-lookup` and
    # `property_lookup` are two legal profile names — both are file stems — that mint one tool.
    # Driven: `tools_by_name` keeps whichever came last, so the other peer becomes a node no tool
    # can reach, and the list sent to the provider carries **two functions of one name**, which an
    # OpenAI-compatible endpoint rejects outright. Neither failure is an authority widening — both
    # peers are root-bounded — but the unreachable node may be the wider of the two, and this is
    # exactly the routing ambiguity the check above exists to prevent, in the form it does not see.
    minted = [handoff_tool_name(name) for name in names]
    if len(set(minted)) != len(minted):
        collided = sorted({name for name in minted if minted.count(name) > 1})
        raise ChemclawError(
            f"peer roster mints one handoff tool for two profiles: {collided} from {sorted(names)} "
            "— `-` and `_` fold together in a tool name, so one peer would be unreachable and the "
            "model would be sent two functions of one name"
        )
    # What this node actually binds, keyed tool name → peer. The arbitration below is over *these*
    # calls rather than over every name of the minted shape: a shape-only filter let an earlier
    # `transfer_to_<self>` (never bound — see the filter) or a hallucinated peer win, so the valid
    # handoff after it was refused as "not the first" while the winner hit an unknown-tool error,
    # and no hop happened with the model told something false.
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

    The closure is the mechanism: `peer` is captured rather than passed as an argument the model
    fills in, so there is no shape of call that names a node the turn graph did not compile. A
    single `transfer_to(peer=…)` tool with an enum argument would be one schema instead of N and
    was rejected for that reason — the enum's values would be bare names, which is
    `D-2026-08-12`'s identical-menu defect rebuilt in a smaller space.

    `bound_handoffs` is every handoff tool this node binds (tool name → peer), shared by the
    sibling tools so two calls in one message are arbitrated over what actually exists here.
    """
    peer = profile.name
    description = describe_peer(profile, bound, menu_tools)

    @tool(handoff_tool_name(peer), description=description)
    def _transfer(
        reason: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
        state: Annotated[dict[str, Any], InjectedState],
    ) -> Command[Any] | str:
        # **The cap is checked here rather than in a middleware**, because this is the one place
        # that can decline the hop while leaving the agent holding control. A `before_model` jump
        # would end the turn, which `agent/loop_cap.py` argues at length is the wrong answer to a
        # bound: a chemist is entitled to the work the turn managed. Returning a string makes it
        # an ordinary refused tool result the model reads and can act on.
        refusal = refuse_a_handoff_past_the_cap(state, max_handoffs) or refuse_a_later_handoff(
            state, tool_call_id, bound_handoffs
        )
        if refusal:
            return refusal

        # The `ToolMessage` is not optional and is not decoration: the assistant message that
        # called this tool carries a `tool_calls` entry, and a thread whose tool call has no
        # response is rejected by an OpenAI-compatible endpoint on the *next* request — after the
        # handoff has already happened, so the failure surfaces in the receiving agent and reads
        # as its bug.
        #
        # **And the whole inner message list travels with it**, not just the `ToolMessage`. A
        # `Command(graph=PARENT)` terminates the inner agent without merging its state, so the
        # `AIMessage` holding this call would otherwise never reach the parent and the thread
        # would carry the orphan the paragraph above exists to prevent. Measured both ways; the
        # module docstring has the two message lists. `add_messages` keys on id, so re-sending
        # what the parent already has is a no-op rather than a duplication.
        #
        # `graph=Command.PARENT` is what makes this a handoff rather than a jump inside the
        # handing agent's own graph: the node names live in the turn graph, one level up.

        # **Announced from here rather than reconstructed by the stream**, for the two reasons
        # `core/turn_signals.HandoffSignal` records: the message list this carries up makes an
        # update-scanner replay every earlier hop, and a tool name cannot hold a `-`, so a peer
        # called `evidence-peer` is unrecoverable from `transfer_to_evidence_peer`. Both names
        # here are closed over at build time and are the profiles' own.
        record_handoff(from_agent=handing, to_agent=peer, reason=reason)

        handed = ToolMessage(
            content=f"Handed to {peer}. Reason given: {reason}",
            tool_call_id=tool_call_id,
            name=handoff_tool_name(peer),
        )
        # **The later handoffs in this message are answered here, by the winner**, because their own
        # refusal strings never reach the thread: ToolNode sends this `Command` up and drops the
        # losing calls' local results, so deepagents' PatchToolCalls filled them with "was
        # cancelled - another message came in", which is false. `add_messages` keys on id and the
        # patch only touches unanswered calls, so these first-party refusals are what the thread
        # keeps — worded by the one function the losing body also returns.
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
                # Counted so a chain can be bounded. The count is the *turn's* rather than the
                # thread's (`ChemclawState.handoffs` is untracked), because a conversation that
                # legitimately moves between agents over twenty turns is not a runaway and a turn
                # that bounces four times is.
                #
                # **The running total, not `1`**, because `TurnTotal` folds `base + (value - base)`
                # over each writer's *advance*. A constant delta is read as no advance at all on
                # every hop after the first: written as `1`, a two-hop turn counted **1**, so the
                # cap was unreachable and `tests/test_turn_graph.py`'s multi-hop assertion is what
                # found it. `ChemclawState.model_calls` and `billed_tokens` write absolute totals
                # for the same reason, and that channel's own docstring says a delta "would be read
                # as a walk backwards and contribute 0" — which is exactly what happened.
                "handoffs": int(state.get("handoffs", 0) or 0) + 1,
            },
        )

    return _transfer


def _bound_handoff_calls(
    state: Any, tool_call_id: str, bound_handoffs: Mapping[str, str]
) -> list[dict[str, Any]]:
    """The bound handoff calls in the assistant message carrying `tool_call_id`, in order.

    Read from the last message that has `tool_calls`, which is the one ToolNode is executing. Only
    names in `bound_handoffs` count: a call of the minted shape that this node does not bind gets
    ToolNode's unknown-tool error and must not take part in the arbitration.

    Returns:
        Those calls, or `[]` when that message does not carry `tool_call_id` as a bound handoff —
        there is then nothing to arbitrate against.
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

    One function because two places write it: the losing tool body returns it, and the winning
    body answers the losing calls with it, since ToolNode drops the losers' own results.
    """
    return (
        f"Only the first handoff in one message is taken, so {peer} was not reached. Hand to "
        "one agent at a time; the agent you handed to can hand on if the work needs it."
    )


def refuse_a_later_handoff(state: Any, tool_call_id: str, bound_handoffs: Mapping[str, str]) -> str:
    """The refusal for a handoff that is not the first one in its assistant message, or `""`.

    **The arbitration between two `transfer_to_…` calls in one message is decided here, first
    party, rather than left to the tool node.** ToolNode runs every call in the message and applies
    only the first `Command(graph=PARENT)`, cancelling the rest — so before this, both tool bodies
    ran and both called `record_handoff`: driven on the compiled mesh, the chemist's stream carried
    a `HandoffEvent` to `safety-peer` that never happened, beside the one to `evidence-peer` that
    did. The cap cannot catch it either: both calls read the same pre-batch `handoffs`.

    The first is the one kept, for `state.LastPeer`'s reason: it is the transfer the model asked
    for with the most context behind it. This string is what suppresses the losing body's
    `record_handoff`; what the *thread* reads for that call is written by the winner (see
    `_one_handoff_tool`), because ToolNode drops a loser's own result.

    Args:
        state: The handing agent's graph state; its last assistant message is the one whose
            `tool_calls` carry this call.
        tool_call_id: This call's id.
        bound_handoffs: The handoff tools this node binds, tool name → peer name. Only these take
            part, so an unbound name of the same shape cannot win.

    Returns:
        The refusal, or `""` when this is the first bound handoff in its message — or when no
        assistant message carrying it can be found, where there is nothing to arbitrate against.
    """
    calls = _bound_handoff_calls(state, tool_call_id, bound_handoffs)
    if not calls or calls[0].get("id") == tool_call_id:
        return ""
    this = next(call for call in calls if call.get("id") == tool_call_id)
    return later_handoff_refusal(bound_handoffs[this["name"]])


def refuse_a_handoff_past_the_cap(state: Any, limit: int) -> str:
    """The refusal text when a turn has handed over `limit` times, or `""` when it has not.

    **A refusal rather than a jump, for `agent/loop_cap.py`'s reason.** Ending the turn would
    discard whatever the conversation had produced; refusing the *tool* leaves the agent holding
    control, having been told why, with every other tool it had still in hand — so a turn that
    bounces hits a wall and answers, rather than stopping mid-sentence.

    It names the cap and what to do instead, which is `agent/refusal_route.py`'s one-shape rule:
    a refusal that names only the wall supports exactly the two moves that do not help.

    Args:
        state: The turn's graph state, read for `handoffs`.
        limit: `settings.agent_max_handoffs`; 0 disables the cap.

    Returns:
        The refusal to return as the tool's result, or `""` if the handoff may proceed.
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
    """Record which peers a turn compiled, at INFO, once per turn graph.

    A deployment that names four peers and gets two — because two profiles' surfaces were empty
    after the intersection — otherwise learns that from behaviour. The turn graph drops such an
    entry for the same reason `_subagents` drops a helper whose surface is empty, and this is the
    line that says which.
    """
    logger.info(
        "turn graph: %d peer(s) compiled — %s",
        len(peers),
        ", ".join(f"{profile.name} ({len(bound)} tools)" for profile, bound in peers),
    )
