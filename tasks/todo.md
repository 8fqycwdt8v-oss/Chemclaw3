# Context management: the eight findings, implemented

Investigation (2026-08-28) measured the context stack end to end and found eight defects. This is
the plan that closes all eight. Each item names the finding it closes and the measurement that
justified it.

## The findings, ranked as measured

- [x] **F1 — a fan-out loses its own results before the model reads them.**
  `agent_max_parallel_tool_calls` is 8, `agent_keep_last_tool_groups` is 2, and upstream's `keep`
  counts tool *results*, not steps. Measured: a five-way fan-out over the 30k trigger had 3 of its
  5 results replaced by a placeholder before the model's first look at them.
  → `ClearOlderToolResultsEdit` raises `keep` to cover the newest batch, structurally.
- [x] **F2 — the budget's unit is 2.2x off from the billed unit, on the half it governs.**
  Measured (chars/4 against cl100k): static prefix 1.04x, tool schemas 1.00x, markdown 1.01x —
  connector JSON 0.45x, xyz geometry 0.47x.
  → the configured budget becomes a *billed*-token budget, converted to the estimator's unit by a
  ratio the system measures rather than guesses.
- [x] **F3 — two tool calls inside their own caps produce a 245,000-token request.**
  Measured: 2 x 200,000 chars = 100,077 estimated (one token over budget), 223,750 billed, both
  edits reclaiming nothing.
  → one cap on one tool result, in the one middleware that sees every result.
- [x] **F4 — the counter cannot see the unreducible turn.** Measured through a compiled graph:
  both series delta 0 on the turn above, while `core/metrics.py` documents a flat zero as "never
  over budget".
  → `chemclaw_context_unreducible_total` and a `context.unreducible` event.
- [x] **F5 — the ground truth is in hand at the point it is needed and unused.**
  → the observer feeds estimated-vs-billed into the ratio F2 reads, publishes it as a gauge, and
  `turn_costs` gains the two columns that make the policy joinable to the bill.
- [x] **F6 — clearing is all-or-nothing.** Measured: 18 of 20 results wiped where roughly half
  would have crossed back under the trigger.
  → `clear_at_least` computed per apply, so clearing stops at the trigger.
- [x] **F7 — no number anywhere is the model's context window.**
  → `llm_context_window_tokens`, and a budget derived from it minus this request's own measured
  prefix and its output reservation.
- [x] **F8 — the prefix ratchet cannot see endpoint tool schemas.**
  → `chemclaw_connector_tool_schema_tokens`, measured at handshake, by connector.

## Work

- [x] Settings: `agent_max_tool_result_chars`, `agent_context_calibration_*`,
      `llm_context_window_tokens`; `.env.example` parity.
- [x] `agent/context_budget.py`: the turn's context watch, the request prefix, the calibration
      ratio, `effective_trigger`.
- [x] `agent/compaction.py`: F1, F2, F4, F5, F6, F7 land here.
- [x] `agent/tool_result_size.py` + its place in the tool chain: F3.
- [x] `core/metrics.py`: four new series, and the corrected claim on the old one.
- [x] `infra/sql/069_turn_cost_context.sql` and the turn-cost plumbing: F5.
- [x] `connectors/transport.py`: F8.
- [x] Tests for every one of the eight, driving the shipped objects.
- [x] ADR + ledger row + CLAUDE.md.

## Review

Closed in `docs/decisions/D-2026-08-28-a-budget-in-the-wrong-unit-is-not-a-budget.md`.
Every finding has a test that fails without its fix. The two probes that found F1 and F3 are now
`tests/test_compaction.py::test_a_fan_out_never_loses_its_own_results` and
`::test_an_unreducible_thread_is_counted`.
