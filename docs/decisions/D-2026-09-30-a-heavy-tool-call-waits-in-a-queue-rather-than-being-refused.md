# D-2026-09-30-a-heavy-tool-call-waits-in-a-queue-rather-than-being-refused — a heavy tool call waits in a global queue for a slot, instead of being refused by a full pod

**Status:** accepted · **Date:** 2026-09-30 · **Decided with** the owner (2026-09-30): every
compute-heavy workflow queues; target p95 ≤ 30 s for seconds-class calls at 50 concurrent chemists;
KEDA availability on the target cluster is unknown, so backlog autoscaling ships switched off; retro
is out of scope · **Pairs with** `Chemclaw3-mcp`'s
`D-2026-09-30-a-full-pod-says-so-in-one-format-and-occupancy-is-the-scaling-signal` (one
at-capacity format for the fleet, admission-occupancy gauges, opt-in occupancy scaling).

## Context

A tool call made inside a turn went straight to its server, and every compute server in the family
**refuses** rather than queues once its admission slots are full — deliberately, because a queue per
pod behind a round-robin Service holds a call on a busy pod while its neighbour idles, and outlives
the caller's timeout. The cost landed on the chemist: measured on `servers/calc`, 60 concurrent
`compute_xtb_energy` calls against one pod were refused 68% of the time, and in-turn nothing
retried. Only calc's refusal was even recognisable as "full"; `rxnpredict`, `pyexec` and the rest
refused with a plain error that read as bad input. And when a busy backend sat behind one of this
repository's own bundles, `connectors/server.py::_sanitize_tool_errors` replaced
`CalcBusyError` — not a `ValueError` — with "an internal error occurred", so it read as broken.

The durable jobs already queued (`connectors/jobs.py`), but they retried a full backend at 112 s,
225 s, 450 s, 900 s: polling, not a queue — slots idle between asks and no arrival order.

## Decision

1. **A manifest may route named tools through a queue**: `endpoint.queued: {tools,
   inline_wait_seconds}` (`connectors/manifest.QueuedDispatch`). Only a tool whose answer is a
   function of its arguments belongs there, because identical concurrent calls join one run.
2. **The seam is the adapter's own tool interceptor** (`connectors/transport.py::_interceptors`,
   `connectors/queued.py`). The bound tool — name, schema, description, `SERVED_BY` stamp, content
   conversion — is the adapter's unchanged object, so authorization, audit, the plan gate and result
   framing see an ordinary connector call. Only the last hop becomes a `QueuedToolWorkflow`
   (`connectors/queued_workflow.py`) on `connector-<name>-interactive`.
3. **A separate interactive queue and worker per connector** (`connectors/interactive_worker.py`),
   so an hour-long job never holds the slot a seconds-long answer waits for.
4. **Pull, not push.** The interactive worker's `worker_max_concurrent_activities` is set to the
   server's slots ÷ its replicas, so the backlog waits in Temporal, first come first served, and a
   refusal is a rare race retried within seconds (`durable/publish.py::queued_tool_retry`, 1 s → 10 s
   cap), not minutes. A domain refusal is the tool's answer and is never retried; any other fault is
   retried `queued_tool_fault_attempts` times and then failed, so an outage is news now.
5. **The turn waits `inline_wait_seconds`** (45 s for calc, BO and rxnpredict). Inside it, the
   answer is the tool's own result. Past it, the turn gets a job id announced with `job_started`, and
   the answer arrives in the session mailbox as `job_completed` — the path every durable job takes,
   decoded by the same envelope, so `get_durable_job_status` needs no branch.
6. **Identical concurrent calls share one run** (`queued_workflow_id`, `USE_EXISTING`): the
   cross-process single-flight `cached_compute` lacks, for queued calls.
7. **A full backend says so, in the fleet's one format**: `core/errors.AtCapacityError` carries
   `[<server>-at-capacity]`, the connector sanitizer puts it at the head of the wire text, and
   `core/mcp_session.at_capacity` matches the format with the head-of-message rule.
8. **Autoscaling on the queue's backlog ships off** (`keda.enabled`,
   `templates/keda-interactive.yaml`, KEDA's `temporal` scaler). The interactive workers have fixed
   replicas by default; the floor, not the autoscaler, is what answers a burst, because a new pod
   takes a minute or more to be ready.

Queued today: calc's nine calculations (not `predict_solubility`/`predict_developability_profile`,
milliseconds of RDKit, and never `report_measurement`, a per-chemist write), BO's `predict_outcome`
(not `suggest_next_experiment`, which records a chemist's own suggestion), and rxnpredict's four
predictions.

## Alternatives weighed

- **Queue inside each server** (a semaphore at admission). Declined for the reason the servers
  already give: per-pod, invisible, and it outlives the caller. The queue belongs where it is global
  and durable, which here is Temporal.
- **Retry in the turn** on an at-capacity refusal. No arrival order, every waiting turn holds a
  model context open, and the retry storm scales with the number of chemists.
- **Convert every heavy tool into a manifest `jobs:` entry.** Rewrites twelve tool surfaces, moves
  their docstrings out of the servers, and runs each through `ConnectorJobWorkflow` on the single
  background worker — a job record, a graph publish and a wrapper per single-point energy. The
  interceptor changes one hop and nothing the model sees.
- **Queue `suggest_next_experiment` as well.** Declined while identical calls join one run: it
  writes `bo_campaigns`/`bo_suggestions` for the chemist who asked.
- **Count the interactive worker's pods in the Postgres budget.** It holds no pool — measured, zero
  connections from an idle worker against the dev database, and its one activity makes an MCP call —
  so charging it one per pod would have pushed the rollout peak from within 256 to 303 for
  connections nothing opens. It is counted at zero and a test holds the zero.

**Revisit when:** the Custom Metrics Autoscaler is confirmed on the target cluster (turn
`keda.enabled` on and size `maxReplicas` together with the servers' own occupancy scaling), when a
queued tool needs per-caller results (the run-sharing id must then carry the actor), or when
`chemclaw_calc_backend_at_capacity_total` stays non-zero with the backlog at zero (the worker
concurrency is set above the server's slots).

## What was measured rather than assumed

- An idle interactive worker opened **0** Postgres connections (41 before, 41 during, sampled every
  5 s for 35 s against `make up`'s database).
- `tests/test_queued_tools.py` drives all of it: the interceptor through `open_connector_specs`
  against a real server; the activity against a real server for an answer, a domain refusal and a
  full backend; and, on a real Temporal dev server, an inline answer, three refusals then an answer,
  five identical concurrent calls costing one run, and a call outlasting the wait delivered as
  `job_completed`.
- **Fifty concurrent calls, direct vs queued**, against a stand-in server with an admission ceiling
  and a fixed call time, on `make up`'s Temporal, through the real interceptor, workflow, activity
  and worker (worker concurrency = slots):

  | slots | call | direct: refused | queued: answered in turn | queued p50 / p95 / max |
  |---|---|---|---|---|
  | 8 (calc's floor) | 2 s | 34 of 50 (68%) | 50 of 50 | 9.4 / 14.9 / 16.0 s |
  | 16 | 2 s | 29 of 50 (58%) | 50 of 50 | 5.9 / 8.7 / 9.1 s |
  | 32 (calc's ceiling) | 2 s | 15 of 50 (30%) | 50 of 50 | 4.8 / 6.9 / 7.0 s |
  | 8 | 5 s | — | 50 of 50 | 20.8 / 32.6 / 36.3 s |
  | 16 | 5 s | — | 50 of 50 | 12.2 / 19.4 / 21.2 s |

  The 68% is the figure measured on the real calc pod, reproduced. Queued, the tail is
  ≈ ⌈50 ÷ slots⌉ × call time plus ~2 s of broker overhead, so **the warm floor is the sizing
  decision**: 8 slots meet p95 ≤ 30 s for 2-s calls and not for 5-s ones, which need 16.
- A retry policy whose first interval exceeded its cap was refused by the server as
  "BadScheduleActivityAttributes: missing task queue name" — found by the tests, fixed by clamping.
