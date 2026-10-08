# Installing ChemClaw (OpenShift, with notes for plain Kubernetes)

This guide stands up a production-shaped ChemClaw from nothing: the backend from this repository's
Helm chart, the MCP tool fleet from `Chemclaw3-mcp`, and the frontend from `Chemclaw3_ui`. Each step
names the file that defines the requirement, so when the code and this guide disagree, the code
wins.

Paths without a prefix are in this repository. Paths in the two sibling repositories are written
`Chemclaw3-mcp:servers/calc/deploy/deployment.yaml` and `Chemclaw3_ui:deploy/openshift/deployment.yaml`.

**Before you start, know these three rules.** The chart refuses to render if you skip them:

1. Each release gets its own Kubernetes namespace, its own Temporal namespace and its own Postgres
   database (`templates/config.yaml`, the `temporal.namespace` refusal). Chart object names are
   fixed (`chemclaw-service`, `chemclaw-config`, …) and do not include the release name. So you
   can run only one release per Kubernetes namespace.
2. The MCP fleet runs in the **same** Kubernetes namespace as the release. Each fleet server's
   NetworkPolicy admits its callers with a bare `podSelector`
   (`Chemclaw3-mcp:servers/calc/deploy/networkpolicy.yaml`), and that only matches pods in its own
   namespace.
3. Every posture has to be written down: where pods may send traffic, how long data is kept, and
   which Temporal namespace to use. The chart has no permissive default for any of them.

---

## 1. What you are deploying

### Components

All backend roles run from **one image** (`deploy/Containerfile`). `deploy/entrypoint.sh` reads
`CHEMCLAW_COMPONENT` and starts the matching process.

| Component | Kubernetes object (default values) | `CHEMCLAW_COMPONENT` | What it does |
|---|---|---|---|
| Front door | Deployment + Service `chemclaw-service`, Route `chemclaw`, HPA, PDB | `service` | HTTP API (`uvicorn chemclaw.api.app:create_app`). Validates Entra tokens and runs conversation turns. |
| Background worker | Deployment `chemclaw-background-worker` | `background-worker` | Temporal worker on the `background-jobs` queue (sync, re-index, retention, schedules' workflows). |
| Connector servers | Deployment + Service `chemclaw-connector-<name>` (`bo`, `calc`, `molfp`, `rxnfp`) | `connector-<name>` | The MCP surface of each bundle this release hosts itself. |
| Connector workers | Deployment `chemclaw-connector-worker-<name>` (`bo`, `calc`, `results`) | `connector-worker-<name>` | Temporal worker on the bundle's durable queue `connector-<name>`. |
| Interactive workers | Deployment `chemclaw-interactive-worker-<name>` (`bo`, `calc`, `chem`, `rxnpredict`) | `interactive-worker-<name>` | Temporal worker on `connector-<name>-interactive`. A heavy tool call waits here for a free slot instead of being refused. |
| Read-only MCP face (off by default) | Deployment `chemclaw-mcp-face` | `mcp-face` | Serves this system's read-only tools to another agent (`mcpFace.enabled`). |
| Migrations | Job `chemclaw-migrate`, a `pre-install,pre-upgrade,pre-rollback` hook | `migrate` | Runs `chemclaw.core.migrate`, `chemclaw.agent.store_setup` and `chemclaw.core.grants`, in that order. |
| Message conversion | Job `chemclaw-convert`, a `post-install,post-upgrade` hook | `convert` | Rewrites stored messages to the current shape. Exits at once when nothing needs converting. |
| Schedules | Job `chemclaw-schedules`, a `post-install,post-upgrade` hook | `schedules` | Creates and updates the Temporal Schedules (`chemclaw.cli.schedules`). |

The chart also renders the `chemclaw-config` ConfigMap (plus a copy for the hooks), the
ServiceAccounts `chemclaw` and `chemclaw-hooks`, four NetworkPolicies, and a ServiceMonitor,
PodMonitor, PrometheusRule and dashboards ConfigMap. An AlertmanagerConfig and KEDA ScaledObjects
are added when you switch them on.

**MCP tool fleet** (`Chemclaw3-mcp`). One server per Deployment. Each server's
`deploy/` directory ships a Deployment, Service, NetworkPolicy, HPA, PDB and ServiceMonitor. The
default chart values dial these servers:

| Server | How the backend reaches it | Port |
|---|---|---|
| `chem` | connector, `connectors.chem.url` | 8858 |
| `safety` | connector, `connectors.safety.url` | 8859 |
| `rxnpredict` | connector, `connectors.rxnpredict.url` | 8857 |
| `calc` | **backend**, `CHEMCLAW_CALC_SERVER_URL` (called inside the calculation cache on a miss) | 8860 |
| `rxnlabel` | **backend**, `CHEMCLAW_RXNLABEL_SERVER_URL` (called by the reaction-label drain) | 8865 |
| `props`, `thermalsafety`, `kinetics`, `unitops`, `suitability` | connectors, **off** by default in `values.yaml` | 8850, 8851, 8852, 8853, 8892 |

`Chemclaw3-mcp:MODULES.md` is the authoritative port registry. Two names exist for `calc`, and
they are different things. The chart's `chemclaw-connector-calc` is this repository's `calc`
bundle: its tools, its durable jobs and the calculation cache. `chemclaw-mcp-calc` is the
semiempirical physics backend that the bundle calls.

**Frontend** (`Chemclaw3_ui`): one Node BFF Deployment `chemclaw3-ui` with two ports, an app port
and an HTML-sandbox port, plus two Routes on two hosts (`Chemclaw3_ui:deploy/openshift/routes.yaml`).

**External systems you provide:**

| System | Read through | Notes |
|---|---|---|
| PostgreSQL with `vector` (pgvector, HNSW support) and `pg_trgm` | `CHEMCLAW_POSTGRES_DSN`, `CHEMCLAW_POSTGRES_MIGRATION_DSN` | §4.1 |
| Temporal frontend (gRPC) | `CHEMCLAW_TEMPORAL_ADDRESS`, `temporal.namespace` | §4.2 |
| An OpenAI-compatible LLM gateway | `CHEMCLAW_LLM_BASE_URL`, `CHEMCLAW_LLM_MODEL`, `CHEMCLAW_LLM_API_KEY` | Nothing in `src/` calls a model vendor directly. The gateway is the only model endpoint. |
| Entra ID (OIDC) | `CHEMCLAW_ENTRA_TENANT_ID`, `CHEMCLAW_ENTRA_AUDIENCE` | §2.4 |
| A Git remote for the knowledge graph (optional) | `knowledge.sync.repoUrl` | Without one, agent-recorded notes cannot be persisted (§5.4) |
| OTLP collector (optional) | `CHEMCLAW_OTEL_ENDPOINT` | Set `CHEMCLAW_OTEL_ENABLED: "false"` if you have none |
| Prometheus Operator / OpenShift user-workload monitoring | ServiceMonitor, PodMonitor, PrometheusRule | §2.1, §8 |

### How the parts talk

```
                 browser
                    │ https (UI Route)            https (sandbox Route, separate host)
                    ▼                                     ▼
        ┌────────────────────────┐ :8080 app / :8081 sandbox
        │  chemclaw3-ui (BFF)    │
        └──────────┬─────────────┘
                   │ http://chemclaw-service:8080   (+ optional API Route "chemclaw")
                   ▼
        ┌────────────────────────┐   OIDC JWKS (https)   ┌──────────────┐
        │ chemclaw-service :8080 │──────────────────────▶│  Entra ID    │
        │ (front door, HPA)      │                       └──────────────┘
        └──┬───────┬──────┬──────┘
           │       │      │ OpenAI-compatible /v1        ┌──────────────┐
           │       │      └─────────────────────────────▶│ LLM gateway  │
           │       │ MCP over HTTP + bearer              └──────────────┘
           │       ├──▶ chemclaw-connector-{bo,calc,molfp,rxnfp} :8080   (this chart)
           │       └──▶ chemclaw-mcp-{chem,safety,rxnpredict} :8857-8859 (Chemclaw3-mcp)
           │
           ├──────────▶ PostgreSQL :5432 (pgvector)   ◀── every role, plus the migrate Job
           └──────────▶ Temporal frontend :7233        ◀── every worker, plus the schedules Job
                              │ task queues
           ┌──────────────────┼──────────────────────────────────────────┐
           ▼                  ▼                                          ▼
  chemclaw-background-   chemclaw-connector-worker-<name>     chemclaw-interactive-worker-<name>
  worker (background-    (connector-<name>)                   (connector-<name>-interactive)
  jobs) ──▶ chemclaw-mcp-rxnlabel :8865      calc ──▶ chemclaw-mcp-calc :8860
```

### Ports and health endpoints

| Listener | Port | Probe paths | Defined in |
|---|---|---|---|
| Front door | `service.port` (8080) | `/healthz` (startup, liveness), `/readyz` (readiness), `/metrics` | `templates/deployment-service.yaml` |
| Chart connector servers | `connectorPort` (8080) | `/healthz` (startup, readiness), `/livez` (liveness), `/mcp`, `/metrics` | `templates/deployment-connectors.yaml` |
| Every Temporal worker | `workerMetricsPort` (9000), port name `metrics` | `/healthz` (startup, liveness), `/readyz` (readiness), `/metrics` | `templates/_helpers.tpl` (`chemclaw.workerProbes`) |
| Temporal SDK metrics (optional) | `monitoring.temporalSdkMetrics.port` (9001) | `/metrics` | `templates/_helpers.tpl` |
| MCP face (optional) | 8080 | `/healthz` (startup, readiness), `/livez` (liveness), `/mcp` | `templates/deployment-mcp-face.yaml` |
| Fleet servers | per `Chemclaw3-mcp:MODULES.md` | `/healthz` (readiness), `/livez` (liveness), `/mcp`, `/metrics` | `Chemclaw3-mcp:servers/<name>/deploy/deployment.yaml` |
| UI | 8080 (`http`), 8081 (`sandbox`) | `/readyz`, `/healthz` on `http`. `/sandbox/frame` on `sandbox` (startup). | `Chemclaw3_ui:deploy/openshift/deployment.yaml` |
| PostgreSQL / Temporal / OTLP | 5432 / 7233 / 4317 | — | `networkPolicy.egressPorts` in `values.yaml` |

---

## 2. Prerequisites

### 2.1 Cluster

- **Kubernetes API.** The chart sets no `kubeVersion`. CI validates it against Kubernetes **1.29**
  (`KUBE_VERSION ?= 1.29.0` in `Makefile`, which is OpenShift 4.16). It uses `apps/v1`, `batch/v1`,
  `autoscaling/v2`, `policy/v1` and `networking.k8s.io/v1`.
- **OpenShift-only object:** `route.openshift.io/v1 Route`, for the front door and the UI. On plain
  Kubernetes, set `route.enabled: false` and add your own Ingress (§7.4).
- **Prometheus Operator CRDs** (`monitoring.coreos.com/v1` ServiceMonitor, PodMonitor,
  PrometheusRule) are rendered while `monitoring.enabled: true`, which is the default. If your
  cluster lacks those CRDs, set `monitoring.enabled: false`. On OpenShift the CRDs exist, but they do
  nothing until user-workload monitoring is enabled (§8.6).
- **Optional operators.** `AlertmanagerConfig` (`monitoring.alertmanager.enabled`, apiVersion
  `monitoring.coreos.com/v1beta1`). KEDA / the OpenShift Custom Metrics Autoscaler, at a version
  with the `temporal` scaler (`keda.enabled`, `templates/keda-interactive.yaml`). A custom-metrics
  API that publishes `chemclaw_turns_in_flight`, which the HPA's occupancy metric uses
  (`service.autoscaling.occupancy`). Without that API the HPA falls back to CPU and
  `kubectl describe hpa` reports `FailedGetPodsMetric`.
- **Security context.** Chart pods and the fleet Deployments both run as `runAsNonRoot`, with
  every capability dropped and no fixed UID, so they admit under OpenShift `restricted-v2` as
  shipped (§6.3).

### 2.2 PostgreSQL

- **Version.** The development stack runs `pgvector/pgvector:pg16` (`infra/docker-compose.yml`), and
  that is the tested version. The grants file assumes PostgreSQL 15+ semantics for schema `public`
  (`infra/sql/grants/app_privileges.sql`).
- **Extensions.** `vector` and `pg_trgm`. The migrations issue `CREATE EXTENSION IF NOT EXISTS` for
  both, in `infra/sql/002_molecule_fingerprints.sql`, `infra/sql/012_note_index.sql`,
  `infra/sql/085_job_record_search_trigrams.sql` and others. The migrations build HNSW indexes, so
  pgvector must be **0.5.0 or later**. On a managed Postgres where the migration role cannot create
  `vector`, have a superuser pre-create both extensions in the database (§4.1).
- **TLS.** Under `CHEMCLAW_ENTRA_REQUIRED=true`, a DSN that points at a non-loopback host must carry
  `sslmode=require`, `verify-ca` or `verify-full`. Otherwise every process refuses to start
  (`core/config/__init__.py`, `_TLS_SSLMODES`).
- **Connections.** The server must accept at least `postgres.maxConnections` (default 256) from
  this release. `Settings` checks the fleet's derived pool count against that number at start-up.
- **One database per release.** Many tables have no deployment discriminator, so a second release
  on the same database would have its live threads pruned by this release's retention sweep
  (`values.yaml`, comment on `temporal:`).

### 2.3 Temporal

- A self-hosted Temporal frontend reachable on gRPC (default 7233). The local stack runs
  `temporalio/auto-setup:1.25.2`. The SDK in the image is `temporalio` 1.31.0 (`uv.lock`).
- **A Temporal namespace of this release's own**, registered before install (§4.2).
- **mTLS client certificates** when `secrets.temporalTls.enabled: true`, which is the default.
  Under `CHEMCLAW_ENTRA_REQUIRED=true`, a non-loopback `CHEMCLAW_TEMPORAL_ADDRESS` with no TLS
  settings and no `CHEMCLAW_TEMPORAL_API_KEY` is refused at start-up (`core/config/__init__.py`).
  Temporal Cloud goes through `CHEMCLAW_TEMPORAL_API_KEY` (`secrets.optionalKeys.temporalApiKey`)
  instead of the certificate Secret.
- Workflows use no custom search attributes, so nothing needs registering beyond the namespace.

### 2.4 Identity: Entra ID app registrations

What the code reads (`api/auth.py`, `core/config/entra.py`):

- Tokens are RS256, verified against the tenant JWKS. `aud` must equal `CHEMCLAW_ENTRA_AUDIENCE`
  and `iss` must equal `https://login.microsoftonline.com/<tenant>/v2.0`, which is derived from
  `CHEMCLAW_ENTRA_TENANT_ID` unless you set `CHEMCLAW_ENTRA_ISSUER`. `exp` is required.
- Claims read: `oid` (required, otherwise 401), `preferred_username` or `upn`, `roles` (app roles),
  and `groups` if you set `CHEMCLAW_ENTRA_GROUP_CLAIMS_AS_ROLES=true`. Groups enter the role set
  with the prefix `group:`.

Register two applications:

1. **The API** (the resource the front door protects).
   - Expose an API and add one delegated scope (the UI example uses `Chat.Access`). Nothing in the
     backend checks `scp`.
   - **Set `accessTokenAcceptedVersion` to `2` in the app manifest.** The derived issuer is the
     v2.0 issuer, and a v1 token carries the `sts.windows.net` issuer, so every v1 token is refused.
   - **Set `CHEMCLAW_ENTRA_AUDIENCE` to the `aud` value your tokens actually carry.** For v2.0
     access tokens, Entra puts the API application's client ID (a GUID) in `aud`, not its
     `api://` URI. This is Entra behaviour and nothing in this repository tests it, so decode one
     real token (§8.4) and copy its `aud`. The shipped placeholder `api://chemclaw` will not match
     a real v2.0 token.
   - Define the **app roles** your chemists hold, and assign users or groups to them. Put the
     privileged role values in `CHEMCLAW_ENTRA_PRIVILEGED_ROLES`. That variable ships **empty**,
     and empty means every expensive job is refused for every user (`deploy/README.md`, "The
     setting that does *not* block boot"). The full set of gates is in the runbook, "(xv) Onboard,
     entitle and offboard a person".
2. **The UI** (a single-page-application registration).
   - Redirect URI: `https://<UI host>/auth/callback` (`Chemclaw3_ui:src/auth/msalAuth.ts`).
   - API permission: the scope from step 1, with admin consent granted.

Values that only your tenant can supply, and which nothing here can check: tenant ID, both client
IDs, role names, and whether your tenant emits `groups`.

### 2.5 Registry, build hosts and tools

- A registry that every namespace can pull from. If it needs credentials, set `image.pullSecrets`.
- Build tooling: buildah, podman, kaniko or docker (`deploy/jenkins/lib/image.sh` detects which).
- Operator tools: `oc` (or `kubectl`), `helm` 3 (CI pins `v3.13.0` in `.github/workflows/ci.yml`),
  `kustomize` 5 (or `kubectl kustomize`), the `temporal` CLI, `psql`, and `uv` (only if you migrate
  or validate from a checkout).

---

## 3. Build the images

The supported path is Jenkins: `Jenkinsfile` (this repository), the fleet's own `Jenkinsfile`, the
UI's `Jenkinsfile`, and `deploy/jenkins/Jenkinsfile.release`, which joins the three repositories'
digests into one release descriptor (see `deploy/jenkins/README.md`). The manual equivalents are
below. **Deploy by digest, never by tag.** When `image.digest` is set the chart ignores
`image.tag` (`templates/_helpers.tpl`, `chemclaw.image`).

### 3.1 Backend (one image for every role)

```sh
cd Chemclaw3
docker build -f deploy/Containerfile \
  --build-arg BASE_IMAGE=registry.access.redhat.com/ubi9/python-311@sha256:<base-digest> \
  --build-arg CHEMCLAW_REVISION="$(git rev-parse HEAD)" \
  -t "$REG/chemclaw:$(git rev-parse --short=12 HEAD)" .
docker push "$REG/chemclaw:$(git rev-parse --short=12 HEAD)"
docker image inspect "$REG/chemclaw:$(git rev-parse --short=12 HEAD)" --format '{{ index .RepoDigests 0 }}'
```

- `BASE_IMAGE` is unpinned by default on purpose. Pin it to a digest for a release (runbook,
  "(xiv) Cut a release: pin the image to bytes").
- `CHEMCLAW_REVISION` becomes `CHEMCLAW_DEPLOYMENT_REVISION` in the image, and every audit record
  carries it. If you omit it, the value is `unknown`.
- The image is UBI9, runs as UID 1001, and is safe under an arbitrary UID.
- To check a build: `CHEMCLAW_COMPONENT=not-a-component` must exit 64, and every
  `chemclaw.connectors.<name>.server.app` and `.worker` module must import (the `Verify the image`
  stage in `Jenkinsfile`).

### 3.2 MCP fleet (one image per server)

Build from the `Chemclaw3-mcp` root. The build context is the repository, because every image
copies `uv.lock` and `packages/mcp_server_kit`:

```sh
cd Chemclaw3-mcp
for name in chem safety rxnpredict calc rxnlabel; do
  docker build -f "servers/$name/Containerfile" \
    --build-arg CHEMCLAW_REVISION="$(git rev-parse HEAD)" \
    -t "$REG/chemclaw-mcp-$name:$(git rev-parse --short=12 HEAD)" .
  docker push "$REG/chemclaw-mcp-$name:$(git rev-parse --short=12 HEAD)"
done
```

Every model weight and corpus is baked in at build time, and the servers make no outbound call at
run time. `calc` (xTB and CREST) and `rxnpredict` (model weights) are the slow builds.
`--build-arg CHEMCLAW_REVISION` is what makes `/healthz` report the revision.

### 3.3 UI

```sh
cd Chemclaw3_ui
docker build -t "$REG/chemclaw3-ui:$(git rev-parse --short=12 HEAD)" .   # ALLOW_DEV_AUTH defaults to false
docker push "$REG/chemclaw3-ui:$(git rev-parse --short=12 HEAD)"
```

Never build a production UI with `--build-arg ALLOW_DEV_AUTH=true`.

---

## 4. Prepare the stateful dependencies

Set these once for the rest of this guide:

```sh
NS=chemclaw-prod            # Kubernetes namespace = Temporal namespace (recommended)
oc new-project "$NS"        # or: kubectl create namespace "$NS"
```

### 4.1 PostgreSQL: database, roles, extensions

Use two principals. The owner runs DDL and exists only inside the migrate Job. `chemclaw_app` is
what every pod connects as (runbook, "Splitting the database principal (optional)"). The role name
`chemclaw_app` is fixed in `infra/sql/grants/app_privileges.sql`. The grants file does nothing if
that role does not exist, and then everything runs as one principal.

```sql
-- as a superuser / admin on the database server
CREATE ROLE chemclaw_owner LOGIN PASSWORD '<owner-password>';
CREATE ROLE chemclaw_app   LOGIN PASSWORD '<app-password>';
CREATE DATABASE chemclaw_prod OWNER chemclaw_owner;
\c chemclaw_prod
CREATE EXTENSION IF NOT EXISTS vector;     -- pgvector >= 0.5.0 (HNSW)
CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

You do not run the migrations by hand. The chart's `chemclaw-migrate` hook runs before any app
container starts (`templates/migrate-job.yaml`). It runs as `CHEMCLAW_POSTGRES_MIGRATION_DSN`, and
falls back to `CHEMCLAW_POSTGRES_DSN` when that is unset:

1. `python -m chemclaw.core.migrate` applies `infra/sql/*.sql` in filename order. Each file is
   recorded in `schema_migrations` with a checksum. The migrator holds an advisory lock and a
   `lock_timeout`.
2. `python -m chemclaw.agent.store_setup`.
3. `python -m chemclaw.core.grants` re-applies `infra/sql/grants/app_privileges.sql`. This runs on
   every release. It grants `chemclaw_app` `CREATE` on schema `public`, which LangGraph's
   checkpointer needs on every process start.

To migrate from a workstation instead, for example to stage a database ahead of time, run this from
a checkout of the **same revision** as the image:

```sh
CHEMCLAW_POSTGRES_DSN='postgresql://chemclaw_owner:…@pg.example.com:5432/chemclaw_prod?sslmode=require' \
  make db-migrate && make db-grants
```

`make db-migrate` also runs the message conversion, which in-cluster is the separate
`chemclaw-convert` Job.

### 4.2 Temporal: namespace and client certificate

Register the namespace before `helm install`, because the post-install schedules Job writes into it:

```sh
temporal operator namespace create --namespace "$NS" --retention 720h \
  --address temporal-frontend.temporal.svc:7233 \
  --tls-cert-path admin.crt --tls-key-path admin.key --tls-ca-path temporal-ca.crt
```

Then issue a **client** certificate for ChemClaw from the CA your Temporal frontend trusts. Store it
with the frontend's CA, using exactly these three keys (`values.yaml`, `secrets.temporalTls`):

```sh
oc -n "$NS" create secret generic chemclaw-temporal-tls \
  --from-file=tls.crt=chemclaw-client.crt \
  --from-file=tls.key=chemclaw-client.key \
  --from-file=ca.crt=temporal-ca.crt
```

The chart mounts this Secret at `/etc/temporal/tls` and points `CHEMCLAW_TEMPORAL_TLS_CERT`,
`CHEMCLAW_TEMPORAL_TLS_KEY` and `CHEMCLAW_TEMPORAL_TLS_CA` at it. If the Secret is missing, pods
fail at creation with `secret "chemclaw-temporal-tls" not found`. Set
`secrets.temporalTls.enabled: false` only when a mesh terminates mTLS for you. That plaintext path
is refused under `CHEMCLAW_ENTRA_REQUIRED=true` unless the address is loopback.

---

## 5. Secrets and configuration

### 5.1 How configuration reaches the pods

- **Non-secret settings** come from the `config:` map in values. That map becomes ConfigMap
  `chemclaw-config`, which every pod loads with `envFrom`. Keys are `CHEMCLAW_<FIELD>` of the one
  `Settings` object (`core/config/`). An unknown `CHEMCLAW_*` key is **silently ignored**, so
  misspell one and nothing tells you. Check names against `.env.example`.
- **Some ConfigMap keys are derived by the chart.** Writing them in `config:` either fails the
  render or produces a duplicate key:
  - `CHEMCLAW_TEMPORAL_NAMESPACE` comes from `temporal.namespace`.
  - `CHEMCLAW_CONNECTOR_URLS` and `CHEMCLAW_CONNECTORS_ENABLED` come from `connectors:`.
  - `CHEMCLAW_CONNECTORS_DIR` comes from `extraConnectors`.
  - `CHEMCLAW_NOTE_REPO_DIR` comes from `knowledge.noteRepoPath`.
  - The retention and artifact bounds come from `retention:`.
  - The connection and replica budgets (`CHEMCLAW_SERVICE_FLEET_REPLICAS`,
    `CHEMCLAW_PG_FLEET_POOLS`, …) come from the topology (`templates/config.yaml`, `chemclaw.configData`).
- **Secrets** come from one pre-existing Secret, `secrets.name` (default `chemclaw-secrets`). The
  chart only *names* it and never creates it, unless you set `secrets.create: true`, which only
  writes placeholders. Fill it with ExternalSecret or SealedSecret in a real cluster.

### 5.2 The `chemclaw-secrets` keys

From `values.yaml` `secrets:` and `templates/_helpers.tpl` (`chemclaw.env`, `chemclaw.migrationEnv`):

| Values entry | Secret key | Mounted on | If absent |
|---|---|---|---|
| `keys.llmApiKey` | `CHEMCLAW_LLM_API_KEY` | every pod (**required**) | `CreateContainerConfigError` |
| `keys.postgresDsn` | `CHEMCLAW_POSTGRES_DSN` | every pod (**required**) | `CreateContainerConfigError` |
| `keys.knowledgeRepoToken` | the name that entry holds in `values.yaml` | every pod: **required** once `knowledge.sync.repoUrl` is set, `optional: true` without one | with a remote, `CreateContainerConfigError`; without, nothing (nothing pushes) |
| `migrationKeys.postgresMigrationDsn` | `CHEMCLAW_POSTGRES_MIGRATION_DSN` | migrate Job only | migrations run as `CHEMCLAW_POSTGRES_DSN` |
| `optionalKeys.framingEnvelopeSecret` | `CHEMCLAW_FRAMING_ENVELOPE_SECRET` | every pod | a startup warning, and prompt-injection framing breaks across replicas and restarts. **Set it.** |
| `optionalKeys.{boMcpToken,calcMcpToken,molfpMcpToken,rxnfpMcpToken}` | `CHEMCLAW_BO_MCP_TOKEN`, `CHEMCLAW_CALC_MCP_TOKEN`, `CHEMCLAW_MOLFP_MCP_TOKEN`, `CHEMCLAW_RXNFP_MCP_TOKEN` | every pod (both ends of the chart's own connectors) | every call to that bundle is refused |
| `optionalKeys.{chemToken,safetyToken,rxnpredictToken}` | `CHEMCLAW_CHEM_TOKEN`, `CHEMCLAW_SAFETY_TOKEN`, `CHEMCLAW_RXNPREDICT_TOKEN` | every pod. **The matching fleet server needs the same value** (§6). | every call to that server is refused |
| `optionalKeys.calcToken` | `CHEMCLAW_CALC_TOKEN` | every pod, plus `chemclaw-mcp-calc` | every SMILES-in calc tool and every durable calc job fails |
| `optionalKeys.rxnlabelToken` | `CHEMCLAW_RXNLABEL_TOKEN` | every pod, plus `chemclaw-mcp-rxnlabel` | reaction labelling fails |
| `optionalKeys.mcpFaceToken` | `CHEMCLAW_MCP_FACE_TOKEN` | only with `mcpFace.enabled` | the face answers 401 |
| `optionalKeys.{llmFallbackApiKey,vectorStoreApiKey,temporalApiKey,sessionStoreDsn}` | `CHEMCLAW_LLM_FALLBACK_API_KEY`, `CHEMCLAW_VECTOR_STORE_API_KEY`, `CHEMCLAW_TEMPORAL_API_KEY`, `CHEMCLAW_SESSION_STORE_DSN` | every pod | that feature stays off |
| `optionalKeys.{propsToken,thermalsafetyToken,kineticsToken,unitopsToken,suitabilityToken,pyexecToken}` | `CHEMCLAW_PROPS_TOKEN`, `CHEMCLAW_THERMALSAFETY_TOKEN`, `CHEMCLAW_KINETICS_TOKEN`, `CHEMCLAW_UNITOPS_TOKEN`, `CHEMCLAW_SUITABILITY_TOKEN`, and the name `pyexec`'s manifest gives `auth.token_env` | every pod. **The matching fleet server needs the same value.** | nothing until the bundle is enabled; then every call to it is refused |

A bundle you switch on later needs no Secret-plumbing edit: each fleet bundle's slot ships, named
after its manifest's `auth.token_env`. Add the key to the Secret when you enable the bundle.

Create the Secret. The command below shows the shape. In production, source each value from your
secret store rather than your shell history. The knowledge-repo key name is read from the chart
because the chart is where it is declared:

```sh
rand() { openssl rand -hex 32; }
KREPO_KEY=$(awk '$1 == "knowledgeRepoToken:" {gsub(/"/, "", $2); print $2}' deploy/helm/chemclaw/values.yaml)
oc -n "$NS" create secret generic chemclaw-secrets \
  --from-literal=CHEMCLAW_LLM_API_KEY='<gateway key>' \
  --from-literal=CHEMCLAW_POSTGRES_DSN='postgresql://chemclaw_app:<app-password>@pg.example.com:5432/chemclaw_prod?sslmode=require' \
  --from-literal=CHEMCLAW_POSTGRES_MIGRATION_DSN='postgresql://chemclaw_owner:<owner-password>@pg.example.com:5432/chemclaw_prod?sslmode=require' \
  --from-literal="${KREPO_KEY}=<git token, or empty if knowledge.sync.repoUrl is empty>" \
  --from-literal=CHEMCLAW_FRAMING_ENVELOPE_SECRET="$(rand)" \
  --from-literal=CHEMCLAW_BO_MCP_TOKEN="$(rand)" \
  --from-literal=CHEMCLAW_CALC_MCP_TOKEN="$(rand)" \
  --from-literal=CHEMCLAW_MOLFP_MCP_TOKEN="$(rand)" \
  --from-literal=CHEMCLAW_RXNFP_MCP_TOKEN="$(rand)" \
  --from-literal=CHEMCLAW_CHEM_TOKEN="$(rand)" \
  --from-literal=CHEMCLAW_SAFETY_TOKEN="$(rand)" \
  --from-literal=CHEMCLAW_RXNPREDICT_TOKEN="$(rand)" \
  --from-literal=CHEMCLAW_CALC_TOKEN="$(rand)" \
  --from-literal=CHEMCLAW_RXNLABEL_TOKEN="$(rand)"
```

`sslmode=require` encrypts the connection but does not verify the server. To use `verify-full`,
the pods need a CA file at the path given in `sslrootcert=`: put the PEM in a ConfigMap (or
Secret), set `trustedCA.configMap` (or `trustedCA.secret`) and `trustedCA.key`, and append
`sslmode=verify-full&sslrootcert=/etc/chemclaw/ca/ca.crt` (`trustedCA.mountPath`/`key`) to each
DSN. The same file can back the LLM gateway (`trustedCA.llm: true` sets
`CHEMCLAW_LLM_TLS_CA_BUNDLE`) and the Entra JWKS host (`trustedCA.entra: true` sets
`CHEMCLAW_ENTRA_CA_BUNDLE`), and a knowledge git host (`trustedCA.git: true` sets
`GIT_SSL_CAINFO` for knowledge sync and note pushes); each switch *replaces* that client's trust
store, so turn one on only when the bundle signs that peer.

### 5.3 Settings that stop the render or the boot

The render refuses (`helm template` fails) when:

| Missing statement | Where it is enforced |
|---|---|
| exactly one of `networkPolicy.egressDestinations` or `networkPolicy.allowAnyDestination: true` | `templates/networkpolicy.yaml` |
| exactly one of `retention.windows` or `retention.unboundedGrowthAccepted: true` | `templates/config.yaml` |
| with windows set: exactly one of `retention.artifactStore` or `retention.artifactGrowthAccepted: true` | `templates/config.yaml` |
| with windows set: exactly one of `retention.windows.CHEMCLAW_RETENTION_SESSION_EXHIBITS_DAYS` or `retention.exhibitsGrowthAccepted: true` | `templates/config.yaml` |
| a retention key that is not one of the `CHEMCLAW_RETENTION_*` settings | `templates/config.yaml` (the refusal lists the valid names) |
| `temporal.namespace` | `templates/config.yaml` |
| `config.CHEMCLAW_SESSION_STORE` other than `postgres` | `templates/config.yaml` |
| no connector with `enabled: true` | `templates/_helpers.tpl` (`chemclaw.connectorsEnabled`) |
| a quoted boolean (`"true"`) for any of the posture flags | `templates/config.yaml`, `templates/networkpolicy.yaml` |
| `monitoring.alertmanager.enabled` without receivers or a default receiver | `templates/alertmanagerconfig.yaml` |

Each process refuses to **boot** (the pod crash-loops and names the setting) when any of these is
true under `CHEMCLAW_ENTRA_REQUIRED=true` (`deploy/README.md`, "A non-production cluster that runs
the enforced posture"):

- the tenant or audience is missing;
- a non-loopback DSN has no `sslmode`;
- a non-loopback Temporal address has no TLS or API key;
- `CHEMCLAW_LLM_BASE_URL` is on loopback without `CHEMCLAW_LLM_ALLOW_LOOPBACK_GATEWAY`;
- `CHEMCLAW_ENTRA_CA_BUNDLE` names a file that does not exist;
- an undeclared `HTTP(S)_PROXY` is present;
- the declared turn ceiling `CHEMCLAW_SERVICE_FLEET_MAX_CONCURRENT_TURNS` is smaller than
  `maxReplicas × CHEMCLAW_SERVICE_MAX_CONCURRENT_TURNS`.

### 5.4 A minimal `values-prod.yaml`

This file renders cleanly against the chart, and the `Settings` it produces construct without
error (§8, "What was proven"). Replace every example host, CIDR, GUID and digest with your own.

```yaml
# values-prod.yaml, layered over deploy/helm/chemclaw/values.yaml with -f.

image:
  repository: registry.example.com/chemclaw/chemclaw
  digest: "sha256:<digest from §3.1>"      # set this and image.tag is ignored
  pullSecrets:
    - name: chemclaw-pull                   # omit if the registry needs no credential

route:
  host: chemclaw-api.apps.example.com       # the front door's own Route (API, CLI, Jenkins smoke)

temporal:
  namespace: chemclaw-prod                  # REQUIRED, no default. Registered in §4.2.

config:
  # LLM gateway: any OpenAI-compatible /v1 endpoint. The key is in chemclaw-secrets.
  CHEMCLAW_LLM_BASE_URL: "https://llm-gateway.example.com/v1"
  CHEMCLAW_LLM_MODEL: "gpt-oss"
  CHEMCLAW_LLM_CONTEXT_WINDOW_TOKENS: "131072"
  # Temporal frontend (mTLS through the chemclaw-temporal-tls Secret).
  CHEMCLAW_TEMPORAL_ADDRESS: "temporal-frontend.temporal.svc:7233"
  # The two fleet backends. These are addresses in config, not discovered connectors.
  CHEMCLAW_CALC_SERVER_URL: "http://chemclaw-mcp-calc:8860/mcp"
  CHEMCLAW_RXNLABEL_SERVER_URL: "http://chemclaw-mcp-rxnlabel:8865/mcp"
  # Entra (§2.4). The audience is the `aud` your real tokens carry.
  CHEMCLAW_ENTRA_REQUIRED: "true"
  CHEMCLAW_ENTRA_TENANT_ID: "<tenant GUID>"
  CHEMCLAW_ENTRA_AUDIENCE: "<API app client ID GUID>"
  CHEMCLAW_ENTRA_PRIVILEGED_ROLES: "chemclaw.chemist"   # empty = every expensive job refused
  # Tracing. Set CHEMCLAW_OTEL_ENABLED: "false" if there is no collector.
  CHEMCLAW_OTEL_ENDPOINT: "http://otel-collector.observability.svc:4317"

# Retention posture (REQUIRED, one of two arms). The keys are CHEMCLAW_RETENTION_* settings.
# Keep TOOL_RESULTS at least as long as EXHIBITS if bound values must stay readable.
retention:
  windows:
    CHEMCLAW_RETENTION_CHECKPOINTS_DAYS: 90
    CHEMCLAW_RETENTION_TOOL_RESULTS_DAYS: 365
    CHEMCLAW_RETENTION_SESSION_EVENTS_DAYS: 365
    CHEMCLAW_RETENTION_SESSION_MESSAGES_DAYS: 365
    CHEMCLAW_RETENTION_SESSION_EXHIBITS_DAYS: 365
    CHEMCLAW_RETENTION_RESULT_PUBLICATIONS_DAYS: 365
  artifactStore:                            # required once windows are stated
    CHEMCLAW_ARTIFACT_STORE_MAX_BYTES: 53687091200
    CHEMCLAW_ARTIFACT_EVICT_IDLE_DAYS: 180

# Egress posture (REQUIRED, one of two arms). The list REPLACES "anywhere", so it must include this
# release's own pods and the fleet, which are same-namespace peers, plus every external host.
# NetworkPolicy cannot name an FQDN, so external systems are ipBlocks.
networkPolicy:
  egressDestinations:
    - podSelector: {matchLabels: {app.kubernetes.io/name: chemclaw}}    # own connector Services
    - podSelector: {matchLabels: {app.kubernetes.io/part-of: chemclaw3}} # the MCP fleet
    - namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: temporal}}
    - namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: observability}}
    - ipBlock: {cidr: 10.20.30.40/32}       # PostgreSQL
    - ipBlock: {cidr: 10.20.31.0/24}        # LLM gateway, Git remote
    - ipBlock: {cidr: 20.190.128.0/18}      # Entra (login.microsoftonline.com). Use your tenant's published ranges.
    - ipBlock: {cidr: 40.126.0.0/18}
  # Who may open the front door. The UI in this namespace is admitted by the shipped
  # `uiPodSelector` (§7.3), so the release namespace itself is not listed.
  ingressNamespaces:
    - network.openshift.io/policy-group: ingress
    - kubernetes.io/metadata.name: openshift-user-workload-monitoring
```

More on what this file says:

- **The ports** come from `networkPolicy.egressPorts`: 5432, 7233, 443, 8000, 4317 and each fleet
  port. They apply to every destination above. If your gateway or Postgres listens elsewhere, add
  the port as a new key.
- **The connectors** are left at the chart defaults: `molfp`, `rxnfp`, `bo`, `calc` and `results`
  run in this release, and `chem`, `safety` and `rxnpredict` are dialled at the fleet's Services.
  `CHEMCLAW_CONNECTOR_URLS` is derived from that block.
- **Turning on another fleet connector** (for example `props`) takes three things: set
  `connectors.props.enabled: true`, add its token to `chemclaw-secrets` (the `secrets.optionalKeys`
  slot and the `networkPolicy.egressPorts` entry already ship for every fleet server), and deploy
  that server (§6). The image already ships the manifests for every fleet server listed in §1
  (they arrive with the `chemclaw-contracts` package), so you do not need `extraConnectors` for
  them. A server whose manifest the image does not ship (a site's own) also needs an
  `extraConnectors.bundles` entry and a ConfigMap holding its `connector.yaml`
  (`deploy/README.md`, "Attaching a connector bundle this image does not ship"). A name the image
  already declares cannot be mounted again: that is a startup error.
- **The egress guard.** Each process also runs its own egress guard (`core/netguard.py`, plus an
  `LD_PRELOAD` layer armed by `deploy/entrypoint.sh`). The guard's allowlist is derived from the
  settings above. A host that comes only from a *manifest*, such as a warehouse ELN `connection:`
  or a result sink, must be listed in `CHEMCLAW_EGRESS_ALLOW` (`core/config/observability.py`).
- **The knowledge graph.** `knowledge.sync.repoUrl` is a Git HTTPS URL. If you leave it empty, pods
  serve the corpus baked into the image and **no agent-recorded note can be persisted**
  (`deploy/knowledge-sync.sh`, `checkout` mode). Keep `workers.background.replicas: 1`, because the
  note writer's checkout lock is per host.
- **The ServiceMonitor label.** If your Prometheus selects ServiceMonitors by label, set
  `monitoring.additionalLabels`.

For an environment managed by Jenkins, this file lives at `deploy/jenkins/environments/<env>.yaml`.
That folder deliberately ships empty: read `deploy/jenkins/environments/README.md`.

---

## 6. Deploy the MCP fleet

Deploy the fleet **before** the backend. The backend dials it, and the release order in
`deploy/jenkins/README.md` is fleet, then core, then UI.

### 6.1 What each server needs

| Server | Manifests | Bearer variable (in the server pod **and** in the chart pods) | `MCP_ALLOWED_HOSTS` (already set in its Deployment) |
|---|---|---|---|
| `chem` | `Chemclaw3-mcp:servers/chem/deploy/*.yaml` | `CHEMCLAW_CHEM_TOKEN` | `chemclaw-mcp-chem:8858` |
| `safety` | `Chemclaw3-mcp:servers/safety/deploy/*.yaml` | `CHEMCLAW_SAFETY_TOKEN` | `chemclaw-mcp-safety:8859` |
| `rxnpredict` | `Chemclaw3-mcp:servers/rxnpredict/deploy/*.yaml` | `CHEMCLAW_RXNPREDICT_TOKEN` | `chemclaw-mcp-rxnpredict:8857` |
| `calc` | `Chemclaw3-mcp:servers/calc/deploy/*.yaml` | `CHEMCLAW_CALC_TOKEN` | `chemclaw-mcp-calc:8860` |
| `rxnlabel` | `Chemclaw3-mcp:servers/rxnlabel/deploy/*.yaml` | `CHEMCLAW_RXNLABEL_TOKEN` | `chemclaw-mcp-rxnlabel:8865` |

These five are the servers the shipped chart turns on. The off-by-default servers (`props` 8850,
`thermalsafety` 8851, `kinetics` 8852, `unitops` 8853, `suitability` 8892, `pyexec` 8899) follow
the same pattern with `CHEMCLAW_<NAME>_TOKEN`; switching one on also needs
`connectors.<name>.enabled: true` and its token in `chemclaw-secrets` (the slot and the egress port
ship, §5); its manifest is in the image like the others'. The full
per-server table, with interactive-queue sizing, is `Chemclaw3-mcp:docs/operations.md` §3.

Each server's bearer variable is whatever its manifest declares as `auth.token_env`
(`Chemclaw3-mcp:manifests/<name>/connector.yaml`, or `Chemclaw3-mcp:manifests-internal/<name>/connector.yaml`
for `calc` and `rxnlabel`). Each server **fails closed**: if the variable is unset, every `/mcp`
call is refused. **The shipped fleet Deployments already wire that variable** from the Secret
`chemclaw-secrets`, under the key of the same name (a `secretKeyRef` that is not `optional`). That
is the Secret this chart reads, so both ends hold one value as long as the fleet runs in the
release's namespace. A missing key keeps the server's pod in `CreateContainerConfigError` rather
than letting it start and refuse every call, so add the key before you apply the server.

The image is a **placeholder** that you must rewrite: `registry.invalid/chemclaw-mcp-<name>:unset`.
`.invalid` is a reserved top-level domain, so the placeholder can never resolve to a registry. The
overlay below rewrites it to your digest.

`MCP_ALLOWED_HOSTS` must contain the exact `host:port` the backend dials. The shipped value is the
Service short name. If you dial a server by any other name, such as a namespace-qualified name or
an ingress host, add that name too, or `/mcp` answers `421` while `/healthz` stays green.

### 6.2 Render and apply with a kustomize overlay

Run this from any directory next to a `Chemclaw3-mcp` checkout. It copies each server's shipped
manifests and pins the image by digest. It does not add the bearer, because the Deployment already
reads it (§6.1). A JSON-patch `add` to `.../env/-` would append a second entry with the same name:

```sh
MCP=../Chemclaw3-mcp; NS=chemclaw-prod; REG=registry.example.com/chemclaw
declare -A DIGEST=( [chem]=sha256:… [safety]=sha256:… [rxnpredict]=sha256:… [calc]=sha256:… [rxnlabel]=sha256:… )
for name in "${!DIGEST[@]}"; do
  src="$MCP/servers/$name/deploy"; dir="fleet-overlay/$name"
  mkdir -p "$dir"; cp "$src"/{deployment,service,networkpolicy,pdb,hpa,servicemonitor}.yaml "$dir/"
  cat >"$dir/kustomization.yaml" <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: $NS
resources: [deployment.yaml, service.yaml, networkpolicy.yaml, pdb.yaml, hpa.yaml, servicemonitor.yaml]
images:
  - name: registry.invalid/chemclaw-mcp-$name
    newName: $REG/chemclaw-mcp-$name
    digest: ${DIGEST[$name]}
EOF
  kustomize build "$dir" | oc apply -n "$NS" -f -
done
```

- Before applying, check that the rendered image is your registry path:
  `kustomize build "$dir" | grep 'image:'`. A placeholder left in the output means the `images:`
  name did not match.
- The token naming rule `CHEMCLAW_<NAME>_TOKEN` holds for every server in the table, and
  `deploy/kind/render-fleet.sh` checks it against each server's source before relying on it.
- Without Prometheus Operator CRDs, drop `servicemonitor.yaml` from both the `cp` and `resources`.
- Optional KEDA scalers live in `Chemclaw3-mcp:servers/<name>/deploy/keda/`, and each server's
  README explains them.
- Resource sizing per server (replicas, requests) is in `Chemclaw3-mcp:MODULES.md`. `calc` alone
  asks for 1 CPU and 1 GiB per pod across 2 to 8 replicas.

### 6.3 OpenShift: no SCC grant needed

The fleet Deployments set `runAsNonRoot: true` and pin no `runAsUser`, `runAsGroup` or `fsGroup`.
`restricted-v2` assigns a UID from the namespace's range, and each server writes only to its `/tmp`
`emptyDir`, which is writable under any UID. Apply them as shipped, with no `nonroot-v2` grant and
no patch.

### 6.4 How the backend finds the fleet

- **Connectors** (`chem`, `safety`, `rxnpredict`, and any you enable later): set
  `connectors.<name>.url`. The chart folds it into `CHEMCLAW_CONNECTOR_URLS` and renders no pods
  for that bundle. The manifest that declares the tool surface ships in the image under
  `src/chemclaw/connectors/<name>/`.
- **Backends** (`calc`, `rxnlabel`): set `config.CHEMCLAW_CALC_SERVER_URL` and
  `config.CHEMCLAW_RXNLABEL_SERVER_URL`. The settings `calc_server_token_env` and
  `rxnlabel_server_token_env` name the bearer variables (`core/config/calculators.py`,
  `core/config/labels.py`).
- **Never put `Chemclaw3-mcp:manifests-internal/` on `CHEMCLAW_CONNECTORS_DIR`**, not even through
  `extraConnectors`. A `calc` manifest that is discovered there wins the name collision with this
  repository's `calc` bundle, and that silently removes the calculation cache, the calibration
  ledger and every durable calc job from the agent. The internal manifests declare
  `mount: backend`, which this repository's manifest model refuses, so the mistake shows up as a
  start-up error that names the file.

---

## 7. Install the backend, then the UI

### 7.1 Render first

```sh
cd Chemclaw3
helm lint deploy/helm/chemclaw -f values-prod.yaml
helm template chemclaw deploy/helm/chemclaw -n "$NS" -f values-prod.yaml > rendered.yaml
```

A posture you have not stated fails here, before anything reaches the cluster (§5.3).

### 7.2 Install or upgrade

```sh
helm upgrade --install chemclaw deploy/helm/chemclaw \
  --namespace "$NS" -f values-prod.yaml \
  --wait --timeout 15m
```

- Use **`--wait` and not `--atomic`.** `chemclaw-convert` is a post-upgrade hook that rewrites
  stored messages. `--atomic` would roll a healthy release back over a slow or failed conversion,
  and that leaves converted rows behind an older reader (`deploy/jenkins/targets/openshift.sh`).
- The order on install is: hook ConfigMap and ServiceAccount, then `chemclaw-migrate`, then the
  release objects, then `chemclaw-convert` and `chemclaw-schedules`.
- A rollout is slow by design. The front door's `terminationGracePeriodSeconds` is the turn
  timeout plus the drain time, so up to about 615 s per pod (`deploy/README.md`, "Draining a pod").
- `deploy/jenkins/targets/openshift.sh` runs the same command from a release descriptor. It
  defaults to `DRY_RUN=true`.

### 7.3 Deploy the UI

The UI repository ships example manifests and no chart. Copy
`Chemclaw3_ui:deploy/openshift/{deployment,service,routes}.yaml`, change the values below, and
apply them into the **same** namespace:

| UI environment variable | Value |
|---|---|
| the backend URL (the entry whose example is `http://chemclaw-service:8080`) | keep `http://chemclaw-service:8080`. That is the chart's Service, and its name is fixed. Give the service root with no path. |
| `AUTH_MODE` | `msal` |
| `ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID` | the tenant, and the **UI** app registration's client ID |
| `API_SCOPE` | the API scope, e.g. `api://<API app client ID>/Chat.Access` |
| `REVIEWER_ROLES` | the app-role values that may review (UI-side display only) |
| `APP_ORIGIN`, `SANDBOX_ORIGIN` | `https://` plus the two Route hosts, **exactly**. Put the sandbox on a separate registrable domain. |

```sh
oc -n "$NS" apply -f ui/deployment.yaml -f ui/service.yaml -f ui/routes.yaml
oc -n "$NS" set image deployment/chemclaw3-ui ui="$REG/chemclaw3-ui@sha256:<ui-digest>"
```

- **Allowing the UI through the front-door NetworkPolicy.** The chart's `chemclaw-service-ingress`
  policy admits the chart's own pods, the namespaces in `networkPolicy.ingressNamespaces`, and pods
  in this namespace matching `networkPolicy.uiPodSelector` — shipped as
  `app.kubernetes.io/name: chemclaw3-ui`, the label `Chemclaw3_ui:deploy/openshift/deployment.yaml`
  puts on its pods. If you relabel the UI, change the selector (and set the old key to `null` in
  the same override: Helm merges maps). The kind lane's UI carries a different label and is
  admitted by its namespace entry instead.
- **The UI startup check.** `Chemclaw3_ui:server/config.ts` refuses to start on a missing tenant,
  client ID, scope or malformed origin.
- **The sandbox Route.** Nothing in front of it may authenticate or rewrite headers
  (`Chemclaw3_ui:deploy/openshift/README.md`).

### 7.4 Plain Kubernetes differences

- `route.enabled: false`, then add an Ingress to `chemclaw-service:8080` (and two for the UI).
  **Session affinity is not required.** Uploaded attachments are stored in Postgres
  (`session_attachments`, D-2026-10-04-an-upload-is-session-state-not-pod-state) and every replica
  reads them, and a running turn can be followed (`GET /sessions/{id}/turn/stream`) or stopped
  (`POST …/turn/stop`) on any replica, which asks the pod holding the turn through Postgres
  (D-2026-10-04-a-running-turn-is-reached-through-postgres-from-any-replica). The UI's BFF, which
  reaches `chemclaw-service:8080` with no cookie, therefore keeps its uploads, its live view and its
  Stop. The Route's affinity cookie (`templates/service-route.yaml`) is harmless and nothing
  depends on it.
- Replace the OpenShift selectors in `networkPolicy.ingressNamespaces` and
  `networkPolicy.monitoringNamespaces` with your ingress controller's and Prometheus's namespaces,
  for example `kubernetes.io/metadata.name: ingress-nginx`. Without Prometheus Operator, set
  `monitoring.enabled: false`. That also drops the dashboards ConfigMap.

---

## 8. Verify the install

Work through these in order. Each step names what a failure there points to.

1. **The release, the notes, the pods.**
   ```sh
   helm -n "$NS" status chemclaw && helm -n "$NS" get notes chemclaw
   oc -n "$NS" get pods -l app.kubernetes.io/instance=chemclaw
   oc -n "$NS" get pods -l app.kubernetes.io/part-of=chemclaw3      # the fleet
   oc -n "$NS" get pods -l app.kubernetes.io/name=chemclaw3-ui
   ```
   Every pod must be `Running` and ready. The hook Jobs are deleted when they succeed
   (`hook-delete-policy: hook-succeeded`), so a `chemclaw-migrate` Job that is still listed has
   failed. Read its logs, then follow the runbook, "(xi) A migration that will not apply, and a
   release stuck in `pending-upgrade`".
2. **Front door.**
   ```sh
   HOST=$(oc -n "$NS" get route chemclaw -o jsonpath='{.spec.host}')
   curl -fsS "https://$HOST/healthz"     # {"status": "ok"}
   curl -fsS "https://$HOST/readyz"      # {"status": "ready", "connectors_unhealthy": 0}
   ```
   `/readyz` gives 503 with `"database unreachable"` or `"schema behind image"` when Postgres or
   the migrations are wrong. It reports connectors in `connectors_unhealthy` but does not fail on
   them unless `CHEMCLAW_CONNECTORS_REQUIRED=true`. **Non-zero means a connector, a fleet server,
   a token or the egress policy is wrong.**
3. **Workers and fleet probes** (from inside the namespace):
   ```sh
   oc -n "$NS" exec deploy/chemclaw-background-worker -c background-worker -- \
     python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:9000/readyz').read())"
   for s in chem:8858 safety:8859 rxnpredict:8857 calc:8860 rxnlabel:8865; do
     oc -n "$NS" exec deploy/chemclaw-service -c service -- python -c \
       "import urllib.request as u; print('$s', u.urlopen('http://chemclaw-mcp-${s%%:*}:${s##*:}/healthz').read()[:200])"
   done
   ```
   A fleet `/healthz` body lists the corpora it verified and the build revision. A revision of
   `"unknown"` means the image was built without `--build-arg CHEMCLAW_REVISION`.
4. **Authentication end to end.** Get a v2.0 access token for the API scope from your tenant. For
   example, `az account get-access-token --scope api://<API app client ID>/Chat.Access --query accessToken -o tsv`
   works only if your tenant pre-authorizes the Azure CLI client for that API, so this step is
   site-specific. Decode the token's payload and confirm that `aud`, `iss` and `roles` are what
   §2.4 expects. Then run one turn:
   ```sh
   TOKEN=…   # never paste it into a shared terminal log
   test "$(curl -s -o /dev/null -w '%{http_code}' -X POST "https://$HOST/sessions")" = 401
   SID=$(curl -fsS -X POST "https://$HOST/sessions" -H "Authorization: Bearer $TOKEN" \
           -H 'content-type: application/json' -d '{}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["session_id"])')
   curl -sN -X POST "https://$HOST/sessions/$SID/messages" -H "Authorization: Bearer $TOKEN" \
     -H 'content-type: application/json' -H 'accept: text/event-stream' \
     -d '{"message": "What is the molecular weight of caffeine?"}'
   ```
   A healthy turn streams frames and ends with an `"type": "answer"` frame. An `"type": "error"`
   frame, a 401 on the authenticated call, or a 503 (the tenant JWKS could not be reached) are the
   failures to look for. This is the shape the kind smoke uses (`deploy/kind/up.sh`, `turn()`).
5. **Migrations applied.**
   ```sql
   SELECT count(*), max(filename) FROM schema_migrations;   -- max = the newest infra/sql file in this image
   SELECT has_schema_privilege('chemclaw_app', 'public', 'CREATE');   -- t (split principal)
   ```
6. **Temporal workers are polling, and the Schedules exist.**
   ```sh
   T="--namespace $NS --address temporal-frontend.temporal.svc:7233 --tls-cert-path chemclaw-client.crt --tls-key-path chemclaw-client.key --tls-ca-path temporal-ca.crt"
   temporal task-queue describe --task-queue background-jobs $T
   for q in connector-bo connector-calc connector-results connector-calc-interactive connector-chem-interactive; do
     temporal task-queue describe --task-queue "$q" $T; done
   temporal schedule list $T
   ```
   Each queue must show at least one poller. An empty Schedule list means `chemclaw-schedules`
   failed.
7. **The UI.** Open `https://<UI host>`, sign in, and send a message. `curl https://<UI host>/readyz`
   calls the backend's `/readyz` through the BFF, so a failure there is either the NetworkPolicy
   from §7.3 or the backend-URL variable.
8. **Metrics and alerts.**
   ```sh
   oc -n "$NS" get servicemonitor,podmonitor,prometheusrule
   ```
   On OpenShift these objects do nothing until user-workload monitoring is on and alert routing
   exists. `helm get notes` prints the exact switches. The runbook's "(x-b) Make the monitoring
   stack actually collect this" is the full procedure. Confirm under Observe → Targets that the
   front door, the connectors and every worker's `metrics` port are `up`. Confirm that
   `chemclaw_connectors_unhealthy` reads 0.
9. **Privileged roles.** As a user who holds a role in `CHEMCLAW_ENTRA_PRIVILEGED_ROLES`, ask for a
   calculation. As a user who does not, confirm the refusal. To list the actions this deployment
   gates:
   `oc -n "$NS" exec deploy/chemclaw-service -c service -- python -c "from chemclaw.agent.authz import expensive_actions; print(sorted(expensive_actions()))"`.
10. **Knowledge write** (only if `knowledge.sync.repoUrl` is set). Ask the agent to record a note,
    then confirm a commit with the `Chemclaw-Note: recorded` trailer reaches the remote. Nothing
    reports a broken notes clone before the first write does (runbook, "Exposing the front door").

**What was proven for this guide, and what was not.**

- **Proven by running:**
  - The `values-prod.yaml` block in §5.4, copied verbatim with its placeholders, renders with
    `helm template chemclaw deploy/helm/chemclaw -n chemclaw-prod -f values-prod.yaml` (helm
    v3.16.2). The output validates with
    `kubeconform -strict -summary -ignore-missing-schemas -kubernetes-version 1.29.0` and the
    datreeio CRDs catalog: 36 objects, 35 valid, 0 invalid. The Route has no schema and is skipped.
  - The same file plus the §7.4 overrides validates with no skips.
  - The rendered `chemclaw-config`, together with the Secret keys, constructs `Settings`.
  - Removing `sslmode` from the DSN makes `Settings` refuse, as §2.2 says.
  - The §6.2 overlay builds with `kubectl kustomize` (kubectl v1.29.9, kustomize v5.0.4) for all
    five servers. The output validates with
    `kubeconform -strict -ignore-missing-schemas` (30 objects, 25 valid, the 5 ServiceMonitors
    skipped), every image is the digest, and each Deployment carries exactly one bearer entry.
- **Not proven:** nothing here was applied to a live OpenShift cluster, so the `restricted-v2`
  admission in §6.3, the Entra `aud` and `iss` behaviour in §2.4 and the `temporal` CLI flags are stated from
  platform documentation rather than from a run.

---

## 9. Upgrades, rollback and day 2

- **Upgrade.** Build, push and pin a new digest, then repeat §7.2. The migrate hook runs first, and
  it is safe while the old pods are still serving. If a release changes workflow code, read
  `docs/guides/workflow-versioning.md` first (`deploy/README.md`, "Before a deploy that touches
  workflow code"). A release that changes a sign-off route must ship backend and UI together
  (runbook, "(xiv) Cut a release: pin the image to bytes").
- **Upgrading from a release that predates the tracked ConfigMap and ServiceAccount.** See the
  runbook, "`helm upgrade` refuses: \"exists and cannot be imported into the current release\"",
  and `deploy/README.md`, "Upgrading a release installed before this chart".
- **Rollback.** Run `helm -n "$NS" rollback chemclaw`. The runbook section "Roll back a release"
  lists what an older image loses against a newer schema, and which of those losses are silent.
- **A stuck migration or `pending-upgrade`.** Runbook, "(xi) A migration that will not apply, and
  a release stuck in `pending-upgrade`".
- **Entitlements and offboarding.** Runbook, "(xv) Onboard, entitle and offboard a person".
- **Alerts.** Runbook, "(x-c) When an alert fires", has one entry per rule in
  `templates/prometheusrule.yaml`.
- **Symptoms and fixes.** [troubleshooting.md](troubleshooting.md).
- **Everything else operational.** [runbook.md](runbook.md).

---

## Appendix: running everything on one machine

| Goal | Command | What it does |
|---|---|---|
| Dependencies only | `make up` | Postgres (`pgvector/pgvector:pg16`) and Temporal (`temporalio/auto-setup:1.25.2`) with its UI on `localhost:8081` (`infra/docker-compose.yml`) |
| Schema | `make db-migrate && make db-grants` | migrations, message conversion and grants against `CHEMCLAW_POSTGRES_DSN` |
| Live processes on the host | `make live-infra`, `make live-up`, `make live-status`, `make live-probes` | connectors, workers and front door as processes (`infra/live/`). Without a gateway this uses `chemclaw.cli.mock_llm` on loopback. |
| Terminal chat | `make chat` | needs `CHEMCLAW_LLM_BASE_URL` reachable |
| All four repositories on the host | `make live-e2e-full-stack` | `infra/live/e2e-full-stack/up.sh` |
| A real cluster on your laptop | `make kind-up`, `make kind-smoke`, `make kind-down` | the production chart plus `deploy/kind/values-kind.yaml`, the fleet's own manifests, the UI and mocks. See `deploy/kind/README.md`. |
| Offline render check of all of it | `make helm-validate`, `make kind-validate` | needs `helm`, `kubeconform` and `promtool` on `PATH` |

Docker in the Claude Code remote environment is installed but not started: run `sudo -n dockerd &`
before `make up`.
