"""The helpers a turn may spawn, and why every one of them is a graph this repository compiled.

`create_deep_agent` makes the `task` tool mandatory (`SubAgentMiddleware` is required middleware),
and left alone it inserts a `general-purpose` subagent assembled from upstream's middleware, with
none of this repository's audit, authorization, dry-run or plan gates. Disabling it through a
`HarnessProfile` fails open when the provider key does not match, so this module instead claims the
name `general-purpose`: upstream skips its default whenever a supplied spec already has that name,
by plain string comparison.

A helper is an attenuation of its caller
(`D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor`). There is one unnamed helper plus
whatever `CHEMCLAW_AGENT_HELPER_ROSTER` names; a rostered helper varies only instructions and model
route, which carry no authority. Fan-out needs no roster: `task` can launch several invocations of
one name.

What a helper does not get: a checkpointer (`checkpointer=False`; `None` would inherit the
caller's), helpers of its own, a store (so no `/memories/` route; its `/scratch/` files cross into
the caller's `files` channel, bounded by `agent_subagent_files_max_chars`), any tool in
`authz.side_effecting_tools()`, or the tools in `SPEAKS_TO_THE_CHEMIST`. It does share the connector
sessions its caller already opened (`helper_connectors`), at no extra connection cost; identity
headers are bound per session and a helper is the same actor.

A helper may run on its own model via `model_route="helper"` in `CHEMCLAW_MODEL_ROUTES`; unset, it
reuses the caller's model.

The narrowing is subtraction and intersection, so a helper holding a tool its caller lacks cannot
arise; there is no profile-to-profile guard to write. `tests/test_subagents.py` compiles the caller
and each helper and asserts the bound tool surfaces form a strict subset.
"""

from collections.abc import Callable, Iterable
from typing import Any

from chemclaw.agent.authz import side_effecting_tools
from chemclaw.agent.profiles import AgentProfile
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.model_prose import ModelProse

#: Tools that change nothing and still reach the person on the other side of the conversation.
#: `ask_clarifying_question` writes a turn signal, so from a helper it would put a question on the
#: chemist's stream from a context the chemist cannot see, with the answer never reaching the
#: helper. `create_exhibit` and `revise_exhibit` write onto the artefact pane the same way;
#: `read_exhibit` reaches nobody and stays. `tests/test_subagents.py` derives this set by scanning
#: for signal writers.
SPEAKS_TO_THE_CHEMIST: frozenset[str] = frozenset(
    {"ask_clarifying_question", "create_exhibit", "revise_exhibit"}
)

#: Upstream's default subagent name, claimed so `create_deep_agent` skips inserting its ungoverned
#: one. Pinned against upstream's constant by `tests/test_upstream_surface.py`.
GENERAL_PURPOSE = "general-purpose"

#: What the helper itself is told, kept beside `general_purpose_helper`'s description of what the
#: caller is told; `tests/test_subagents.py` asserts the two state the same bounds.
HELPER_BRIEF = ModelProse("""

You are a helper spawned by another Chemclaw agent to work one task in your own context window.
You see nothing of the conversation that spawned you beyond the brief you were given, and nothing
you write reaches the chemist except the single report you return — so answer the brief you were
given, completely, and say what you could not establish rather than leaving it out.

**Carry the id of every note behind every claim, as [[wikilinks]] in your report.** Your caller
cannot see anything you read: your reading happens entirely in this context and only your report
crosses back, so a note you found and did not name is a note your caller cannot cite, cannot open,
and has no way to learn exists. An unattributed summary is the one thing your report must never be
— it would reach a chemist as this system's own assertion rather than as the record it came from.

Every tool you hold only reads, and that includes the connector tools. What you cannot do is act:
you cannot start a durable job, record a knowledge note, record an answer, or ask the chemist a
question. The agent that spawned you can do all of those, and the right way to make one happen is
to say so in your report.

You do hold file tools that write, and a file you write is **not** private to you:
it crosses back to the agent that spawned you along with your report, and it stays there — the
conversation you were spawned from can read it again on a later turn, long after you are gone. So
treat anything you put in one as something you are handing over for keeps. Do not describe work as
started, scheduled or arriving later: nothing you can reach starts anything.""")


def governed_roster(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return `specs` unchanged, or raise if any of them is one upstream would assemble itself.

    Every entry must be a `CompiledSubAgent` carrying a `runnable` this repository built. A
    declarative `SubAgent` dict (the shape upstream's documentation shows) would be assembled from
    upstream's middleware, without audit or authorization, and would fail silently in production.

    Raises:
        ChemclawError: A spec carries no `runnable`, so upstream would assemble it.
    """
    for spec in specs:
        if not spec.get("runnable"):
            raise ChemclawError(
                f"subagent {spec.get('name', '<unnamed>')!r} was declared without a compiled "
                "runnable, so `create_deep_agent` would assemble it from upstream's middleware — "
                "with no audit trail, no authorization gate and no plan gate. Compile it with "
                "`build_langgraph_agent(helper=True)` and wrap it in a spec, as "
                "`general_purpose_helper` does."
            )
    return specs


def general_purpose_helper(runnable: Any) -> dict[str, Any]:
    """The unnamed helper's spec, as a `CompiledSubAgent` claiming upstream's default name.

    Compiled rather than declarative, because upstream uses a compiled runnable as provided. The
    description is what the model reads when deciding to delegate: it offers isolation and
    parallelism, and rules out reaching a tool the caller lacks, which is impossible here.

    Args:
        runnable: A graph from `build_langgraph_agent`, carrying the caller's middleware chain and
        narrowed profile, with no helpers of its own.

    Returns:
        The spec to hand `create_deep_agent(subagents=…)`.
    """
    return {
        "name": GENERAL_PURPOSE,
        "description": (
            "A helper that works in its own context window and reports back a single summary. "
            "Spawn one — or several at once — when a task splits into independent pieces whose "
            "intermediate reading would otherwise crowd this conversation: sweeping several "
            "evidence sources in parallel, or working through a long search whose steps do not "
            "matter to the final answer. It reads and it reports, and that is all: it holds the "
            "read-only subset of every tool you hold — your own and the connectors' — so it can "
            "look anything up that you can look up, and it cannot start a durable job, record a "
            "note, record an answer or ask the chemist anything. Do those here, yourself, after "
            "reading what it found. It is never a way to reach something you cannot reach "
            "yourself. Give it the full context in the prompt, since it sees nothing of this "
            "conversation, and say exactly what to return."
        ),
        "runnable": runnable,
    }


def refuse_an_unknown_roster(
    known: Iterable[str], describable: Callable[[str], str | None]
) -> None:
    """Raise if `CHEMCLAW_AGENT_HELPER_ROSTER` names a profile that does not exist.

    The loud half of a split: `_subagents` skips an unknown name with a WARNING so a turn never dies
    over a misspelling, and startup refuses it so the missing helper is noticed. Also refused: a
    profile with no `description:` (the model would see a bare tool list) and `general-purpose` (the
    name this module already claims).

    Args:
        known: The registered profile names, which the caller must have loaded already —
        `registered_profile_names()` holds `default` alone until `load_profiles()` has run.
        describable: How to read a profile's description, injected so this module needs no import of
        the registry it checks. `api/app.py` passes `get_profile`'s.

    Raises:
        ChemclawError: A rostered name resolves to no profile, claims the general-purpose name, or
        names a profile with no description.
    """
    unknown = sorted(set(settings.helper_roster) - set(known))
    if unknown:
        raise ChemclawError(
            f"CHEMCLAW_AGENT_HELPER_ROSTER names unknown agent profile(s) {unknown}, so "
            f"each would be silently absent from the task roster; known: {sorted(known)}"
        )
    if GENERAL_PURPOSE in settings.helper_roster:
        raise ChemclawError(
            f"CHEMCLAW_AGENT_HELPER_ROSTER names {GENERAL_PURPOSE!r}, which is the name this "
            "repository claims to displace upstream's own ungoverned helper. `_subagents` keeps "
            "ours and ignores the entry, so the roster name buys nothing and reads as though it "
            "configured something"
        )
    undescribed = sorted(
        name for name in settings.helper_roster if not (describable(name) or "").strip()
    )
    if undescribed:
        raise ChemclawError(
            f"CHEMCLAW_AGENT_HELPER_ROSTER names agent profile(s) {undescribed} with no "
            "`description:`, so each would reach the model as a bare tool list with no statement "
            "of what it is for — which is the menu "
            "`D-2026-08-12-a-supervisor-that-holds-every-tool-has-no-reason-to-delegate` measured "
            "costing every delegation"
        )


def specialist_override(specialist: AgentProfile, bound: Iterable[str]) -> str:
    """What a named helper is told about the gap between its prompt and its surface.

    A rostered helper carries its specialist's instructions verbatim over a strictly smaller surface
    (everything that acts is subtracted), so the prose may name tools it does not hold. This
    appends, last in the system message so it wins, the tools it actually holds.

    Args:
        specialist: The rostered profile whose instructions this helper carries.
        bound: The capability tool names this helper really holds.

    Returns:
        Text to append to the helper's system message.
    """
    names = ", ".join(sorted(bound))
    return (
        f"\n\n**You are the `{specialist.name}` helper, and your surface is narrower than "
        f"the instructions above describe.** Those instructions were written for an agent "
        f"that can also act; everything that acts has been removed from you. You hold "
        f"exactly these tools and no others: {names}. Where the instructions above tell you "
        f"to call something absent from that list — to record a result, start a job, or ask "
        f"the chemist — do not try it and do not report it as done. Say in your report that "
        f"it is the caller's to do, and name what you would have called."
    )


def roster_names(profile: AgentProfile) -> frozenset[str]:
    """The tool names a **rostered** profile (helper or peer) contributes to an intersection.

    On a session profile `tool_names is None` means "does not narrow"; on a roster entry it would
    hand the whole surface to a name promising less, so here it narrows to nothing. One definition,
    because a copy drifting to the session reading would widen silently.
    """
    return profile.tool_names if profile.tool_names is not None else frozenset()


def bounded_tool_list(bound: Iterable[str], limit: int) -> str:
    """`bound` sorted and joined, the first `limit` names enumerated and the rest counted.

    The capability half of both roster menus (`describe_helper` and `handoff.describe_peer`),
    bounded because the list grows with whatever the sibling fleet serves.
    """
    ordered = sorted(bound)
    shown = ", ".join(ordered[:limit])
    rest = len(ordered) - limit
    return f"{shown}, and {rest} more" if rest > 0 else shown


def describe_helper(profile: AgentProfile, bound: Iterable[str]) -> str:
    """One roster entry's description: a written purpose, then the surface the graph really bound.

    The tool list is derived from the compiled graph, so it cannot go stale and it describes the
    helper rather than the profile it is named for (a helper binds only the non-acting subset).
    Sorted, because this string is in every model call's prefix and must be stable for prompt
    caching.

    Args:
        profile: The rostered profile, whose `description` supplies the written half.
        bound: The tool names this helper's compiled graph actually bound.

    Returns:
        The `description` for this entry's `CompiledSubAgent` spec.
    """
    purpose = (profile.description or "").strip()
    # Bounded by `agent_helper_menu_tools`, because the list grows with the sibling fleet, which the
    # per-tool context ratchet cannot see.
    held = bounded_tool_list(bound, settings.agent_helper_menu_tools)
    return f"{purpose} Reads only, and holds exactly: {held}."


def helper_profile(
    caller: AgentProfile, held: frozenset[str], specialist: AgentProfile | None = None
) -> AgentProfile:
    """The caller's profile, narrowed to what a helper is for and routed to its own model.

    Every dimension not named here (connector selection, effort, and for the unnamed helper the
    instructions) stays the caller's:

    1. **The surface loses everything that acts**: `side_effecting_tools()` (the set the plan gate
       and dry-run refusal already use, held to the registry by `tests/test_authz.py`) and
       `SPEAKS_TO_THE_CHEMIST` are subtracted.
    2. **`model_route` becomes `"helper"`** (or the specialist's own), inert until a deployment maps
       it in `CHEMCLAW_MODEL_ROUTES`.
    3. **The name gains a `-helper` suffix**, so logs and spans say which graph spoke. The profile
       is never registered: it is derived per build.
    4. **The harness is turned off** (see the comment in the body).

    A `specialist` intersects the surface with what it names and replaces the instructions, which
    carry no authority. Every operation removes from what the caller resolved, so a helper can never
    hold a tool its caller does not. Connector tools are narrowed the same way by
    `helper_connectors`.

    Args:
        caller: The resolved profile of the agent that would spawn this helper.
        held: The in-process tool names the caller's build actually resolved, passed in because the
        registry is complete only after `_capability_tools` has registered generated tools.
        specialist: A rostered profile whose tools this helper is further narrowed to and whose
        instructions it carries. `None` is the unnamed helper.

    Returns:
        A profile to build the helper's graph from. Never registered, never cached.
    """
    # `model_copy` so a later field is carried into the helper automatically; the values below are
    # typed as the model declares them, so skipping validation is safe.
    #
    # `harness_enabled=False` is a real narrowing: the inherited `None` would resolve to the
    # deployment default and give the helper a todo list and plan gate, both pure cost here. The
    # gate has nothing to protect: the durable `/memories/` writes it catches cannot happen, because
    # a helper is compiled without a store and so has no `/memories/` route
    # (`tests/test_subagents.py` pins that). And a helper's plan is discarded with its state, read
    # by nobody.
    reading = held - side_effecting_tools() - SPEAKS_TO_THE_CHEMIST
    if specialist is None:
        return caller.model_copy(
            update={
                "name": f"{caller.name}-helper",
                "tool_names": reading,
                "model_route": "helper",
                "harness_enabled": False,
            }
        )
    # `&` and not `|`, and a specialist naming nothing narrows to nothing — see `roster_names`.
    named = roster_names(specialist)
    return caller.model_copy(
        update={
            "name": f"{caller.name}-{specialist.name}",
            "tool_names": reading & named,
            "instructions": specialist.instructions or caller.instructions,
            # The specialist's own route if it declares one, else the shared `helper` key.
            "model_route": specialist.model_route or "helper",
            "harness_enabled": False,
        }
    )


def helper_connectors(
    connectors: list[Any] | None, specialist: AgentProfile | None = None
) -> list[Any] | None:
    """The caller's open connector tools, minus every one that acts.

    The connector half of `helper_profile`'s subtraction, separate because connector tools arrive as
    already-open `BaseTool`s on a different parameter. They are shared, not reopened: the caller
    holds the sessions open for the turn, and concurrent calls over one session are safe.

    The subtraction is `side_effecting_tools()`, which already includes every enabled connector's
    `state_changing` names and jobs. `SPEAKS_TO_THE_CHEMIST` is not subtracted: a connector tool
    cannot write a turn signal. A `specialist` narrows this half too, since a profile's `tool_names`
    spans both halves.

    Args:
        connectors: The caller's already-open connector tools, or `None` for a turn with none.
        specialist: A rostered profile whose `tool_names` also bounds this half. `None` is the
        unnamed helper, which keeps every connector tool that does not act.

    Returns:
        The subset a helper may call, or `None` if the caller had none, so "no out-of-process
        capability" stays one value through the builder.
    """
    if not connectors:
        return None
    acting = side_effecting_tools()
    kept = [tool for tool in connectors if tool.name not in acting]
    if specialist is None:
        return kept
    named = roster_names(specialist)
    return [tool for tool in kept if tool.name in named]
