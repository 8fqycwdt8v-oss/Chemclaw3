# D-2026-08-29-an-iteration-cap-is-not-a-cost-cap — a turn's spend gets a ceiling, a session gets a fork, and a profile gets an effort

**Status:** accepted · **Date:** 2026-08-29

## Context

An audit of the Claude Agent SDK against this repository's LangGraph stack asked which of its
features are worth having here. Most of the answers were "already, and better": the SDK's file-based
memory tool is weaker than `chemclaw/memory/`'s episodic/semantic knowledge-graph tiers, its
allow/deny/ask permission modes are weaker than the authorization chain, its skills are the same
mechanism this repository already narrows on the backend, and its session persistence is local disk
where this is Postgres. Two were declined outright and stay declined — a `Bash`/code-execution tool
(`agent/scratchpad.py` withholds `execute` deliberately) and `WebSearch`/`WebFetch` (the fleet's
no-egress posture) — and both remain new decisions for whoever wants them.

Three were real gaps. This ADR records them and one correction to the audit itself.

## The correction, because it is the interesting part

The audit's first finding was "this repository caps by call count only". That was half wrong, and
being wrong about it in the *reassuring* direction is what makes it worth writing down.
`api/budget.py` does meter tokens — per session and per user, refusing a turn that would breach a
cap. What it cannot do is see *inside* a turn: `check()` runs before a turn against usage already
booked, and `record()` books the turn after it ended.

That module's own docstring states the belief that leaves the hole:

> A single agent turn is already iteration-capped (`harness_max_loop_iterations`), so one turn cannot
> loop forever.

One turn cannot *loop* forever. One turn can *spend* without a bound. A call is not a unit of cost —
inside the shipped 25-iteration ceiling, a prose turn bills a few thousand tokens and a turn that
fans out over large tool results against a long context bills millions — and the session budget
learns about it one turn too late. Which is the "$400 in twenty minutes" failure `api/budget.py` was
written against, arriving through the door it left open.

## Decisions

### 1. A per-turn billed-token cap (`agent/spend_cap.py`)

`agent_max_turn_billed_tokens` bounds what one turn may bill. Three implementation choices, each
forced by something already on record:

**Enforced in `before_model`.** `D-2026-08-15-an-after-model-counter-is-a-counter-that-can-be-skipped`
measured an `after_model` counter being short-circuited by a middleware jumping from `after_model` —
a cap of 2 letting 4 model calls through. `before_model` cannot be skipped. This is the slot
`loop_cap` occupies, for the same reason.

**Counted in a state channel, not an ambient.** The count must cross the subagent boundary or a
fan-out gets one budget per branch — regression 3 in `agent/loop_cap.py`'s list. `billed_tokens` is a
`TurnTotal`, which folds a superstep's concurrent writes additively. An ambient would also have made
the cap inert wherever no caller started a watch, which is the "per-turn is a property of every call
site" mistake `agent/state.py` records moving away from — `test_the_cap_binds_with_no_watch_at_all`
is that property asserted.

**Metered in `wrap_model_call`, because only the response carries the bill.** That `wrap_model_call`
can *also* write state was measured on a compiled graph before the module was written rather than
read off documentation: `ExtendedModelResponse` carries a `Command` LangGraph applies through the
channel's own reducer. The first probe wrote a channel `ChemclawState` did not declare and LangGraph
dropped it in **silence** — the exact failure `tests/test_state_channels.py` exists to catch, walked
into while designing a guard, which is why every test in `tests/test_spend_cap.py` drives a compiled
graph.

The cap is a ceiling on what a turn may spend **before its next call**, so the last allowed call may
carry it past the number by at most one call's bill. Bounding the overshoot exactly would require
predicting a call's cost, and the estimator that could is measured at 0.45x on this payload class.
One call loose and honest beats exact against a number nobody has.

**It ships at 0 (off).** The guard ends turns, and a turn ended early on a corpus the setting was
never sized against loses a chemist's work. A deployment sets it from its own `turn_costs` rows. The
iteration cap is unaffected, so this is not a runaway guard shipping disabled.

### 2. Session fork (`agent/session_fork.py`, `POST /sessions/{id}/fork`)

Branch a thread at its current state without touching the parent — a copy, not a pointer, because
retention prunes **by thread** and a shared row would be one whose lifetime is the parent's and whose
reader is the child.

There is no fork verb on `BaseCheckpointSaver`; what makes the SQL safe is that every checkpoint
table's primary key leads with `thread_id`. Three things a naive copy gets wrong, all found by
reading the schema rather than by trying it:

- **The whole thread, not the tip.** `checkpoint_blobs` is keyed `(thread_id, ns, channel, version)`
  and rows are shared across a thread's checkpoints, so copying the newest checkpoint's rows loses
  every channel value written at an earlier version — silently, resuming with holes.
- **The transcript too.** A session with no `session_messages` rows is invisible to `GET /sessions`,
  whose owner listing `LATERAL`-joins `max(created_at)` and drops sessions with none.
- **The parent's profile.** A profile only ever narrows, so restoring the default would let a fork do
  more than what it was forked from.

Authorized by the existing `resolve_session` dependency and nothing else, so a fork is exactly as
reachable as reading the parent's transcript and refuses with 404 rather than 403.

`tests/pg.py::create_checkpoint_tables` was fixed in the same change: it ran `MIGRATIONS[1:4]` — the
three `CREATE TABLE`s and none of the `ALTER`s — while its docstring claimed "the shape under test is
the shape production has". Invisible to every test that only `INSERT`s named columns, and immediate
for one driving a real saver (`UndefinedColumn: column "task_path"`).

### 3. Per-profile reasoning effort

`llm_effort` plus `AgentProfile.effort`, threaded through `build_chat_model`. The expectation going
in was a per-provider translation — `reasoning_effort` on one side, `thinking` with a token budget on
the other, the latter needing to be budgeted under `max_tokens` and refusing a set `temperature`.
Measured on the installed distributions, both clients take `reasoning_effort`, so it joins
`_generation_options` and no translation exists to write.

Published as `low | medium | high`, the **intersection**: `ChatAnthropic` types it
`Literal["max","xhigh","high","medium","low"]` and `ChatOpenAI` types it `str | None`. The union would
make `max` work on the dev path and mean nothing on the shipped one.

Unset means the key is **absent** from the request. That rule binds harder here than for
`temperature`: a 400 from a rejected parameter is deliberately not failed over
(`_failover_exceptions`), so a knob defaulting to sending something would fail every turn on an
endpoint nobody had asked. Both clients are `extra="ignore"`, so the tests assert the attribute on the
constructed object — a client that stopped accepting the kwarg would drop it in silence, and a test
asserting "we passed it" would stay green through exactly that.

## Consequences

- A turn now has a cost ceiling as well as an iteration ceiling, and `spend_cap_reached` joins
  `loop_cap_reached` as an error that shares its turn with a partial answer. `events.py` said
  `loop_cap_reached` was the *only* such code; that sentence is now false and was corrected.
- A chemist can branch a twenty-turn campaign instead of choosing between polluting it and
  re-establishing it by hand.
- Effort is per profile, so a property lookup and a campaign design can ask different amounts of the
  same deployment.
- The fourth finding — deferred connector tool schemas, the most valuable and the only one touching
  the middleware chain — is designed and deliberately not built:
  `D-2026-08-29-a-tool-schema-nobody-calls-is-still-paid-for`.
