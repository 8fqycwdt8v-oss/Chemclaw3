# D-2026-08-29-a-tool-schema-nobody-calls-is-still-paid-for — 33,310 tokens of tool schema on every model call, 7,799 of them invisible to the ratchet, and the design that would defer them

**Status:** accepted (design only) · **Date:** 2026-08-29

Nothing in `agent/` is implemented by this ADR, deliberately — see "Why this is an ADR before it is
a change".

## Context

An audit of the Claude Agent SDK against this repository's LangGraph stack found four features
worth having. Three were small and shipped alongside this document: a per-turn spend cap
(`agent/spend_cap.py`), session forking (`agent/session_fork.py`) and a per-profile reasoning-effort
knob. The fourth is the valuable one and is the only one that touches the middleware chain, so it
gets a decision before it gets an edit.

The SDK defers MCP tool *schemas*: a `ToolSearch` verb lets the model discover tools by description
and only then binds their full schemas, so a turn pays for the tools it might use rather than for
every tool that exists. This repository binds all of them, every model call, always.

**`CLAUDE.md` already names this as the unratcheted half of the prefix.** `tests/test_context_floor.py`
gates every in-process tool schema; `chemclaw_connector_tool_schema_tokens` measures the endpoint
half because "an endpoint tool's schema comes from a server this repository does not build". What
neither says is what the whole thing costs today.

## The measurement

Against the default profile, on the graph `build_langgraph_agent` compiles, with **no connectors
attached** — in-process tools only. Measured through `convert_to_openai_tool` (the function
LangChain calls when it binds tools to a model) with `count_tokens_approximately` (the counter
compaction budgets with), on the tool objects the model is **actually sent**:

| | |
| --- | --- |
| tools bound at the model call | 56 |
| tool-schema tokens | **33,310** |
| mean per tool | 594 |
| most expensive | `draft_experiment_protocol` 2,568 · `start_optimization_campaign` 2,307 · `propose_knowledge_note` 1,126 |
| least expensive | `list_watches` 66 · `forget_preference` 92 · `stop_watching` 98 |

Three things make that number worse than it looks, and each is a property of this deployment rather
than of the idea:

1. **The estimator is honest here.** `agent/context_budget.py` measures chars/4 at 1.00x on tool
   schemas — the one payload class where the estimate and the bill agree. So 33,310 estimated is
   ~33,310 billed, unlike the connector JSON the same module measures at 0.45x.
2. **It is paid per model call, not per turn.** The prefix is re-sent on every iteration. At the
   shipped `harness_max_loop_iterations=25`, a turn that runs to its cap sends ~833,000 tokens of
   tool schema — before a single word of the chemist's question, the instructions, or any result.
3. **The shipped provider does not cache it.** `prompt_caching_middleware` returns `[]` for
   `llm_provider != "anthropic"` (`llm_provider.py`), and `values.yaml` ships
   `CHEMCLAW_LLM_PROVIDER: openai_compatible`. On the dev path the prefix is a cache read at ~0.1x
   after the first call; on the *target* stack it is full-price input every time.

And 56 is the floor, not the ceiling: it is the surface with no connector attached. Every server
added to the `Chemclaw3-mcp` fleet — the seam explicitly designed so a new capability costs **zero
core edits** — adds its tools to this number, forever, with nothing in this repository able to fail.
That is the structural problem: the cheapest possible action for a fleet author (ship another
server) raises a per-model-call cost that no test in either repository can see.

### The ratchet undercounts, by 7,799 tokens, and the reason is instructive

`tests/test_context_floor.py` is the file that exists to stop exactly this growth, and its docstring
makes the strongest possible claim for its own number: `convert_to_openai_tool` "is the function
LangChain itself calls when binding tools to a model, so this is the payload rather than an
approximation of it." Reconciling it against the measurement above says otherwise:

| | tokens | tools |
| --- | --- | --- |
| what the ratchet counts (`_capability_tools`, raw callables) | 25,511 | 49 |
| what the model is sent (bound tool objects) | 33,310 | 56 |
| **understated by** | **7,799** | 7 |

The 7,799 splits two ways, and neither half is a rounding error:

- **5,230 across all 49 shared tools.** `core/tool_registry`'s `@tool` decorator is identity, so
  `_capability_tools` returns plain callables; `create_agent` binds the wrapped objects, whose
  derived schemas are larger. Every one of the 49 differs — `get_durable_job_status` 274 → 662,
  `gather_evidence` 490 → 878, `start_optimization_campaign` 2,020 → 2,307. The file already
  records getting this wrong once in the *other* direction (reading `.name`/`.description` off a
  raw callable measured ~11 tokens per tool); this is the same trap one layer shallower.
- **2,569 in 7 tools it does not count at all** — `ls`, `read_file`, `write_file`, `edit_file`,
  `glob`, `grep`, `task`. They are registered by `FilesystemMiddleware` and `SubAgentMiddleware`
  rather than by `_capability_tools`, so they are bound on every turn and invisible to the gate.

So the real static prefix is ~39,983 against a ceiling of 33,000 that currently reads 32,184 and
passes with 816 to spare. The ratchet is not broken — it has caught real growth, and its ceiling
comments record it doing so — but it is gating a number ~24% below the one the deployment pays.

**Corroborated from a code path that shares nothing with the arithmetic above.**
`RecordContextCompaction._note_billing` estimates the whole outgoing request and hands it to
`note_model_call`. Driven on a compiled default-profile graph with a one-word question and a
one-word answer, it reports **40,466 estimated tokens** — a turn whose entire conversation is two
words. That number is computed by the compaction middleware for its own purposes, from
`prefix_tokens()` plus the message list, and it lands within 1.2% of the ~39,983 derived here by
summing bound schemas. Two independent measurements of the same quantity agreeing is the reason
this section states a number rather than a range.

**Not fixed in this commit, deliberately.** Correcting the basis raises the measured floor above its
own ceiling, and this repository's rule is that raising a ceiling "belongs in a commit that says
why" rather than riding along in one about something else. It is a `BACKLOG.md` row, and it is
listed here because it changes the size of the problem this ADR is about: the prefix is larger than
the file designed to bound it believes.

## The tension this sits in, stated before the design

The prefix is also load-bearing. `agent/skill_backend.py` exists because deepagents publishes skill
*paths* into the prompt, and the whole D-2026-08-10 rebuild turned on the model being able to see
what it may do. A model that cannot see a tool will not ask for it: the failure mode of deferral is
not a slow turn, it is a **wrong answer that never mentions the capability it needed** — and this
repository has already measured that shape once, in `_empty_answer_event`'s 29-tool-call, 197-second
silent death. So the cost of getting this wrong is not paid in tokens.

## Decision

Defer connector tool schemas behind a discovery verb, on four conditions, none of which is
negotiable and one of which is a stop.

**1. Connector tools only. In-process tools stay bound.** The 56 measured above are first-party,
`tests/test_context_floor.py` already ratchets them, and they are the ones the system prompt and the
skills reason about by name. The unbounded growth is the endpoint half — the half that arrives from
servers this repository does not build and cannot ratchet. Deferring the half that is already
bounded would buy the smaller number and take the larger risk.

**2. The discovery verb returns descriptions, and descriptions are already written for this.**
`Chemclaw3-mcp`'s `CLAUDE.md` states that "tool docstrings are the prompt" and requires each to say
what the tool is, its units, and *what it is not*. That is a search corpus. A deferred surface
advertises `name + one-line description` — measured above at a small fraction of a full schema for
the expensive tools — and binds the full schema only on request.

**3. It is off by default and measured before it is trusted.** The counter that decides this is not
token count, it is **whether the model still finds the tool**. `chemclaw.evals` scores answers
against probes; the acceptance bar is that the deferred arm matches the bound arm on tool selection
across the eval corpus, not that it is cheaper. Cheaper is already known.

**4. The stop.** `D-2026-08-12`/`D-2026-08-13` are on record: this corpus could not settle the
delegation question, measuring 2/15 and then 14/15-against-14/15 with the old arm at ceiling. If the
eval corpus cannot separate the two arms here either, **the honest outcome is to leave the schemas
bound and keep the measurement**, exactly as `D-2026-08-15` deleted a specialist team rather than
ship a capability whose benefit nothing could demonstrate. A token saving is not a reason to ship a
control this repository cannot show is safe.

## Three alternatives, rejected with reasons

- **Narrow by profile instead.** `AgentProfile.mcp_server_names` already narrows connectors, and a
  tighter profile set would cut the prefix with no new machinery. Rejected as *insufficient rather
  than wrong*: a profile is chosen when the session is created and fixed for its life, so it cannot
  respond to what a turn turns out to need — and the profiles that exist are broad because the
  questions are. Worth doing anyway; it is not this.
- **Summarize schemas rather than defer them.** Send a shortened schema and let the model call with
  approximate arguments. Rejected outright: `bad_tool_arguments` is already a first-class error code,
  and a deliberately lossy schema manufactures it. The `Chemclaw3-mcp` rule that a tool must "refuse
  rather than approximate" applies to the schema that describes it.
- **Adopt an upstream deferral middleware.** Rejected on the evidence of
  `D-2026-08-14`/`D-2026-08-15`: this repository adopted `ModelCallLimitMiddleware` and reverted it a
  day later over four regressions, and `tests/test_upstream_surface.py` exists because six places
  read shapes upstream never promised. A deferral middleware would sit *inside* the governance chain
  and decide which tools exist, which is the one place a silent upstream behaviour change is
  unacceptable — `enforce_tool_authz` can only refuse a tool it is asked about.

## Consequences

- `chemclaw_connector_tool_schema_tokens` becomes a number somebody acts on rather than one that
  only records. Its `sum()` plus the ratcheted floor is what a turn pays before the chemist speaks.
- A new `Chemclaw3-mcp` server stops being an unpriced addition to every turn on every profile.
- The eval corpus gains a job it does not have today: proving a tool was *found*, not merely that an
  answer was good. That is worth building whether or not the deferral ships, which is the strongest
  argument for doing the measurement first.
- Until it ships, the cost stands and is now written down. `agent_max_turn_billed_tokens` (this same
  batch) is what bounds a turn that runs away inside it.

## Why this is an ADR before it is a change

The three features shipped beside this one touch a hook, a table and a kwarg. This one changes what
the model can see, inside the chain that authorizes tool calls, against a corpus that has twice
failed to settle a question of this shape. `CLAUDE.md`'s rule is that a non-trivial change plans
first and that an abstraction with one caller gets inlined; the corresponding rule for a control is
that it is measured before it is believed. The measurement above is the part that was missing, and
it is now on record whether or not anyone builds this.
