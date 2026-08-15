# D-2026-08-15-the-plan-gate-stays-a-refusal-because-an-interrupt-cannot-ask-the-question — `HumanInTheLoopMiddleware` is declined for plan approval, on four measurements

**Status:** accepted · **Date:** 2026-08-15 · Closes `BACKLOG:529` and the `HumanInTheLoopMiddleware` row in `docs/planning/BACKLOG.md`. Does **not** supersede `D-2026-08-11`'s `interrupt()` declination — it replaces that ADR's *reason*, which was wrong, with reasons that are measured.

## Context

`D-2026-08-11` declined `interrupt()` because "a PR-gate review takes days and an SSE stream cannot
be held open across one". That reason is false, and `BACKLOG:529` had already recorded the
refutation: `invoke()` returns normally with `__interrupt__` in the result, verified across two
processes. A wrong reason for a decision is worse than no reason, because the decision looks settled.

So the plan for this workstream proposed retiring `agent/plan_gate.py` (350),
`agent/plan_approval_store.py` (225), `agent/plan_state.py` (100) and `api/routes/plan.py` (142) in
favour of `InterruptOnConfig(when=…)` + `interrupt()` + `Command(resume=…)`, moving cross-turn
durability from the `plan_approvals` table into the checkpointer. It named three obstacles it
expected to design around.

None of the three is the obstacle. What follows was measured against langchain 1.3.15 /
langgraph 1.2.11 / deepagents 0.7.6, with scripts rather than reading.

## The decision

**Keep the refusal-based gate.** Do not move plan approval onto `HumanInTheLoopMiddleware`.

### 1. `when` cannot be async, and the gate's question is I/O

`InterruptOnConfig.when` is declared `Callable[[ToolCallRequest], bool]` and invoked at
`_should_interrupt` as a bare `return when(req)` — no `await`, no `isawaitable` check. The async
entry point `aafter_model` delegates to the sync `after_model` verbatim.

Hand it an async predicate and a coroutine object comes back. A coroutine is truthy, so **the
middleware interrupts unconditionally and the predicate body never executes**:

| predicate | body ran? | interrupted? |
|---|---|---|
| async, returns `False` ("do not interrupt") | **no** | **yes** |
| sync, returns `False` | yes | no |

It fails closed, which is the safe direction, and it fails *silently* — no exception, and no
`RuntimeWarning: coroutine was never awaited` surfaced from the graph run. A gate whose predicate
is skipped without a symptom is the exact failure class this repository has been burned by.

A sync predicate cannot bridge to the loop either, because `when` runs inside the running one:
`asyncio.run` raises `cannot be called from a running event loop`, and `run_until_complete` raises
`This event loop is already running`.

Both halves of the gate's question are async. `approval_stands` reads Postgres; `_plan_behind`'s
fallback reads the checkpointer through `session_todos`, and that fallback exists precisely for the
subagent case, where `SubAgentMiddleware` strips `todos` from state. So `when` can answer *"is this
tool state-changing"* and *"what plan is in this turn's state"* — it cannot answer *"…and is this
session's plan approved"*.

What survives is an interrupt on **every** state-changing call, which is a different product: no
plan-level approval, a prompt per action. That may be worth building one day for genuinely
irreversible actions, where per-call is the *correct* semantics. It is not a migration of this gate.

### 2. A new user message silently discards a pending interrupt, and corrupts the thread

Measured on one `thread_id`: interrupt on `danger(x="FIRST")`, then `ainvoke` again with a new human
message instead of a resume. No error. The pending interrupt is replaced by a new one for
`danger(x="SECOND")`, and a subsequent approve executes `SECOND`.

The user's chosen answer to this was "refuse the new turn with 409". That answer is still right and
it is not cheap: closing the SSE stream at an interrupt releases all four turn guards — the
in-process lease, the durable claim, the admission permit and the wall clock — and **nothing in
`api/` records that a session is suspended**. Building that state is most of what
`plan_approval_store.py` already is.

Worse than the discard is what it leaves behind. The abandoned `AIMessage` keeps its `tool_use`
block with **no matching `ToolMessage`** — measured: `tool_call ids with NO matching ToolMessage:
['c1', 'c2']` — and that dangling pair stays in `state["messages"]` and is replayed to the model on
every later turn. The old interrupt is unreachable from `aget_state`; only `aget_state_history`
still holds it. That is thread corruption, not a UX wrinkle.

### 3. A resuming turn with different `interrupt_on` silently bypasses the gate

Resume across graph instances works, which is the good news and matches this repository's per-turn
`build_langgraph_agent`: a fresh graph over the same saver resumed correctly and ran the tool under
its *own* tool object. But the resuming graph's config must match, and the two mismatches fail in
opposite directions, both silently:

| resuming graph | result |
|---|---|
| same `interrupt_on` | correct — tool runs, `ToolMessage` written |
| no HITL middleware (`gate_applies` false) | the pending call is **abandoned**, no error |
| `interrupt_on` keyed on a different tool | **the tool runs with the human decision never validated** |

`after_model` returns early when no config matches, so `interrupt()` is never called and the tool
node proceeds. `gate_applies` reads a profile and settings, so "the gate is attached" would have to
become an invariant *of the thread* rather than of the turn — a new durability requirement the plan
did not have.

### 4. Durability moves from a table that is never pruned to one that is

`plan_approvals` is not in `retention._PRUNABLE` at all; `infra/sql/README.md` records its disposal
as "consumed rows are marked, not removed".

A pending interrupt lives in `checkpoint_writes`. `_prune_checkpoints` deletes by `thread_id` across
all three checkpoint tables, selected by `max(checkpoint->>'ts') < now() - N days`, with the
disposability predicate for `checkpoints` a bare `TRUE`. **Nothing exempts a thread with an
unresolved interrupt** — grep finds no mention of `interrupt` or `pending_write` in
`durable/retention.py`. A thread whose newest checkpoint *is* the interrupt starts its expiry clock
at the moment it suspended.

`retention_checkpoints_days` defaults to `0`, which disables pruning, so this bites only a
deployment that turned retention on — which is to say the deployments that care most.

## What the accounting actually is

The plan counted 817 lines retired. Measured by AST, split by what `interrupt()` genuinely replaces:

- **`plan_gate.py`** — 182 lines replaced, **37 survive** as the `when` predicate (`plan_identity`,
  `gated_call`, `rewrites_the_plan_in_this_batch`, two constants — all already sync, and
  `rewrites_the_plan_in_this_batch` does work from `when`, confirmed against the real
  `ToolCallRequest`), 43 unchanged (the attach decision), 22 split.
- **`plan_approval_store.py`** — 225, genuinely all replaced.
- **`plan_state.py`** — 100, survives for the read side (`api/routes/plan.py`, `cli/chat.py`).
- **`api/routes/plan.py`** — 142, **rewritten rather than deleted**; `mode`, `approved` and
  `decided_by` have no analogue because an `Interrupt` records no actor.

Against that: ~40 external references across nine modules, ten test files, a new suspended-session
store, and a coordinated change in `Chemclaw3_ui` and `Chemclaw3_mock`.

## Consequences

- The gate stays where it is. `D-2026-08-11`'s declination stands with a corrected reason; this ADR
  is the reason.
- **Two defects in the current gate are worth fixing on their own terms**, and neither needs a
  migration: `PlanEvent` carries no `plan_hash`, so a client must round-trip `GET /plan` before it
  can post a decision; and the refusal reaches the wire as a `ToolFailedEvent` distinguished only by
  substring-matching its sentence (`evals/live.py`'s `PLAN_GATE_MARKER`). Both are backlog rows now.
- **`HumanInTheLoopMiddleware` is not declined for everything.** Per-call approval of an
  irreversible action is what it is shaped for, and the interrupt/resume mechanism is proven to work
  across processes. The restart condition for revisiting it here is upstream awaiting an async
  `when` — at which point three of the four findings above still stand and only the first is lifted.
- `graph_stream._from_update` skips non-dict updates, so an `__interrupt__` would be **silently
  dropped** and the turn classified `empty_answer`. Nothing emits one today, but that is one line of
  latent failure for anyone who adds an interrupt anywhere; recorded as a backlog row rather than
  fixed here, because fixing it without a producer is a branch no test can reach.
