# D-2026-08-01-a-drain-is-not-a-kill-with-extra-steps — A drain is not a kill with extra steps

**Status:** accepted · **Date:** 2026-08-01 · **Extends:** D-011 (never compute twice), D-069 (the
host-local checkout lock), D-121 (the Route pins a browser to one pod),
D-2026-08-01-every-process-carries-its-own-witness (the worker HTTP surface)

## Context

A backlog row filed three things together — no PodDisruptionBudget, no topology spread, no graceful
shutdown — and they share a premise worth stating plainly: **nothing in this system distinguished
"this pod is going away" from "this pod died".** Every mechanism Kubernetes offers for the first was
absent, so every voluntary disruption behaved like an involuntary one.

**The front door.** `terminationGracePeriodSeconds` was unset, so the default 30 s applied against a
`service_turn_timeout_seconds` of 600. Every rolling update, node drain and scale-down therefore
SIGKILLed whatever turns were in flight — and for this service that is worse than lost capacity,
because the state that would make a turn resumable is deliberately in the pod's memory: uploaded
attachments, the harness todo list and the live `AgentSession` (D-121, which is also why the Route
pins a browser to one pod). There was no `preStop` hook either, so the Endpoint removal and the
SIGTERM raced: the router kept choosing a pod that had already stopped accepting.

**The workers.** `asyncio.run(main())` wrapped `worker.run()` and contained no shutdown at all.
Python installs no SIGTERM handler, so the default disposition applied and the process died
immediately, mid-activity, with nothing unwound. Temporal makes the *work* survivable, and that
sentence was doing more work than it could carry: a long activity is re-run from the beginning, the
retry does not start until the start-to-close timeout elapses (for `qm`, the whole HPC poll budget),
and the pod's own cleanup never ran — `db.pooling()`'s connections dropped rather than closed, a
PR-gate git checkout abandoned mid-way.

**Placement and eviction.** `minReplicas: 2` bounds what the HPA runs and says nothing about where
those two land or how many may be evicted at once. Both could be scheduled onto one node, and one
drain could take both, so the second replica bought nothing against either failure it exists for.

## Decision

**Derive every shutdown budget from the work it must outlast, and spread what can be spread.**

`durable/serve.py` is one function, `serve_worker(worker, component=...)`, that installs SIGINT and
SIGTERM handlers, opens the Postgres pool, serves the probe/scrape surface, and on a stop signal
calls `Worker.shutdown()` — stop polling, let in-flight activities finish, cancel what remains after
`graceful_shutdown_timeout`. Both worker entrypoints are now one call.

The chart stops writing timeouts as numbers:

| Pod | Grace period | Derived from |
|---|---|---|
| front door | turn timeout + `service.drainSeconds` | `CHEMCLAW_SERVICE_TURN_TIMEOUT_SECONDS` |
| every worker | drain budget + 30 s | `CHEMCLAW_WORKER_GRACEFUL_SHUTDOWN_SECONDS` |

Both keys move into `values.yaml`'s `config:` block for exactly this reason, and a test pins that
they exist — `int nil` renders 0, so an absent key would silently collapse a grace period to its
margin. The front door gains a `preStop` sleep of `drainSeconds`, a `topologySpreadConstraints` over
`kubernetes.io/hostname`, and a PodDisruptionBudget.

## Why not the alternatives

**One number for the grace period.** It reads fine and drifts the moment anyone tunes the thing it
was chosen against. An operator raising the turn budget would start having turns SIGKILLed at the
old grace period, with nothing to indicate why — the same shape as the `connectorPort` rule the
NetworkPolicy already follows ("one value, no drift").

**`minAvailable: 1` on the front door.** Equivalent at two replicas and wrong at six: it would
permit five of six pods to be evicted simultaneously. Each pod holds session state a sibling cannot
see, so the quantity that matters is how many conversations one drain can end, and that is
`maxUnavailable`.

**A PDB on the background worker.** Rejected as actively harmful. `workers.background.replicas: 1`
is a hard singleton because the PR-gate checkout lock is host-local (D-069), and over a singleton
`minAvailable: 1` makes the pod un-evictable and blocks every node drain in the cluster
indefinitely, while `maxUnavailable: 1` permits exactly what no PDB permits. The singleton is the
real availability gap; it needs the distributed lock, it remains an open backlog row, and papering
over it with a policy object would make the cluster worse rather than the worker better.

**`whenUnsatisfiable: DoNotSchedule`.** It would make the spread a guarantee and make a single-node
dev or CI cluster unable to run the chart. `ScheduleAnyway` is the honest strength of the claim:
spreading is not something this chart can promise on infrastructure it does not own.

**A longer worker drain than 120 s.** The bound is real in both directions. Below it, a drain that
cancels everything immediately is a hard kill with extra steps. Above it, a node drain is held open
by work Temporal would happily retry — the retry exists, it is correct, and paying ten minutes of
drain to avoid using it is the wrong trade. 120 s finishes a short activity (a note re-index, a
digest, an ELN page) and abandons a long one to the mechanism already built for it. The front door
is the opposite case and gets the opposite answer: there is no retry for a chemist's turn, so its
grace period is the whole turn budget.

## Consequences

- A rolling update of the front door can now take up to `600 + 15` seconds per pod. That is the
  stated cost, and it is the right one: the alternative is ending conversations to deploy faster.
- A worker drains instead of dying, so `db.pooling()` closes and an in-flight activity finishes
  rather than waiting out a start-to-close timeout to be re-run elsewhere.
- SIGINT takes the same path as SIGTERM, so a developer's Ctrl-C exercises the code the cluster
  runs rather than a second path nobody tests.
- One mutation could not be killed by the test as first written, and the fix is worth recording:
  removing the `await` on the run task after `shutdown()` looked redundant (the real `shutdown()`
  waits for the drain) and survived. What it actually buys is *propagation* — an error raised while
  finishing the last activity would otherwise be attached to a task nobody looks at, and the pod
  would exit 0. A broken drain reported as a clean one is the same class of lie as the `Running` pod
  with a dead poll loop that the previous ADR removed. There is now a test for that specific timing.

## Not in this change

The background worker is still a singleton, and this ADR deliberately does not hide that behind a
PDB. `maxReplicas: 6` still multiplies the per-process admission ceiling — a separate row about
what the guard means, not about how pods die. The migration Job still takes no advisory lock and
has no `activeDeadlineSeconds`; that is the next row in the same section and a decision about DDL
safety rather than about draining.
