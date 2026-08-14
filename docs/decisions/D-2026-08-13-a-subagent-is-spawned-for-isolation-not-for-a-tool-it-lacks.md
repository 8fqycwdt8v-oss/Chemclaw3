# D-2026-08-13-a-subagent-is-spawned-for-isolation-not-for-a-tool-it-lacks — The five specialists become tool surfaces, and the reason to delegate goes back to upstream's

Why the team delegated twice in fifteen and what the *other* fix is, why the five names survive with
a different meaning, and the one way a dynamically-spawned agent must never be built.

## The finding this reverses half of

`D-2026-08-12-a-supervisor-that-holds-every-tool-has-no-reason-to-delegate` measured the team
delegating **2 of 15** and found the cause structural rather than promptable:

> `reject_widening` requires specialists ⊆ supervisor … the supervisor is never *missing* the tool
> that answers a question, so delegating is always a strictly longer path to a tool already in hand.
> No prompt makes that a good trade on a one-tool question, and the model declining it is the model
> being right.

That is correct, and it is an argument about exactly one reason to delegate: **to reach a
capability.** It says nothing about spawning for context isolation, for parallelism, or for a second
agent's independent look — none of which the supervisor's own inventory makes redundant. A
supervisor holding every tool still cannot read four documents at once, and still cannot give itself
a second opinion.

That ADR named one lever: narrow the supervisor so it *lacks* what its specialists hold, inverting
D-2026-08-10's invariant 1. It deferred that, correctly, because its trigger is "a deployment where
the specialists' surfaces are genuinely disjoint, and this one's are not".

**This is the other lever, and it costs no invariant.** The surfaces stay exactly as they are.
What changes is what the supervisor is told a helper is *for*.

## Decision

**`SPECIALISTS` is a list of tool surfaces, not a routing partition**, and the reason to spawn is
upstream's: the work is large enough to isolate, independent pieces can run at once, or a separate
look is more reliable than doing it inline. `_SUPERVISOR_PROMPT` and `_TASK_TOOL_DESCRIPTION` are
rewritten to say that, and to say the same thing as each other.

The five profiles, their five files and their tool lists are untouched. `reject_widening`,
`_narrowed_connectors`, `_AttributedSpecialist` and `running_specialist` are untouched.
`agent_teams_enabled` stays `False`.

This is the third framing these two texts have had, so it is worth being precise about what changed
and what did not. D-2026-08-12 found them *disagreeing* — the system prompt said route-by-surface
while the tool description said complex-multi-step-work — and settled the disagreement toward the
capability partition. The disagreement was the real defect and it stays fixed: both texts now
describe one mechanism. What is reversed is which mechanism they describe.

`tests/test_agent_team.py::test_the_task_tool_and_the_supervisor_prompt_agree` asserts the property
rather than the wording: the menu is interpolated, both texts offer the same grounds for spawning,
and every buildable surface is named where the supervisor reads it.

## The `task` tool already had the shape this needs

`TaskToolSchema` (deepagents 0.7.5, `middleware/subagents.py:272-282`) is two required strings:
`subagent_type` and `description`, and `description` becomes the subagent's entire `HumanMessage`
(`:539`). So a helper's brief is **already** freeform and per-task; only its *surface* is constrained
to a registered name. Nothing had to be built to make delegation task-tailored — the framing was the
only thing in the way.

That split is also the security line, and it is why this ADR is comfortable letting a model author a
helper's whole instruction: **the model authors the brief, the code authors the capability.** A
brief is text the helper reads; it is never a set of tools the helper gets.

## A bare `SubAgent` dict is forbidden

`SubAgentMiddleware` accepts two spec shapes and they are not equivalent. `create_sub_agent`
(`middleware/subagents.py:333-385`) builds a plain `SubAgent` dict's agent with:

```python
middleware: list[AgentMiddleware] = list(spec.get("middleware", []))
```

— *exactly* the middleware in the spec, with no parent chain and no defaults. Upstream's default
stack is injected by `create_deep_agent` (`graph.py:667-739`), which **this repo never calls**; it
imports only `deepagents.backends`, `middleware.skills` and `middleware.subagents`.

So an ad-hoc subagent built the obvious way would run with **no audit trail, no per-tool
authorization, no dry-run gate and no plan gate** — and nothing would fail while it did. The tool
calls would work, the answer would be correct, and the GxP trail would be silently empty. That is
the same class of defect as the history-persistence flag D-2026-08-10 found: every unit test passes
and the mechanism is not there.

**Every spawned agent is therefore compiled by `build_langgraph_agent` and passed as a
`CompiledSubAgent`.** That is the one constructor that attaches the chain. It is enforced by
`tests/test_challenge.py::test_a_challenger_is_built_with_the_full_governance_chain`, which asserts
against the builder's call signature — a resolved `AgentProfile`, the turn's actor and correlation
id — because a dict spec has none of those and could not fake them.

There is also **no runtime registration**: `self._subagents` is set once (`:682`) and
`subagent_names` is a frozen snapshot (`:686`), so the set of surfaces is fixed per compiled graph,
which is per turn. A model cannot invent a surface, only a brief.

## Consequence

A turn now records how many helpers it spawned (`team.delegations()`, advanced by
`_AttributedSpecialist` and read by the challenge gate). The count rides the work-delegation path
rather than `running_specialist`, because the challenge panel brackets its own members there for
attribution and counting those would make the gate mistake its own review for a team.

`agent_teams_enabled` stays off. Whether the reframing actually raises the spawn rate is a
measurement, not an argument — the M12 routing corpus is the instrument, and a default-on decision
belongs to whatever ADR reports that number, exactly as D-2026-08-12 kept the default and the
measurement in one place.

## Measured — and it does not support the argument above

The corpus was then run, live on `claude-sonnet-5`, both arms differing only in the two prompt
strings (`/tmp` harness: `build_langgraph_agent()` per probe, `agent_teams_enabled=true`,
delegation counted by `team.delegations()`).

| arm | delegated |
|---|---:|
| old (capability-partition framing) | **14 / 15** |
| new (spawn-for-isolation framing) | **14 / 15** |

**Two things follow, and the first one is against this ADR.**

*The reframing changed nothing measurable.* Identical rates, and the same single probe (`rt-08`)
self-answered in both. The argument this ADR is built on — that restoring upstream's reason to spawn
gives the supervisor a reason the capability framing denied it — is **not evidenced**. It may still
be true and simply unmeasurable here, because the old arm was already at 14/15 and there was no
headroom to detect an improvement; a ceiling is not a refutation, but it is not support either.

*The harness does not reproduce D-2026-08-12's baseline, so neither number transfers.* That ADR
measured **2 of 15** through the front door, with connectors, history and a session profile; this
ran the compiled agent directly with none of them. A seven-fold gap on the *same corpus and the same
prompts* means the two setups are not measuring the same thing, and the likeliest candidates are the
model (this run's `claude-sonnet-5` is not necessarily what that run used) and the missing connector
surface. Sampling noise is real too: `rt-01` self-answered on a throwaway single run and delegated
in both scored arms, so one sample per probe is thin.

**What this leaves standing.** The *structural* changes here — surfaces instead of a routing
partition, the two texts agreeing, the ban on bare `SubAgent` dicts, the delegation tally — are
sound independent of the rate, and the ban in particular is a security property that no measurement
bears on. What is not established is the claim that the reframing buys delegation. Nothing in this
change should be read as having demonstrated that, and `agent_teams_enabled` staying off is now
better supported than when it was written: a capability whose headline benefit is unevidenced is
certainly not a default. Re-measuring through the front door, against the same model D-2026-08-12
used, with repeats per probe, is the open row in `docs/planning/BACKLOG.md`.

## Result

`make lint type test` green. Tests: `test_agent_team.py` (the two prompts agree; the tally counts a
work delegation, reads 0 outside a turn, and does not count a challenger), `test_challenge.py` (the
governance-chain guard).
