# D-2026-08-06-a-tool-cannot-say-it-has-nothing-twice — A tool cannot say it has nothing twice

**Status:** accepted · **Date:** 2026-08-06

## Context

The live full-stack pass (2026-08-04) recorded `find_past_jobs` called **7-8 times in a single
turn**, across three separate probes — the same tool, the same arguments, the same answer — with
`load_skill` ×6 and `find_notes` ×5 beside it. Every call was cheap, which is why nothing failed and
nothing caught it. What it cost was the turn: a median of 128-142 s against 16.9 s on the archived
comparison run, plus every repeat's result spent back into the context window the answer had to be
built from.

Nothing in the system bounded this. `harness_max_loop_iterations` caps the harness's *iterations*
and says nothing about how many calls one iteration makes; a profile narrows *which* tools exist,
not how often they are asked.

The repeat is not a bug in any tool. It is the model doing the one thing a tool call cannot tell it
is useless: `find_past_jobs` returning nothing looks, from the model's side, exactly like a call
that has not been made yet.

## Decision

**Within one turn, a tool call identical to one already made `max_identical_tool_calls` times is
refused with a message the model can act on.** `chemclaw.agent.repeat_guard.refuse_repeated_calls`
is a MAF function middleware, attached unconditionally beside `refuse_writes_on_dry_run`.

### Refuse, do not cache

Serving the first call's result would be faster still, and wrong. `get_durable_job_status` is
read-only and legitimately changes *within* one turn, so a cached answer would pin a job at
"running" for a model that was correctly re-checking it. A refusal cannot go stale: it reports what
happened and hands the decision back. Nothing is invented for a call that never ran.

### The third call, not the second

One re-check is a real pattern — a job polled after a wait, a note re-read after a write. Seven is a
loop. `max_identical_tool_calls` defaults to **2**, so the legitimate shape still goes through and
the measured one does not. `1` disables repeats entirely; a deployment whose tools are cheap and
whose answers move can raise it.

### It reuses two mechanisms rather than adding wiring

`RepeatedCallRefusal` is a `ChemclawError`, so the audit middleware records it as an `error` outcome
and `surface_domain_errors` hands the message to the model verbatim instead of MAF's opaque
"Function failed." That matters more here than anywhere else: the whole point is to tell the model
something it can act on, and a refusal it cannot act on would just move the loop one step out. The
message names the tool and states the three ways forward — change the arguments, use a different
tool, or answer from what it already has, saying plainly if that is not enough.

It sits *outside* `announce_tool_failures`, because nothing ran. A refusal is not a tool failure,
and showing it to the chemist as one would misdescribe a turn that is working correctly.

## Consequences

- The counter is a contextvar started and torn down by the runner beside the signal buffer, so it is
  task-local, empty off the request path (CLI, tests, the classic agent), and restored on teardown —
  a watch that leaked its counter would make the *second* chemist in a worker's lifetime the one who
  gets refused, which is the worst possible failure for a guard whose job is invisibility.
- Calls are keyed on `(tool, canonical arguments)`. `sort_keys` because a model re-emitting one call
  is under no obligation to serialize its arguments in the same order, and key order is not a
  difference any tool can observe. `default=str` because roughly half this system's tools take a
  pydantic model rather than a JSON object (`start_optimization_campaign(spec: CampaignSpec)`) and
  `json.dumps` refuses one outright — a middleware that raised on that argument shape would break
  the calls it exists to protect.
- `chemclaw_repeated_tool_calls_total{tool}` is the only trace a deployment gets, because the turn
  still answers. The live run that found this had no signal beyond a median three times slower than
  a comparison nobody was running.
- This addresses the *cost* half of the du-03 finding, not the behavioural half. Whether a turn that
  loops on retrieval and never reaches the capability it needed is a retrieval problem, a prose
  problem, or a 38-note corpus giving it nothing to stop on is still unmeasured, and the corpus
  caveat means it cannot be settled on that data.
