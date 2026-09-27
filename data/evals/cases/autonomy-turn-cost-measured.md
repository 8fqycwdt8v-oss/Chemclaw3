---
{
  "id": "autonomy-turn-cost-measured",
  "metrics": [
    "turn_cost_ratio"
  ],
  "output": {
    "turns": [
      {
        "correlation_id": "4e00fab3c85948e29761085c85961f6f",
        "profile": "default",
        "input_tokens": 299826,
        "output_tokens": 240,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "estimated_tokens": 0,
        "duration_seconds": 0.4395516829972621,
        "completed": true,
        "tool_calls": 1,
        "compacted": false,
        "context_unreducible": true
      },
      {
        "correlation_id": "9670949c80b2470db42a11f6c5b8ebd9",
        "profile": "default",
        "input_tokens": 299826,
        "output_tokens": 240,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "estimated_tokens": 0,
        "duration_seconds": 0.4465562909899745,
        "completed": true,
        "tool_calls": 1,
        "compacted": true,
        "context_unreducible": true
      },
      {
        "correlation_id": "a2fee8555fbf4150b675fa575ecd4534",
        "profile": "default",
        "input_tokens": 299826,
        "output_tokens": 240,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "estimated_tokens": 0,
        "duration_seconds": 0.48561189899919555,
        "completed": true,
        "tool_calls": 1,
        "compacted": true,
        "context_unreducible": true
      }
    ]
  },
  "reference": {
    "baseline_tokens": 1000000
  }
}
---
**Measured, not written.** Every number above came out of `turn_costs` after
`python -m chemclaw.cli.live_turn_cost --emit` drove 3 scripted turns through a
running front door; none of it was chosen. That is the whole difference between this case and
`autonomy-turn-cost`, which commits invented records and therefore scores a constant.

It is still a committed literal, and so is still `pinned` in `make eval-baseline-check` — a file
cannot be anything else. What makes the metric score the *system* is the command that produced it:
`make live-turn-cost` re-drives the same three questions, scores the fresh ledger rows with the same
metric, and fails on a worsening drift past `eval_drift_epsilon` against the value here. The static
prefix is inside that number, because the workload asks the mock behaviour whose bill follows the
serialized request.

Refresh it the way a baseline is refreshed: deliberately, in a reviewed commit, when the cost
genuinely changed and the change is the intended one.
