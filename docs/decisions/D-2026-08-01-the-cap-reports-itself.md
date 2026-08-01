# D-2026-08-01-the-cap-reports-itself — The cap reports itself

**Status:** accepted · **Date:** 2026-08-01 ·
**Supersedes:** `D-2026-08-01-a-scripted-transcript-gates-the-harness-not-the-judgment`, in the one
claim that `runaway_rate` reads the loop cap's *residue* · **Closes:** the backlog row "the loop cap
is silent, so nothing can alert on a runaway in production"

## Context

`runaway_rate` scored a turn a runaway when it answered with unchecked todos still open. That was
never a measurement of runaways — it was a proxy adopted because the thing it wanted to measure
emitted nothing: `AgentLoopMiddleware` stops at `harness_max_loop_iterations` and **returns
normally**, so a capped turn is externally identical to a finished one except for what it leaves
behind.

The proxy is wrong in the direction that matters. `mark_awaiting_job` opens a todo when a durable
job launches, and its marker lives in the todo's **description**, while `PlanEvent.todos` carries
only rendered display strings. So a turn that correctly starts a DFT run and says so leaves residue
byte-identical to a turn that gave up mid-plan. With `eval_runaway_max = 0.0`, one correct
deferral failed the gate.

`todo_plan_items` already strips those rows for exactly this reason — its docstring says counting
them "would let an approved plan revoke its own approval the first time it started a job". The
metric read the other view.

This was introduced earlier in this same programme. The metric's own tests and docstring were part
of the defect, which is why neither was treated as evidence when fixing it.

## Decision

**The cap reports itself, and the metric reads the report.**

MAF exposes no hook on the cap — `_evaluate_stop` short-circuits `should_continue`, and the
middleware is constructed inside `create_harness_agent`. But it does take *our* stop predicate, and
one fact is sufficient: **the loop stopped at the cap exactly when its last stop decision was
"keep going".** Every other stop is the predicate returning `False`. `observe_loop_cap` wraps the
predicate, decides nothing, preserves the `(bool, str | None)` feedback tuple, and records the last
answer on a contextvar holding a mutable record — the task-boundary reasoning `turn_signals`
already documents.

A capped turn emits `ErrorEvent(code="loop_cap_reached", retryable=False)` **before** its
`AnswerEvent`, for `CapabilityDegradedEvent`'s reason: a surface must mark an answer partial as it
lands, not after. The answer still goes out and the turn is still billed complete.

`loop_cap_reached` is an `ErrorCode`, not a new `Event` type — "the turn was cut off" already has a
shape, and this is the third member of the family beside `turn_timeout` and `budget_exhausted`.

**The residue heuristic is deleted, not narrowed.** `_open_steps` is gone and `runaway_rate` has one
rule: an exhaustion-family `ErrorEvent`. Any surviving form of the heuristic would either keep firing
on `awaiting-job:` residue or be a filter looking for evidence that is not in the transcript. A
metric that measures less and means it beats one that gates at 0.0 on a signal it cannot interpret.

A cap of 1 reports nothing, because MAF never consults the predicate. That is the honest reading — a
loop that never wanted another iteration was not stopped from taking one — and the module docstring
says so rather than leaving it as a silent edge.

## Consequences

- A production runaway is now visible: an SSE error event, a WARNING, and
  `chemclaw_turn_loop_caps_total`. Nothing could alert on it before.
- `chemclaw_turns_failed_total`'s help changes from "ended in an error event" to "emitted an error
  event", which is now the true statement — a capped turn emits one and still answers.
- The shipped eval case's first transcript is now the defect itself: a turn that launches a durable
  job, leaves two todos unchecked, and answers. It scored 1/3 and failed the gate before; it scores
  0/3 now.
- `Chemclaw3_ui` renders the new code through its generic error path without a specific label. No UI
  change is required for correctness, and the row recording that is closed with this noted.

## Alternatives rejected

- **Keep a narrowed residue check.** There is nothing to narrow it on: the `awaiting-job:` marker is
  in the description and the transcript carries only rendered strings, so the metric cannot see the
  distinction it would need.
- **Put the identity view into `PlanEvent`.** Widens a wire event, seen by every surface, to serve
  one metric — and it would still be inference. The cap knows it capped; asking it is shorter and
  exact.
- **Patch MAF or reimplement the loop.** A vendored fork of the harness to recover one bit that a
  predicate wrapper already yields.
