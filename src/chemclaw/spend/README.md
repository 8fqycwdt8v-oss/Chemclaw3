# `spend/` — what a turn costs, and what bounds it

**Three modules, one policy.** Spend is measured, stopped and refused at three different moments;
they share one place that states the limits.

```
spend/
├── limits.py   # SpendPolicy (every limit) · BudgetTracker (per session, per principal) · the durable window
├── guard.py    # the per-turn brakes: runaway cap, billed-token cap, the metering that feeds it
└── ledger.py   # TurnUsage arithmetic · the ambient ledger · the `turn_costs` row
```

## Which limit stops what

| Limit | Setting | Enforced by | At |
| --- | --- | --- | --- |
| Model calls per turn | `harness_max_loop_iterations` | `guard.enforce_loop_cap` (`before_model`, over `ChemclawState.model_calls`) | each model call |
| Billed tokens per turn | `agent_max_turn_billed_tokens` | `guard.enforce_spend_cap`, fed by `guard.MeterTurnSpend` | each model call |
| Turns and tokens per session / per user window | `budget_*` | `limits.BudgetTracker.check` / `record` | before and after a turn |
| Conversation size | `session_max_thread_bytes` | `limits.check_thread_size` | before a turn |

`SpendPolicy.current()` reads the settings at the moment of a decision; no site keeps a copy.

## What does not change here

- The runaway cap is a first-party `before_model` counter, never upstream's
  `ModelCallLimitMiddleware`: a counter in `after_model` can be skipped by a later hook that jumps.
- A capped turn ends rather than raising, so its answer still goes out, marked partial. Both caps
  grant one tool-less wrap-up call (`guard.AnswerAtTheCap`) before ending: a turn that ends on a
  cap with no answer has spent its budget for nothing, and no number prevents that.
- `TurnCost` (the record) stays in `core/turn_cost.py` so `evals/` can score it without importing
  `agent/`. `agent/context_budget.py` is a context concern, not a spend one.
- The per-user window is the `budget_usage` table and the ledger is `turn_costs`; neither moved.
