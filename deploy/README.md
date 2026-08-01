# Deploy — Chemclaw on OpenShift (plan F6)

The stack runs in-cluster with OIDC, secrets, workers, and probes. One image, many roles; one
config source (the pydantic `Settings`) fed from a `ConfigMap` + a small set of plain `Secret`s.

## What ships

| Component | Entry (`CHEMCLAW_COMPONENT`) | Runs |
|---|---|---|
| Front door | `service` | `uvicorn chemclaw.api.app:create_app` behind an OIDC **Route** |
| Background worker | `background-worker` | `python -m chemclaw.durable.background_worker` — `background-jobs` (light) |
| Connector worker | `connector-worker-<name>` | `python -m chemclaw.connectors.<name>.worker` — that bundle's own queue |
| Connector server | `connector-<name>` | `uvicorn chemclaw.connectors.<name>.server.app:app` — that bundle's MCP tools |

All four are the **same image** (`deploy/Containerfile`), rootless (UID 1001, arbitrary-UID safe for
OpenShift SCC), no secret baked in. `deploy/entrypoint.sh` dispatches on `CHEMCLAW_COMPONENT`.

The last two are *patterns*, not a fixed list — that is the connector seam (D-109) working: adding a
bundle adds pods, not entrypoint cases. There is deliberately no `mcp-molfp`/`mcp-rxnfp` row here.
This table carried one until D-156, naming a component `entrypoint.sh` had no case for and the chart
never declared, which is the D-117 failure in miniature: prose asserting a deployable that does not
exist. `tests/test_deploy_chart.py` checks the chart against the entrypoint in both directions; it
does not read this file, so the row survived. Fingerprints deploy as `connector-molfp` and
`connector-rxnfp` like every other bundle.

## Config & secrets (F6-T2 / F6-T6)

- **Non-secret** config is the Helm `values.yaml` `config:` block → a `ConfigMap` → `CHEMCLAW_*` env.
  Keys mirror `chemclaw/config.Settings` **exactly** — there is no second config system in-cluster.
- **Plain secrets are the exceptions, not the model.** Five exist, and each is a credential for a
  system that does not speak Entra: the generic LLM API key (F0, the one documented Entra
  exception), the Postgres DSN, the HPC-bridge credential, the knowledge-repo push token, and the
  git host's webhook-signing secret. The Temporal mTLS certs are a sixth, mounted as files rather
  than env. Everything that *can* federate does: **Workload Identity Federation** (F4-T2) annotates
  the pod's ServiceAccount so its projected token is exchanged for an Entra token, with no client
  secret at rest. (This section said "only three" from F6-T6 until each later gap added one; the
  count is now derived from `values.yaml`'s `secrets.keys` rather than restated here, because a
  number in prose is exactly what went stale.)
- Populate them via `ExternalSecret`/`SealedSecret`; the chart only *names* them.

### Two settings that decide whether the pod boots at all

- **`CHEMCLAW_ENTRA_REQUIRED=true` is mandatory for any exposed deployment.** With it off, every
  request runs as the shared dev principal and all authorization gates are open, so the front door
  **refuses to start** when that mode is bound to a non-loopback interface (the `0.0.0.0` default) —
  it exits with `SECURITY: entra_required is False but the service binds a non-loopback interface …`
  instead of silently serving an open deployment. `CHEMCLAW_SERVICE_ALLOW_INSECURE=true` is the deliberate opt-out (boots with a
  loud warning) and belongs in local dev only. Under `entra_required`, `CHEMCLAW_ENTRA_TENANT_ID`
  and `CHEMCLAW_ENTRA_AUDIENCE` must also be set — a half-configured identity setup fails fast at
  startup rather than at the first request.
- **`CHEMCLAW_ENTRA_CLIENT_ID` no longer exists.** `Settings` is `extra="forbid"`, so a stale export
  of the removed field aborts startup with a validation error naming it. Drop it from any inherited
  ConfigMap/env before upgrading.

## Stateful dependencies (F6-T3, ADR **D-A6a**)

- **Temporal: self-hosted in-cluster** (not Temporal Cloud). Rationale: keeps the durable core inside
  the same cluster + OIDC trust boundary as everything else, and avoids egress of workflow payloads
  (which carry the Entra `oid`, D-044) to a third party. Temporal Cloud stays a values-swap away
  (`temporal_api_key` instead of the mTLS trio) if that trade changes.
- **Postgres/pgvector**: an operator- or managed-instance with mTLS and the existing
  `pg_statement_timeout_seconds`. Migrations run as a **pre-deploy Helm hook** Job
  (`templates/migrate-job.yaml` → `python -m chemclaw.science.calc.migrate`, i.e. `make db-migrate`, D-034) that
  completes before any app container starts — no container ever races the DDL. The migrator takes a
  transaction advisory lock (so two overlapping deploys serialize) and a `lock_timeout` (so an
  `ALTER TABLE` that cannot get `ACCESS EXCLUSIVE` fails in seconds instead of queueing in front of
  every later query on that table — Postgres's lock queue is FIFO, so an unbounded wait is an
  outage, not a delay). The Job has an `activeDeadlineSeconds`, because Helm waits for a hook and a
  retrying Job would otherwise hold the release in `pending-upgrade`. Recovery for all three is
  `docs/guides/runbook.md` §(xi).

## Network & probes

- **NetworkPolicy** (`templates/networkpolicy.yaml`): default-deny egress with an allow-list — DNS,
  Postgres (5432), Temporal (7233), HTTPS (443, for the internal LLM + HPC launcher + Entra). Nothing
  else leaves a pod.
- **Probes**: every process exposes `/readyz` (readiness) and `/healthz` (liveness) — the front door
  and the connector servers on their service port, the Temporal workers on the `metrics` port
  (`CHEMCLAW_WORKER_METRICS_PORT`, default 9000). A worker's readiness is its own `is_running`, and
  its liveness is answered on its event loop, so a loop wedged inside an activity restarts the pod.
  This line used to read "the workers' health is their Temporal poll loop", which described an
  intent nothing enforced: a dead poll loop kept the process open and Kubernetes reported `Running`
  (D-2026-08-01-every-process-carries-its-own-witness).
- HPA scales the stateless front door on CPU; workers scale by hand (queue depth), not HPA.
- **Request bounds** (D-2026-08-01-a-cheap-request-is-still-a-request): uvicorn is launched with
  `--limit-concurrency`, `--timeout-keep-alive` and `--h11-max-incomplete-event-size` (all from
  `CHEMCLAW_SERVICE_*` settings, none of which the app can impose on itself); an ASGI middleware
  refuses a body over `CHEMCLAW_SERVICE_MAX_REQUEST_BYTES` with 413 before it is read; and a
  per-principal token bucket refuses with 429, on in the chart and off in code. Tuning and
  symptoms: `docs/guides/runbook.md` §(xii).

## Draining a pod (D-2026-08-01-a-drain-is-not-a-kill-with-extra-steps)

Nothing distinguished "this pod is going away" from "this pod died", so every rolling update, node
drain and scale-down behaved like a crash. Both grace periods are now **derived** from the budget
they have to outlast, so tuning one moves the other:

| Pod | `terminationGracePeriodSeconds` | Derived from |
| --- | --- | --- |
| front door | turn timeout + `service.drainSeconds` | `CHEMCLAW_SERVICE_TURN_TIMEOUT_SECONDS` (600) |
| every worker | drain budget + 30 s | `CHEMCLAW_WORKER_GRACEFUL_SHUTDOWN_SECONDS` (120) |

**What this costs.** A rolling update of the front door can take up to 615 s per pod, because a
turn may run that long and the state that would make it resumable lives in the pod's memory by
design (D-121). Deploying faster means ending conversations; that is the trade, taken deliberately.
A `preStop` sleep of `service.drainSeconds` runs first, so the router stops choosing the pod before
the pod stops accepting — Kubernetes removes the Endpoint and sends SIGTERM concurrently, and
without the sleep those race.

**Workers get the shorter budget on purpose.** An activity that does not finish in 120 s is
cancelled and re-run by Temporal, which is a mechanism that already exists and works; holding a node
drain open for ten minutes to avoid using it is the wrong trade. The front door is the opposite
case — there is no retry for a chemist's turn — which is why it gets the whole turn budget.

`policy/v1` PodDisruptionBudget covers the front door only (`maxUnavailable: 1`, its own toggle in
case a PDB ever wedges a cluster upgrade). The workers get none: `workers.background.replicas: 1` is
a singleton (the PR-gate checkout lock is host-local, D-069), and over a singleton `minAvailable: 1`
makes the pod un-evictable and blocks every drain in the cluster forever, while `maxUnavailable: 1`
permits exactly what no PDB permits.

## Before a deploy that touches workflow code

Temporal replays workflow **code** against recorded **history**, so a control-flow change deployed
while a run is in flight fails that run with a nondeterminism error. Every release that touches a
`@workflow.defn` body (or a helper called from one) goes through the checklist in
[`docs/guides/workflow-versioning.md`](../docs/workflow-versioning.md): gate the change with
`workflow.patched()`, or pause the Schedules and drain in-flight runs as an explicit deploy step.
Renaming a workflow or activity type is never safe in place — it is a different command in history.

No live cluster holds Chemclaw histories yet, so the changes made so far need no retroactive gates;
this becomes binding at the first production deploy.

## Observability (F6-T5)

`CHEMCLAW_OTEL_ENABLED=true` + `CHEMCLAW_OTEL_ENDPOINT` wire OTLP to the in-cluster collector
(`chemclaw/logging.py` bridges the one config value to `OTEL_EXPORTER_OTLP_ENDPOINT`).

**Two first-party spans, and the propagation that joins them up.** A `chemclaw.turn` span wraps a
turn and a `chemclaw.tool` span wraps each tool call, so "the question took 40 seconds and 31 of
them were one xTB call" is answerable — which it was not. Connector calls carry W3C `traceparent`
alongside the custom `X-Chemclaw-Correlation` header, and the connector adopts it, so a
calculation's spans appear *inside* the turn that asked for it instead of as an orphan trace. The
two headers are not redundant: the correlation id is what `audit_events` is keyed on and works with
no collector at all; `traceparent` is what makes a distributed trace a tree.

These three paragraphs used to read "Spans cover a turn and a job; dashboards track loop iterations,
tool latency, and job status." None of that existed — the only spans were MAF's own model calls,
and there are no dashboards in this repo to track anything
(D-2026-08-01-a-turn-you-can-follow-across-a-process). What is *still* absent is named rather than
implied: no span around a durable job (it spans two processes and a Temporal boundary, so it needs
the workflow to carry the context), and no FastAPI/httpx/Temporal auto-instrumentation.

**Metrics come from every process, not only the front door.** `templates/servicemonitor.yaml`
collects the Services (the front door and each connector's MCP server, by their `http` port name);
`templates/podmonitor.yaml` collects the pods that have none — core's background worker and each
bundle's — by the `metrics` port they declare. Until D-2026-08-01 the ServiceMonitor selected the
front door alone, so everything the workers counted (durable jobs launched, PR-gate proposals and
their failures, lost audit records) was recorded in each worker's own registry and read by nobody.
Set `monitoring.additionalLabels` to whatever your Prometheus's `serviceMonitorSelector` and
`podMonitorSelector` match; the defaults match nothing, which is the safe direction.

**Logs are JSON in-cluster** (`CHEMCLAW_LOG_JSON`, on in the chart) and every line carries
`correlation_id`, `actor` and `session_id` from the turn's ContextVars — so an ordinary WARNING
joins to the audit row that recorded the same call and, through the same correlation id, to the
trace that spans it. A filter also replaces any configured secret's value with `***` before a
record reaches a stream, including one passed as a `%s` argument
(D-2026-08-01-a-log-line-that-joins-and-a-secret-that-does-not).

**What is deliberately not redacted:** the audit trail's tool-call arguments. `SECURITY.md` states
that they are user free text, may contain PII, and are recorded *intentionally* — GxP requires an
attributable "who did what to which inputs" record. A deployment's retention, access control and
PII policy must cover the trail; that remains a policy obligation and not something this code
silently satisfies by deleting the evidence.

## CI/CD (F6-T4)

Two workflows, both at the **repository root** — the only place GitHub Actions reads them from.
Until D-117 these lived under `services/chemclaw/.github/`, where nothing executed them.

- `ci.yml` — `make lint type cov` against a real Postgres, the seven validators, and a `chart` job
  that renders the chart against the Kubernetes schemas (`helm template | kubeconform -strict`).
- `image.yml` — on pull requests and `main`, **builds** the multi-target image and smoke-imports
  every component the entrypoint dispatches as a non-root UID, then asserts an unknown component
  exits 64. The component list is derived from the bundles present, so it cannot drift from what
  ships; the two directions of chart↔entrypoint agreement are checked offline in
  `tests/test_deploy_chart.py`.

The push-to-registry + `helm upgrade` rollout is **not** wired: the stranded workflow carried it as
a job whose whole body was an `echo`, and a stub is not a pipeline. Its trigger — a real cluster and
its credentials — is recorded in `docs/planning/DEFERRED.md`. Migrations run as the pre-deploy Job
(`templates/migrate-job.yaml`), never inside an app container.

> **Verified offline:** pure-YAML parse + template brace-balance + `Settings` key mapping. `helm
> template`/`kubeconform`/the image build run in CI (no helm/daemon in the dev sandbox) — this is
> inherent to a deploy phase, not a gap in the manifests.
