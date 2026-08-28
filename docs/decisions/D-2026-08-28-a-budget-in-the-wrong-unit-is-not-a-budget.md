# D-2026-08-28-a-budget-in-the-wrong-unit-is-not-a-budget — eight measured defects in the context stack

## Status

Accepted. Found by measuring the whole context stack end to end — the static prefix, the thread
policy, the payload ceilings, the offload surfaces, persistence and metering — rather than by
reading it. Every number below was produced by running the shipped objects.

## Context

`D-2026-08-11-a-policy-nobody-can-see` restored D-025's compaction policy after the framework
rebuild had left three settings, a config comment and a system-prompt sentence describing a
mechanism that no longer ran. That fix was right and it holds: the conversation window genuinely
bounds a prose thread (measured: 40 groups, 152,630 estimated tokens in, 45,792 out, no orphaned
tool pairing across a sweep of budgets), the degradation guards work, and the system prompt now
describes something real.

What was never checked is the arithmetic the policy is handed. Eight defects, and the first three
each cost a chemist something.

**The unit is not the unit anybody meant.** Both triggers count with `count_tokens_approximately`
— chars/4. Measured against a real BPE tokenizer over this repository's own payloads: the static
prefix **1.04x**, tool schemas 1.00x, a knowledge-graph note 1.01x, an ELN export 0.80x — and a
connector JSON result **0.45x**, an xyz geometry 0.47x. The estimator is good at prose and schemas
and roughly half the truth about structured chemistry, which is precisely the payload class the two
triggers exist to reclaim. So a thread the policy put at 100,077 tokens was **223,750 billed**.

**A fan-out loses its own results before the model reads them.** `agent_max_parallel_tool_calls` is
8; `agent_keep_last_tool_groups` is 2; upstream's `keep` counts tool *results*, not steps — a
distinction this repository had already written down. The edit runs in `wrap_model_call`, so the
list it reduces already holds the results that came back in the step immediately before. Measured
on a five-way fan-out past the trigger: **three of five replaced by a placeholder** the model had
never had a chance to read, each reading "Earlier tool result" about a result that was not earlier.
The comment beside `agent_max_parallel_tool_calls` records a live **40-call** fan-out, which at
`keep=2` is 38 results discarded before first read. What reaches the chemist is not a slow turn: it
is an answer resting on two of five values with nothing saying so.

**Nothing bounded one result.** `connector_max_request_bytes` caps what this system *sends* a
server; there was no cap on what came back, from a server or from an in-process tool. Both edits
carve out the newest results and the newest group, so a single oversized result is structurally the
one thing neither can reclaim. Measured, with each result inside its own tool's ceiling
(`document_read_max_chars` 200,000; `calc_find_max_results` x `calc_find_max_result_chars` =
200,000): two of them are 100,077 estimated tokens, ~224,000 billed, ~245,000 with the prefix, and
**both edits ran and reclaimed nothing**.

**And the counters could not see that turn.** Driven through a compiled graph, that thread moved
`chemclaw_context_compactions_total` and `chemclaw_context_reclaimed_tokens_total` by **zero** and
emitted no event, because a reduction of zero publishes nothing. `core/metrics.py` documented a
flat zero as "never over budget". It meant "never *reduced*" — and the difference is the turn about
to fail at the provider's context limit.

Four more, each smaller and each real:

- **The ground truth was in hand and unused.** `RecordContextCompaction` computes the estimate and
  then awaits the call whose response carries `usage_metadata["input_tokens"]`. Both numbers, one
  function, three lines apart, never compared. `turn_costs` likewise had no column saying whether
  the policy acted, so its cost could not be joined to the bill it exists to reduce.
- **Clearing was all-or-nothing.** `clear_at_least` defaults to 0, which never breaks upstream's
  loop: measured, one token over the trigger wiped **18 of 20** results where roughly half would
  have crossed back under.
- **No number anywhere was the model's context window.** The ceiling was discovered from a
  `BadRequestError` after the request had been assembled, sent and rejected.
- **The prefix ratchet sees half the prefix.** `tests/test_context_floor.py` gates every in-process
  tool schema (28,123 tokens on `default`) and cannot see an endpoint tool's, which arrives from a
  running server at handshake — as that file says about six `chem` tools.

## Decision

**The configured budgets are budgets in billed tokens, and the conversion is measured.**
`agent/context_budget.py` folds every model call's `billed / estimated` into a smoothed
process-wide ratio and `effective_trigger` divides by it. The ratio is **1.0 until
`agent_context_calibration_min_calls` observations**, so an un-upgraded deployment and a fresh
process behave exactly as before, and it is **clamped at 1.0 from below**: a mismeasurement can
make the policy compact earlier than needed and can never let it believe a request is smaller than
it is. `chemclaw_context_estimator_ratio` publishes what it is dividing by.

**The newest tool-call batch is never cleared.** `ClearOlderToolResultsEdit` raises `keep` to cover
the batch that answers the newest tool-calling assistant message, so a fan-out survives its own
model call structurally rather than because a number happened to be large enough. It also passes
the overshoot as `clear_at_least`, so clearing stops at the trigger instead of at the end of the
candidate list.

**One tool result is bounded.** `agent/tool_result_size.py` cuts a result over
`agent_max_tool_result_chars` head-and-tail — both ends, because a procedure states its outcome at
the end (`agent/condense.py`'s argument, generalised) — with a notice naming the tool, the
arithmetic and the remedy, marked as system text. It sits inside `frame_connector_results` so the
envelope wraps a bounded payload, and outside the governance chain so the audit trail still records
what the tool returned. Default 60,000 characters: the number this repository already chose for
`gather_evidence_max_chars`, its largest deliberate evidence payload. 0 restores the old behaviour
as a decision someone makes.

**The unreducible request is counted and named.** `chemclaw_context_unreducible_total` and a
`context.unreducible` event fire when the policy has finished and the request is still over the
budget — whether it reclaimed nothing or reclaimed plenty and could not reach the line. It is the
only leading indicator this system has for a context-length failure. The false claim beside the
compaction counter is corrected in the same commit.

**`llm_context_window_tokens` states what the model can hold**, 0 when a deployment cannot say. When
declared, the conversation budget becomes the smaller of the configured number and
`window - this request's measured prefix - llm_max_tokens`. The prefix has to be measured per
request because it is ~28,000 tokens and sits outside the budget; a `ContextEdit` cannot see it, so
`MeasureRequestPrefix` publishes it into an ambient the edits read.

**`turn_costs` gains `compacted` and `context_unreducible`** (migration 069), fed from a per-turn
`TurnContext` started beside the repeat guard's watch by the two callers that bracket a turn. That
also moves `peak_reclaimed` off `TurnCallWatch`, where compaction had parked a fact about a subject
the repeat guard has nothing to do with.

**`chemclaw_connector_tool_schema_tokens` measures the fleet's half of the prefix** at handshake, by
connector. It cannot be a ratchet — the number belongs to a server this repository does not build —
so it is a measurement, and its sum plus the ratcheted floor is what a turn costs before anybody
speaks.

## Consequences

- A deployment that observes nothing behaves exactly as it did. Every correction above is inert
  until it has been measured, declared, or exceeded.
- The tool-result cap **changes** what a very large `read_document` or `find_calculations` call
  returns. That is the point — such a result could not fit the budget it was inside — and the cut
  says so in the result rather than silently.
- Stopping the clearing at the trigger costs a token recount per cleared result, because that is
  how upstream's `clear_at_least` loop is written. Measured on threads of 20 and 60 results at
  20,000 characters each: **0.35 ms against 0.19 ms**, and **1.38 ms against 0.45 ms**, while
  clearing 3 instead of 18 and 7 instead of 58. A millisecond against a model call that takes
  seconds, for results the turn keeps.
- The calibration is a feedback loop on every model call, which is why it has an off switch and a
  clamp, and why it only tightens.
- Two things are deliberately *not* done. The cleared payload is still not offloaded to `/scratch/`
  — that would let a placeholder name a path the model could re-read, and it needs a state write
  from `wrap_model_call` that upstream's `ContextEdit` protocol has no room for. And
  `token_count_method="model"` is not adopted: it is upstream's own exact counter, but for Anthropic
  it is an HTTP round trip per model call, and a counter that raises is swallowed by `GuardedEdit`
  into "compaction silently disabled".

## Verification

`make lint type test` green. Per finding, a test that fails without its fix:

- the fan-out — `tests/test_compaction.py::test_a_fan_out_never_loses_its_own_results`, asserted
  over the whole batch rather than a count. Re-measured on that thread after the fix: the
  unwrapped upstream edit clears **3 of 5** of the newest batch and the first-party one **0 of
  5**, while both still clear all five older results — bounded, not disabled;
- the unit — `tests/test_context_budget.py`, both directions of the clamp and the sample floor;
- the single result — `tests/test_tool_result_size.py`, including that both ends survive and a
  block list keeps its blocks. Re-measured end to end on the two-call thread above: **277,922
  billed tokens before, 102,386 after**, the estimate falling from 100,077 to 30,239;
- the unreducible turn — `tests/test_compaction.py::test_an_unreducible_thread_is_counted`, driven
  through a compiled graph, asserting the compaction counter does **not** move;
- the window, the prefix and the ratio's wiring — `tests/test_context_budget.py`;
- the fleet's prefix —
  `tests/test_connector_transport.py::test_a_handshake_publishes_what_its_tool_schemas_cost_every_turn`.
