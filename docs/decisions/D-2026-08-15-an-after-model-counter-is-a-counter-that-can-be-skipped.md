# D-2026-08-15-an-after-model-counter-is-a-counter-that-can-be-skipped — The runaway cap goes back to a first-party `before_model` hook, and why delegating it was wrong four times over

**Status:** accepted · **Date:** 2026-08-15 · Supersedes §1 of
`D-2026-08-14-the-coupling-is-the-cost-not-the-line-count` (the cap's delegation to
`ModelCallLimitMiddleware`) and the claim in that ADR that "the two counts agree exactly". §2–§6 of
that decision stand: the skills channel, the upstream-surface assertions, the declined adoptions and
the v3 finding are unaffected.

## Context

D-2026-08-14 replaced a ~10-line first-party `@before_model` counter with a subclass of upstream's
`ModelCallLimitMiddleware`, on the argument that a duplicated *behaviour* should be delegated rather
than reimplemented. That argument is still right in general. It was wrong here, and a review of the
merged change found four independent regressions — none of which a recording subclass can fix,
because all four follow from *where upstream increments* and *what its channels are*.

Everything below was measured on a compiled graph, not read.

## Decision

Revert to the first-party hook. Restore `ChemclawState.model_calls`.

### 1. The increment is skippable, and the challenge gate skips it

Upstream counts in `after_model`. `after_model` hooks run in **reverse list order**, and
`build_langgraph_agent` attaches `_harness_middleware` (the cap) *before* `_challenge_middleware` —
so the challenge gate's `after_model` runs **first**, and its `jump_to: "model"` short-circuits the
rest of the chain, including the only increment.

Measured with a stand-in gate at the same position, cap = 2:

| arrangement | model calls |
| --- | --- |
| upstream middleware, no jumping `after_model` | 2 ✓ |
| **upstream middleware, gate jumps twice** | **4 ✗** |
| first-party `before_model` hook, gate jumps twice | 2 ✓ |

`before_model` runs before the model regardless of what any later hook decides. That is not a
stylistic preference; it is the property that makes the count a bound.

**The ordering matters and is easy to get backwards** — putting the gate *before* the cap in the list
reproduces nothing, because then the cap's `after_model` runs first. The first attempt at a
regression test made exactly that mistake and passed against the broken code. It is now
`tests/test_langgraph_stream.py::test_the_cap_stops_the_loop_at_exactly_its_limit`, parametrised over
`jumping_after_model`.

### 2. `exit_behavior="end"` fabricates an assistant message, and three surfaces read it

Upstream returns `{"jump_to": "end", "messages": [AIMessage("Model call limits exceeded: ...")]}`.
The first-party hook returns `{"jump_to": "end", "loop_capped": True}` and no message.

- `cli/chat.py` renders `messages[-1].content`, so a capped CLI turn printed the limit string
  **instead of** the partial answer — the outcome `agent/loop_cap.py`'s own docstring rejects when it
  explains why it does not raise.
- `SubAgentMiddleware` builds a specialist's report from the last non-empty `AIMessage`, so a capped
  specialist reported the limit string and dropped its work.
- `messages` is checkpointed, so the fabricated turn is replayed into the model's context on every
  later turn of that session.

The HTTP stream was unaffected — but only because `graph_stream._text_of` returns `""` for anything
that is not an `AIMessageChunk`. Nothing pinned that; it was load-bearing by accident.

### 3. The team's budget silently became per-specialist

`model_calls` is `UntrackedValue` and *not* private, so it crosses into and out of a subagent and one
budget spans a whole team turn. Upstream's `run_model_call_count` carries `PrivateStateAttr`, which
`SubAgentMiddleware` strips in both directions, so every specialist started at 0 — a five-specialist
turn's ceiling went from ~N model calls to ~6N. Nothing in the change said so.

### 4. `thread_model_call_count` is checkpointed

It is a `LastValue`, verified in `graph.channels`. Subclassing put a monotonically growing int that
nothing reads, resets or prunes into every session's checkpoint. Inert while `thread_limit is None` —
but a trap, because passing `thread_limit` later would brick sessions exactly as the old checkpointed
`model_calls` did, and there is no reset path.

Removing `model_calls` also *shrank* `FIRST_PARTY_CHANNELS`. The upgrade direction is safe (an old
stamp is a superset, so nothing is missing), but an **old** pod reading a checkpoint a **new** pod
wrote raises `CheckpointSchemaMismatch` — a rolling-deploy hazard. Restoring the field closes it.

## The general finding

**`ModelCallLimitMiddleware` is unsafe to compose with any middleware that jumps from `after_model`.**
Its only increment lives there, and `after_model` is the one hook position another middleware can
short-circuit. Reconsider only if upstream moves the increment to `before_model`, or offers an exit
that does not synthesise a message.

This is worth stating as a rule rather than a note, because the delegation looked obviously correct:
the middleware does enforce the same limit, its per-run channel is the same `UntrackedValue` shape
this repo had copied from it, and the swap passed a green suite. What it did not survive was being
composed with the rest of the stack.

## Consequences

- `agent_supersteps_per_model_call` goes back to a measured `4N + 3` (cap 1 → 7, cap 2 → 11,
  cap 3 → 15). The ceiling's constant **stays at `+ 3`** rather than returning to `+ 1`: at a cap of
  1 the old formula granted exactly 7 against a requirement of 7, and that zero margin is precisely
  what turned one added node into a `GraphRecursionError`. A ceiling sized to the exact requirement
  is one middleware away from being wrong.
- `tests/test_state_channels.py` is new and generalises the cause. Three defects in one week —
  `loop_capped`, `challenge_attempts`, and the skills marker — shared one shape: a hook tested by
  direct call, writing a channel the graph did not have. LangGraph drops such a write silently. The
  file drives a **compiled graph** for every channel `ChemclawState` declares, derived rather than
  listed so a field added tomorrow is covered.
- `D-2026-08-14`'s §1 measurement — that delegation cost +4 executable lines while removing a
  ~10-line hook — now reads as the whole case against it rather than as an aside.
