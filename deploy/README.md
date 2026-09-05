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
| Read-only MCP face | `mcp-face` | `python -m chemclaw.api.mcp_face` — this system's read-only tools, served to another agent |

All five are the **same image** (`deploy/Containerfile`), rootless (UID 1001, arbitrary-UID safe for
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
  Keys mirror `src/chemclaw/core/config/`'s `Settings` **exactly** — there is no second config
  system in-cluster.
- **Plain secrets are the exceptions, not the model.** Each is a credential for a system that does
  not speak Entra, and the set is `values.yaml`'s `secrets.keys` — declared there with the argument
  for each one written beside it, and pinned by `tests/test_helm_chart.py`. The Temporal mTLS certs
  are the one that is not env: they mount as files. **Workload Identity Federation is not one of the
  controls here**, and this paragraph used to say it was: F4-T2 built the token exchange, nothing
  ever called it, and `D-2026-08-15` deleted it along with OBO and the HPC identity bridge. What
  survives is inert scaffolding — an `azure.workload.identity/client-id` annotation on the
  ServiceAccount (`values.yaml`) and an `azure.workload.identity/use: "true"` label on every pod
  template — kept because a site that wires the Azure webhook up itself is one values file away from
  a federation path, and removed the day nobody wants that. Nothing under `src/` reads
  `AZURE_FEDERATED_TOKEN_FILE` or exchanges a projected token, so every credential in
  `secrets.keys` is at rest as a secret today.
  (This section said "only three" from F6-T6, then "five", while the real number reached six —
  which is the whole reason the count now lives in the chart and the test rather than in this
  sentence. It had already said so, in the sentence after the one that restated it.)
- Populate them via `ExternalSecret`/`SealedSecret`; the chart only *names* them.

#### The one Secret that is files, not env

`secrets.temporalTls.secretName` (default **`chemclaw-temporal-tls`**) is mounted at
`secrets.temporalTls.mountPath` (default `/etc/temporal/tls`) and must carry exactly three keys:

| Key | What it is |
|---|---|
| `tls.crt` | the client certificate this component authenticates to the Temporal frontend with |
| `tls.key` | that certificate's private key |
| `ca.crt` | the CA that signs the Temporal frontend, so the client can pin it |

A `kubernetes.io/tls` Secret already uses the first two names; add `ca.crt` beside them. The chart
does not create it — like every secret here, it only names it.

**`secrets.temporalTls.enabled` decides whether it is needed at all**, and this used to have no
answer. `chemclaw.env` exported the three PEM *paths* unconditionally while the volume was mounted
`optional: true`, and `core/temporal_client._tls_config()` short-circuits only when all three
settings are empty — so a cluster without that Secret got `FileNotFoundError:
/etc/temporal/tls/tls.crt`: the post-install Schedules hook failed with a message naming neither
Temporal nor a Secret, the workers crash-looped, and the front door passed both probes because
`/readyz` never touches Temporal. No value could turn the env off, so the plaintext path
`connect_options()` documents was unreachable from the chart.

- `enabled: true` (default) — env, volume and a **required** mount. A missing Secret now fails at
  pod creation: `MountVolume.SetUp failed … secret "chemclaw-temporal-tls" not found`, on the pod,
  before any process starts.
- `enabled: false` — none of the three, and the client connects plaintext. The right setting for a
  dev cluster, or a Temporal frontend fronted by a service mesh that terminates mTLS itself.

### Settings that decide whether the pod boots at all

(No count in this heading. It said "Two" over three bullets, for the same reason the secret count
moved into the chart and its test: a number written in prose is a number that goes stale.)

- **`CHEMCLAW_ENTRA_REQUIRED=true` is mandatory for any exposed deployment.** With it off, every
  request runs as the shared dev principal and all authorization gates are open, so the front door
  **refuses to start** when that mode is bound to a non-loopback interface (the `0.0.0.0` default) —
  it exits with `SECURITY: entra_required is False but the service binds a non-loopback interface …`
  instead of silently serving an open deployment. `CHEMCLAW_SERVICE_ALLOW_INSECURE=true` is the deliberate opt-out (boots with a
  loud warning) and belongs in local dev only. Under `entra_required`, `CHEMCLAW_ENTRA_TENANT_ID`
  and `CHEMCLAW_ENTRA_AUDIENCE` must also be set — a half-configured identity setup fails fast at
  startup rather than at the first request.
- **`CHEMCLAW_ENTRA_CLIENT_ID` no longer exists. Drop it before upgrading, because nothing will
  tell you if you don't.** This entry used to promise the opposite — that `extra="forbid"` aborts
  startup with a validation error naming the stale field — and that is true of a key in a *dotenv
  file* and false of the environment. pydantic-settings' `EnvSettingsSource` looks up only the names
  it has fields for, so a `CHEMCLAW_*` variable that matches nothing is never seen, let alone
  rejected: a `CHEMCLAW_`-prefixed variable matching no field boots cleanly, and a ConfigMap is
  exactly how config arrives in-cluster. So a stale export is silently ignored rather than loud,
  and the same silence is what makes a rolled-back or hand-edited ConfigMap quiet rather than
  obvious.
- **`CHEMCLAW_SERVICE_FLEET_MAX_CONCURRENT_TURNS` is the ceiling the whole deployment may put on the
  shared LLM endpoint** (D-2026-08-01-a-per-process-cap-multiplied-by-a-number-nobody-wrote-down).
  The admission cap is per-process by design, so the load that endpoint really sees is
  `maxReplicas × uvicorn workers × CHEMCLAW_SERVICE_MAX_CONCURRENT_TURNS` — 48 for the shipped chart,
  which is exactly what it declares. A configuration whose product exceeds the declared ceiling
  **refuses to start**, in every pod, with a message naming the product and each factor. Raising
  `service.autoscaling.maxReplicas` or the per-process cap therefore means raising this too, with a
  number from the endpoint's throughput budget — and the repository's own tests fail on a chart whose
  autoscaling shape outruns its declaration, so that decision cannot reach a cluster by accident.
  `0` disables the check, which is the code default: a CLI or a single-pod dev run has no fleet.

### The setting that does *not* block boot, and closes every expensive job

**`CHEMCLAW_ENTRA_PRIVILEGED_ROLES` ships empty, and empty means every expensive job is refused for
everyone.** This is the one identity setting whose misconfiguration a pod cannot refuse to start
over, so it gets its own section beside the ones that can.

`expensive: true` in a connector manifest now derives into the trigger gate, and that gate fails
closed on an empty role set rather than open. Under the shipped `CHEMCLAW_ENTRA_REQUIRED=true`, with
this unset, the following are refused for every authenticated user:

| Job | Bundle |
|---|---|
| `compute_interaction_energy` | `connectors/calc` |
| `sample_conformers` | `connectors/calc` |
| `start_optimization_campaign` | `connectors/bo` |

Nothing else breaks: the pod boots, both probes pass, reads and knowledge lookups work, and a
chemist asking for a DFT run is told they lack a privileged role. That combination — healthy
deployment, one capability silently shut — is why this is documented at the same volume as a
crash-loop rather than in a comment nobody reads at 2am.

**The remedy is this setting alone.** Set it to a comma list of the Entra app roles your chemists
hold; `CHEMCLAW_ENTRA_EXPENSIVE_ACTIONS` is *not* needed beside it, because the action set comes from
the manifests. Config validation enforces the pair in one direction only: naming actions with no
role is rejected at startup (nobody could pass that gate), naming roles with no actions is the normal
production configuration. It used to demand both, which made the instruction above un-followable.

**Why the chart does not ship a placeholder role name.** A plausible-looking value would be a config
that *looks* configured — it survives review, reaches the cluster, grants nothing, and sends the
operator hunting through Entra group membership instead of through this file, because "you do not
hold this role" is the gate working correctly. The other placeholders in `values.yaml` are safe
because their shape is inert (a zero GUID fails a token exchange loudly); a role name has no such
shape. The key is written out as an explicit `""` instead, so the emptiness appears in
`helm show values`, in the rendered ConfigMap, and in any values diff — where an absent key appears
in none of them.

## Stateful dependencies (F6-T3, ADR **D-049**, Teilentscheidung D-A6a)

- **Temporal: self-hosted in-cluster** (not Temporal Cloud). Rationale: keeps the durable core inside
  the same cluster + OIDC trust boundary as everything else, and avoids egress of workflow payloads
  (which carry the Entra `oid`, D-044) to a third party. Temporal Cloud stays a values-swap away
  (`temporal_api_key` instead of the mTLS trio) if that trade changes.
- **Postgres/pgvector**: an operator- or managed-instance with mTLS and the existing
  `pg_statement_timeout_seconds`. Migrations run as a **pre-deploy Helm hook** Job
  (`templates/migrate-job.yaml` → `python -m chemclaw.core.migrate`, i.e. `make db-migrate`, D-034) that
  completes before any app container starts — no container ever races the DDL. The migrator takes a
  transaction advisory lock (so two overlapping deploys serialize) and a `lock_timeout` (so an
  `ALTER TABLE` that cannot get `ACCESS EXCLUSIVE` fails in seconds instead of queueing in front of
  every later query on that table — Postgres's lock queue is FIFO, so an unbounded wait is an
  outage, not a delay). The Job has an `activeDeadlineSeconds`, because Helm waits for a hook and a
  retrying Job would otherwise hold the release in `pending-upgrade`. Recovery for all three is
  `docs/guides/runbook.md` §(xi).

## Where the knowledge graph lives in a pod

One directory, not two. `Settings.knowledge_path` is `note_repo_dir / knowledge_dir` and there is no
second resolution — every reader goes through that property — so the chart publishes the synced
graph to exactly that path (`chemclaw.knowledgePublishPath` = `knowledge.noteRepoPath` +
`CHEMCLAW_KNOWLEDGE_DIR`) and the PR-gate submitter branches from the clone around it. Two
containers therefore write one tree, and the sync takes the submitter's own advisory lock — the
`flock` under the checkout's git directory that `src/chemclaw/kg/git_submitter.py` already uses to
exclude a second process — for the duration of each publish. A held lock means a submission is in
flight, and the publish waits for the next tick.

There used to be a separate `knowledge.publishPath: /app/knowledge` on its own `emptyDir`. Nothing
read it. The consequence was not an error at any layer: `rglob` over the tree readers *did* resolve
yielded nothing and raised nothing, so the agent answered with no knowledge-graph evidence and said
so nowhere — and the same `emptyDir` masked the corpus the image ships at that path, which is why
"leave `repoUrl` empty to run against whatever the image shipped" was false. With no remote
configured the sync now seeds the published tree from `/app/knowledge` instead.

`knowledge.sync.checkoutPath` survives as the shallow replica the publish copies *from*, which is
its stated reason for existing: a failed fetch must never leave the directory the app reads
half-written.

## Network & probes

- **NetworkPolicy** (`templates/networkpolicy.yaml`): egress is allowed on a fixed **port** list —
  DNS, Postgres (5432), Temporal (7233), HTTPS (443, for the internal LLM + Entra),
  OTLP (4317), and the connector ports — to the **destinations you name**. Ingress rules bound which
  *peers* may open a connection to the front door, the connectors and the workers' probe port.
- **The destinations are yours to state, and the chart will not render until you do.** This section
  used to say "nothing else leaves a pod", which was false in the way that matters: an empty
  `networkPolicy.egressDestinations` renders `to: []`, and in a NetworkPolicy that means *any*
  destination on those ports — so TCP/443 to the whole internet was permitted from every pod behind
  an object that read as a restriction. The chart cannot invent your CIDRs, so it now refuses to
  render unless exactly one of two things is stated: `networkPolicy.egressDestinations` (a list of
  NetworkPolicyPeer objects), or `networkPolicy.allowAnyDestination: true` — the deliberate,
  greppable statement that the old default was what you wanted. `helm template` on the shipped
  defaults therefore takes `--set networkPolicy.allowAnyDestination=true`, which is what the two
  renders in the `Makefile` pass (`D-2026-08-26-a-knob-that-renders-nothing-is-not-a-knob`).
- **The retention posture must be stated the same way.** Every `CHEMCLAW_RETENTION_*` window
  defaults to disabled — a disposal policy is a deployment's decision, not a code default — and
  what that silence shipped was durable tables that grow for the release's lifetime (the LangGraph
  checkpoint tables re-serialize a session's whole thread roughly once per superstep). The chart
  now refuses to render unless exactly one of `retention.windows` (the day windows, rendered into
  the ConfigMap with `CHEMCLAW_RETENTION_ENABLED` derived) or
  `retention.unboundedGrowthAccepted: true` is stated; the Makefile's renders pass the latter.
- **`/metrics` is on the public host, and the NetworkPolicy is not what bounds it.** The Route
  declares no `spec.path`, and neither a Route nor a NetworkPolicy filters by path — the ingress
  rule must allow the router, and the router publishes every path. What makes an unauthenticated
  `/metrics` acceptable is the exposition: counts, capacity and an operator-chosen `profile` label,
  never a session id, an actor or turn content, enforced by D-152's declared-label allowlist. Three
  places (this chart, `api/app.py`, an ADR) used to name the NetworkPolicy as the control instead.
  The residual exposure is operational reconnaissance; `route.ipWhitelist` restricts the whole Route
  to a set of source CIDRs for a deployment that will not accept it.
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
  (`chemclaw.core.asgi.BodySizeLimit`) refuses a body over `CHEMCLAW_SERVICE_MAX_REQUEST_BYTES`
  with 413 before it is read; and a per-principal token bucket refuses with 429, on in the chart
  and off in code. Every connector server installs the same middleware over its own, smaller
  `CHEMCLAW_CONNECTOR_MAX_REQUEST_BYTES` — its `/mcp` carries one JSON-RPC call, never a file
  upload. Tuning and symptoms: `docs/guides/runbook.md` §(xii).

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
  Helm reports success. Both objects therefore carry `helm.sh/resource-policy: keep`, which Helm
  reads off the live object at deletion time; with it, the same rollback leaves them standing. The
  trade is stated where it is made (`templates/config.yaml`): **`helm uninstall` now leaves those
  two objects behind**, which is what the old chart did. Everything else the move bought is intact
  — `keep` skips deletion only, so a rollback inside this chart's lineage still restores the
  previous revision's ConfigMap contents.

Neither is permanent. When no release's retained history (`helm history`) still reaches a revision
installed before this chart, the annotation is a line to delete and the adoption step has nothing
left to adopt.

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
(`src/chemclaw/core/logging.py` bridges the one config value to `OTEL_EXPORTER_OTLP_ENDPOINT`).

**Two first-party spans, and the propagation that joins them up.** A `chemclaw.turn` span wraps a
turn and a `chemclaw.tool` span wraps each tool call, so "the question took 40 seconds and 31 of
them were one xTB call" is answerable — which it was not. Connector calls carry W3C `traceparent`
alongside the custom `X-Chemclaw-Correlation` header, and the connector adopts it, so a
calculation's spans appear *inside* the turn that asked for it instead of as an orphan trace. The
two headers are not redundant: the correlation id is what `audit_events` is keyed on and works with
no collector at all; `traceparent` is what makes a distributed trace a tree.

These three paragraphs used to read "Spans cover a turn and a job; dashboards track loop iterations,
tool latency, and job status." None of that existed — the only spans were the agent framework's own
model calls (D-2026-08-01-a-turn-you-can-follow-across-a-process). **The dashboard half of that
sentence is now true**: `templates/configmap-dashboards.yaml` ships five, and between them every
metric this system declares has either a panel or an alert. What is *still* absent is named rather
than implied: no span around a durable job (it spans two processes and a Temporal boundary, so it
needs the workflow to carry the context), and no FastAPI/httpx/Temporal auto-instrumentation.

**The pipeline is first-party now, and one thing went with the framework.** `configure_telemetry`
builds the `TracerProvider`, the `BatchSpanProcessor` and the OTLP span exporter itself rather than
calling `agent_framework.observability.configure_otel_providers`, so removing that package cannot
silently stop tracing. **The chart now sets `OTEL_SERVICE_NAME` per Deployment** — `chemclaw-service`,
`chemclaw-background-worker`, `chemclaw-connector-<name>`, `chemclaw-connector-worker-<name>` — so
the four process roles are four services in the backend rather than the one they all reported as,
with `service.version=<revision>` and `k8s.pod.name`/`k8s.namespace.name` from the downward API
through `OTEL_RESOURCE_ATTRIBUTES`. The framework's per-model
`gen_ai.client.token.usage` histogram stopped being exported the moment this pipeline replaced its
own — a span pipeline exports no metrics — and nothing in `langchain`/`langgraph`/`langsmith` emits
it. `CHEMCLAW_OTEL_LLM_SPANS: "true"` answers the same question in the pipeline that *is* here: one
span per model call carrying its token counts, model name and provider, through OpenInference's
LangChain instrumentation, with content suppressed unless
`CHEMCLAW_OTEL_INCLUDE_SENSITIVE_DATA` says otherwise (`docs/guides/runbook.md` § OpenTelemetry).
Point the collector at Arize Phoenix to read those conventions natively — any OTLP backend receives
them, and the instrumentation in this image is Apache-2.0 where Phoenix's server is ELv2.
Metrics and logs are unchanged and are deliberately not exported over OTLP — `/metrics` is scraped
per pod and logs are JSON on **stderr** (`configure_logging` calls `basicConfig` with no `stream=`,
whose default is `sys.stderr`; three documents including this one said stdout).

**Metrics come from every process, not only the front door.** `templates/servicemonitor.yaml`
collects the Services (the front door and each connector's MCP server, by their `http` port name);
`templates/podmonitor.yaml` collects the pods that have none — core's background worker and each
bundle's — by the `metrics` port they declare. Until D-2026-08-01 the ServiceMonitor selected the
front door alone, so everything the workers counted (durable jobs launched, PR-gate proposals and
their failures, lost audit records) was recorded in each worker's own registry and read by nobody.
`monitoring.additionalLabels` is for a **self-managed Prometheus Operator**, whose `Prometheus`
resource carries a `serviceMonitorSelector`/`podMonitorSelector` these labels have to match. It is
*not* what decides anything on OpenShift, which this file used to claim: user-workload monitoring
selects every ServiceMonitor and PodMonitor in every user namespace with no label selector at all,
so the empty default is correct there.

**What does decide it on OpenShift is off by default and this chart cannot set it**: user-workload
monitoring itself (`openshift-monitoring/cluster-monitoring-config`, `enableUserWorkload: true`).
Without it every monitor and rule here is an inert custom resource that lists, scrapes nothing and
alerts nothing, with no error anywhere. `templates/NOTES.txt` prints the procedure at install and
`docs/guides/runbook.md` § "Make the monitoring stack actually collect this" is the long form —
including the second switch, `enableUserAlertmanagerConfig`, without which the alerts fire into the
platform Alertmanager and are dropped.

**Logs are JSON in-cluster** (`CHEMCLAW_LOG_JSON`, on in the chart) and every line carries
`correlation_id`, `actor` and `session_id` from the turn's ContextVars — so an ordinary WARNING
joins to the audit row that recorded the same call and, through the same correlation id, to the
trace that spans it. A filter also replaces any configured secret's value with `***` before a
record reaches a stream, including one passed as a `%s` argument
(D-2026-08-01-a-log-line-that-joins-and-a-secret-that-does-not).

**What is deliberately not redacted:** the audit trail's tool-call arguments. `SECURITY.md` states
that they are user free text, may contain PII, and are recorded *intentionally* — the trail exists
to be an
attributable "who did what to which inputs" record. A deployment's retention, access control and
PII policy must cover the trail; that remains a policy obligation and not something this code
silently satisfies by deleting the evidence.

**Spend is recorded twice, on purpose.** `chemclaw_tokens_total{profile}` and
`chemclaw_job_runtime_seconds_total{connector}` are the fleet-wide rates; `turn_costs` and
`job_records.runtime_seconds` are the per-actor, per-run ledger that answers "what did this team
cost last quarter" (D-2026-08-01-spend-is-a-ledger-not-a-label). They are not redundant: the metric
registry refuses a counter past 64 label series because a label value is attacker-influenced, so an
Entra `oid` cannot be a label — attribution needs a database. Neither records **money**: a rate card
is a deployment's own fact, so the ledger holds tokens and seconds and leaves the multiplication to
whoever knows the numbers. Both are written only under `CHEMCLAW_SESSION_STORE=postgres`.

**A family of alerts exists because config validation can only see the shape it was handed** — no
count here, because the one that was written said "one" while two were already deployed. Each
fleet budget is checked at startup, and a `kubectl scale`, an HPA edited in the cluster, or a
rollout leaving both generations up all push the live fleet past its ceiling while every pod's own
configuration stays valid. Each alert therefore compares a *live* left-hand side against the
declared ceiling and is self-disabling when none is declared:
`ChemclawFleetAboveItsTurnCeiling` (`sum(chemclaw_turn_capacity)` against
`chemclaw_fleet_turn_ceiling`), `ChemclawFleetAboveItsConnectionCeiling`
(`sum(chemclaw_pg_pool_max_size)` against `chemclaw_pg_fleet_max_connections`, **each server
separately** — see below), and
`ChemclawCalcBackendOverCommitted` (`sum(chemclaw_calc_requests_in_flight)` against
`chemclaw_calc_backend_max_concurrent_requests`).

**The connection one is charged in pools, and it used to be charged in pods.** `pg_pool_max_size`
bounds one *pool*: `core/db` keys a pool on `(dsn, libpq options, requested max_size)`, so a process
holds one per distinct key plus any foreign pool it registers. Measured by driving each role's
composition root, a front-door process holds the stores' pool, the `/readyz` probe's (its own
statement timeout is a distinct key — deliberately, since sharing the stores' pool makes readiness
answer 503 while the pool is merely busy) and the LangGraph checkpointer's autocommit pool; every
worker, connector server and the mcp-face holds one. Counting pods declared a ceiling covering
about two thirds of what the shipped fleet opens, which is why `postgres.maxConnections` rose in
the same commit that corrected the arithmetic — **a release provisioning to the old number has to
raise its Postgres `max_connections` to the new one, or lower `CHEMCLAW_PG_POOL_MAX_SIZE` until the
fleet fits.** The startup check names both sides in every pod; `sum(chemclaw_pg_pool_max_size)` has
been the honest live reading throughout, which is why the alert could fire on a fleet whose per-pod
validation passed.

**It is a sum and not a product, for two reasons that arrived together.** Pools are not all the same
width — the `/readyz` one asks for a single connection, because the probe is single-flighted — so
charging every pool `pg_pool_max_size` over-declared the shipped fleet by a fifth and refused a
legal `service.autoscaling.maxReplicas: 9` outright, CrashLooping every pod against a database that
could serve it. And they are not all on the same *server*: `CHEMCLAW_SESSION_STORE_DSN` moves the
front door's `/readyz` and checkpointer pools to a second database and adds one pool to every pooled
process, which the chart cannot see because that DSN lives in an operator-managed Secret no template
reads. `Settings.fleet_connections_per_server` places each pool on the server it will be opened
against; `postgres.maxConnections` bounds `postgres_dsn`'s and `postgres.sessionStoreMaxConnections`
bounds the split store's. Undeclared while the split is real warns at startup and names the
connections nobody is checking; declared with no split is refused, because a ceiling for a server
that does not exist is one the alert would add to the real one.

**The alert checks each server, and it is two comparisons rather than one sum for a reason worth
knowing.** `sum(pools) > primaryCeiling + sessionCeiling` looks conservative and is the opposite:
if each server is inside its own ceiling the sum cannot exceed the sum, so it never false-positives
— and `p₁ > A` with `p₁ + p₂ ≤ A + B` is satisfiable, so it goes silent while one server is over.
Enumerated over 200,000 random draws: 0 false positives, 25% misses. On this chart's topology with
the session server declared at 180 it is over from seven front-door replicas and the sum waits
until thirteen. The same expression also paged a *healthy* split whose second ceiling was
undeclared — the whole fleet's 278 against `256 + 0` — while the primary sat at 44%.
`chemclaw_pg_session_pool_max_size` is the part of the live left-hand side that lands on the split
store, so the primary's side is the difference; with no split it is 0 in every pod and both
branches are the original comparison.

The last of those reads *held sessions* rather than a configured capacity, and the difference is
forced rather than stylistic: two kinds of process dispatch to the calculation backend and they do
not share a cap — a `calc` worker is bounded by `CHEMCLAW_WORKER_MAX_CONCURRENT_ACTIVITIES`, while
that bundle's own MCP server pods dispatch straight from a tool call and are bounded by nothing
local (`D-2026-08-27-a-per-worker-cap-is-not-a-backend-ceiling`). Its ceiling ships as `0`,
undeclared, because it describes a pod in another release; set it to what that server admits.

**Thirty-six alerts across eight groups, and five dashboards for what is left.** The rule file's own
header names the three absences it was written against — "no PrometheusRule anywhere in the repo, no
SLO, and no Alertmanager route" — and all three are now closed:
`templates/prometheusrule.yaml` covers records, correctness, availability, cost, fleet liveness
(`up`/`absent()`, the only rules that fire for a process that is *gone*), turn latency, the durable
tier and the silent-degradation counters; `templates/alertmanagerconfig.yaml` is the route, gated on
`monitoring.alertmanager.enabled` and refusing to render without receivers; and every rule carries a
`runbook_url` into `docs/guides/runbook.md` § "When an alert fires", which indexes all thirty-six.
`templates/configmap-dashboards.yaml` carries the rest, so no declared metric is left with no reader
— `tests/test_deploy_chart.py::test_every_declared_metric_has_a_consumer` is what keeps that true
rather than believed.

`make helm-validate` now runs `promtool check rules` over both, which is a gate `kubeconform` could
not be: it validates that an `expr` is a *string*, not that the string is PromQL, so a syntax error
would pass CI and the API server and then be rejected by Prometheus at rule-group load — taking the
whole group with it, silently, with the object still reading as `Valid`.

## CI/CD (F6-T4)

Two workflows, both at the **repository root** — the only place GitHub Actions reads them from.
Until D-117 these lived under `services/chemclaw/.github/`, where nothing executed them.

- `ci.yml` — `make lint type cov` against a real Postgres, every validator the `ci` target names,
  and a `chart` job that renders the chart against the Kubernetes schemas and the rules against
  `promtool` (`helm template | kubeconform -strict`, then `promtool check rules`). **The count is
  deliberately not written here.** It said "seven" while `ci.yml` ran eight plus `helm-validate`,
  which is the same failure `CLAUDE.md` records for its own validator list and for the Makefile's
  target count; `tests/test_repo_map.py` derives the real list from the `ci` target.
- `image.yml` — on pull requests and `main`, **builds** the multi-target image and smoke-imports
  every component the entrypoint dispatches as a non-root UID, then asserts an unknown component
  exits 64. The component list is derived from the bundles present, so it cannot drift from what
  ships; the two directions of chart↔entrypoint agreement are checked offline in
  `tests/test_deploy_chart.py`.

The push-to-registry + `helm upgrade` rollout is **not** wired: the stranded workflow carried it as
a job whose whole body was an `echo`, and a stub is not a pipeline. Its trigger — a real cluster and
its credentials — is recorded in `docs/planning/DEFERRED.md`. Migrations run as the pre-deploy Job
(`templates/migrate-job.yaml`), never inside an app container.

> **Verified how, and where.** Pure-YAML parse, template brace-balance and `Settings` key mapping
> run anywhere. `helm template` and `kubeconform` run wherever `helm` is on PATH — which includes
> CI's `check` job, because the `ubuntu-latest` runner image ships Helm, so the
> `shutil.which("helm")`-gated chart assertions have always run there even though `azure/setup-helm`
> is pinned only in the `chart` job. **This note used to say helm runs "in CI (no helm/daemon in the
> dev sandbox)" and read as though the sandbox was the gap.** It is not: the sandbox skips those
> assertions silently, and installing helm there is one `curl`. What actually let five chart defects
> through was narrower and worse — the tests rendered the *default* values and nothing else, so a
> switch nobody flipped was a switch nobody checked. The render set is derived from `values.yaml`
> now. A live cluster remains genuinely out of reach here, and `helm rollback` against a real API
> server is the one thing a render cannot stand in for.
