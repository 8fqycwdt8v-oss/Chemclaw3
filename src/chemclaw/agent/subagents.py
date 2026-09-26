"""The helpers a turn may spawn, and why every one of them is a graph this repository compiled.

**Not optional, which is why this module exists at all.** Adopting `create_deep_agent` makes the
`task` tool mandatory: `SubAgentMiddleware` is in upstream's `_REQUIRED_MIDDLEWARE`, and
`_apply_excluded_middleware` *raises* rather than let a `HarnessProfile` strip it. So the question
is never whether a turn can spawn a helper — it is what the helper reaches. Left alone,
`create_deep_agent` auto-inserts its own `general-purpose` subagent holding every tool the parent
holds, assembled from upstream's middleware list, which carries none of this repository's audit
trail, authorization gate, dry-run refusal or plan gate.
`D-2026-08-13-a-subagent-is-spawned-for-isolation-not-for-a-tool-it-lacks` recorded the consequence:
"nothing would fail while it did".

**Two suppressions exist and only one of them is reliable.**
`GeneralPurposeSubagentProfile(enabled=False)` reaches upstream through a `HarnessProfile` resolved
by `provider:identifier`, and on a key miss the profile is *silently* not applied — measured, with
one warning logged and the default subagent left in place. A security narrowing that depends on a
registry key matching a model's self-reported provider fails open on a model swap, so it is not one.

The other suppression is the name. `create_deep_agent` skips its default whenever a caller-supplied
spec already claims `GENERAL_PURPOSE_SUBAGENT["name"]` — a plain string comparison against the specs
it was handed, with no registry and no model identity in the path. Measured across three arms:
claiming the name replaced upstream's entry in the `task` roster; claiming a *different* name left
upstream's in place beside ours; the default arm had it alone. So this module claims the name, and
what a chemist's agent reaches through `task` is a graph `build_langgraph_agent` compiled.

**One unnamed helper, plus whatever `CHEMCLAW_AGENT_HELPER_ROSTER` names**
(`D-2026-09-16-a-roster-varies-the-two-dimensions-that-carry-no-authority`). This paragraph used to
say "one helper rather than five", ending "a second name gets added when a measurement asks for
one" — and the measurement still has not asked. What arrived instead is a *requirement*: expert
selection has to happen automatically, with the chemist configuring nothing, and a session profile
picked by a person is the opposite of that. The ADR is explicit that this is an honest reason to
build a roster and a dishonest one to claim it pays.

What survives that paragraph unchanged is the reason a roster is not a *partition*. `D-2026-08-13`
framed five tool surfaces; M12 measured the framing before it delegating 2 of 15, a later run put
both framings at 14/15 on the same corpus, and neither number is a deployment's rate. Fan-out still
needs no partition — `task` tells the model to launch several agents concurrently when their tasks
are independent, so a parallel evidence sweep is N invocations of one name, named or not. What a
roster adds is a *view*: three prompts over three surfaces, each an intersection of the caller's.

**What a helper does not inherit, and where each bound is enforced.** No checkpointer —
`checkpointer=False`, which is not the same as passing nothing, and for a while this line described
a bound that was not in force. Upstream's contract is that a helper sees only the prompt it was
given and returns one report, but `None` is how a LangGraph subgraph asks to *inherit* its parent's
saver, so every helper checkpointed its own thread onto its caller's under a `tools:<uuid>`
namespace (`D-2026-09-18-a-checkpointer-of-none-is-the-callers-checkpointer`, which measured it at
98% of what a spawn cost). No
helpers of its own, which is the recursion guard `build_langgraph_agent(helper=…)` carries. No
store, so there is no `/memories/` route and nothing it writes reaches the knowledge graph or the
memory tiers — which is **not** the same as "nothing it writes outlives the turn", the claim this
sentence used to make. A helper's `/scratch/` file crosses into its caller's `files` channel and is
checkpointed under the caller's thread, so a later turn can read it back
(`D-2026-09-04-a-helpers-file-crosses-back-and-stays`); what bounds it is
`agent_subagent_files_max_chars`, not its lifetime.

**It does reach a connector, since
`D-2026-09-15-a-helper-shares-the-session-its-caller-already-opened`, and for two rounds it did
not.** The first reason given was a concurrency bound — two readers of one MCP tool object deadlock
— and `D-2026-08-29-a-helper-reaches-no-connector-because-of-the-lifecycle-not-the-deadlock`
corrected it to a lifecycle bound without driving it. Driven, the concurrency claim is false: over
**one** open `HeldConnectorSession`, four concurrent 1.88 s `pyexec.run_python` calls finish in
1.99 s with no error and the server's own log shows them overlapping, 32 fast `props` calls finish
in 348 ms, and a call that fails mid-flight beside another damages neither it nor the session. The
second half of that bound — misattribution in the connector's log — does not reach a helper either:
`core/call_identity.py` binds the headers from the ambient context when the *session* opens, and a
helper is the same actor, session and correlation id by
`D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor`, so the headers are right.

The lifecycle argument survives intact and simply never applied to this shape. It is about a helper
opening sessions of its **own**, which would have to happen eagerly on every turn because
`build_langgraph_agent` is synchronous and the roster is frozen per compiled graph. Sharing needs
none of that: the caller's sessions are already open when `_subagents` runs, so `helper_connectors`
hands the same objects down at **zero** extra sockets, handshakes or server-side session state —
which is better than either shape that ADR weighed, and is why it is the one that shipped.

**And, since `D-2026-08-29-a-helper-is-cheaper-and-narrower-than-its-caller`, nothing that changes
anything.** `helper_profile` is what made that true, and it was written because the surface and the
story had drifted apart: the `task` description said isolation and parallel reading while the helper
held every in-process tool its caller did — measured against the live registry, 54 of them,
including nine `run_*` durable job launchers, `record_knowledge_note`,
`start_optimization_campaign` and `request_external_input`. So a helper spawned on a brief the
*model* wrote could open a pull
request against the knowledge graph, start a CREST search costing hours of pod time, and post a
durable question into a person's inbox, all from a context the chemist never sees. Every gate held —
the audit row, the authorization decision, the plan gate and the spend cap are the same chain, which
is why this was a design defect rather than a hole — but "a helper reads, it does not act" was a
sentence in a docstring rather than a property of the graph.

**The narrowing is derived, not listed.** It subtracts `authz.side_effecting_tools()`, the
partition this repository already maintains and already tests, so a connector or a template added
next year is outside a helper's reach on the day it is enabled rather than the day somebody
remembers this module. The one name subtracted beyond it is `ask_clarifying_question`, subtracted
for a reason the side-effect classification cannot see: it is a true read of nothing, but it writes
a turn signal, so a helper calling it puts a question on the *chemist's* stream from a context the
chemist cannot see, and then never receives the answer.

**A helper may also run on its own model.** Its profile carries `model_route="helper"`, so
`CHEMCLAW_MODEL_ROUTES='{"helper": "<a smaller model>"}'` is the whole of the cost lever and there
is no code change in it. Unset — the shipped default — the helper reuses the model its caller
already built, which is what every helper has always done. This is the one dimension where a helper
is deliberately *not* an attenuation of its caller: a model carries no tools and therefore no
authority, so the invariant `AgentProfile` is arranged around has nothing to say about it.

Only some of this is a build-time assertion, and the split is deliberate rather than an omission.
For the *unnamed* helper, which is built from its caller's own profile, any check comparing the two
profiles — the shape `reject_widening` had before the specialist team was deleted — compares a value
with itself for every dimension the derivation leaves alone. **A rostered helper is the case where
that stopped being true**: `tool_names` and `instructions` genuinely differ, so a profile-to-profile
check would now have content. It is still not written, and the reason is the same one that deleted
`reject_widening` — the property is guaranteed by *arithmetic* rather than by a rule that could be
violated. `helper_profile` reaches a specialist's surface by intersecting with `held`, so a helper
holding a tool its caller lacks is not a case a guard would catch; it is a case that cannot arise
unless intersection stops meaning intersection.

The invariants are therefore asserted where they can be observed, for named and unnamed alike:
`tests/test_subagents.py` compiles the caller *and* each helper and compares the tool surfaces the
graphs really bound. That is the difference between enforcing an attenuation and restating it — and
the subtraction above is what makes the comparison a *strict* subset rather than an equality nobody
could fail.
"""

from collections.abc import Callable, Iterable
from typing import Any

from chemclaw.agent.authz import side_effecting_tools
from chemclaw.agent.profiles import AgentProfile
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError

#: Tools that change nothing and still reach the person on the other side of the conversation.
#:
#: `side_effecting_tools()` answers "does this change something outside the turn", which is the
#: right question for the plan gate and the dry-run refusal and the wrong one here.
#: `ask_clarifying_question` writes no row and starts no workflow — it is correctly classified as a
#: read — but it records a turn signal, and a turn signal is delivered on the *turn's* stream. A
#: helper runs inside its caller's turn, so a question it asks appears to the chemist as though
#: the agent they are talking to had asked it, while the answer comes back to a conversation the
#: helper cannot see and has already left. `tests/test_subagents.py` derives this set by scanning
#: for the
#: signal writers rather than trusting the constant, so a second tool of this shape fails the suite
#: instead of quietly reaching a helper.
SPEAKS_TO_THE_CHEMIST: frozenset[str] = frozenset({"ask_clarifying_question"})

#: Upstream's own default subagent name, claimed by this repository so that `create_deep_agent`
#: skips inserting its ungoverned one. One definition, because three places compare against it
#: now — the spec that claims it, the roster loop that will not let an entry take it, and the
#: startup refusal. `tests/test_upstream_surface.py` pins it against upstream's own constant.
GENERAL_PURPOSE = "general-purpose"

#: What the helper itself is told, beside `general_purpose_helper`'s description of what the
#: *caller* is told. The two texts live in one module deliberately: `D-2026-08-13` found the
#: supervisor prompt and the `task` description describing two different mechanisms, and recorded
#: that the disagreement was the real defect. `tests/test_subagents.py` asserts they still agree —
#: on the bounds each states, not on wording, because wording that must match cannot be improved.
HELPER_BRIEF = """

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
started, scheduled or arriving later: nothing you can reach starts anything."""


def governed_roster(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return `specs` unchanged, or raise if any of them is one upstream would assemble itself.

    **The one build-time assertion this module can honestly make**, and the reason it can is that
    it is not about attenuation. The module docstring explains why comparing a helper's profile with
    its caller's could never turn red under a one-name roster; this checks something else entirely —
    that every entry is a `CompiledSubAgent` carrying a `runnable` *we* built, rather than a
    declarative `SubAgent` upstream would assemble from `spec["middleware"]` alone.

    That distinction is the whole governance boundary. `create_deep_agent` uses a compiled runnable
    as provided, but builds a declarative spec itself — and the middleware it uses is upstream's,
    which carries none of this repository's audit trail, authorization gate, dry-run refusal or plan
    gate. `D-2026-08-13-a-subagent-is-spawned-for-isolation-not-for-a-tool-it-lacks` recorded what
    that looks like from outside: **"nothing would fail while it did."**

    Today every entry `_subagents` builds carries a `runnable` this module wrapped, so this raises
    for nobody. It exists because the *next* helper is the risk: a second name added as a
    dict — which is how upstream's own documentation shows subagents being declared, and the
    obvious thing to write — is ungoverned and silent, and the failure appears in production as a
    tool call with no audit row rather than as a red test. A guard that costs one comparison is
    cheaper than the review that would otherwise have to catch it.

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

    A `CompiledSubAgent` — `{name, description, runnable}` — rather than a declarative `SubAgent`,
    because upstream uses a compiled runnable *as provided* while it assembles a declarative spec's
    middleware itself. Handing it a graph this repository compiled is the only arrangement in which
    "the helper is governed" is a property of this code rather than of somebody else's assembly
    order.

    The description is what the model reads when deciding whether to delegate, so it states the
    reason to spawn that survived the M12/M13 measurements — isolation and parallelism — and closes
    off the one that did not, reaching a tool the caller lacks, which is impossible here by
    construction. It also says the connectors are not reachable from inside, because a model that
    tries and is refused has spent a turn learning what one sentence could have told it.

    Args:
        runnable: A graph from `build_langgraph_agent`, carrying the same middleware chain and the
            same profile as its caller, with no connector tools and no helpers of its own.

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

    **The loud half of a deliberate split.** `_subagents` skips an unknown name with a WARNING,
    because a turn must not die because a deployment misspelled a roster entry — the cost of
    skipping is a missing helper, which is delegation lost and never authority gained. But a
    capability nobody is told is missing is one nobody restores, so the same typo is refused where
    a deployment's settings first meet the profiles they name: startup.

    Here rather than in `api/app.py` so it is reachable without driving a lifespan, and so the rule
    sits beside the roster it is about. A function rather than a validator target because the
    roster is a *deployment's* setting: `make skill-validate` checks what this repository ships,
    and no CI gate can see the environment a pod is started with.

    **Three things, because a name that resolves is not yet a name worth offering.** A profile with
    no `description:` reaches the model as a bare tool list — the menu `D-2026-08-12` measured
    costing every delegation — and `general-purpose` is the name this repository claims to displace
    upstream's ungoverned helper, so rostering it configures nothing while reading as though it
    did. Two of the six shipped profiles are rosterable today and carry no description, so this is
    a live case rather than a hypothetical.

    Args:
        known: The registered profile names, which the caller must have loaded already —
            `registered_profile_names()` holds `default` alone until `load_profiles()` has run.
        describable: How to read a profile's description, injected so this module needs no import
            of the registry it is checking against. `api/app.py` passes `get_profile`'s.

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

    **A rostered helper carries its specialist's instructions verbatim over a strictly smaller
    surface, and that is structural rather than an oversight.** `helper_profile` subtracts
    `side_effecting_tools()` from the tools and subtracts nothing from the prose, so any specialist
    whose job includes acting describes tools its helper does not hold. Measured against the full
    declared connector surface, the `computation` helper binds 12 tools and its prompt names **10**
    it lacks — it is told "You compute molecular and reaction properties", to "record it with
    `report_measurement`, because that ledger is the only thing `calculator_trust` reads", and to
    poll a job it cannot start.

    `describe_helper` fixes the *caller's* view of this and its own docstring names the hazard —
    "a description written about the profile would advertise a helper that computes" — while the
    prompt half was left saying precisely that. This is the other half: the helper is told, after
    the specialist's prose rather than before it, what it actually holds and that the list wins.

    Last in the system message on purpose. The specialist's instructions are prose a profile author
    wrote for a session agent; this is the one sentence that knows about the narrowing, and a
    contradiction resolved in favour of whichever came first would resolve the wrong way.

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

    `tool_names is None` means "this profile does not narrow", which is the right reading for a
    session profile and the wrong one for a roster entry: there it would hand a named helper or
    peer the whole surface it is intersected with, under a name promising less. So on a roster,
    naming nothing narrows to nothing. One definition because the rule is security-relevant and
    was written three times — a copy that drifted to the session reading would widen silently.
    """
    return profile.tool_names if profile.tool_names is not None else frozenset()


def bounded_tool_list(bound: Iterable[str], limit: int) -> str:
    """`bound` sorted and joined, the first `limit` names enumerated and the rest counted.

    The capability half of both roster menus — a helper's (`describe_helper`) and a peer's
    (`handoff.describe_peer`) — derived from what the compiled graph bound. Bounded because the
    list grows with whatever the sibling fleet serves, which no ratchet here can see; the
    sentence around it stays each caller's own, because the two describe different acts.
    """
    ordered = sorted(bound)
    shown = ", ".join(ordered[:limit])
    rest = len(ordered) - limit
    return f"{shown}, and {rest} more" if rest > 0 else shown


def describe_helper(profile: AgentProfile, bound: Iterable[str]) -> str:
    """One roster entry's description: a written purpose, then the surface the graph really bound.

    **The derived half is the point.**
    `D-2026-08-12-a-supervisor-that-holds-every-tool-has-no-reason-to-delegate`
    measured a five-name roster whose menu was built as `instructions.split(". ")[0]` — and all five
    profiles open with "You are Chemclaw's `<name>` specialist", so the model chose from five
    entries that differed only in a name. Writing better sentences fixes that for one commit; a
    sentence and a *derived* list fixes it for good, because the list comes off the compiled graph's
    own tool surface and a profile edited next year cannot leave it stale.

    It also removes a mistake that is otherwise very easy to make here, and one this repository
    would have made: a rostered helper is **narrower than the profile it is named for**, since every
    tool that acts is subtracted. `computation` names 41 tools and its helper binds 12 of them —
    the enumeration family, topology, the calibration ledger and calculation lookup. A description
    written about the profile would advertise a helper that computes, and the model would delegate a
    calculation and get back a report saying it could not run one.

    Sorted, because a set's iteration order is not stable across processes and this string lands in
    the prefix of every model call: an unsorted list would make the same deployment send two
    different prompts and defeat any prompt cache keyed on them.

    Args:
        profile: The rostered profile, whose `description` supplies the written half.
        bound: The tool names this helper's compiled graph actually bound.

    Returns:
        The `description` for this entry's `CompiledSubAgent` spec.
    """
    purpose = (profile.description or "").strip()
    # **Bounded, because what this enumerates is not a surface this repository can measure.** The
    # list comes off the compiled graph, so it grows with whatever the sibling fleet serves — and
    # `tests/test_context_floor.py`'s per-tool bound binds no fleet connector, so `task` measured
    # 897 against a 900-token ceiling while a deployment that serves `safety` would send ~1,009.
    # A ratchet that cannot see its input is not the place for this bound; see
    # `agent_helper_menu_tools`.
    held = bounded_tool_list(bound, settings.agent_helper_menu_tools)
    return f"{purpose} Reads only, and holds exactly: {held}."


def helper_profile(
    caller: AgentProfile, held: frozenset[str], specialist: AgentProfile | None = None
) -> AgentProfile:
    """The caller's profile, narrowed to what a helper is for and routed to its own model.

    Three changes and nothing else, so that every dimension this does not name — the instructions,
    the connector selection, the harness mode, the effort — stays the caller's. A helper is meant to
    be the same agent working on a smaller piece with a clearer desk, not a different agent.

    **A `specialist` makes it a fourth change and not a different function**, because the one thing
    a roster entry must never do is widen. It intersects: the surface becomes what the *caller*
    holds ∩ what the specialist names, still minus everything that acts. Intersection is the whole
    safety argument and it is arithmetic rather than a check — a caller that narrowed itself hands
    in the smaller set, and no specialist can name its way past it, so
    `D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor` holds by construction on a roster
    exactly as it did on one unnamed helper.

    The specialist's `instructions` do replace the caller's, and that is deliberate rather than an
    inconsistency: a prompt carries no authority. This is the same asymmetry `model_route` already
    has — the two dimensions a roster varies are the two that cannot reach anything.
    `D-2026-08-12-a-supervisor-that-holds-every-tool-has-no-reason-to-delegate` is why the roster
    varies these and not capability: making a specialist hold what its caller lacks is the redesign
    that ADR names, and this is not it.

    1. **The surface loses everything that acts.** `side_effecting_tools()` is subtracted rather
       than an allow-list being written here, because that set is already the one this repository
       maintains, already assembled from three sources that own their own knowledge (the in-process
       classification, every enabled connector's declared `state_changing` names plus its jobs, and
       every enabled template launcher), and already held to a partition of the tool registry by
       `tests/test_authz.py`. A list written here would be a fourth source, correct on the day it
       was written. `SPEAKS_TO_THE_CHEMIST` goes with it, for the reason its own comment gives.
    2. **`model_route` becomes `"helper"`**, which does nothing at all until a deployment maps that
       key in `CHEMCLAW_MODEL_ROUTES` — see `AgentProfile.model_route`.
    3. **The name gains a `-helper` suffix**, so a log line or a span says which of the two graphs
       in a turn it came from. The profile is deliberately *not* registered: it is derived per
       build from whatever profile the caller resolved, and a registry entry would be a second,
       staler answer to a question `build_langgraph_agent` can always compute.

    **The subtraction is the whole attenuation argument.** What comes in is what the caller's own
    build resolved — a caller that already narrowed itself, like `property-lookup` with its four
    names, hands in the smaller set — and every operation here removes from it. There is no path
    that adds a name, which is what makes "a helper holds no tool its caller does not" a property of
    the construction rather than a check bolted beside it. Connector names are not in `held` at all:
    they arrive already open on a different parameter, and `helper_connectors` subtracts the same
    `side_effecting_tools()` from them — one switch, two halves, one set.

    Args:
        caller: The resolved profile of the agent that would spawn this helper.
        held: The in-process tool names that caller's build actually resolved. Passed in rather than
            re-derived here because the registry is complete only after `_capability_tools` has run
            `_register_generated_tools()`, and a set read before that is missing every launcher a
            deployment generated — see the call site.
        specialist: A rostered profile whose tools this helper is further narrowed to and whose
            instructions it carries. `None` is the unnamed helper, which is the caller's own agent
            with the acting half removed.

    Returns:
        A profile to build the helper's graph from. Never registered, never cached.
    """
    # `model_copy` rather than a fresh `AgentProfile(...)`: a field added to the model later is
    # carried into the helper automatically, where an explicit constructor call would drop it in
    # silence and read as deliberate. The four values below are typed as the model declares them,
    # which is what makes skipping validation safe here.
    #
    # **`harness_enabled=False` is the one of the four that is a narrowing rather than a rename**,
    # and it became necessary when D-2026-09-13 made the harness the deployment default: a helper
    # profile inherits `None`, which resolves to that default, so the helper silently acquired a
    # todo list and a plan gate. Both are pure cost here, for two independent reasons:
    #
    #   - The gate has nothing to protect here, **and the first version of this comment argued
    #     that from a false premise.** It said the gate "can never fire" because the line above
    #     removes `side_effecting_tools()`, and that is not what the gate reads:
    #     `authz.side_effecting_call` is `name in side_effecting_tools() or
    #     writes_durable_memory(name, arguments)`, and the second half is *argument*-driven — it
    #     matches `write_file`/`edit_file` under `/memories/`, which are neither in that set nor in
    #     `held` at all, because `FilesystemMiddleware` splices them in downstream of this
    #     narrowing. So the subtraction one line above does not reach them and the gate could
    #     match.
    #
    #     What makes it safe is one line over: a helper is compiled with **no `store=`**, so
    #     `scratchpad.scratchpad_backend` adds a `/memories/` route only `if store is not None and
    #     actor` and a helper gets neither. `/memories/…` therefore falls to the `StateBackend`
    #     default and dies with the helper — the durable write the gate exists to catch cannot
    #     happen, rather than being refused when it does. `tests/test_subagents.py` pins that
    #     absence, so passing a store to a helper turns this narrowing red instead of quietly
    #     re-opening it.
    #   - The plan is written where nobody reads it. A helper's state is discarded when its report
    #     crosses back (`agent/tool_result_shape.py` keeps the spend keys and not the rest), so
    #     `write_todos` would cost 1,372 tokens of the helper's own prefix to write a plan that
    #     never reaches the chemist, the caller, or the gate.
    #
    # Measured: the helper's namespace wrote an eighth checkpoint per turn with the harness
    # inherited, which `tests/test_checkpointer_prune.py` bounds at one turn's own writes. That is
    # the assertion that caught this, and it is the cheaper statement of the same argument
    # `D-2026-08-29-a-helper-is-cheaper-and-narrower-than-its-caller` makes throughout.
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
            # The specialist's own route if it declares one, so a deployment can make the cheap
            # readers cheap per name; otherwise the shared `helper` key, which is what every helper
            # has always used.
            "model_route": specialist.model_route or "helper",
            "harness_enabled": False,
        }
    )


def helper_connectors(
    connectors: list[Any] | None, specialist: AgentProfile | None = None
) -> list[Any] | None:
    """The caller's open connector tools, minus every one that acts.

    The connector half of `helper_profile`'s subtraction, and a second function rather than a
    second argument because the two halves arrive at `build_langgraph_agent` on different
    parameters and in different shapes: the in-process half is narrowed through `tool_names`
    before `_capability_tools` resolves it, while a connector tool is an already-open `BaseTool`
    that goes straight to `_bound_surface`. One switch (`helper=True`) applies both, which is what
    keeps "a helper reads, it does not act" a property of the construction.

    **Shared, not reopened, and that is the whole cost argument.** These objects belong to sessions
    the caller opened for this turn and holds open for its duration, so a helper that calls one
    spends no handshake, no socket and no server-side session state.
    `D-2026-08-29-a-helper-reaches-no-connector-because-of-the-lifecycle-not-the-deadlock` weighed
    two shapes — passing these down, and opening a second full set eagerly — and rejected the first
    on a concurrency measurement and the second on cost.
    `D-2026-09-15-a-helper-shares-the-session-its-caller-already-opened` drove the first and found
    it false: 4 concurrent 1.88 s calls over one open tool object finish in 1.99 s, and a call that
    fails mid-flight beside another damages neither it nor the session. What is left of that ADR is
    its cost argument, which now points the other way.

    The subtraction is `side_effecting_tools()`, the same set and for the same reason
    `helper_profile` gives: it already carries every enabled connector's declared `state_changing`
    names and every connector job, assembled from the manifests rather than from a list written
    here, so a bundle added next year is outside a helper's reach on the day it is enabled.

    `SPEAKS_TO_THE_CHEMIST` is deliberately not subtracted here: it names an in-process tool, and a
    connector tool cannot write a turn signal — the signal is written in this process, by the
    capability, and a connector's answer comes back over the wire as a `ToolMessage`.

    **A `specialist` narrows this half too, and it has to**, because a profile's `tool_names` spans
    both halves of the surface — `evidence.yaml` names `gather_evidence` in this process and
    `similar_reactions` out of it, and a reader could not tell from the file which is which, by
    design. Narrowing only the in-process half would give a named helper every connector tool its
    caller held, so `evidence` and `computation` would differ in their local tools and be identical
    across the wire. The same intersection as `helper_profile`, for the same reason.

    Args:
        connectors: The caller's already-open connector tools, or `None` for a turn with none.
        specialist: A rostered profile whose `tool_names` also bounds this half. `None` is the
            unnamed helper, which keeps every connector tool that does not act.

    Returns:
        The subset a helper may call, or `None` if the caller had none — `None` rather than `[]` so
        that "this agent has no out-of-process capability" stays one value through the builder.
    """
    if not connectors:
        return None
    acting = side_effecting_tools()
    kept = [tool for tool in connectors if tool.name not in acting]
    if specialist is None:
        return kept
    named = roster_names(specialist)
    return [tool for tool in kept if tool.name in named]
