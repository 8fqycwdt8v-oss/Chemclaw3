# D-2026-08-27-a-per-worker-cap-is-not-a-backend-ceiling — the calculation backend gets the fleet check the other two budgets already have

## Status

Accepted. Found in the blind-spot audit (BS-07, core half) and fixed in the same pass. The
serving-side admission semaphore in `Chemclaw3-mcp`'s `servers/calc` is that repository's own
change; this ADR is about the half this repository can enforce.

## Context

Every calculation this system runs is dispatched to one pod
(`D-2026-08-16-the-physics-leaves-the-cache-stays`, `D-2026-08-26-semiempirical-is-the-whole-tier`):
`servers/calc`, addressed by `CHEMCLAW_CALC_SERVER_URL`, reached from
`connectors/calc/remote.py::calc_session` — one MCP session per call.

The concurrency cap that exists is `worker_max_concurrent_activities`, and it is **per worker
process**. `D-2026-08-05-a-worker-may-not-outrun-its-pool` set it to 8 against a Postgres pool of 8,
which is the right number for the resource that argument was about. It is not a bound on anything
the calculation server sees: `connectors.calc.workerReplicas` multiplies it, so scaling that worker
— an ordinary operational lever, one `kubectl scale` or an edited value — multiplies concurrent
CPU-bound load on a single shared pod. Nothing in either repository stated the product.

The failure is not graceful. That server pins `OMP_NUM_THREADS=1` deliberately, to avoid intra-run
contention, so surplus concurrency does not queue — it thrashes. Thrashing lengthens every run,
which trips the activities' heartbeat timeouts, whose retries land back on the same overloaded pod.
The audit reports this already cost roughly 50 minutes of saturated CPU on one occasion.

**This repository has solved this exact shape twice already**, and both times the finding was the
same: a per-process cap is not a fleet number until something computes the product.

- `D-2026-08-05-the-connection-budget-is-a-fleet-number` — `pg_pool_max_size` bounds one process;
  `pg_fleet_pooled_processes × pg_pool_max_size` is what the database sees. Stated in prose for a
  year, computed by nothing, and the shipped chart's real ceiling was ~272 against a
  `max_connections` of 100.
- SCALE-1's successor — `service_max_concurrent_turns` bounds one process; `replicas × uvicorn
  workers × cap` is what the shared LLM endpoint sees.

Both are `Settings` validators, both self-disable when the ceiling is undeclared, both are paired
with two gauges so that a Deployment scaled by hand — which no startup check can see, because
`Settings` never re-runs — is visible to an alert.

There is no reason for the calculation backend to be the one that gets a third shape.

## Decision

**The same mechanism, one subject over.**

`calc_fleet_worker_processes × worker_max_concurrent_activities` is checked against
`calc_backend_max_concurrent_requests` in the same `Settings` validator as the other two, with the
same self-disabling convention (`0` = undeclared = inert) and an error naming both sides, every
factor, and the two levers. The chart derives the left factor with a helper
(`chemclaw.calcWorkerProcesses`) from the `calc` bundle's own `workerReplicas`, so it is the number
Kubernetes obeys rather than a second copy of the topology — the reason `CHEMCLAW_PG_FLEET_POOLED_
PROCESSES` and `CHEMCLAW_SERVICE_FLEET_REPLICAS` are derived too. It renders `0` when this release
runs no calc worker, because a release that dispatches no durable calculation must not be refused
over calculations it never makes.

**Both settings live in `core/config/temporal.py`, beside the per-process cap they multiply**, not
in `calculators.py`. `tests/test_config.py` refuses a calculator field whose only reader is a
config validator, for a sharp reason: the calculation server reads that section's names under the
*same* `CHEMCLAW_` prefix, so a knob left there is one an operator can set on the wrong deployment
and watch do nothing. This is a worker-fleet budget, and it belongs where the fleet's cap is.

**The runtime pair is `chemclaw_calc_requests_in_flight` against
`chemclaw_calc_backend_max_concurrent_requests`, and its left-hand side is *live* where the other
two budgets use a configured capacity.** That difference is forced rather than chosen. Two kinds of
process dispatch to this backend and they do not share a cap:

- a `calc` **worker** pod, bounded by `worker_max_concurrent_activities`; and
- the `calc` bundle's own **MCP server** pods, which dispatch straight from a tool call
  (`compute_xtb_energy`, `predict_pka`, `compute_thermochemistry`, …) with no per-process cap at
  all.

No product covers the second. Counting the sessions actually held is the only number that covers
both, and `sum()` of it across pods is what `servers/calc` is being asked to serve right now. It is
counted in `calc_session`, the one place every remote calculation passes through, and it counts
sessions rather than round trips because a session is a connection that pod holds for as long as
the block runs — `cached_remote` deliberately keeps one open across its key lookup so a miss does
not pay a second connect.

The gauge is bound at import of `connectors/calc/remote.py`, on `core/db.py::pooling`'s rule: a
process cannot acquire the resource without also acquiring its witness. Importing that module is
what makes a process able to dispatch, and both dispatching pod kinds import it.

**The shipped ceiling is `0` — undeclared, therefore inert — deliberately, and that is a departure
from the other two.** `postgres.maxConnections: 136` and
`CHEMCLAW_SERVICE_FLEET_MAX_CONCURRENT_TURNS: 48` ship as real statements because both describe
something this release provisions: its own replicas, and the database it is given. This one
describes the admission semaphore of a pod in *another* release, whose CPU allocation this chart
cannot see. A number invented here would be a guess wearing the shape of a statement — the failure
mode this repository keeps finding in its own prose. An operator sets it to what that server
admits; until then the startup check and the alert are off and the live gauge still reports.

`ChemclawCalcBackendOverCommitted` is the alert, on the same self-disabling `max(ceiling) > 0`
guard as its two siblings.

## Consequences

- Scaling `connectors.calc.workerReplicas` past what the backend declares fails `Settings()` at
  startup, in every pod, naming the product and both levers — instead of being discovered as
  heartbeat timeouts and retry churn. That is a loud failure on an ordinary operational action, and
  it is the intended control: the same trade `pg_fleet_max_connections` already makes.
- The check covers the **durable half only**, because that is the half that can be derived. The
  interactive half is covered by the gauge and the alert, not by the startup check, and this is
  stated in the setting's own comment rather than left to be discovered.
- One number is added to what an operator must know about `servers/calc`. It is the same number
  that repository's admission semaphore is configured with, and the two halves are complementary:
  the semaphore protects the pod whatever reaches it, and this check tells a deployment it is
  misconfigured before anything reaches it at all.

## Evidence

- `tests/test_config.py` — an undeclared ceiling checks nothing; a fleet exactly at its ceiling is
  allowed (`>`, not `>=`); zero worker processes is legal and is not floored to 1; and the refusal
  names `32`, `16`, `4 calc worker process`, `8 activities each` and both settings. The last fails
  on the unfixed tree.
- `tests/test_calc_remote.py` — a held session reads `1` on the live exposition and returns to `0`,
  and three consecutive failed opens leave it at `0`, so an outage cannot make the saturation
  signal climb on an idle pod. Both fail on the unfixed tree.
- `tests/test_helm_chart.py` — the derived key is rendered from the chart's own values and loaded
  through `Settings`, the same way the other two derived keys are.
