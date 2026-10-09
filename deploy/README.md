# Deploy — Chemclaw on OpenShift

The stack runs in-cluster with Entra sign-in, Temporal workers, a Postgres it does not own, and
probes on every process. One image runs every role; one config source (the pydantic `Settings` in
`src/chemclaw/core/config/`) is fed from a `ConfigMap` and a small set of plain `Secret`s. The chart
is `deploy/helm/chemclaw`; `values.yaml` beside it documents every key next to the argument for its
default.

For a local, cluster-shaped run of all four repositories (kind, mock tenant, mock LLM) see
[`deploy/kind/README.md`](kind/README.md). For the Jenkins delivery pipelines see
[`deploy/jenkins/README.md`](jenkins/README.md). Day-two operation and alert response are in
[`docs/guides/runbook.md`](../docs/guides/runbook.md).

## Install, step by step

### 0. What the cluster must already have

| Dependency | Who provides it | What this release needs from it |
| --- | --- | --- |
| OpenShift (or Kubernetes with a Route CRD; `route.enabled: false` otherwise) | the platform | one namespace per release |
| Postgres with the `pgvector` extension available | a DBA or a managed service | **one database per release**, TLS on, reachable on 5432 |
| Temporal (self-hosted, one broker per cluster) | the platform team | **one Temporal namespace per release**, registered; mTLS on the frontend |
| An OpenAI-compatible LLM gateway | the platform team | a base URL, a model name, an API key |
| An Entra tenant | identity team | tenant id, an app registration whose audience the front door validates, app roles for privileged users |
| The `Chemclaw3-mcp` tool fleet | this system's sibling repository | its servers deployed **in the same namespace** (see step 5) |
| A git remote for the knowledge graph | your git host | a repository URL and a push token (optional: without one the image's own corpus is served read-only) |
| An OTLP collector, Prometheus user-workload monitoring | the platform | optional, but every monitor in the chart is inert without the second (see `templates/NOTES.txt`) |

### 1. Build and publish the images

- **Core** — one image for every role: `docker build -f deploy/Containerfile --build-arg
  CHEMCLAW_REVISION=$(git rev-parse HEAD) -t <registry>/chemclaw:<tag> .` (UBI9 Python 3.11,
  runs as UID 1001, arbitrary-UID safe). A release also pins the base by digest with `--build-arg
  BASE_IMAGE=registry.access.redhat.com/ubi9/python-311@sha256:<...>`, and deploys by **digest**:
  set `image.digest` (the tag is then ignored) — runbook § "(xiv) Cut a release: pin the image to
  bytes". `image.pullSecrets` takes a list of `{name: <secret>}` for a private registry.
- **The tool fleet** — one image per `Chemclaw3-mcp` server, built from that repository's
  `servers/<name>/Containerfile`. Which servers you need follows from step 5.
- **The UI** — `Chemclaw3_ui`, deployed beside this release (it has no chart).

The Jenkins pipelines in `deploy/jenkins/` do this build-push-render-apply cycle by digest.

### 2. Prepare the database

1. Create a database for this release alone. **Two releases need two databases**: dozens of tables
   carry no deployment discriminator, and one release's retention sweep would prune another's live
   threads. No chart guard can check this.
2. Create the migrator principal (it owns the schema and must be able to `CREATE EXTENSION
   vector` — the migrations issue it). That DSN is `CHEMCLAW_POSTGRES_DSN` in a single-principal
   setup.
3. Optional, recommended: split the principal. Create a role named exactly `chemclaw_app`, put its
   DSN in `CHEMCLAW_POSTGRES_DSN` and the owner's DSN in `CHEMCLAW_POSTGRES_MIGRATION_DSN`
   (`secrets.migrationKeys`, mounted on the migrate Job only). The migrate Job applies
   `infra/sql/grants/app_privileges.sql` on every deploy, granting `chemclaw_app` exactly the verbs
   `src/` executes — it may read `audit_events` and append to it, never update or delete; with no
   such role it no-ops. Runbook § "Splitting the database principal (optional)".
4. Every DSN must carry `sslmode=require`, `verify-ca` or `verify-full` and name a host: under
   `CHEMCLAW_ENTRA_REQUIRED=true` (the chart default) a non-loopback DSN without one stops every
   pod at boot.
5. Size `max_connections` for `postgres.maxConnections` (256 shipped) plus whatever else shares the
   server. A stock `max_connections=100` refuses this release at full scale, and every pod refuses
   to start if the fleet's derived peak exceeds the declared ceiling (see "Observability" below).

You never run migrations by hand: the chart's `migrate` hook Job (`pre-install`, `pre-upgrade`,
`pre-rollback`) runs `python -m chemclaw.core.migrate`, then the store setup, then the grants,
before any app container starts.

### 3. Prepare Temporal

1. Pick this release's Temporal namespace — the release's own Kubernetes namespace name is the
   natural choice — and register it on the broker (`temporal operator namespace create
   --namespace <ns>`, or your platform's equivalent).
2. Issue a client certificate for this release and create the Secret `chemclaw-temporal-tls` with
   keys `tls.crt`, `tls.key`, `ca.crt` (details under "The one Secret that is files, not env"). A
   Temporal frontend without TLS is refused at boot under `CHEMCLAW_ENTRA_REQUIRED=true`; for
   Temporal Cloud set `secrets.temporalTls.enabled: false` and put the API key in
   `CHEMCLAW_TEMPORAL_API_KEY`.
3. Set `config.CHEMCLAW_TEMPORAL_ADDRESS` if the frontend is not
   `chemclaw-temporal-frontend.temporal.svc:7233`.

### 4. Create the Secrets

The chart **names** Secrets and never fills them; populate them with an
`ExternalSecret`/`SealedSecret` (or `oc create secret`). `secrets.create: true` templates a
`CHANGE-ME` placeholder for a dev install only.

**`chemclaw-secrets`** (`secrets.name`) — each key is the environment variable it becomes:

| Key | Required? | What |
| --- | --- | --- |
| `CHEMCLAW_LLM_API_KEY` | **yes** (key must exist) | the gateway's API key |
| `CHEMCLAW_POSTGRES_DSN` | **yes** | the runtime DSN (step 2) |
| the knowledge-repo push token (`secrets.keys.knowledgeRepoToken`) | **yes once `knowledge.sync.repoUrl` is set**; with no remote it is mounted `optional: true` and may be absent | the token `deploy/knowledge-sync.sh` clones with and the note writer pushes with — both through the image's `chemclaw-git-askpass` helper, so it never lands in a URL, `.git/config` or argv |
| `CHEMCLAW_POSTGRES_MIGRATION_DSN` | optional, migrate Job only | the owner's DSN when the principal is split |
| `CHEMCLAW_FRAMING_ENVELOPE_SECRET` | optional, **set it** | HMAC key for the retrieved-content envelope; unset, each process picks its own and replicas disagree |
| `CHEMCLAW_CHEM_TOKEN`, `CHEMCLAW_SAFETY_TOKEN`, `CHEMCLAW_RXNPREDICT_TOKEN`, `CHEMCLAW_CALC_TOKEN`, `CHEMCLAW_RXNLABEL_TOKEN` | optional to the chart, **required by the capability** | bearers this release presents to the fleet servers; unset, every call to that server is refused |
| `CHEMCLAW_BO_MCP_TOKEN`, `CHEMCLAW_CALC_MCP_TOKEN`, `CHEMCLAW_MOLFP_MCP_TOKEN`, `CHEMCLAW_RXNFP_MCP_TOKEN` | optional to the chart, **required by the capability** | bearers for the connector servers this release runs itself (both ends read the same variable) |
| `CHEMCLAW_PROPS_TOKEN`, `CHEMCLAW_KINETICS_TOKEN`, `CHEMCLAW_THERMALSAFETY_TOKEN`, `CHEMCLAW_UNITOPS_TOKEN`, `CHEMCLAW_SUITABILITY_TOKEN`, and `pyexec`'s (`secrets.optionalKeys.pyexecToken`) | only when that bundle is enabled | bearers for the fleet bundles that ship off; slotted already, so enabling one is the `connectors:` line plus this key |
| `CHEMCLAW_MCP_FACE_TOKEN` | only with `mcpFace.enabled` | the read-only MCP face's bearer; unset, it answers 401 |
| `CHEMCLAW_LLM_FALLBACK_API_KEY`, `CHEMCLAW_VECTOR_STORE_API_KEY`, `CHEMCLAW_TEMPORAL_API_KEY`, `CHEMCLAW_SESSION_STORE_DSN` | optional | the failover gateway, a non-pgvector store, Temporal Cloud, a split session database |

The required keys are mounted with a non-optional `secretKeyRef` on every pod, so a missing key is
`CreateContainerConfigError` on the migrate Job first and the install never proceeds — the LLM key
and the DSN always, the push token only on a release with a knowledge remote. Every
other key is `optional: true`, so adding one never breaks an upgrade — and "optional" says nothing
about whether the capability works without it. The full argument for each slot is beside it in
`values.yaml` under `secrets:`.

**`chemclaw-temporal-tls`** (`secrets.temporalTls.secretName`) — step 3.

**A private CA** (`trustedCA`) — one PEM bundle in a ConfigMap or Secret you create, mounted
read-only into every container, the migrate and convert hook Jobs included:

```yaml
trustedCA:
  configMap: site-ca        # or `secret:`; exactly one
  key: ca.crt               # mounted at /etc/chemclaw/ca/ca.crt (`mountPath`/`key`)
  llm: true                 # sets CHEMCLAW_LLM_TLS_CA_BUNDLE to that file
  entra: true               # sets CHEMCLAW_ENTRA_CA_BUNDLE to that file
  git: true                 # sets GIT_SSL_CAINFO (knowledge sync and note pushes)
```

Each switch *replaces* the trust store of its client rather than adding to it, so turn one on only when
the bundle signs that peer. Postgres takes no switch: append
`sslmode=verify-full&sslrootcert=/etc/chemclaw/ca/ca.crt` to each DSN in the Secret. With
publicly-trusted certificates none of this is needed (`sslmode=require` verifies nothing;
`verify-full` needs the CA file).

Other objects you create yourself, when you turn on what needs them: one ConfigMap per
`extraConnectors.bundles[]` entry (step 5), the `documentShare.claimName` PersistentVolumeClaim (state its `documentShare.accessMode`;
ReadWriteMany or ReadOnlyMany with more than one background worker) for an SMB/CIFS share, and the image pull secrets.

### 5. Wire the MCP tool fleet

Most scientific capability is served by `Chemclaw3-mcp`, which this chart **does not deploy**. The
shipped `connectors:` block already enables `chem`, `safety` and `rxnpredict` as externally hosted
(`url:` set, so this chart renders no pod for them), and `config` points the two backends at
`CHEMCLAW_CALC_SERVER_URL` and `CHEMCLAW_RXNLABEL_SERVER_URL`. For each fleet server you rely on:

1. Deploy it from its own `servers/<name>/deploy/` files **into this release's namespace** — its
   NetworkPolicy admits callers with a same-namespace `podSelector`, so a namespace-qualified
   address resolves and is then dropped. Its Service name is what the URLs in `values.yaml` already
   say (`chemclaw-mcp-<name>`); its own `MCP_ALLOWED_HOSTS` must name that Service's `name:port`.
2. Give the server the **same** bearer variable and value this release sends (the table above):
   both ends read one variable from one Secret. Those deployment files already read it from
   `chemclaw-secrets` under the variable's own name, and not `optional`, so the key must exist
   before the server's pod can start. Rewrite their placeholder image
   (`registry.invalid/chemclaw-mcp-<name>:unset`) to your published digest — `docs/guides/deployment.md` §6.2.
3. Allow the dial: the host in `networkPolicy.egressDestinations` (unless you state
   `allowAnyDestination`), and its port in `networkPolicy.egressPorts` — the fleet's ports are
   already listed there.

The front door's `CHEMCLAW_CONNECTOR_URLS` and `CHEMCLAW_CONNECTORS_ENABLED` are **derived** from
the `connectors:` block; never set them in `config`. A bundle this image does not ship (a
site's own) additionally needs its manifest mounted — see "Attaching a connector bundle
this image does not ship". The five process-development bundles (`props`, `thermalsafety`,
`kinetics`, `unitops`, `suitability`) ship `enabled: false`: each one enabled costs prompt prefix
on every model call.

### 6. Write the values file — what the chart refuses to render without

`helm template`/`install` fails, naming the problem, until these are stated. Verified against the
chart with Helm 3.16:

| Value | What to set | Why it has no default |
| --- | --- | --- |
| `networkPolicy.egressDestinations` **or** `networkPolicy.allowAnyDestination: true` (exactly one) | a list of NetworkPolicyPeer objects covering **everything** a pod dials — this release's own pods (`podSelector: {matchLabels: {app.kubernetes.io/name: chemclaw}}`, for the connector Services), the fleet's pods (`podSelector: {matchLabels: {app.kubernetes.io/part-of: chemclaw3}}`), Postgres, Temporal, the LLM gateway, the OTLP collector, Entra's JWKS host and the git host — or the explicit "any destination" statement. The list replaces `to: []` for every egress port, so a peer left out is a silently dropped dependency | an empty list renders `to: []`, which a NetworkPolicy reads as *every* destination. Must be a YAML boolean, never a string (`--set-string` is refused). Not asked when `networkPolicy.enabled: false` |
| `retention.windows` **or** `retention.unboundedGrowthAccepted: true` (exactly one) | `windows` is a map of `CHEMCLAW_RETENTION_*` day windows, e.g. `{CHEMCLAW_RETENTION_SESSION_MESSAGES_DAYS: "365", CHEMCLAW_RETENTION_CHECKPOINTS_DAYS: "30", CHEMCLAW_RETENTION_TOOL_RESULTS_DAYS: "30"}` | every window defaults to disabled, so the durable tables grow forever unless a policy is stated. Each key must be one of the retention settings the template lists (a typo is refused); `CHEMCLAW_RETENTION_ENABLED` is derived and refused if written |
| on the `windows` arm only: `retention.artifactStore` **or** `retention.artifactGrowthAccepted: true` | `artifactStore: {CHEMCLAW_ARTIFACT_STORE_MAX_BYTES: "53687091200"}` and/or `CHEMCLAW_ARTIFACT_EVICT_IDLE_DAYS` | the calculation artifact store is swept by its own job and no retention window reaches it |
| on the `windows` arm only: `retention.windows.CHEMCLAW_RETENTION_SESSION_EXHIBITS_DAYS` **or** `retention.exhibitsGrowthAccepted: true` | e.g. `"365"`; keep `CHEMCLAW_RETENTION_TOOL_RESULTS_DAYS` at least as long if bound artefact values must stay readable | artefacts are bounded by nothing else, and a session holding one is never forgotten |
| `temporal.namespace` | this release's registered Temporal namespace (step 3) | the broker is cluster-shared; a shared namespace means shared queues and Schedules, and one release's upgrade rewrites and deletes the other's. `config.CHEMCLAW_TEMPORAL_NAMESPACE` is derived from it and refused if written |

And these, which the shipped `values.yaml` already satisfies — they fail only if an override
removes or breaks them:

- `config.CHEMCLAW_SESSION_STORE` must be `"postgres"`.
- At least one `connectors.<name>.enabled: true` (an empty enabled set would mean "every bundle").
- `service.autoscaling.maxReplicas` (or `service.replicas` with the HPA off) at least 1;
  `rollout.maxSurgePods` a whole, non-negative number.
- `config` keys the chart derives probe timeouts and grace periods from:
  `CHEMCLAW_SERVICE_TURN_TIMEOUT_SECONDS`, `CHEMCLAW_WORKER_GRACEFUL_SHUTDOWN_SECONDS`,
  `CHEMCLAW_CONNECTOR_HEALTH_TIMEOUT_SECONDS`, `CHEMCLAW_SERVICE_READINESS_DB_TIMEOUT_SECONDS`,
  `CHEMCLAW_CALC_SERVER_TIMEOUT_SECONDS`, `CHEMCLAW_CALC_ATOMIC_TIMEOUT_SECONDS`,
  `CHEMCLAW_KNOWLEDGE_DIR`, and `CHEMCLAW_SERVICE_MAX_CONCURRENT_TURNS` while
  `service.autoscaling.occupancy.enabled`.
- `connectors.<name>.replicas` (or `serverReplicas`/`workerReplicas`) for every rendered half, and
  `interactive.replicas` + `interactive.maxConcurrentActivities` for every `interactive:` entry.
- `workers.background.documentParseMemoryBytes` a positive whole byte count (`320Mi` is refused).
- Only when switched on: `mcpFace.route.enabled` needs `mcpFace.ingressNamespaces`;
  `monitoring.alertmanager.enabled` needs `monitoring.alerts.enabled`, `receivers`, and a
  `defaultReceiver` (and any `criticalReceiver`) naming one of them.

Then the site facts the chart cannot guess and that do **not** fail the render — get them wrong and
a pod refuses at boot or a capability is silently closed:

| Value | Set to |
| --- | --- |
| `config.CHEMCLAW_ENTRA_TENANT_ID`, `config.CHEMCLAW_ENTRA_AUDIENCE` | your tenant and API audience (the shipped zero GUID fails loudly) |
| `config.CHEMCLAW_ENTRA_PRIVILEGED_ROLES` | the Entra app roles allowed to start expensive jobs — **empty closes them for everyone** (see below) |
| `config.CHEMCLAW_LLM_BASE_URL`, `config.CHEMCLAW_LLM_MODEL`, `config.CHEMCLAW_LLM_CONTEXT_WINDOW_TOKENS` | the gateway, its model, and that model's window (they move together) |
| `config.CHEMCLAW_TEMPORAL_ADDRESS`, `config.CHEMCLAW_OTEL_ENDPOINT` | where your broker and collector are |
| `config.CHEMCLAW_EGRESS_ALLOW` | hosts the in-process egress guard cannot derive from settings: a warehouse ELN's or result sink's database, a delivery channel, an HTTP proxy |
| `knowledge.sync.repoUrl` | the knowledge repository (empty: serve the corpus the image ships) |
| `route.host`, `route.ipWhitelist` | the front door's hostname (empty: OpenShift assigns one) and, optionally, the source CIDRs allowed to use it |
| `networkPolicy.ingressNamespaces`, `networkPolicy.monitoringNamespaces` | the labels of your router and monitoring namespaces (shipped: OpenShift's). The front door admits this release's own pods, these namespaces, and the `Chemclaw3_ui` BFF's pods in this namespace by `networkPolicy.uiPodSelector` (shipped: `app.kubernetes.io/name: chemclaw3-ui`, the label its OpenShift manifests carry; `null` removes it) |
| `image.repository`, `image.digest` | step 1 |

### 7. Install

```console
$ helm upgrade --install chemclaw deploy/helm/chemclaw -n <ns> -f <your-values>.yaml --wait --timeout 15m
```

Run the same command with `--dry-run` first. Do **not** pass `--atomic`: the post-upgrade message
conversion is deliberately not rolled back with the release
(`templates/migrate-job.yaml`). Hook order on every install and upgrade: the `migrate` Job
(DDL, store tables, grants) before any pod changes; then the Deployments; then the `convert` Job
(stored-message conversion) and the `schedules` Job (reconciles this release's Temporal Schedules).
A rolling update of the front door can take up to ten minutes per pod by design — see "Draining a
pod". Upgrading a release first installed from an older chart: see "Upgrading a release installed
before this chart".

### 8. Verify

1. `helm status chemclaw -n <ns>` prints `templates/NOTES.txt`: these checks as commands, the
   Secrets the release reads, a warning for each permissive posture or empty privileged-role set
   it was installed with, and — because it is the most likely way this deployment ships and
   observes nothing — the two cluster-monitoring switches it needs.
2. `helm status` reports `deployed`, and `oc get jobs -n <ns>` lists no `chemclaw-migrate`,
   `chemclaw-convert` or `chemclaw-schedules`: each hook Job is deleted when it succeeds, so one
   still listed failed or is still running (`oc logs job/<name>`; runbook §(xi) for a stuck
   migration).
3. Every pod is Ready: `oc get pods -n <ns> -l app.kubernetes.io/instance=chemclaw`. A pod that
   exits at once is a boot refusal; its log names the setting (see "Settings that decide whether
   the pod boots at all" and "A non-production cluster that runs the enforced posture").
4. The front door answers: `curl -fsS https://<route-host>/healthz` (liveness, does no work) and
   `/readyz`, which is 503 with `"database unreachable"` or `"schema behind image"` when Postgres
   is the problem. Connectors do not gate readiness: `/readyz` stays 200 with
   `connectors_unhealthy` counting the enabled bundles it cannot reach — it must be `0`. Which ones
   is in each pod's WARNING log and in `chemclaw_connectors_unhealthy` on `/metrics`. A deployment
   that would rather not serve at all with a connector down sets `CHEMCLAW_CONNECTORS_REQUIRED=true`
   (startup then fails instead).
5. Sign in through the UI with an account holding one of `CHEMCLAW_ENTRA_PRIVILEGED_ROLES` and run
   one turn that calls a fleet tool and one expensive job.
6. Check the egress guard counted no refusals: `chemclaw_egress_refused_total` flat on every pod.
   A non-zero value names a destination missing from the allowlist (`CHEMCLAW_EGRESS_ALLOW`) or
   from `networkPolicy.egressDestinations`.

## What ships

| Component | `CHEMCLAW_COMPONENT` | Runs | Rendered as |
|---|---|---|---|
| Front door | `service` | `uvicorn chemclaw.api.app:create_app --factory` behind the **Route**; any replica follows or stops a turn another replica is running, through Postgres, so the UI's BFF needs no affinity to reach one (D-2026-10-04-a-running-turn-is-reached-through-postgres-from-any-replica) | Deployment + Service + HPA + PDB `chemclaw-service` |
| Background worker | `background-worker` | `python -m chemclaw.durable.background_worker` on `background-jobs` | Deployment `chemclaw-background-worker` (one replica) |
| Connector server | `connector-<name>` | `python -m chemclaw.connectors.server_entry <name>` — that bundle's MCP tools | Deployment + Service `chemclaw-connector-<name>`, for a bundle with `server: true` and no `url:` |
| Connector worker | `connector-worker-<name>` | `python -m chemclaw.connectors.<name>.worker` — that bundle's durable queue | Deployment, for a bundle with `worker: true` |
| Interactive worker | `interactive-worker-<name>` | `python -m chemclaw.connectors.interactive_worker <name>` — queued heavy tool calls | Deployment, for a bundle with an `interactive:` entry (scaled by KEDA when `keda.enabled`) |
| Read-only MCP face | `mcp-face` | `python -m chemclaw.api.mcp_face` — read-only tools served to another agent | Deployment, only with `mcpFace.enabled` |
| Migrations | `migrate` | migrate, store setup, grants | hook Job `chemclaw-migrate` |
| Message conversion | `convert` | `python -m chemclaw.agent.message_migration` | hook Job `chemclaw-convert` |
| Schedules | `schedules` | `python -m chemclaw.cli.schedules` | hook Job `chemclaw-schedules` |

All of them are the **same image** (`deploy/Containerfile`), rootless (UID 1001, arbitrary-UID safe
for OpenShift SCC), no secret baked in; `deploy/entrypoint.sh` dispatches on `CHEMCLAW_COMPONENT`
and exits 64 on an unknown one. The `<name>` rows are patterns: adding a bundle adds pods, not
entrypoint cases (D-109). `tests/test_deploy_chart.py` checks the chart against the entrypoint in
both directions.

## Config & secrets

- **Non-secret** config is the `values.yaml` `config:` block → the `chemclaw-config` ConfigMap →
  `CHEMCLAW_*` env. Keys mirror the `Settings` fields exactly — there is no second config system
  in-cluster. Some keys are derived by the chart and refused if written by hand
  (`CHEMCLAW_TEMPORAL_NAMESPACE`, `CHEMCLAW_RETENTION_ENABLED`); others are derived and simply
  overwritten by the topology (`CHEMCLAW_CONNECTOR_URLS`, `CHEMCLAW_CONNECTORS_ENABLED`,
  `CHEMCLAW_SERVICE_FLEET_REPLICAS`, the `CHEMCLAW_PG_FLEET_*` counts).
- **Plain secrets are the exceptions, not the model.** Each is a credential for a system that does
  not speak Entra, and the set is `values.yaml`'s `secrets.keys`/`optionalKeys`/`migrationKeys`,
  with the argument for each written beside it and pinned by `tests/test_helm_chart.py`. No
  component mints an outbound Entra token: workload identity federation, OBO and the HPC identity
  bridge were deleted (`D-2026-08-15`), so every credential is at rest as a secret. Two inert
  leftovers remain — an `azure.workload.identity/client-id` annotation on the ServiceAccount and an
  `azure.workload.identity/use: "true"` pod label — and nothing under `src/` reads a projected
  token.
- Populate secrets via `ExternalSecret`/`SealedSecret`; the chart only *names* them.

#### The one Secret that is files, not env

`secrets.temporalTls.secretName` (default **`chemclaw-temporal-tls`**) is mounted at
`secrets.temporalTls.mountPath` (default `/etc/temporal/tls`) and must carry exactly three keys:

| Key | What it is |
|---|---|
| `tls.crt` | the client certificate this component authenticates to the Temporal frontend with |
| `tls.key` | that certificate's private key |
| `ca.crt` | the CA that signs the Temporal frontend, so the client can pin it |

A `kubernetes.io/tls` Secret already uses the first two names; add `ca.crt` beside them. The chart
does not create it.

- `secrets.temporalTls.enabled: true` (default) — env, volume and a **required** mount. A missing
  Secret fails at pod creation: `MountVolume.SetUp failed … secret "chemclaw-temporal-tls" not
  found`, on the pod, before any process starts.
- `enabled: false` — none of the three; the client connects plaintext, or with
  `CHEMCLAW_TEMPORAL_API_KEY` (Temporal Cloud). Plaintext to a non-loopback broker is refused under
  `CHEMCLAW_ENTRA_REQUIRED=true`, so this is for a dev cluster, Temporal Cloud, or a service mesh
  that terminates mTLS itself.

### Settings that decide whether the pod boots at all

- **`CHEMCLAW_ENTRA_REQUIRED=true` is mandatory for any exposed deployment** (the chart default).
  With it off, every request runs as the shared dev principal and all authorization gates are open,
  so the front door **refuses to start** when that mode is bound to a non-loopback interface — it
  exits with `SECURITY: entra_required is False but the service binds a non-loopback interface …`.
  `CHEMCLAW_SERVICE_ALLOW_INSECURE=true` is the deliberate opt-out (boots with a loud warning) and
  belongs in local dev only. The **workers** bind no request surface, so each refuses to start
  with sign-in off on its own (`SECURITY: this Temporal worker would run with
  CHEMCLAW_ENTRA_REQUIRED=false …`), and `CHEMCLAW_WORKER_ALLOW_UNAUTHENTICATED=true` is its
  opt-out, local dev only. Under `entra_required`, `CHEMCLAW_ENTRA_TENANT_ID` and
  `CHEMCLAW_ENTRA_AUDIENCE` must also be set — a half-configured identity setup fails fast at
  startup rather than at the first request.
- **`CHEMCLAW_ENTRA_CLIENT_ID` no longer exists; drop it before upgrading, because nothing will
  tell you if you don't.** pydantic-settings looks up only the names it has fields for, so a
  `CHEMCLAW_*` variable that matches no field is silently ignored — in a ConfigMap as anywhere in
  the environment. (Only a key in a *dotenv file* is rejected.)
- **`CHEMCLAW_SERVICE_FLEET_MAX_CONCURRENT_TURNS` is the ceiling the whole deployment may put on the
  shared LLM endpoint** (D-2026-08-01-a-per-process-cap-multiplied-by-a-number-nobody-wrote-down).
  The admission cap is per-process by design, so the load the endpoint really sees is
  `maxReplicas × uvicorn workers × CHEMCLAW_SERVICE_MAX_CONCURRENT_TURNS`, and the chart renders
  that product into every pod. A configuration whose product exceeds the declared ceiling
  **refuses to start**, in every pod, naming the product and each factor. Raising
  `service.autoscaling.maxReplicas` or the per-process cap therefore means raising this too, with a
  number from the endpoint's throughput budget; the repository's own tests fail on a chart whose
  autoscaling shape outruns its declaration. `0` disables the check (the code default: a CLI or a
  single-pod dev run has no fleet).
- **`postgres.maxConnections`** is checked the same way against the fleet's derived peak pool count
  — see "Observability" below.

### A non-production cluster that runs the enforced posture

A test cluster (kind, a throwaway namespace, the four-repo browser E2E against `Chemclaw3_mock`'s
tenant) that sets `CHEMCLAW_ENTRA_REQUIRED=true` is held to **the same boot refusals as
production**. There is no "test mode" that relaxes them. This lists what such a cluster has to
*state* so that every process boots; every refusal names its setting when it fires.

| Refusal (the process exits at boot) | What a test cluster sets |
|---|---|
| Entra half-configured (`entra_audience` / tenant-or-issuer / tenant-or-JWKS) | `CHEMCLAW_ENTRA_AUDIENCE`, plus either `CHEMCLAW_ENTRA_TENANT_ID` or **both** `CHEMCLAW_ENTRA_ISSUER` and `CHEMCLAW_ENTRA_JWKS_URL`. For a mock tenant, use the issuer string its tokens carry, byte for byte, and its keys URL. |
| The tenant's JWKS is served over https by a private or self-signed CA | `CHEMCLAW_ENTRA_CA_BUNDLE=<path to that CA's PEM>` on the front door — in the chart, `trustedCA.configMap` (or `.secret`) plus `trustedCA.entra: true`. It *replaces* certifi for this one fetch, and verification cannot be switched off. Unset, the fetch fails its TLS handshake and every request is a 503. A path that is missing, or a file with no PEM certificate in it, stops the front door at boot. |
| A non-loopback `CHEMCLAW_TEMPORAL_ADDRESS` with no TLS and no API key | Run the Temporal frontend with TLS and set `CHEMCLAW_TEMPORAL_TLS_CA` (server-auth TLS is enough to pass this guard; add `_CERT`/`_KEY` for mTLS). In the chart, that is `secrets.temporalTls.enabled: true` plus the `chemclaw-temporal-tls` Secret described above. |
| A non-loopback Postgres DSN without `sslmode=require`/`verify-ca`/`verify-full` (checked on `CHEMCLAW_POSTGRES_DSN`, `CHEMCLAW_POSTGRES_MIGRATION_DSN` and `CHEMCLAW_SESSION_STORE_DSN`, each one that is set) | Give the Postgres a server certificate and append `sslmode=verify-full&sslrootcert=<ca>`, or at least `sslmode=require`, to every DSN. A DSN that names no host is refused too, so name the host. |
| `CHEMCLAW_LLM_BASE_URL` on loopback (every process that makes model calls, in every posture) | Point it at the gateway. If the cluster really does serve the model on loopback, as a sidecar or the `chemclaw.cli.mock_llm` mock in the same pod, set `CHEMCLAW_LLM_ALLOW_LOOPBACK_GATEWAY=true` to say so. |
| `CHEMCLAW_HARNESS_AUTONOMY=plan_only` with `CHEMCLAW_HARNESS_ENABLED=false` | The chart already sets the harness on. Without the chart, set `CHEMCLAW_HARNESS_ENABLED=true`. |
| An `HTTP(S)_PROXY`/`ALL_PROXY` in the pod environment that is not declared | Unset it, put the proxy host in `CHEMCLAW_EGRESS_ALLOW`, or set `NO_PROXY=*`. |
| A non-loopback `http://` result sink or delivery channel (refused when the sink or channel is built rather than at import) | Use `https://`, or leave the sink or channel disabled. |

What a test cluster under enforcement does **not** need is `CHEMCLAW_SERVICE_ALLOW_INSECURE` or
`CHEMCLAW_WORKER_ALLOW_UNAUTHENTICATED`. Both are opt-outs for running with sign-in *off*, and they
have no effect once it is on.

### The setting that does *not* block boot, and closes every expensive job

**`CHEMCLAW_ENTRA_PRIVILEGED_ROLES` ships empty, and empty means every expensive job is refused for
everyone.** This is the one identity setting whose misconfiguration a pod cannot refuse to start
over, so it gets its own section beside the ones that can.

`expensive: true` in a connector manifest derives into the trigger gate, and that gate fails closed
on an empty role set rather than open. Under the shipped `CHEMCLAW_ENTRA_REQUIRED=true`, with this
unset, **every action the trigger gate protects is refused for every authenticated user**.

The set is not listed here, because most of it is declared by bundles served out of
`Chemclaw3-mcp` and any list here goes stale on somebody else's merge. What this deployment closes
is a question its own enabled bundles answer:

```console
$ uv run python -c "from chemclaw.agent.authz import expensive_actions; print(chr(10).join(sorted(expensive_actions())))"
```

Three sources feed it: every job an enabled manifest declares `expensive: true`, the durable work
core itself owns (`CORE_EXPENSIVE_ACTIONS` in `src/chemclaw/agent/authz.py`), and anything
`CHEMCLAW_ENTRA_EXPENSIVE_ACTIONS` adds. `tests/test_authz.py` runs that command's own payload and
compares it with the live set.

What does *not* break: the pod boots, both probes pass, reads and knowledge lookups work, and a
chemist asking for a conformer search is told they lack a privileged role. That combination —
healthy deployment, a whole tier of capability silently shut — is why this is documented at the
same volume as a crash-loop.

**The remedy is this setting alone.** Set it to a comma list of the Entra app roles your chemists
hold; `CHEMCLAW_ENTRA_EXPENSIVE_ACTIONS` is *not* needed beside it, because the action set comes from
the manifests. Config validation enforces the pair in one direction only: naming actions with no
role is rejected at startup (nobody could pass that gate), naming roles with no actions is the normal
production configuration.

**Why the chart does not ship a placeholder role name.** A plausible-looking value would be a config
that *looks* configured — it survives review, reaches the cluster, grants nothing, and sends the
operator hunting through Entra group membership instead of through this file. The key is written
out as an explicit `""` instead, so the emptiness appears in `helm show values`, in the rendered
ConfigMap, and in any values diff.

## Attaching a connector bundle this image does not ship

`Chemclaw3-mcp`'s servers need only the first line of what follows: their manifests are in the
image, installed with the `chemclaw-contracts` package, and each is switched on with
`connectors.<name>.enabled: true`. A private bundle a site keeps of its own reaches an OpenShift
release through four declarations — all four in a values file, none of them a chart edit
(`D-2026-09-07-a-seam-that-stops-at-the-chart-is-not-a-seam`). The *absence* of each fails
differently:

| declaration | what it gives | how it fails without it |
| --- | --- | --- |
| `connectors.<name>.{enabled,server,url}` | the address, and the bundle on the agent's surface | no capability; and `enabled` without the manifest is a **crash loop**, see below |
| `extraConnectors.bundles[]` | the manifest directory, as a ConfigMap mounted into every pod | `connectors_enabled names unknown connector(s)`, raised at import in every pod |
| `networkPolicy.egressPorts.<name>` | permission to dial that port | every packet dropped, **silently** — the bundle reports as merely unreachable |
| `secrets.optionalKeys.<name>Token` | the bearer the server enforces | every call refused |

The bearer's *variable* name is whatever that bundle's manifest declares as `auth.token_env`, which
is why `make prose-validate` refuses a concrete one here — a name in operator prose has to resolve
to a manifest something in this checkout can see, and a bundle mounted from elsewhere brings its
own.

Plus a `networkPolicy.egressDestinations` entry for the host, on the same terms as the sibling
servers this release already dials. A worked example, a site's own `our-eln` bundle. The fleet's
own bundles (`pyexec` and the five process-development bundles, `props` among them) already have
their egress port, their token slot and their `connectors:` entry in `values.yaml`:

```yaml
connectors:
  our-eln: {enabled: true, server: true, url: http://our-eln:8899/mcp}
extraConnectors:
  bundles:
    - name: our-eln
      configMap: chemclaw-connector-our-eln   # keys are that bundle's files; `connector.yaml` must be one
networkPolicy:
  egressPorts:
    our-eln: 8899
secrets:
  optionalKeys:
    our-elnToken: <the name its manifest gives auth.token_env>
```

Two things worth knowing before you write that:

- **`enabled` without a mounted manifest is a crash loop, not a missing tool.**
  `CHEMCLAW_CONNECTORS_ENABLED` is derived from the `connectors:` block, and `registry.enabled()`
  refuses a name no bundle provides — deliberately, because the alternative is a capability that
  silently stops working. The two halves go in together.
- **A mounted bundle cannot replace a shipped one.** A connector name declared in two directories
  is a startup error naming both files, so mounting a bundle called `chem` or `pyexec` fails every
  pod at boot. To dial a shipped connector at another server, set its `url:` instead
  (`CHEMCLAW_CONNECTOR_URLS`). `extraConnectors.mountPath` is the first directory of
  `CHEMCLAW_CONNECTORS_DIR`, then `contractsPath` (the fleet's manifests) and `shippedPath` (this
  image's own bundles and the skills that go with the fleet's). Never mount `Chemclaw3-mcp`'s
  internal manifests (`calc`, `rxnlabel`): those servers are addressed by
  `CHEMCLAW_CALC_SERVER_URL` / `CHEMCLAW_RXNLABEL_SERVER_URL`, and mounting them is refused at
  startup.

**Upgrading a release that mounted a fleet bundle.** Until the fleet's manifests shipped in the image,
`pyexec` had to be mounted through `extraConnectors.bundles`. That mount is now a collision, and
`helm template`/`helm upgrade` fails on it, naming the bundle (`extraConnectors.imageBundles` lists
every name the image declares). Before upgrading: delete the `pyexec` entry from
`extraConnectors.bundles` (and its ConfigMap), keep `connectors.pyexec` (`enabled`, `url`), and
leave `networkPolicy.egressPorts.pyexec` and `secrets.optionalKeys.pyexecToken` as they are.

The addresses above are the Services `Chemclaw3-mcp` creates in **this** namespace: its servers'
NetworkPolicies admit their caller with a bare `podSelector`, which is same-namespace only, so a
namespace-qualified address resolves and is then dropped on the far side.
`tests/test_helm_chart.py::test_every_fleet_address_names_a_service_the_sibling_actually_creates`
holds every fleet address in `values.yaml` against those manifests, and skips — naming each address
it did not check — where no sibling checkout is present.

### A result sink this image does not ship

`extraSinks.sinks[]` is the same seam for `publish/`: each entry is a ConfigMap holding one sink
folder (`sink.yaml` among its keys), mounted read-only at `extraSinks.mountPath/<name>` on every
pod and prepended to `CHEMCLAW_RESULT_SINKS_DIR`, so a folder named `postgres` replaces the shipped
`postgres` sink's address without a derived image. Mounting does not enable it —
`config.CHEMCLAW_RESULT_SINKS` does — and the destination still needs its credentials in
`secrets.optionalKeys` and its host in `CHEMCLAW_EGRESS_ALLOW` and `networkPolicy.egressDestinations`
([runbook § (xvi)](../docs/guides/runbook.md#xvi-attach-an-external-results-database)).

## Stateful dependencies (ADR D-049, sub-decision D-A6a)

- **Temporal: self-hosted in-cluster** (not Temporal Cloud), one broker per cluster with one
  namespace per release. Rationale: keeps the durable core inside the same cluster and trust
  boundary, and avoids egress of workflow payloads (which carry the Entra `oid`, D-044) to a third
  party. Temporal Cloud stays a values change away (`CHEMCLAW_TEMPORAL_API_KEY` in the Secret and
  `secrets.temporalTls.enabled: false`) if that trade changes.
- **Postgres/pgvector**: an operator-run or managed instance, over TLS, with the existing
  `pg_statement_timeout_seconds`. The chart does not deploy it. Migrations run as a **pre-deploy
  Helm hook** Job (`templates/migrate-job.yaml` → the `migrate` component: `chemclaw.core.migrate`,
  the store setup and `chemclaw.core.grants`, i.e. `make db-migrate` + `make db-grants`, D-034)
  that completes before any app container starts — no container ever races the DDL. The migrator
  takes a transaction advisory lock (so two overlapping deploys serialize) and a `lock_timeout` (so
  an `ALTER TABLE` that cannot get `ACCESS EXCLUSIVE` fails in seconds instead of queueing in front
  of every later query on that table). The Job has an `activeDeadlineSeconds` (`migrateJob`),
  because Helm waits for a hook and a retrying Job would otherwise hold the release in
  `pending-upgrade`. Recovery: `docs/guides/runbook.md` §(xi).

## PgBouncer in front of Postgres (the default above three replicas)

Every pooled process opens up to `CHEMCLAW_PG_POOL_MAX_SIZE` connections per pool, and
`postgres.maxConnections` has to cover the sum over the whole fleet at a rollout's peak
(`ChemclawFleetNearItsConnectionCeiling` warns at 80% of it, `ChemclawFleetAboveItsConnectionCeiling`
at 100%). That sum is a *declared* ceiling, and a process holds far fewer than it declares, as
measured on one process of each role against a scratch database (`pg_stat_activity`, 16-wide pools):

| Role | Pools | Declared | Backends at rest | Peak measured |
| --- | --- | --- | --- | --- |
| Front door | 3: the stores' (2 to 16), `/readyz`'s own (1), the checkpointer's (0 to 16) | 33 | 5 | 18 under 32 concurrent queries |
| Background or connector worker | 1 (opened on first use) | 16 | 0 before the first query, 3 after | 8 at 8 concurrent activities |

Above three replicas, put PgBouncer between the pods and the server so the database sees the
pooler's server-side connections rather than the declared sum. Below that, direct connections are
simpler and the 80% alert is the signal to move.

**Two aliases for one database, because not every connection survives transaction pooling.**

| Setting | Points at | Pool mode |
| --- | --- | --- |
| `CHEMCLAW_POSTGRES_DSN` | the pooler, alias `chemclaw` | `transaction` |
| `CHEMCLAW_SESSION_STORE_DSN` | the pooler, alias `chemclaw-session`, or the server itself | `session` (or direct) |
| `CHEMCLAW_POSTGRES_MIGRATION_DSN` | the server itself | direct |

The same `host:port` for both aliases is one server to the chart's accounting; a different port (the
server directly) is a split session store, and `postgres.sessionStoreMaxConnections` then bounds it.
Declare `postgres.maxConnections` as the pooler's `max_client_conn` for the transaction alias, since
that is what the fleet's pools are checked against, and size `default_pool_size` and the session
alias so that all server-side connections plus Temporal's and any hand-run `psql` fit the server's
`max_connections`.

**Connections that must bypass a transaction pooler** (they hold session state across statements, or
need a statement a pooled transaction cannot run), and where each dials:

| Connection | Why | Dials |
| --- | --- | --- |
| LangGraph checkpointer pool (`agent/checkpointer.py`) | autocommit, `setup()` runs `CREATE INDEX CONCURRENTLY` | `session_store_dsn` |
| Checkpoint-table setup lock (`_setup_once`) | session-level `pg_try_advisory_lock` held across `setup()` | `session_store_dsn` |
| Git note submission lock (`kg/git_writer.py`) | session-level `pg_advisory_lock` held across fetch, commit and push | `session_store_dsn` |
| Single-instance job locks (`core/job_lock.py`) | session-level `pg_try_advisory_lock` held for the pass | `session_store_dsn` |
| Migrations and grants (`postgres_migration_dsn`) | DDL, `CREATE INDEX CONCURRENTLY`, no statement timeout | the server |
| Session-layer tables (`session_*`, turn claims, pending requests) | resolved through `session_store_dsn` already; coordination built on locks or `LISTEN/NOTIFY` belongs here | `session_store_dsn` |

A transaction pooler hands a session-level lock to whichever server connection ran the statement and
the next transaction may land on another, so the lock is leaked or never held; a `LISTEN` is lost the
same way. Nothing in `src/` uses `LISTEN/NOTIFY` today. Code that adds a session-level advisory lock,
`LISTEN`, a plain `SET`, or a held cursor must dial `settings.session_store_dsn or
settings.postgres_dsn` on a dedicated connection, as `core/job_lock.py` does. These are safe in
transaction mode: `pg_advisory_xact_lock` (the session queue, attachments, exhibits, skill store,
behaviour proposals, migrations), `FOR UPDATE SKIP LOCKED` inside one transaction, and
`set_config(..., true)` (the pgvector recall settings).

**Also check, through the pooler, before relying on it.**

- Statement bounds. Pools connect with libpq `options` carrying `statement_timeout` and
  `plan_cache_mode=force_custom_plan`. PgBouncer either refuses that startup parameter or, with
  `ignore_startup_parameters = options`, drops it without saying so, and then no statement is
  bounded. Set the same values on the role (`ALTER ROLE chemclaw SET statement_timeout =
  '<pg_statement_timeout_seconds>s'`, and `plan_cache_mode`) and confirm `SHOW statement_timeout`
  through each alias.
- Prepared statements. psycopg prepares a statement after five uses. Transaction pooling needs a
  PgBouncer that tracks prepared statements (1.21 or later, with `max_prepared_statements` above
  zero), or those pools fail with "prepared statement does not exist".
- `server_idle_timeout`, `idle_transaction_timeout` and `query_wait_timeout` stay above
  `pg_pool_timeout_seconds` and `pg_statement_timeout_seconds`, or the pooler cuts the connection
  a pod believes it still holds.

## Where the knowledge graph lives in a pod

One directory, not two. `Settings.knowledge_path` is `note_repo_dir / knowledge_dir` and every
reader goes through that property, so the chart publishes the synced graph to exactly that path
(`chemclaw.knowledgePublishPath` = `knowledge.noteRepoPath` + `CHEMCLAW_KNOWLEDGE_DIR`) and the
note writer commits into that same clone. Two containers therefore write one tree, and the sync
takes the submitter's own advisory lock — the `flock` under the checkout's git directory that
`src/chemclaw/kg/git_writer.py` uses — for the duration of each publish. A held lock means a
submission is in flight, and the publish waits for the next tick.

With `knowledge.sync.repoUrl` empty the sync seeds the published tree from the corpus the image
ships at `/app/knowledge`. `knowledge.sync.checkoutPath` is the shallow replica the publish copies
*from*, so a failed fetch never leaves the directory the app reads half-written. A remote git host
must be reachable through `networkPolicy.egressDestinations` (HTTPS).

## Network & probes

Object names: the chart's name helper is the constant `chemclaw`, so every object is
`chemclaw-<role>` (`chemclaw-service`, `chemclaw-connector-calc`, the Route `chemclaw`) whatever
the Helm release is called. Pods carry `app.kubernetes.io/instance=<release>`, so two releases
must live in different namespaces.

- **NetworkPolicy** (`templates/networkpolicy.yaml`): egress is allowed to DNS (53) and to the
  ports in `networkPolicy.egressPorts` — Postgres 5432, Temporal 7233, HTTPS 443 (Entra, git), the
  LLM gateway 8000, OTLP 4317, each fleet server's own port — plus `connectorPort` for this
  release's own connector Services, to the **destinations you name**. Ingress rules bound which
  *peers* may open a connection to the front door (`networkPolicy.ingressNamespaces`, plus the
  `Chemclaw3_ui` BFF's pods in this namespace by `networkPolicy.uiPodSelector`), the connectors and
  the workers' probe port (`networkPolicy.monitoringNamespaces`).
- **The destinations are yours to state, and the chart will not render until you do.** An empty
  `networkPolicy.egressDestinations` would render `to: []`, which in a NetworkPolicy means *any*
  destination on those ports. So the chart requires exactly one of `egressDestinations` (a list of
  NetworkPolicyPeer objects) or `networkPolicy.allowAnyDestination: true` — the deliberate,
  greppable statement that any destination is what you want
  (`D-2026-08-26-a-knob-that-renders-nothing-is-not-a-knob`). `make helm-validate` passes the
  latter on every render.
- **The retention posture must be stated the same way.** Every `CHEMCLAW_RETENTION_*` window
  defaults to disabled — a disposal policy is a deployment's decision, not a code default — and
  silence meant durable tables that grow for the release's lifetime (the LangGraph checkpoint
  tables re-serialize a session's whole thread roughly once per superstep). The chart refuses to
  render unless exactly one of `retention.windows` (rendered into the ConfigMap with
  `CHEMCLAW_RETENTION_ENABLED` derived) or `retention.unboundedGrowthAccepted: true` is stated.
  **A stated window must name a setting**: pydantic-settings ignores an unknown prefixed variable,
  so a key that is not one of the `CHEMCLAW_RETENTION_*` fields the template lists refuses to
  render, naming the set.
- **Stating windows asks two more halves.** `artifact_blobs` — a calculation's Hessians, geometries
  and conformer ensembles — is swept by its own job under `CHEMCLAW_ARTIFACT_STORE_MAX_BYTES` /
  `CHEMCLAW_ARTIFACT_EVICT_IDLE_DAYS`, both 0 (off) by default, and no window reaches it; and
  artefacts are bounded only by `CHEMCLAW_RETENTION_SESSION_EXHIBITS_DAYS`. So on the
  `retention.windows` arm the chart also requires exactly one of `retention.artifactStore` or
  `retention.artifactGrowthAccepted: true`, and exactly one of
  `retention.windows.CHEMCLAW_RETENTION_SESSION_EXHIBITS_DAYS` or
  `retention.exhibitsGrowthAccepted: true`. `unboundedGrowthAccepted` already says everything
  grows, so it asks neither — which is why the shipped defaults render with the three `--set`s
  below and no more.
- **Which Temporal namespace this release owns must be stated, and there is no default.**
  `CHEMCLAW_TEMPORAL_ADDRESS` names one broker for the whole cluster. Inside it the Temporal
  namespace is the only boundary: the background task queue is the constant `background-jobs`,
  every Schedule id `durable/schedules.py` owns is a bare constant, and a job's workflow id carries
  no site. Measured against a live broker, a second release's `helm upgrade` rewrote the first's
  `eln-sync` Schedule and deleted its `eval-drift` Schedule outright; beyond Schedules, workers on
  one queue take each other's tasks and a deduplicated job resolves to a peer's completed
  execution. So `temporal.namespace` has no default — a default is exactly what two releases would
  share — and a real release names one per environment and registers it on the broker. **A shared
  Postgres is the same hazard and this knob does not cover it**: two releases need their own
  database as well. Rendering the shipped defaults therefore takes:

  ```console
  $ helm template chemclaw deploy/helm/chemclaw \
      --set networkPolicy.allowAnyDestination=true \
      --set retention.unboundedGrowthAccepted=true \
      --set temporal.namespace=chemclaw
  ```
- **`/metrics` is on the public host, and the NetworkPolicy is not what bounds it.** The Route
  declares no `spec.path`, and neither a Route nor a NetworkPolicy filters by path. What makes an
  unauthenticated `/metrics` acceptable is the exposition: counts, capacity and an operator-chosen
  `profile` label, never a session id, an actor or turn content, enforced by D-152's
  declared-label allowlist. The residual exposure is operational reconnaissance; `route.ipWhitelist`
  restricts the whole Route to a set of source CIDRs for a deployment that will not accept it.
- **Probes**: the front door serves `/readyz` (readiness) and `/healthz` (liveness) on its service
  port; every Temporal worker (core's, each bundle's, each interactive worker) serves both on the
  `metrics` port (`workerMetricsPort`, `CHEMCLAW_WORKER_METRICS_PORT`, default 9000). The
  connector servers and the MCP face serve `/healthz` for startup and readiness and `/livez` for
  liveness (and `/metrics`); `/livez` consults nothing, so a readiness check can take the pod out
  of its Service without ever becoming a reason to kill it. A worker's readiness is its own `is_running`, and its liveness is answered on its
  event loop, so a loop wedged inside an activity restarts the pod
  (D-2026-08-01-every-process-carries-its-own-witness). Every process
  also has a `startupProbe` (`probes.*.startup`, 30 × 10 s) so cold imports are not killed. The
  front door's readiness timeout is derived from `CHEMCLAW_CONNECTOR_HEALTH_TIMEOUT_SECONDS` +
  `CHEMCLAW_SERVICE_READINESS_DB_TIMEOUT_SECONDS` + `probes.service.readiness.marginSeconds`.
- **The HPA scales the front door on admission occupancy, with CPU beside it as a fallback**;
  workers do not autoscale (interactive workers can, through KEDA). CPU is the wrong quantity here
  and that was measured: a turn is 8.32 s of wall clock and 0.581 s of CPU — 93% of it waiting on
  the model — and the load lane shed 33 of 48 offered turns at 35% of one core. So
  `service.autoscaling.occupancy` adds a `Pods` metric on `chemclaw_turns_in_flight`, targeted at a
  percentage of the permit count the pods enforce (the chart multiplies them out, so the two cannot
  drift). **This needs something the chart does not install**: a custom-metrics API —
  prometheus-adapter, KEDA, or the cluster's own — publishing that series for these pods. Without
  one the HPA shows `FailedGetPodsMetric` in `kubectl describe hpa` and falls back to the CPU
  metric (an unfetchable metric blocks scale-*down* only, never a scale-up driven by one that
  reads). `service.autoscaling.occupancy.enabled: false` renders the CPU-only HPA.
- **Request bounds** (D-2026-08-01-a-cheap-request-is-still-a-request): every HTTP surface — the
  front door, the MCP face, every `connector-*` pod and the worker probe server — is launched with
  a concurrency limit, a keep-alive timeout and a header-size ceiling from `CHEMCLAW_SERVICE_*`
  settings; `chemclaw.core.asgi.transport_bounds` is the one place they are decided, and
  `tests/test_transport_bounds.py` fails a launcher added without them. An ASGI middleware
  (`chemclaw.core.asgi.BodySizeLimit`) refuses a body over `CHEMCLAW_SERVICE_MAX_REQUEST_BYTES`
  with 413 before it is read; a per-principal token bucket refuses with 429 (on in the chart, off
  in code). Every connector server installs the same middleware over its own, smaller
  `CHEMCLAW_CONNECTOR_MAX_REQUEST_BYTES`. Tuning and symptoms: `docs/guides/runbook.md` §(xii).

## Draining a pod (D-2026-08-01-a-drain-is-not-a-kill-with-extra-steps)

Every grace period is **derived** from the budget it has to outlast, so tuning one moves the other:

| Pod | `terminationGracePeriodSeconds` | Derived from (shipped) |
| --- | --- | --- |
| front door, MCP face | turn timeout + `service.drainSeconds` | `CHEMCLAW_SERVICE_TURN_TIMEOUT_SECONDS` (600) + 15 |
| connector server | max(calc client timeouts) + `connectorDrainSeconds` | `CHEMCLAW_CALC_SERVER_TIMEOUT_SECONDS` (900) / `CHEMCLAW_CALC_ATOMIC_TIMEOUT_SECONDS` (3600) + 10 |
| every worker | drain budget + 30 s | `CHEMCLAW_WORKER_GRACEFUL_SHUTDOWN_SECONDS` (120) + 30 |

**What this costs.** A rolling update of the front door can take up to 615 s per pod, because a
turn may run that long and the state that would make it resumable lives in the pod's memory by
design (D-121). Deploying faster means ending conversations; that is the trade, taken deliberately.
A `preStop` sleep of `service.drainSeconds` runs first, so the router stops choosing the pod before
the pod stops accepting. A grace period is a ceiling, not a wait: an idle pod exits promptly.

**Workers get the shorter budget on purpose.** An activity that does not finish in 120 s is
cancelled and re-run by Temporal; holding a node drain open for ten minutes to avoid using that is
the wrong trade. The front door is the opposite case — there is no retry for a chemist's turn.

A `policy/v1` PodDisruptionBudget covers the front door (`maxUnavailable: 1`, its own toggle
`service.disruptionBudget.enabled` in case a PDB ever wedges a cluster upgrade) and the background
worker (`minAvailable: 1` of its two replicas, `workers.background.disruptionBudget`). The worker's
is rendered only from two replicas: over a singleton `minAvailable: 1` would make the pod
un-evictable and block every drain in the cluster. It covers drains, not a chart rollout, which is
`Recreate` for the worker. Connector workers and servers have none.

## Upgrading a release installed before this chart

`chemclaw-config` and the runtime ServiceAccount used to be `pre-install,pre-upgrade` **hooks**.
Helm does not record hook resources in the release manifest, so `helm rollback` restored the pods
and left the previous release's configuration live and `helm uninstall` left both behind. They are
ordinary tracked resources now. That crossing costs two things, both one-time and both measured
against a real API server (k3s v1.29.9):

- **`helm upgrade` from the previous chart refuses**, because the live objects were created by a
  hook and so carry no `meta.helm.sh/release-name`/`-namespace`: *"exists and cannot be imported
  into the current release"*. It is a prepare-time refusal — nothing is half-applied, and
  `--dry-run` refuses identically. `deploy/jenkins/targets/openshift.sh` adopts them itself
  (reporting without acting under its default `DRY_RUN=true`); for a hand-run upgrade the two
  commands are in `docs/guides/runbook.md` § (xi), keyed on `meta.helm.sh/release-name`.
- **`helm rollback` to a pre-change revision would delete both**, while restoring Deployments that
  name `chemclaw-config` in a non-optional `envFrom` and run as ServiceAccount `chemclaw` — and
  Helm reports success. Both objects therefore carry `helm.sh/resource-policy: keep`; with it, the
  same rollback leaves them standing. The trade is stated where it is made
  (`templates/config.yaml`): **`helm uninstall` leaves those two objects behind**. `keep` skips
  deletion only, so a rollback inside this chart's lineage still restores the previous revision's
  ConfigMap contents.

Neither is permanent. When no release's retained history (`helm history`) still reaches a revision
installed before this chart, the annotation is a line to delete and the adoption step has nothing
left to adopt.

## Before a deploy that touches workflow code

Temporal replays workflow **code** against recorded **history**, so a control-flow change deployed
while a run is in flight fails that run with a nondeterminism error. Every release that touches a
`@workflow.defn` body (or a helper called from one) goes through the checklist in
[`docs/guides/workflow-versioning.md`](../docs/guides/workflow-versioning.md): gate the change with
`workflow.patched()`, or pause the Schedules and drain in-flight runs as an explicit deploy step.
Renaming a workflow or activity type is never safe in place — it is a different command in history.

No live cluster holds Chemclaw histories yet, so the changes made so far need no retroactive gates;
this becomes binding at the first production deploy.

## Observability

`CHEMCLAW_OTEL_ENABLED=true` + `CHEMCLAW_OTEL_ENDPOINT` wire OTLP to the in-cluster collector
(`src/chemclaw/core/logging.py` bridges the one config value to `OTEL_EXPORTER_OTLP_ENDPOINT`);
the chart sets both. The collector's host must be in `networkPolicy.egressDestinations`, or every
export is dropped silently.

**Spans.** A `chemclaw.turn` span wraps a turn and a `chemclaw.tool` span wraps each tool call, so
"the question took 40 seconds and 31 of them were one xTB call" is answerable
(D-2026-08-01-a-turn-you-can-follow-across-a-process). Connector calls carry W3C `traceparent`
alongside the `X-Chemclaw-Correlation-Id` header, and the connector adopts it, so a calculation's
spans appear *inside* the turn that asked for it. The two are not redundant: the correlation id is
what `audit_events` is keyed on and works with no collector at all; `traceparent` is what makes a
distributed trace a tree. Not spanned: a durable job end to end (it crosses two processes and a
Temporal boundary), and there is no FastAPI/httpx/Temporal auto-instrumentation.

`configure_telemetry` builds the `TracerProvider`, the `BatchSpanProcessor` and the OTLP span
exporter itself. **The chart sets `OTEL_SERVICE_NAME` per Deployment** —
`chemclaw-service`, `chemclaw-background-worker`, `chemclaw-mcp-face`,
`chemclaw-connector-<name>`, `chemclaw-connector-worker-<name>`,
`chemclaw-interactive-worker-<name>` — with `service.version=<revision>` and
`k8s.pod.name`/`k8s.namespace.name` from the downward API through `OTEL_RESOURCE_ATTRIBUTES`.
`CHEMCLAW_OTEL_LLM_SPANS: "true"` (on in the chart) adds one span per model call carrying its token
counts, model name and provider, through OpenInference's LangChain instrumentation, with content
suppressed unless `CHEMCLAW_OTEL_INCLUDE_SENSITIVE_DATA` says otherwise (`docs/guides/runbook.md`
§ OpenTelemetry). Point the collector at Arize Phoenix to read those conventions natively — any
OTLP backend receives them, and the instrumentation in this image is Apache-2.0 where Phoenix's
server is ELv2. Metrics and logs are deliberately not exported over OTLP — `/metrics` is scraped
per pod and logs are JSON on **stderr**.

**Metrics come from every process.** `templates/servicemonitor.yaml` collects the Services (the
front door and each connector's MCP server, by their `http` port name); `templates/podmonitor.yaml`
collects the pods that have none — core's background worker, each bundle's worker and the
interactive workers — by the `metrics` port they declare. `monitoring.additionalLabels` is for a
**self-managed Prometheus Operator** whose `Prometheus` resource selects monitors by label;
OpenShift user-workload monitoring selects every ServiceMonitor and PodMonitor in every user
namespace with no selector, so the empty default is correct there.

**What does decide it on OpenShift is off by default and this chart cannot set it**: user-workload
monitoring itself (`openshift-monitoring/cluster-monitoring-config`, `enableUserWorkload: true`).
Without it every monitor and rule here is an inert custom resource that lists, scrapes nothing and
alerts nothing, with no error anywhere. `templates/NOTES.txt` prints the procedure at install and
`docs/guides/runbook.md` § "(x-b) Make the monitoring stack actually collect this" is the long form
— including the second switch, `enableUserAlertmanagerConfig`, without which the alerts fire into
the platform Alertmanager and are dropped.

**Logs are JSON in-cluster** (`CHEMCLAW_LOG_JSON`, on in the chart) and every line carries
`correlation_id`, `actor` and `session_id` from the turn's ContextVars — so an ordinary WARNING
joins to the audit row that recorded the same call and, through the same correlation id, to the
trace that spans it. A filter also replaces any configured secret's value with `***` before a
record reaches a stream, including one passed as a `%s` argument
(D-2026-08-01-a-log-line-that-joins-and-a-secret-that-does-not).

**What is deliberately not redacted:** the audit trail's tool-call arguments. `SECURITY.md` states
that they are user free text, may contain PII, and are recorded *intentionally* — the trail exists
to be an attributable "who did what to which inputs" record. A deployment's retention, access
control and PII policy must cover the trail.

**Spend is recorded twice, on purpose.** `chemclaw_tokens_total{profile}` and
`chemclaw_job_runtime_seconds_total{connector}` are the fleet-wide rates; `turn_costs` and
`job_records.runtime_seconds` are the per-actor, per-run ledger that answers "what did this team
cost last quarter" (D-2026-08-01-spend-is-a-ledger-not-a-label). An Entra `oid` cannot be a metric
label (label values are attacker-influenced and the registry caps series), so attribution needs a
database. Neither records **money**: the ledger holds tokens and seconds and leaves the rate card
to whoever knows it. Both are written only under `CHEMCLAW_SESSION_STORE=postgres`.

**Fleet-budget alerts exist because config validation can only see the shape it was handed.** Each
fleet budget is checked at startup, but a `kubectl scale`, an HPA edited in the cluster, or a
rollout leaving both generations up all push the live fleet past its ceiling while every pod's own
configuration stays valid. Each alert compares a *live* left-hand side against the declared ceiling
and is self-disabling when none is declared: `ChemclawFleetAboveItsTurnCeiling`
(`sum(chemclaw_turn_capacity)` against `chemclaw_fleet_turn_ceiling`),
`ChemclawFleetAboveItsConnectionCeiling` (`sum(chemclaw_pg_pool_max_size)` against
`chemclaw_pg_fleet_max_connections`, **each server separately** — see below), and
`ChemclawCalcBackendOverCommitted` (`sum(chemclaw_calc_requests_in_flight)` against
`chemclaw_calc_backend_max_concurrent_requests`).

**The connection budget is charged in pools, not pods.** `pg_pool_max_size` bounds one *pool*:
`core/db` keys a pool on `(dsn, libpq options, requested max_size)`. A front-door process holds the
stores' pool, the `/readyz` probe's (its own statement timeout is a distinct key, so readiness does
not answer 503 while the stores' pool is merely busy; it asks for a single connection) and the
LangGraph checkpointer's autocommit pool; every worker, connector server and the MCP face holds one.
`chemclaw.fleetPools` derives the count from the topology, and `Settings.fleet_connections_per_server`
sums each pool's real width on the server it will be opened against. **A release provisioning to
an older, smaller `postgres.maxConnections` has to raise its Postgres `max_connections`, or lower
`CHEMCLAW_PG_POOL_MAX_SIZE` until the fleet fits.** The startup check names both sides in every pod.

**A split session store is a second server with its own ceiling.** `CHEMCLAW_SESSION_STORE_DSN`
moves the front door's `/readyz` and checkpointer pools to a second database and adds one pool to
every pooled process; the chart cannot see that DSN (it lives in a Secret). Declare that server's
ceiling in `postgres.sessionStoreMaxConnections`: undeclared while the split is real warns at
startup; declared with no split is refused (the alert's second branch would compare an always-zero
gauge against it). The alert checks each server separately rather than one sum, because a sum
across two servers goes silent while one of them is over.

**A result sink's connection is counted only when its warehouse is `postgres_dsn`'s own server**
(`D-2026-09-13-a-connection-counted-where-the-budget-applies`). `publish/drivers/postgres.py` holds
one un-pooled connection per enabled sink for the life of each drain pass; on `postgres_dsn` it
counts as one backend in the worker that holds it, and a sink on a warehouse of its own contributes
nothing here (`D-2026-08-25-a-cache-is-not-a-record`): size that warehouse yourself — one
connection per worker replica that runs the drain, per enabled sink, held for the pass.

The calc-backend alert reads *held sessions* rather than a configured capacity, because two kinds of
process dispatch to the calculation backend and they do not share a cap — a `calc` worker is
bounded by `CHEMCLAW_WORKER_MAX_CONCURRENT_ACTIVITIES`, while that bundle's own MCP server pods
dispatch straight from a tool call (`D-2026-08-27-a-per-worker-cap-is-not-a-backend-ceiling`). Its
ceiling, `CHEMCLAW_CALC_BACKEND_MAX_CONCURRENT_REQUESTS`, ships as `0` (undeclared) because it
describes a pod in another release; set it to what that server admits.

**Eight alert groups, and five dashboards.** The number of *alerts* is not written here;
`templates/prometheusrule.yaml` is the roster and `tests/test_deploy_chart.py` renders it. The rule
file covers records, correctness, availability, cost, fleet liveness (`up`/`absent()`, the only
rules that fire for a process that is *gone*), turn latency, the durable tier and the
silent-degradation counters; `templates/alertmanagerconfig.yaml` is the route, gated on
`monitoring.alertmanager.enabled` and refusing to render without receivers; and every rule carries
a `runbook_url` into `docs/guides/runbook.md` § "(x-c) When an alert fires" —
`tests/test_deploy_chart.py::test_every_alert_carries_a_runbook_url_that_resolves` resolves each to
a real heading. `templates/configmap-dashboards.yaml` carries the dashboards, so no declared metric
is left with no reader (`tests/test_deploy_chart.py::test_every_declared_metric_has_a_consumer`).
By default they land in the release namespace; `monitoring.dashboards.namespace` /
`monitoring.dashboards.labels` place them where the OpenShift console or a Grafana sidecar reads
them (NOTES.txt prints which).

`make helm-validate` runs `promtool check rules` over the rendered rules as well as `kubeconform`
over the manifests, because `kubeconform` validates that an `expr` is a *string*, not that it is
PromQL — and Prometheus rejects a whole rule group, silently, for one bad expression.

## CI/CD

Every workflow sits at the **repository root** (`.github/workflows/`).

- `ci.yml` — `make lint type cov` against a real Postgres, every validator the `ci` target names,
  and a `chart` job that renders the chart against the Kubernetes schemas and the rules against
  `promtool` (`make helm-validate`), plus `make kind-validate`. The validator count is deliberately
  not written here; `tests/test_repo_map.py` derives the real list from the `ci` target.
- `image.yml` — on pull requests and `main`, **builds** the image and smoke-imports every component
  the entrypoint dispatches as a non-root UID, then asserts an unknown component exits 64. The
  component list is derived from the bundles present; chart↔entrypoint agreement is checked
  offline in `tests/test_deploy_chart.py`.

**Delivery is Jenkins**, not GitHub Actions: `Jenkinsfile` builds, publishes by digest, renders and
applies this repository's chart, and `deploy/jenkins/Jenkinsfile.release` rolls the four
repositories out together from one release descriptor — see
[`deploy/jenkins/README.md`](jenkins/README.md). Those pipelines are written and **not yet run
against a real cluster**; that is the `docs/planning/BACKLOG.md` row "Push-to-registry +
`helm upgrade` rollout, run". Migrations run as the pre-deploy Job (`templates/migrate-job.yaml`),
never inside an app container.

> **Verified how, and where.** Pure-YAML parse, template brace-balance and `Settings` key mapping
> run anywhere. `helm template`, `kubeconform` and `promtool` run wherever they are on PATH — CI's
> runners have Helm, and the `shutil.which("helm")`-gated chart assertions run there; locally they
> skip silently without it, and installing Helm is one download (runbook § "`make helm-validate`
> says a binary is \"not installed\""). The chart tests render the variant set derived from
> `values.yaml`, not only the defaults, so a switch nobody flips is still rendered. A live cluster
> remains out of reach in CI, and `helm rollback` against a real API server is the one thing a
> render cannot stand in for.
