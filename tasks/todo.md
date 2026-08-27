# Logging and monitoring review — 2026-08-27

## What this was

A deep review of logging and monitoring across all three repositories, against a stated goal that is
*not* GxP: **no audit trail is required**, but every task must be elaborately logged and monitoring
must be good enough to identify issues in routine operation. Ten parallel review teams, each given
one surface and told to measure rather than read. Their evidence is what every item below cites.

The finding that frames the rest: **this system's observability was built to prove things and not to
diagnose them.** The audit trail is a good forensic record; the metric registry is unusually
disciplined (declared labels, a cardinality cap, a static test in both directions). What neither
could answer is "what is wrong right now, and where" — because the surfaces that carry that answer
were either shaped so they could not hold it, or were never wired at all.

Three measurements make the point better than any summary:

- A **successful durable job emits zero log records**, and a failed one emits zero first-party
  records and moves no metric. Measured live against a real broker; Postgres held one `job_records`
  row for two jobs, because the failing path raises before the row is written.
- `grep perf_counter|monotonic` across `ingest/`, `retrieval/`, `memory/`, `kg/`, `publish/` and
  `core/` returns **two hits, both a cache TTL**. Not one duration is measured in ~26,000 lines.
- `JsonFormatter` built a fixed seven-key payload and never read `record.__dict__`, so **every
  `extra=` field was discarded**. There was exactly one `extra=` logging call in the tree, and it
  was the one line designed to be alerted on.

## Confirmed sound — not to be "fixed"

Recorded because a review that only lists faults invites someone to rebuild what already works.
`ServiceMonitor`, `PodMonitor` and `PrometheusRule` all exist and are correct (16 valid alerts,
every process role scraped, NetworkPolicy right in both directions, the worker grace-period
invariant genuinely pinned by a test). Every metric name the runbook cites is real. The chemist's
message text is never logged. `/healthz` returning 200 unconditionally is correct for liveness, and
readiness deliberately not probing Temporal is documented and right. The loop cap's
measured-not-inferred design is the model the rest of this work follows. The UI has zero stray
`console.log` calls, its error taxonomy is careful, and its error-path test coverage is real.

## Plan

- [x] Ten review teams, one per surface; findings in the session scratchpad.
- [x] **Foundation** (`core/logging.py`, `core/metrics.py`) — the two chokepoint files, done first
      and alone so the parallel workstreams only add call sites. Committed as `d7c70f6`.
      Redaction of `extra=` deliberately landed *before* the formatter began publishing it: the
      other order opens a leak, and a handler whose format string referenced an extra was measured
      printing a DSN with its password.
- [ ] **Front door** — access log, request-scoped correlation, RED metrics, the turn record, and the
      `DetachableTurn` hang (the only correctness defect found: `_DONE` is discarded when the queue
      is full, reproduced at n=257 and n=513).
- [ ] **Agent layer** — refusals distinguishable from crashes, an LLM error taxonomy, the tool span
      that reports UNSET on every returned failure, `plan_step` on the trail.
- [ ] **Durable layer** — a worker interceptor binding the ambient trio and emitting lifecycle
      records, job outcome counters, Temporal SDK metrics, the trace that stops dead at every job.
- [ ] **Data paths** — sync run records, ingest lag, retrieval latency and *surviving* chunks,
      embedding and database instrumentation, the outbox backlog formula that is wrong three ways.
- [ ] **Deploy** — dashboards, Alertmanager routing, the undocumented OpenShift prerequisite that
      makes the whole stack inert, `up`/`absent` alerts, and the connector probes that SIGKILL a
      cold-starting pod after 30 s.
- [ ] **Chemclaw3-mcp** — a log configuration the fleet actually owns, per-tool metrics, an error id,
      `servers/calc` instrumentation, the silent egress guard, a real readiness check.
- [ ] **Chemclaw3_ui** — correlation read-back, a client logger, a BFF access log, and the four
      silent failure paths.
- [ ] Verify `make lint type test` green in each repo; PR and merge per repo.

## Review

To be written when the workstreams land.
