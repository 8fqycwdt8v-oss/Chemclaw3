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

None of those three is a build-time assertion, and that is deliberate rather than an omission. Under
a one-name roster a helper is built from its caller's own profile, so any check comparing the two
profiles — which is the shape `reject_widening` had before the specialist team was deleted — would
compare a value with itself and could never turn red. The invariant is instead asserted where it can
actually be observed: `tests/test_subagents.py` compiles the caller *and* the helper and compares
the tool surfaces the two graphs really bound. That is the difference between enforcing an
attenuation and restating it.
"""

from typing import Any


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
            "matter to the final answer. It holds the same in-process tools you do and no others, "
            "so it is never a way to reach something you cannot reach yourself, and it cannot call "
            "external connector tools — do those here. Give it the full context in the prompt, "
            "since it sees nothing of this conversation, and say exactly what to return."
        ),
        "runnable": runnable,
    }
