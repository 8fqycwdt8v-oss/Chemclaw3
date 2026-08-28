# D-2026-08-27-a-queue-with-no-poller-is-unreachable — the durable half of a connector had no probe

**Status:** accepted

## Context

`connectors/health.py` is the one reachability sweep this system has: it feeds `/readyz`, the
`chemclaw_connectors_unhealthy` gauge, the `connectors_required` fail-fast gate and — since
`D-2026-08-27-the-breaker-is-the-readiness-verdict-already-taken` — the per-turn breaker. Every
target it probed came from `health_url(manifest)`, which is `None` for a bundle that declares
`jobs:` and no `endpoint:`.

`results` is such a bundle, and so is any future one whose whole capability is durable. The sweep
reported it `unprobed` with its worker fleet at two replicas and with it at zero; the gauge counted
only `unreachable`, so it moved for neither; and `check_connectors_at_startup` raised only on
`unreachable`, so the posture whose entire purpose is refusing to serve degraded — a deployment that
prefers death to a silently reduced tool surface — was structurally unable to see the reduction with
the largest blast radius. A connector's HTTP pod going dark costs a read tool for a turn. A bundle's
worker fleet at zero costs every job launched onto that queue: the launch **succeeds**, the child is
accepted onto `connector-<name>`, the chemist is told "running", and the answer arrives when
`connector_job_timeout_seconds` — 25 h at the shipped default — expires.

That failure already has an offline guard one level up. `make connector-validate` refuses a job
naming a workflow the bundle's own modules do not register, precisely because "the child starts on a
queue whose worker serves no such type" ends in the same day-long wait. What the manifest gate
cannot see is a manifest that is entirely correct and a *deployment* with no worker pod: a scaled-to
-zero StatefulSet, a crash-looping worker image, a queue name diverging from `bundle_queue` in a
chart values file. This is that gate's runtime twin, and it is the only half that can observe a
replica count.

## Decision

**A durable bundle's reachability is whether anything polls the queue its jobs run on.** The sweep
now asks `DescribeTaskQueue(bundle_queue(name), TASK_QUEUE_TYPE_WORKFLOW)` for every enabled bundle
that declares `jobs:` and has no health route, and the poller list is the verdict.

The state vocabulary grows from three to five, and each state answers a different question:

| state | what was asked | counted down | gates |
| --- | --- | --- | --- |
| `healthy` | a health route answered 2xx, **or** the queue has ≥1 poller | no | no |
| `unreachable` | the health route did not answer, or answered non-2xx | **yes** | **yes** |
| `unpolled` | Temporal answered, and nothing is polling the queue | **yes** | **yes** |
| `unknown` | the queue could not be asked at all | no | no |
| `unprobed` | there was nothing to ask | no | no |

`unpolled` counts and gates exactly as `unreachable` does, because for a bundle whose capability is
its jobs the two mean the same thing to a chemist. The predicate lives once, as
`ConnectorHealth.unhealthy` over `UNHEALTHY_STATES`, because the gauge and the gate must not become
two definitions of "down" — the failure mode this repository has recorded for two live definitions
of one tool name.

Measured against a real broker (`make up`, Temporal 1.x, `connector-fixture`):

```
no worker:                     0 pollers, 4 ms
with one worker:               1 poller,  3 ms
immediately after it stopped:  1 poller,  7 ms
```

And against the shipped registry with this checkout's own workers running, the whole sweep now
answers for the bundle it had never answered for at all:

```
results  healthy      (connector-results: 1 poller, '9419@vm')
```

where every previous sweep in this repository's history reported `results unprobed`, identically,
whether that poller was there or not.

The third line of the table above is the honest caveat: Temporal reports pollers seen in roughly the last minute, so
`unpolled` **lags** a fleet's death by up to that long. That is the right direction for this signal
— a rolling worker restart does not flap the gauge, and a fleet that is actually gone is reported
within a minute — but it means this probe is not a liveness check for an individual pod and must not
be read as one.

### An inconclusive probe is its own state

Temporal being unreachable is not the same fact as a queue having no poller, and this repository has
already decided both halves of how to treat that pair.

`D-2026-08-08-an-outage-is-not-a-missing-job` is the reason `unknown` exists at all: reporting an
outage as the more specific, more actionable fact ("nobody is polling — check your replicas") is the
exact defect catalogued there, and under `connectors_required` it would additionally turn every
broker restart into a boot failure for a front door whose HTTP connectors are all fine.

`D-2026-08-08-a-degraded-check-must-not-clear-the-gate` is the reason `unknown` is not `healthy`.
Its rule is that a check which did not run must not be able to clear the gate it guards, and the
concrete sin it was written about was a **degraded judge emitting the same verdict a working judge
emits** — confidence 1.0, `fields differing: set()`. `unknown` is a state no successful probe can
return: it is on the readiness snapshot, in the sweep's `name=state` INFO line, and in a WARNING of
its own at startup that says the reachability of those bundles was not determined and is not being
counted. The gate is not cleared by a check that silently passed; it is told there is nothing to
clear it with.

So `unknown` neither counts nor gates. The broker is one dependency shared by every durable bundle,
so counting it would move the gauge with the number of bundles rather than with the fault, and
alert N times for one cause — and Temporal's own reachability already has a signal of its own
(`chemclaw_durable_unreachable_total`, D-2026-08-08) plus a `SubsystemUnavailableError` on every
path that needs it.

Only a **successful** `DescribeTaskQueue` produces a verdict. There is no status code to interpret:
a queue nobody has ever polled is not an error, Temporal answers it with an empty poller list. Every
failure — UNAVAILABLE, DEADLINE_EXCEEDED, a namespace that does not exist, and UNIMPLEMENTED, which
is what the time-skipping test server actually answers — is `unknown`.

### Bounding it

The connect and the RPC are both bounded by `connector_health_timeout_seconds`, the knob the HTTP
half already uses; the two halves of the sweep are `gather`ed, so a deployment with both kinds of
bundle pays the slower rather than the sum. `connect()` is the process-wide cached client every
durable caller shares, so this opens no second channel where the front door already holds one, and
it caches only on success — a bounded failure here does not poison the singleton the job tools use.
A refused broker fails in under a millisecond (measured); the timeout is there for the one that
blackholes the SYN, which would otherwise hold startup for the SDK's own connect timeout.

## Consequences

**The front door now opens a Temporal channel at startup** and issues one light RPC per durable
bundle per readiness sweep — cached for `service_readiness_cache_seconds` like the rest of the
sweep. A deployment with no broker gets one `unknown` row per durable bundle and a WARNING, not a
crash-loop.

**Two readers of the verdict were not brought onto the shared predicate, and both are follow-ups
rather than oversights.**

- `api/routes/ops.py` still computes `/readyz`'s `connectors_unhealthy` as
  `state == "unreachable"`, so that body under-reports an `unpolled` bundle by one while `/metrics`
  reports it. It is a one-expression change to `item.unhealthy` and it must be made; it is outside
  the file budget of the branch that took this decision, which is the whole of the reason it is not
  in it.
- **A bundle with *both* halves is still judged on its endpoint alone.** `bo` and `calc` declare an
  endpoint and jobs, so their queues are not asked about: one `ConnectorHealth` carries one state,
  and a composite verdict ("healthy only if both halves are") needs the readers above to agree on
  what a two-part state means before it can be reported honestly. That leaves the shipped fleet's
  heaviest worker — `connector-calc`, whose CREST searches are the longest activity in the system —
  covered by this ADR's argument and not yet by its code. The trigger to finish it is the `/readyz`
  fix above; the argument is already made here and does not need remaking.

- `docs/guides/runbook.md` tells an operator a connector is probed as "`healthy`, `unreachable` or
  `unprobed`", which is now three of five. Same budget, same fix: the troubleshooting section needs
  the two new words and the sentence that `unpolled` means a worker deployment rather than a server
  pod.

**A namespace that does not exist reports `unknown` forever**, not a misconfiguration. That is the
price of "only a success is a verdict", and it is paid down by the WARNING carrying the RPC's own
message, which names the namespace.

`tests/test_connector_health.py` drives the real sweep through the real registry — tmp bundles, real
`connector.yaml`, real `ConnectorManifest` — with only the Temporal client replaced, and that
stand-in answers with the SDK's own `DescribeTaskQueueResponse` and fails with its own `RPCError`,
because both verdicts are properties of that wire type. The time-skipping test server, which every
other durable test here uses, cannot stand in for it: measured, it answers `DescribeTaskQueue` with
UNIMPLEMENTED, which is the inconclusive case rather than a poller count.

**A manifest with neither an endpoint nor a job cannot exist** — `_contributes_capability` refuses
it — so `unprobed` now means exactly one realizable thing: a bundle with an endpoint that declares
no health route (a third-party MCP server, or stdio) and no durable work of its own. Guessing a
health path there would manufacture the false alarm that state exists to avoid, and that is
unchanged.
