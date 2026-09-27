# D-2026-09-14-a-cost-metric-that-reads-a-file-measures-the-file — a cost metric that reads a file measures the file

**Status**: accepted

## Context

`turn_cost_ratio` is a good metric wired to a case that cannot exercise it.
`data/evals/cases/autonomy-turn-cost.md` commits four literal `TurnCost` records, so the metric
returns `0.9845458333333333` to every decimal whatever changes in the agent. The case file says so,
the metric's docstring says so, and `evals.baseline.render_comparison` labels the row `pinned` — so
this is not a hidden defect, it is a stated one nobody had the inputs to close. The consequence is
concrete: the 32% static-prefix growth `tests/test_context_floor.py` caught would leave the
`baseline.json` row untouched, because nothing in the scoring path ever runs the agent.

`BACKLOG.md` recorded it as blocked on "a deployment with turns in it". That turned out to be true
of the *shape* of the fix and false of its availability: `make live-up` brings a front door, four
workers, seven connectors and an OpenAI-compatible mock gateway up on loopback in about a minute,
and every turn it serves writes a real `turn_costs` row.

## Decision

`make live-turn-cost` (`chemclaw.cli.live_turn_cost`) drives a fixed three-turn workload through a
running front door, reads back the `turn_costs` rows *that session* produced, scores them with the
same `turn_cost_ratio`, and fails on a worsening drift past `eval_drift_epsilon` against a recorded
case. `--emit` re-records that case, the same deliberate act as `make eval-baseline`.

Three things decide whether this measures anything, and each was measured rather than assumed.

**The bill has to follow the request.** The obvious wiring — read whatever rows the lane happens to
hold — produces a second gate that cannot fire: 20 of the 22 behaviours in `cli/storm_behaviours.py`
name a *constant* `input_tokens` of 900, so a turn against the default bills 900 whether the prefix
is 40,000 tokens or 400,000. The workload therefore carries `h-size-billed`'s marker, the one
behaviour whose bill is `input_tokens_per_char` over the serialized request — which is where the
instructions, the skills listing and every bound tool schema are. Against a real gateway the marker
is inert prose and the gateway bills for itself.

**The workload has to be driven, not found.** Cost is a property of (system, workload). A reader
over whatever rows a lane left behind compares two different questions and calls the difference a
regression.

**The recorded expectation is an ordinary eval case.** One format, one loader, one metric: `make
eval` prints the measured case beside the arithmetic fixture with its own provenance. The fixture
stays, because it is the only case that exercises the cache-read/cache-write weighting and the
counting of a turn that never answered, and it is honest about being a fixture.

## What was measured

**The gate goes red for the regression it exists to catch.** Prefix mutation: ~10,000 characters
appended to `instructions_for`'s default prompt, the shape of the growth the context-floor ratchet
catches.

| | `make eval-baseline-check` | `make live-turn-cost` |
| --- | --- | --- |
| shipped tree | exit 0 | exit 0 — ratio `0.900198` |
| prefix grown | **exit 0** | **exit 1** — ratio `0.950454`, delta `+0.05026` against a band of `0.04501` |

The offline gate is blind to it in both directions, which is the row's claim, now driven rather
than argued.

**And a cost this lane can see that nobody had seen.** Across two boots of the *same commit*, the
same three questions cost **900,198** and **429,076** billed token-equivalents — a factor of 2.1,
stable and byte-identical within each boot across repeated runs. The ledger says how the two differ
in its own columns: `context_unreducible` true on 3 of 3 turns of the expensive boot and false on 3
of 3 of the cheap one, with the model calling a tool on every turn against one of three. It is not
attributed here, and the honest reading of the per-call sizes (299,826 chars against 214,206) is
about 21,000 estimated tokens of prefix present in one boot and absent in the other, against a
measured connector schema total of 31,208 — so a bundle whose tools were not bound is the leading
candidate and is not evidence. `BACKLOG.md` carries the row.

That instability is why the command **prints the regime** beside the ratio, and why the emitted case
carries `tool_calls`, `compacted` and `context_unreducible` although the metric reads none of them:
a red run that says "and the context was unreducible on 3 of 3 turns" is a diagnosis, and the same
red without it is a mystery. It is also why this is a `live-*` target and not a `ci` step — the
lane's job is to tell somebody a cost moved, not to block a pull request on a boot.

## Consequences

- A fourth module reads `turn_costs`, listed in `tests/test_turn_cost.py` with the command that
  asks it, per `D-2026-08-29-a-trail-nobody-can-read-answers-no-question`.
- The emitted case carries what a turn *cost* and nothing that identifies who asked for it. The
  first version used `model_dump()` and published `model: ""` and `outcome: "unknown"` beside real
  numbers — defaults wearing the appearance of measurements, in a file whose whole claim is that
  every number in it was measured — along with `actor` and `session_id`.
- Case-set `live-cost-2026-09-14`; `turn_cost_ratio`'s aggregate moved because a second case joined
  the mean, which is not a cost change.
- `turn_cost_ratio` is still `pinned` in `make eval-baseline-check`, and that is correct: a
  committed file cannot be anything else. What scores the system is the command.

## What keeps it true

- `tests/test_live_turn_cost.py::test_the_workload_asks_the_one_behaviour_whose_bill_follows_the_request`
  — asserted against the behaviour catalogue, not the marker string, so pointing the workload at a
  constant-billing behaviour fails here instead of producing a lane that measures nothing. Driven:
  `[[a-cheap]]` reddens it.
- `tests/test_live_turn_cost.py::test_a_worsening_drift_is_a_nonzero_exit` — the recorded turns exit
  0, a tenth more input exits 1, half the input exits 0. Driven: deleting the `return 1` reddens it.
- `tests/test_live_turn_cost.py::test_the_recorded_case_was_measured_rather_than_written` — every
  recorded turn bills more than `Behaviour`'s constant default.
- `tests/test_live_turn_cost.py::test_an_unreachable_lane_is_never_a_pass` — exit 3, never 0.
- `tests/test_live_turn_cost.py::test_the_emitted_case_carries_no_identity` — driven:
  `model_dump()` without the field set reddens it.
- `tests/test_turn_cost.py::test_every_turn_cost_reader_has_the_surface_that_asks_it` — the new
  reader is listed with its command.
