"""The helpers a turn may spawn, and why the roster is one name this repository compiles itself.

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

**One helper rather than five.** `D-2026-08-13` framed the roster as five tool *surfaces*. M12 had
measured the framing before it delegating 2 of 15; a later run put both framings at 14/15 on the
same corpus and concluded the reframing bought nothing detectable — and that neither number is the
deployment's rate. What survives both is that a named partition is a routing hypothesis nobody here
has measured against real work. Fan-out needs no partition: `task` already tells the model to launch
several agents concurrently when their tasks are independent, so a parallel evidence sweep is N
invocations of one name. A second name gets added when a measurement asks for one.

**What a helper does not inherit, and where each bound is enforced.** No connector tools — a helper
is concurrent with its caller by construction, and two concurrent turns over one MCP tool object
deadlock, which is the measurement `langgraph_agent.build_langgraph_agent` gives as the reason a
graph is compiled per turn at all. No checkpointer, because upstream's contract is that a helper
sees only the prompt it was given and returns one report. No helpers of its own, which is the
recursion guard `build_langgraph_agent(helper=…)` carries.

**And, since `D-2026-08-29-a-helper-is-cheaper-and-narrower-than-its-caller`, nothing that changes
anything.** `helper_profile` is what made that true, and it was written because the surface and the
story had drifted apart: the `task` description said isolation and parallel reading while the helper
held every in-process tool its caller did — measured against the live registry, 54 of them,
including nine `run_*` durable job launchers, `propose_knowledge_note`,
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
Under a one-name roster a helper is built from its caller's own profile, so any check comparing the
two profiles — which is the shape `reject_widening` had before the specialist team was deleted —
compares a value with itself for every dimension the derivation leaves alone. The invariants are
therefore asserted where they can be observed: `tests/test_subagents.py` compiles the caller *and*
the helper and compares the tool surfaces the two graphs really bound. That is the difference
between enforcing an attenuation and restating it — and the subtraction above is what finally makes
that comparison a *strict* subset rather than an equality nobody could fail.
"""

from typing import Any

from chemclaw.agent.authz import side_effecting_tools
from chemclaw.agent.profiles import AgentProfile
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

You hold read-only tools only. You cannot start a durable job, propose a knowledge note, record an
answer, ask the chemist a question, or call an external connector tool; the agent that spawned you
can do all of those, and the right way to make one happen is to say so in your report. Do not
describe work as started, scheduled or arriving later: nothing you can reach starts anything."""


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

    Today `_subagents` returns one hand-built entry and the property holds by construction, so this
    raises for nobody. It exists because the *next* helper is the risk: a second name added as a
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
    """The one helper spec, as a `CompiledSubAgent` claiming upstream's default name.

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
        "name": "general-purpose",
        "description": (
            "A helper that works in its own context window and reports back a single summary. "
            "Spawn one — or several at once — when a task splits into independent pieces whose "
            "intermediate reading would otherwise crowd this conversation: sweeping several "
            "evidence sources in parallel, or working through a long search whose steps do not "
            "matter to the final answer. It reads and it reports, and that is all: it holds the "
            "read-only subset of the in-process tools you hold, so it cannot start a durable job, "
            "propose a note, record an answer or ask the chemist anything, and it cannot call "
            "external connector tools — do all of those here, yourself, after reading what it "
            "found. It is never a way to reach something you cannot reach yourself. Give it the "
            "full context in the prompt, since it sees nothing of this conversation, and say "
            "exactly what to return."
        ),
        "runnable": runnable,
    }


def helper_profile(caller: AgentProfile, held: frozenset[str]) -> AgentProfile:
    """The caller's profile, narrowed to what a helper is for and routed to its own model.

    Three changes and nothing else, so that every dimension this does not name — the instructions,
    the connector selection, the harness mode, the effort — stays the caller's. A helper is meant to
    be the same agent working on a smaller piece with a clearer desk, not a different agent.

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
    the construction rather than a check bolted beside it. Connector names are simply not in `held`
    and do not need to be excluded: a helper is built with no connectors at all.

    Args:
        caller: The resolved profile of the agent that would spawn this helper.
        held: The in-process tool names that caller's build actually resolved. Passed in rather than
            re-derived here because the registry is complete only after `_capability_tools` has run
            `_register_generated_tools()`, and a set read before that is missing every launcher a
            deployment generated — see the call site.

    Returns:
        A profile to build the helper's graph from. Never registered, never cached.
    """
    # `model_copy` rather than a fresh `AgentProfile(...)`: a field added to the model later is
    # carried into the helper automatically, where an explicit constructor call would drop it in
    # silence and read as deliberate. The three values below are typed as the model declares them,
    # which is what makes skipping validation safe here.
    return caller.model_copy(
        update={
            "name": f"{caller.name}-helper",
            "tool_names": held - side_effecting_tools() - SPEAKS_TO_THE_CHEMIST,
            "model_route": "helper",
        }
    )
