---
id: autonomy-turn-cost
metrics: [turn_cost_ratio]
output:
  turns:
    - correlation_id: cost-01
      profile: default
      input_tokens: 18805
      output_tokens: 640
      cache_read_tokens: 0
      cache_write_tokens: 18805
      duration_seconds: 11.2
      completed: true
    - correlation_id: cost-02
      profile: default
      input_tokens: 2100
      output_tokens: 810
      cache_read_tokens: 18805
      cache_write_tokens: 0
      duration_seconds: 9.4
      completed: true
    - correlation_id: cost-03
      profile: default
      input_tokens: 3400
      output_tokens: 1220
      cache_read_tokens: 18805
      cache_write_tokens: 0
      duration_seconds: 14.8
      completed: true
    - correlation_id: cost-04
      profile: default
      input_tokens: 2950
      output_tokens: 0
      cache_read_tokens: 18805
      cache_write_tokens: 0
      duration_seconds: 30.0
      completed: false
reference:
  baseline_tokens: 60000
---
A four-turn session on the `default` profile, scored for what it cost rather than for what it
said — the axis `make eval` did not have until this case existed.

**The shape is the point, and it is the shape the ledger really produces.** The first turn pays a
cache *write* over the whole static prefix; the three after it pay a cache *read* of the same
prefix plus whatever the conversation added. That is why the metric weights the two counters
differently and scores billed token-equivalents rather than tokens sent: measured on 2026-08-25 the
default profile's static prefix is **18,805 tokens**, and a change that adds a cache breakpoint
moves the sent count up and the billed count down. A metric scoring what was sent would book that
improvement as a regression.

**The fourth turn never answered and is counted anyway.** `completed: false` is a turn torn down
before it produced anything — thirty seconds of wall clock, 2,950 input tokens against the cache,
and nothing to show. Those tokens were spent. A case that kept only the turns that finished would
be wrong in precisely the direction that hides a runaway, which is why `TurnCost` records the flag
instead of filtering on it and why the provenance line reports how many there were.

**Ungated on purpose**, the same posture `autonomy-plan-execute-utility` takes. There is no history
yet that says what a cost regression looks like on this case set, and a threshold guessed now would
gate the suite on a number nobody measured. What it buys today is a row in `baseline.json` that
`make eval-baseline-check` watches for drift — so the first time a change makes a turn materially
more expensive, something says so.

The baseline of 60,000 is this session's own recorded cost at the time the case was written, so the
ratio reads as 1.0-ish at rest. It is not a target and not a budget; `agent_context_token_budget` is
the budget and it is a different instrument entirely.

**What this case does not yet do, stated plainly: it does not measure the system.** Every number
above is a literal committed to this file, so `turn_cost_ratio` computes a constant of the case
data — 0.9845458333333333, to every decimal, no matter what changes in the agent. An earlier
version of this paragraph said the ratio "moves only when the system does", and that was written
about the *metric* while being read as a claim about *this case*; the metric does have that
property, and this case cannot exercise it, because nothing here is measured at run time. The
32%-static-prefix growth that the sibling ratchet in `tests/test_context_floor.py` caught would
leave this row in `baseline.json` untouched.

That makes it a fixture that proves the arithmetic — the cache weighting, the billed-vs-sent
distinction, the counting of turns that never answered — and a placeholder for the measurement.
Wiring it to real recorded `TurnCost` rows needs a deployment with turns in it, which is the same
blocker `BACKLOG.md` records for the memory-distillation work, and it is a row there.
