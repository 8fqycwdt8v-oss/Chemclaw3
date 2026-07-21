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

## Observability (F6-T5)

`CHEMCLAW_OTEL_ENABLED=true` + `CHEMCLAW_OTEL_ENDPOINT` wire OTLP to the in-cluster collector
(`chemclaw/logging.py` bridges the one config value to `OTEL_EXPORTER_OTLP_ENDPOINT`). Spans cover a
turn and a job; dashboards track loop iterations, tool latency, and job status.

## CI/CD (F6-T4)

`.github/workflows/deploy.yml`: every push **builds** the image + smoke-imports each entrypoint as a
non-root UID, and **lints/renders** the chart (`helm lint`, `helm template | kubeconform`). The
push-to-registry + `helm upgrade` rollout is guarded to the default branch and needs cluster creds;
migrations run as the pre-deploy Job before rollout.

> **Verified offline:** pure-YAML parse + template brace-balance + `Settings` key mapping. `helm
> template`/`kubeconform`/the image build run in CI (no helm/daemon in the dev sandbox) — this is
> inherent to a deploy phase, not a gap in the manifests.
