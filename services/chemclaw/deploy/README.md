# Deploy — Chemclaw on OpenShift (plan F6)

The stack runs in-cluster with OIDC, secrets, workers, and probes. One image, many roles; one
config source (the pydantic `Settings`) fed from a `ConfigMap` + three plain `Secret`s.

## What ships

| Component | Entry (`CHEMCLAW_COMPONENT`) | Runs |
|---|---|---|
| Front door | `service` | `uvicorn service.app:create_app` behind an OIDC **Route** |
| HPC worker | `hpc-worker` | `python -m workers.hpc_worker` — `hpc-jobs` queue (few, heavy) |
| Background worker | `background-worker` | `python -m workers.background_worker` — `background-jobs` (light) |
| MCP servers | `mcp-molfp` / `mcp-rxnfp` | fingerprint capability servers |

All five are the **same image** (`deploy/Containerfile`), rootless (UID 1001, arbitrary-UID safe for
OpenShift SCC), no secret baked in. `deploy/entrypoint.sh` dispatches on `CHEMCLAW_COMPONENT`.

## Config & secrets (F6-T2 / F6-T6)

- **Non-secret** config is the Helm `values.yaml` `config:` block → a `ConfigMap` → `CHEMCLAW_*` env.
  Keys mirror `chemclaw/config.Settings` **exactly** — there is no second config system in-cluster.
- **Only three plain secrets** exist: the generic LLM API key (F0, the one documented Entra
  exception), the Temporal mTLS certs, and the HPC-bridge credential. Everything else is **Workload
  Identity Federation** (F4-T2): the pod's ServiceAccount is annotated so its projected token is
  exchanged for an Entra token — no client secret at rest.
- Populate the three secrets via `ExternalSecret`/`SealedSecret`; the chart only *names* them.

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
  (`templates/migrate-job.yaml` → `python -m calc.migrate`, i.e. `make db-migrate`, D-034) that
  completes before any app container starts — no container ever races the DDL.

## Network & probes

- **NetworkPolicy** (`templates/networkpolicy.yaml`): default-deny egress with an allow-list — DNS,
  Postgres (5432), Temporal (7233), HTTPS (443, for the internal LLM + HPC launcher + Entra). Nothing
  else leaves a pod.
- **Probes**: the service exposes `/readyz` (readiness) and `/healthz` (liveness); the workers' health
  is their Temporal poll loop. HPA scales the stateless front door on CPU; workers scale by hand
  (queue depth), not HPA.

## Before a deploy that touches workflow code

Temporal replays workflow **code** against recorded **history**, so a control-flow change deployed
while a run is in flight fails that run with a nondeterminism error. Every release that touches a
`@workflow.defn` body (or a helper called from one) goes through the checklist in
[`docs/workflow-versioning.md`](../docs/workflow-versioning.md): gate the change with
`workflow.patched()`, or pause the Schedules and drain in-flight runs as an explicit deploy step.
Renaming a workflow or activity type is never safe in place — it is a different command in history.

No live cluster holds Chemclaw histories yet, so the changes made so far need no retroactive gates;
this becomes binding at the first production deploy.

## Observability (F6-T5)

`CHEMCLAW_OTEL_ENABLED=true` + `CHEMCLAW_OTEL_ENDPOINT` wire OTLP to the in-cluster collector
(`chemclaw/logging.py` bridges the one config value to `OTEL_EXPORTER_OTLP_ENDPOINT`). Spans cover a
turn and a job; dashboards track loop iterations, tool latency, and job status.

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
its credentials — is recorded in `DEFERRED.md`. Migrations run as the pre-deploy Job
(`templates/migrate-job.yaml`), never inside an app container.

> **Verified offline:** pure-YAML parse + template brace-balance + `Settings` key mapping. `helm
> template`/`kubeconform`/the image build run in CI (no helm/daemon in the dev sandbox) — this is
> inherent to a deploy phase, not a gap in the manifests.
