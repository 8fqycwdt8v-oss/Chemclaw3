# D-2026-08-01-every-process-carries-its-own-witness — Every process carries its own witness, and the sentence that stopped two of them

**Status:** accepted · **Date:** 2026-08-01 · **Extends:** D-139 (the two PR-gate counters), D-143
(the ServiceMonitor), D-152 (declared metric labels)

## Context

Two findings in the v1.0 readiness analysis were filed separately and turned out to be one thing.

**"Worker and connector metrics go nowhere."** `deploy/` scraped exactly one target: the front
door's Service. The reasoning was recorded in three places, in almost the same words —
`core/metrics_bridge.py`'s module docstring, the ServiceMonitor's template comment, and an assertion
in `tests/test_deploy_chart.py` that pinned `component: service` into the selector. All three said
that a metric recorded outside the front door is a no-op, because "there is no registry and no HTTP
surface in those processes".

The second half was true and the first half never was. `api/metrics.py` imports `logging`,
`threading`, `bisect` and `collections.abc` — nothing else — and `chemclaw/api/__init__.py` is a
docstring. So `record_metric`'s lazy import succeeds in *every* process, and the background worker
and every connector worker have been incrementing a live, correct registry since the day the bridge
was written. `chemclaw_jobs_started_total` counts a durable job launched from a workflow.
`chemclaw_notes_proposed_total` and its failure counterpart (D-139) count the PR-gate from
`publish_note_best_effort`, which runs in the background worker. `chemclaw_audit_sink_failures_total`
counts a GxP record lost inside a background activity — the metric whose entire purpose is to reveal
an incomplete audit trail. Every one of them was recorded, and read by nobody.

**"Workers and connectors have no probes."** The same three chart templates carried the comment
"no probe HTTP port — liveness is the Temporal poll itself". That is a claim about a guarantee, and
nothing anywhere enforced it: a worker whose poll loop has died still holds its process open, so
Kubernetes reports `Running`, and — per the finding above — no counter contradicted it either.

The two gaps hid each other, and they had one cause: the only process in the system with an HTTP
surface was the one serving chat.

## Decision

**Every process serves the same three routes, and the ones with no Service are collected by a
PodMonitor.**

`chemclaw/core/worker_http.py` is an async context manager wrapping a Temporal worker's `run()`:

- `GET /healthz` — liveness. Not "the process exists", which Kubernetes already knows: it is served
  on the *worker's own event loop*, so a loop wedged by a blocking call inside an activity stops
  answering it and the kubelet restarts the pod. That is precisely the failure the old comment
  named and no probe could see.
- `GET /readyz` — readiness, from `worker.is_running`. Readiness rather than liveness on purpose: a
  worker that has stopped polling serves no traffic, so there is nothing to take it out of, but it
  must be *visibly* not-ready — and restarting on that signal alone would turn an ordinary Temporal
  reconnect into a crash loop.
- `GET /metrics` — the registry that was already there.

The connector servers already had an HTTP surface and needed only the route. `ServiceMonitor` drops
`component: service` from its selector, so it now collects every Service the chart renders — the
front door and one per connector — and a connector enabled tomorrow is scraped without an edit.
Worker pods have no Service by design, so `podmonitor.yaml` selects them by the `metrics` container
port that `chemclaw.workerProbes` declares. The two selectors partition the fleet; nothing is
scraped twice.

## Why not the alternatives

**Push the metrics somewhere instead (statsd, an OTel metric exporter).** The registry exists, is
correct, and is already rendered in the exposition format; the gap was a reader, not a pipeline.
Adding a push path would mean two ways to report one number and a second thing to reconcile when
they disagree — D-152 rejected duplicating MAF's token histogram into this registry for the same
reason, in the other direction.

**Scrape annotations rather than a PodMonitor.** Same answer as D-143: the target is OpenShift,
whose user-workload monitoring stack is the Prometheus Operator, and annotations are the older
convention its default configuration does not read.

**Move the registry out of `api/` into `core/`.** Tempting, and the docstrings almost ask for it:
`metrics_bridge` exists only because the registry lives under a package the workers should not
depend on. But the module is stdlib-only and importing it pulls in no framework, so the layering
concern the move would settle is a naming concern rather than a dependency one. A rename touching
twelve import sites buys a tidier tree and changes no behaviour; the bad sentence is what cost the
deployment its observability, and deleting that sentence is what fixes it.

**A `/readyz` on the connector servers too.** Rejected as a second assertion of one fact. Uvicorn
accepts connections only after the app's lifespan has completed, and that lifespan is what starts
the MCP session manager and opens the Postgres pool — so `/healthz` *answering* already is the
readiness evidence. A separate route could only restate it, and a health notion that exists twice
is one that can disagree with itself.

**Leave the escape hatch out.** `CHEMCLAW_WORKER_METRICS_PORT=0` skips the surface, which reads
like a way to reintroduce the problem. It stays because two workers on one developer machine cannot
both bind 9000, and the alternative — a bind failure that a developer works around by not running
the second worker — is worse. The chart sets the port on every worker Deployment and
`test_every_worker_is_probed_and_scraped` pins that it does, so a deployment cannot reach the
disabled state by omission.

## Consequences

- The background worker and every bundle worker are scraped and probed. What was invisible and is
  now not: durable jobs launched from a workflow, PR-gate proposals and their failures, lost audit
  records, and every latency histogram recorded outside a turn.
- A wedged event loop in a worker is now a restart rather than a permanently `Running` pod.
- `metrics_bridge`'s swallow-all is documented as what it always actually was — a guarantee that a
  metrics update cannot break the caller's path — rather than a claim about where metrics exist.
- One mutation in this change survived its first test and is worth recording: deleting the connector
  worker's probes left `tests/test_deploy_chart.py` green, because the assertion was a substring
  check and the template's *comment* mentions the helper by name. A test a comment can satisfy is a
  test of the comment; the assertion now matches the include as a template action anchored to its
  line.

## Not in this change

`chart` still has no PodDisruptionBudget, no topology spread and no `preStop`, and the migration Job
still takes no advisory lock — the neighbouring rows in the same backlog section. They are separate
because they are separate decisions about availability, where this one is about visibility, and
because a scrape target is the precondition for judging any of them: an autoscaling or drain policy
tuned against metrics nobody collects is guesswork with a number attached.
