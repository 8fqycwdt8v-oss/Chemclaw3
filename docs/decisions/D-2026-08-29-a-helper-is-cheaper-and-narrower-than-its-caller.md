# D-2026-08-29-a-helper-is-cheaper-and-narrower-than-its-caller — the `general-purpose` helper loses everything that acts, and gains a model route

**Status:** accepted · **Date:** 2026-08-29 · Narrows the helper that
`D-2026-08-15-a-harness-is-adopted-whole-or-its-defaults-are-inherited-silently` introduced. Keeps
`D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor`'s invariants and makes the first of them
checkable for the first time. Does **not** reopen
`D-2026-08-15-a-capability-that-ships-off-is-not-a-capability`: there is still one roster name.

## Context

This started as a challenge to the position that this tree is closed to subagents, and the first
finding was that the position does not exist. `SubAgentMiddleware` is in `create_deep_agent`'s
`_REQUIRED_MIDDLEWARE` and `_apply_excluded_middleware` raises rather than strip it, so **every
agent this deployment builds ships a `task` tool**, and `agent/subagents.py` claims upstream's
`general-purpose` name so that what it reaches is a graph `build_langgraph_agent` compiled. That is
sound, and `CLAUDE.md` did not mention it: its only subagent paragraph is the deletion of the
specialist team, so the repository's own memory read as "delegation was removed" while the code said
"delegation is mandatory and governed".

**What the deletion ADRs actually established is narrower than what they are read as.**
D-2026-08-15's argument is hygiene — 3,300 lines unreachable in every shipped configuration — and it
is correct as hygiene. It is not a capability verdict, and the measurements behind it cannot supply
one: D-2026-08-12 measured 2/15 through the front door with connectors, history and a session
profile; D-2026-08-13 measured 14/15 against 14/15 by invoking the compiled agent directly with none
of those, one sample per probe, on a model that ADR could not confirm matched. A seven-fold gap on
the same corpus and the same prompts means the two runs measured different systems. And the
dependent variable was **delegation rate**, which is a mediator rather than an outcome: the corpus
was fifteen one-tool questions, so the benefit a helper exists for — isolating reading-heavy work —
had no mechanism by which it could have appeared.

**Meanwhile the surviving helper's surface did not match its own description.** The `task` tool told
the model a helper was for isolation and parallel reading. Measured against the live registry, the
helper held **54 in-process tools**, its caller's entire set: nine `run_*` durable job launchers,
`propose_knowledge_note`, `start_optimization_campaign`, `request_external_input`. So a helper
spawned on a brief the *model* wrote could open a pull request against the knowledge graph, start a
CREST search costing hours of pod time, and put a durable question into a person's inbox — from a
context the chemist never sees. Every gate held; the audit row, the authorization decision, the plan
gate and the spend cap are the same chain either way. This was a design defect rather than a hole,
and the distinction matters: nothing was unsafe, and "a helper reads, it does not act" was a
sentence in a docstring rather than a property of the graph.

**Three shapes from an audit of the Anthropic SDK's own multiagent surfaces**, none of which this
tree could express: a worker on a *cheaper* model (Managed Agents leads with it — the coordinator
spends its tokens on planning and synthesis, the worker does the bulk reading and is billed at its
own model's rate); a roster member with a *narrower* tool set than its coordinator; and an advisor,
which inverts the hierarchy — a **more** capable model consulted mid-turn, holding no tools at all.
One convergence worth recording in the other direction: Anthropic's session budget is a single cap
shared across all threads, which is exactly what `ChemclawState.billed_tokens` already does as a
`TurnTotal`, arrived at here independently and already driven across a real fan-out by
`tests/test_spend_cap.py`.

## Decision

**A helper is its caller, minus everything that acts, on whatever model a deployment routes it to.**

`helper_profile(caller, held)` derives the helper's profile from the caller's, and every operation
in it removes:

- **`authz.side_effecting_tools()` is subtracted**, rather than an allow-list being written in
  `agent/subagents.py`. That set is already assembled from three sources that own their own
  knowledge — the in-process classification, every enabled connector's declared `state_changing`
  names plus its jobs, and every enabled template launcher — and already held to a partition of the
  tool registry by `tests/test_authz.py`. A list written here would be a fourth source, correct on
  the day it was written. A connector or template added next year is out of a helper's reach on the
  day it is enabled.
- **`SPEAKS_TO_THE_CHEMIST` is subtracted too**, and today it is one name. `ask_clarifying_question`
  is correctly classified read-only — it writes no row and starts no workflow — but it records a
  turn *signal*, and a signal is delivered on the turn's stream. A helper calling it shows the
  chemist a question apparently asked by the agent they are talking to, while the answer arrives in
  a conversation the helper has already left. `side_effecting_tools()` asks "does this change
  something outside the turn", which is the right question for the plan gate and the wrong one here.
- **`held` is passed in rather than read from the registry**, because the registry is complete only
  after `_capability_tools` has run `_register_generated_tools()`. Read earlier, a deployment's
  generated launchers are simply absent — which gives the right answer today, since they are
  side-effecting and subtracted anyway, for a reason that stops being true the first time a
  generated tool is a read.

Measured on the default profile: the caller binds **61** tools, the helper binds **24**, and the
difference is every launcher, every write, `ask_clarifying_question` and `task`. Nothing widened.

**`AgentProfile.model_route` names a key in `settings.model_routes`, never a model id.** The helper's
derived profile carries `"helper"`, so `CHEMCLAW_MODEL_ROUTES='{"helper": "<a smaller model>"}'` is
the whole cost lever and there is no code change in it. A *model id* on a profile would be a site's
model name checked into this repository, which is what `model_routes` exists so nobody has to do; a
route key resolves through `build_chat_model(task)`, still the one place a model is built.

Two properties of `_resolve_chat_model` are deliberate. A configured route **wins over a supplied
model**, because `_subagents` hands the helper its caller's model and a supplied model that won
would defeat the route in exactly the configuration the feature is for. An **unconfigured** route
reuses the model already built rather than asking `build_chat_model` for its identical fallback: a
turn compiles two graphs, so taking the fallback would build two clients per turn to hold one
configuration, and would hand a test that supplied a fake model a real one.

**This is the one dimension where a helper is deliberately not an attenuation of its caller.** A
model carries no tools and therefore no authority, so D-2026-08-10's invariant has nothing to say
about a route pointing at a larger model. What a route can move is cost, and cost has its own bound
one layer down in `agent/spend_cap.py`, whose `TurnTotal` a fan-out shares rather than multiplies.

**The narrowing is applied inside `build_langgraph_agent(helper=True)`**, not in `_subagents`, so
`helper=True` carries everything a helper is and a second caller cannot get a governed-but-unnarrowed
one by forgetting a step. `_subagents` already has the scar: its first version expressed "no helpers
of its own" by passing an empty roster, and upstream filled the roster back in.

**`HELPER_BRIEF` is appended to the helper's system prompt**, and lives beside the `task` description
in one module. D-2026-08-12 found the supervisor prompt and the `task` description describing two
different mechanisms and recorded that the disagreement was the real defect; the same pair exists
here, since the caller reads one when deciding whether to spawn and the helper reads the other when
deciding what it may do. The `task` description's "it holds the same in-process tools you do" is now
false and is rewritten.

## Consequences

- **The attenuation assertion is a strict subset for the first time.** The test that stated it
  passed for months over two surfaces that were equal by construction: it could not have failed,
  because the only way to break it was to add a tool to the helper that nobody had a way to add.
- **A helper's skills narrow with its tools, at no cost and by a mechanism that already existed** —
  `skills_backend` is computed from the profile's advertised tools, so D-2026-08-10's fourth
  invariant ("skills do not inherit") now arrives as a consequence rather than as a second gate.
- **Nothing about the shipped default changes except the helper's surface.** `model_routes` ships
  empty, so no second client is built and no model changes.
- **The delegation question is still open, and this ADR does not claim to answer it.** What would
  answer it is an instrument the deleted one could not be: outcomes measured per *task* (answer
  quality, billed tokens, wall clock) rather than delegation rate, on reading-heavy multi-source
  work rather than one-tool probes, through one harness with repeats. That is a `BACKLOG.md` row,
  and the arms it needs — routed helper, unrouted helper, no helper — exist as of this change.
- **Three alternatives were examined and are not built here**, each with what would change the
  answer: per-helper connector sessions (C), an advisor (D), and a second roster name (E). Their
  findings are in `docs/planning/BACKLOG.md`; the short version is that C rests on a measurement
  about *sharing* one MCP tool object which does not reach the concurrency ban it is quoted for, D
  is compatible with every invariant here and blocked only on a provider that serves two model
  tiers, and E is still a routing hypothesis with no measurement asking for it.
