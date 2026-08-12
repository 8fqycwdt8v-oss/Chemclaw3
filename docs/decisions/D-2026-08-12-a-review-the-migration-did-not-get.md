# D-2026-08-12-a-review-the-migration-did-not-get — What 181 reviewers found in a migration that shipped green

**Status:** accepted · **Date:** 2026-08-12

Amends [`D-2026-08-10-langgraph-rebuild-of-the-conversation-layer`](D-2026-08-10-langgraph-rebuild-of-the-conversation-layer.md)
and [`D-2026-08-11-what-the-removal-found`](D-2026-08-11-what-the-removal-found.md). Both are merged
and are left untouched, as a merged ADR must be; the claims of theirs that turned out to be false
are corrected here rather than in place. Supersedes the *implementation* half of
[`D-025`](D-025-the-agent-keeps-its-chat-thread-within-a-token.md): the mechanism it chose was
deleted with the framework and this records what replaced it.

## Context

The rebuild shipped with `make lint type test` green, every validator green, CI green, and a suite
of 4155 tests. A post-merge review then ran 16 domain lanes over the 242-file diff, each finding
verified by two independent skeptics instructed to refute by default and one required to
*reproduce*, plus two critics on coverage and cross-lane seams, and three orthogonal passes over
deletions, documentation claims and the dependency surface. 81 findings were raised and 72 survived.

**The interesting part is not the count, it is what a green suite failed to see.** Three separate
defects each ended in a permanently unusable conversation, and none of them failed a test.

## What the green suite could not see, and why

### 1. Every field the checkpointer holds is per-*session* unless something resets it

`model_calls` was declared as an ordinary state channel and the graph is invoked with
`thread_id = session_id`. Nothing zeroed it, so the "per-turn" runaway cap counted the *session*:
measured at `harness_max_loop_iterations=3`, turns 0-2 answered and turn 3 returned the chemist's
own question, having never called the model — as did every turn after it.

The unit tests could not see it because they each drove **one** turn. A durable counter is only
wrong on the turn after the one you tested.

`state.turn_input` now names the per-turn fields and zeroes them, and it is the only way a turn
starts. The general rule: **the checkpointer's default is "remembered forever", so a per-turn field
is a claim that needs a mechanism**, not an adjective.

### 2. A batch of tool calls is judged against a pre-batch snapshot

`ToolNode` builds every call's runtime from one `_extract_state` and then `asyncio.gather`s them.
So `write_todos(plan B)` and a gated write in **one assistant message** were both judged against
plan A — the DARK-1 sequence the plan gate exists to prevent, reproduced end to end against the real
graph, the real middleware and the real approval store, on the chart's own defaults. The control
(same two calls, two messages) refuses correctly, which is what identifies batching as the whole
difference.

Every test in `test_plan_gate.py` drove the middleware one request at a time, which cannot express a
batch. The gate now refuses a gated call that arrives beside a plan rewrite, without asking the
store: the batch is atomic to the model, so "which came first" is unanswerable from inside it.

### 3. A durable thread that nothing bounds is a cliff, not a slope

Context compaction (D-025) went with the framework and nothing replaced it. Past the provider's
window a turn fails, the oversized thread is still checkpointed, and every later turn fails
identically. `ContextEditingMiddleware` with `ClearToolUsesEdit` is D-025's strategy in this
engine's vocabulary — deterministic, no second model call — wired to the two settings that map.
Measured: a 12-turn thread of evidence sweeps goes 180k → 30k estimated tokens once it crosses the
budget, and a 2-turn thread under it is untouched.

## The pattern behind most of the rest

Sixteen of the confirmed findings are one shape: **a property was moved to a new mechanism and only
the declaration moved.**

- `reject_widening` compares specialist *profiles*, and connector tools are passed down already
  open — so a specialist declaring one bundle received every bundle the supervisor had.
- `ChemclawState.awaiting_jobs` was declared as the replacement for the old marker convention and
  never written or read.
- `skill_backend`'s docstring says "every reach path, not the obvious one"; `BackendProtocol` has 22
  methods and `download_files` — which returns a file's full bytes — was not among the overrides.
- The test that was supposed to catch that says the paths are "enumerated from the protocol, not
  from a written list" and was a hand-written list of seven.
- `loop_capped` compared a count that cannot answer the question: the stopping branch does not
  increment, so a capped turn and a turn that spent its last call and answered end identically.

In each case the *claim* is in a docstring and the *mechanism* is absent, which is exactly the
failure mode this repo already names ("prose is evidence about what its author believed"). What is
new is the direction: these were written by the same change that removed the old mechanism, so there
was never a moment when the sentence was true.

## Corrections to the two ADRs this amends

Recorded here because a merged ADR is not edited:

- **`interrupt()` / `Command(resume=…)` / checkpointer time-travel never shipped.** The rebuild ADR
  names them as the mechanism unifying three human gates and replacing the rollback watermark.
  `plan_approval_store.py` and `interaction_tools.py` are byte-identical to their pre-migration
  selves and still wired; `session_todos` reads a checkpoint directly. The "−350 LOC" for the gates
  is 0, and `session_store.py` is −67 rather than −400.
- **`session_messages` is not "written from the checkpoint stream".** It is written from the turn's
  own text in `runner._record_transcript`, whose own docstring says so.
- **`api/events.py` was not "unchanged throughout".** It is +67 lines: two event types and an
  `agent` field, added by M9/M10 *after* the two engines were scored against each other.
- **The tool chain is seven middlewares, not six** (eight with the plan gate) — repeated in four
  places.
- **`message_from_row` was not "the only shape reader".** `message_pairing.stored_call_ids` decided
  the same question by a different rule, on the nightly deletion path. It takes the stamp now.
- **The live gate the rebuild ADR set for deleting the framework branch was not met.** It named a
  live concurrency probe, a durable-launcher probe, an end-to-end plan→approve→execute and
  `make eval-strict` against the old baseline. The branch was deleted with none of them run. That
  was the right call for a different reason — the defect the concurrency probe measured has no
  surface left once the dependency is uninstalled — but it was not the stated one, and nothing
  marked the gate as waived.

## Consequences

- A turn starts through `state.turn_input`; a caller that hand-builds `{"messages": …}`
  reintroduces defect 1, and `tests/test_langgraph_agent.py` asserts the reset.
- A gated call in the same batch as a plan rewrite is refused. A legitimate turn costs one retry.
- `surface_domain_errors` converts *any* tool exception into a result. A failed tool had always been
  a recoverable step; for the length of this migration it ended the turn instead.
- The migration pass refuses rows it would otherwise destroy (parallel results, unknown content
  types) rather than converting them, and `make db-migrate` actually runs it — it had no caller.
- Six tests that could not fail now can, four of them guarding security properties. Each was
  mutation-checked in the fixing commit.

## What this does not close

The three M12 probes are still unrun and still need a live credential; this review was static by
construction. Two capabilities are lost rather than fixed and carry BACKLOG rows with triggers: a
plan no longer shows which step waits on a durable job, and a conversation of pure prose is still
unbounded. `agent_teams_enabled` stays off by default, now for a second reason: its routing is
unmeasured *and* two of its four invariants were only enforced on declarations.
