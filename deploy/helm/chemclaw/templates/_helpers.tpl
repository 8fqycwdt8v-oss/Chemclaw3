{{- /* Shared names, labels, and the common env/pod bits every component reuses (DRY across templates). */ -}}

{{- define "chemclaw.name" -}}chemclaw{{- end -}}

{{- define "chemclaw.labels" -}}
app.kubernetes.io/name: chemclaw
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "chemclaw.selectorLabels" -}}
app.kubernetes.io/name: chemclaw
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- /* Env shared by every component: the ConfigMap (non-secret) + every declared secret key.

       The three mTLS paths are gated on `secrets.temporalTls.enabled`, and that gate is the whole
       point of the value. `core/temporal_client._tls_config()` short-circuits only when all three
       settings are empty; set, it `read_bytes()` each one. Exported unconditionally against a
       Secret the chart never creates, the failure was `FileNotFoundError: /etc/temporal/tls/tls.crt`
       — from the post-install Schedules hook, which names neither Temporal nor a Secret, and from
       every worker as a crash loop, while the front door passed both probes because `/readyz` never
       touches Temporal. The plaintext connect path `connect_options()` documents was unreachable
       from the chart at any value.

       `enabled: true` is the default because in-cluster Temporal with mTLS is the deployment this
       chart describes (D-049); the Secret is correspondingly mounted **non-optionally**, so a
       missing one fails at pod creation with an event naming it rather than at the first
       `read_bytes()`. `enabled: false` is the deliberate plaintext choice, and it removes the env,
       the volume and the mount together — there is no state where one exists without the others. */ -}}
{{- define "chemclaw.env" -}}
{{- /* Declared off rather than left to a default. `langsmith` is in the runtime closure — a hard
       requirement of `langchain-core` and pulled again by `deepagents` — and it enables itself from
       ambient environment: either LANGSMITH_TRACING or LANGCHAIN_TRACING_V2 being truthy sends
       conversation content to api.smith.langchain.com. It is off by default today (measured), which
       is exactly the kind of fact that changes in a patch release or gets set by a base image. A
       deployment's egress posture should not rest on a library default, so both names are
       pinned false here, beside the NetworkPolicy that is the other half of the control. */}}
- name: LANGSMITH_TRACING
  value: "false"
- name: LANGCHAIN_TRACING_V2
  value: "false"
{{- if .Values.secrets.temporalTls.enabled }}
- name: CHEMCLAW_TEMPORAL_TLS_CERT
  value: "{{ .Values.secrets.temporalTls.mountPath }}/tls.crt"
- name: CHEMCLAW_TEMPORAL_TLS_KEY
  value: "{{ .Values.secrets.temporalTls.mountPath }}/tls.key"
- name: CHEMCLAW_TEMPORAL_TLS_CA
  value: "{{ .Values.secrets.temporalTls.mountPath }}/ca.crt"
{{- end }}
{{- range $configKey, $secretEnv := .Values.secrets.keys }}
- name: {{ $secretEnv }}
  valueFrom:
    secretKeyRef:
      name: {{ $.Values.secrets.name }}
      key: {{ $secretEnv }}
{{- end }}
{{- /* `optional: true`, and a separate map rather than more entries above, because these two
       properties do not go together. A key in `secrets.keys` is *required*: absent, the pod does
       not start, which is right for a credential whose absence silently breaks a capability (an
       LLM key, the knowledge-repo push token). But `secrets.create` defaults to false, so the
       Secret is operator-managed and predates the chart version that names a new key — adding one
       to the required map takes every pod in an existing release into CreateContainerConfigError
       on `helm upgrade`. That is a full outage caused by a chart bump.

       So a setting that is *safe when unset* goes here instead. It still gets a Secret slot rather
       than living in the ConfigMap, and an existing release keeps starting. */ -}}
{{- range $configKey, $secretEnv := .Values.secrets.optionalKeys }}
- name: {{ $secretEnv }}
  valueFrom:
    secretKeyRef:
      name: {{ $.Values.secrets.name }}
      key: {{ $secretEnv }}
      optional: true
{{- end }}
{{- end -}}

{{- /* The migration hook Job's own secrets, deliberately a second helper rather than more keys in
       `chemclaw.env`. That helper is included by every Deployment, so anything added to
       `secrets.keys` is mounted on the front door and every worker for the life of the pod — and
       the credential this carries is the one that owns the schema and can rewrite `audit_events`
       (D-2026-08-05-append-only-by-grant-not-by-contract). It belongs on a Job that exists for the
       seconds a release takes, and nowhere else.

       `optional: true` because a single-principal deployment is fully supported: absent, the key
       is simply unset and `postgres_migration_dsn` falls back to `postgres_dsn`. A required key
       would make splitting the principal mandatory for every dev database and CI run. */ -}}
{{- define "chemclaw.migrationEnv" -}}
{{- range $configKey, $secretEnv := .Values.secrets.migrationKeys }}
- name: {{ $secretEnv }}
  valueFrom:
    secretKeyRef:
      name: {{ $.Values.secrets.name }}
      key: {{ $secretEnv }}
      optional: true
{{- end }}
{{- end -}}

{{- /* The image reference every pod uses, in one place so a digest cannot be honoured in some
       templates and not others.

       `values.yaml` deployed a mutable tag (`0.1.0`) and nine templates each interpolated it
       themselves. A tag is a pointer: `helm rollback` to a release that names `0.1.0` fetches
       whatever `0.1.0` means *now*, which is the one thing a rollback must not do — and for a
       system whose audit trail stamps a build revision onto every result (AG-14), "which bytes
       produced this" stops being answerable the moment the tag is re-pushed.

       `image.digest` wins when set, because a digest names bytes and nothing can re-point it. The
       tag stays as the default so a dev install still works with `helm install .`; a release sets
       the digest, and `docs/guides/runbook.md` §(xiv) says how to get it. */ -}}
{{- define "chemclaw.image" -}}
{{- if .Values.image.digest -}}
{{ .Values.image.repository }}@{{ .Values.image.digest }}
{{- else -}}
{{ .Values.image.repository }}:{{ .Values.image.tag }}
{{- end -}}
{{- end -}}

{{- /* Registry credentials for a private registry. Absent entirely before, so an operator whose
       registry needs auth had no field to set and no signal that the chart expected an open one. */ -}}
{{- define "chemclaw.imagePullSecrets" -}}
{{- with .Values.image.pullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{- /* Which component a trace came from, per Deployment.

       `configure_telemetry` builds one `Resource` for the whole process and names the service
       `chemclaw` unless `OTEL_SERVICE_NAME` says otherwise — a decision `core/logging.py` argues
       for explicitly ("a deployment that wants the front door and each worker to appear as separate
       services sets `OTEL_SERVICE_NAME` per Deployment"). **The chart never set it.** So all four
       process roles reported `service.name=chemclaw`, and a span could not say whether the front
       door, a connector server, core's worker or a bundle's worker emitted it — which is most of
       what a trace is for once a turn crosses a process.

       `OTEL_RESOURCE_ATTRIBUTES` adds the pod and namespace on top, and it works for a reason worth
       stating: `Resource.create(...)` merges the SDK's own env detector *under* the attributes
       passed to it, so what this variable carries survives while `service.name` stays the explicit
       one above. Read from the downward API rather than templated, because a pod name is only known
       to the pod.

       Ordering is load-bearing. Kubernetes expands `$(VAR)` only against variables declared
       *earlier in the same container*, so `POD_NAME` and `POD_NAMESPACE` must precede the attribute
       string — reversed, the pod exports the two literals and nothing errors. */ -}}
{{- define "chemclaw.otelResourceEnv" -}}
- name: POD_NAME
  valueFrom:
    fieldRef:
      fieldPath: metadata.name
- name: POD_NAMESPACE
  valueFrom:
    fieldRef:
      fieldPath: metadata.namespace
- name: OTEL_SERVICE_NAME
  value: "chemclaw-{{ .component }}"
- name: OTEL_RESOURCE_ATTRIBUTES
  value: "k8s.pod.name=$(POD_NAME),k8s.namespace.name=$(POD_NAMESPACE),chemclaw.component={{ .component }}"
{{- end -}}

{{- /* The port a worker serves its probes and its scrape on. Env, so `chemclaw.core.worker_http`
       binds the same number the container port and the PodMonitor name — one value, no third place
       for it to drift. Worker-only: the front door and the connector servers already have an HTTP
       surface on `service_port` and must not start a second one. */ -}}
{{- define "chemclaw.workerMetricsEnv" -}}
- name: CHEMCLAW_WORKER_METRICS_PORT
  value: {{ .Values.workerMetricsPort | quote }}
{{- if .Values.monitoring.temporalSdkMetrics.enabled }}
{{- /* The Temporal SDK's own Prometheus exporter, and it is bound *here* rather than only
       declared. The chart already opened a `temporal-metrics` container port and pointed a
       PodMonitor endpoint at it, and nothing set the variable that makes the SDK listen on it —
       so turning the switch on gave every worker a declared, scraped, permanently-down target,
       which `ChemclawTargetDown` would then report forever. A port with nothing behind it is
       worse than no port: it manufactures the alert it was added to make possible. */}}
- name: CHEMCLAW_TEMPORAL_METRICS_PORT
  value: {{ .Values.monitoring.temporalSdkMetrics.port | quote }}
{{- end }}
{{- end -}}

{{- /* A worker's container port and its two probes, written once for core's worker and every
       bundle's.

       These replace a comment. Three templates asserted "no probes: liveness is the Temporal poll
       itself", which described an intent nothing enforced — a worker whose poll loop had died held
       its process open, so Kubernetes saw `Running`, no probe disagreed, and (until the same
       surface started serving `/metrics`) no counter reached anyone either.

       `/readyz` is the worker's own `is_running`, and it is a *readiness* signal rather than a
       liveness one on purpose: a worker that has stopped polling should be taken out of nothing —
       it serves no traffic — but it should be visibly not-ready, and restarting it on that alone
       would turn a Temporal reconnect into a crash loop. `/healthz` is what restarts the pod, and
       it means more here than "the process exists": it is served on the worker's own event loop, so
       a loop wedged inside an activity stops answering it.

       `failureThreshold: 6` on liveness against a 20 s period — two minutes of a wedged loop before
       a restart. Generous deliberately: an activity doing real chemistry can hold the loop for a
       while, and a false restart mid-job is more expensive than a slow true one.

       **And the `startupProbe` is what makes that generosity safe rather than a guess.** Those two
       numbers plus `initialDelaySeconds: 10` put the sixth consecutive liveness failure at
       10 + 5 x 20 = 110 s, and a worker spends that window importing: the same image, the same
       RDKit/agent/connector-registry tree the front door and the connector servers were given a
       startup budget for, all of it pulled by `chemclaw.durable.serve` before
       `chemclaw.core.worker_http` binds a port. So a cold or throttled node SIGKILLed a start that
       had not failed, and each restart paid the imports again. Liveness and readiness now do not
       run at all until the process answers once, which is what lets both stay tuned for a
       *running* worker. `tests/test_deploy_chart.py` pins the budget and the arithmetic. */ -}}
{{- define "chemclaw.workerProbes" -}}
ports:
  - name: metrics
    containerPort: {{ .Values.workerMetricsPort }}
{{- if .Values.monitoring.temporalSdkMetrics.enabled }}
  {{- /* The Temporal SDK's own Prometheus exporter, which is a listener inside the SDK core rather
         than a route on `chemclaw.core.worker_http` — so it cannot share the port above. Declared
         here and scraped by the second `podMetricsEndpoint` in `podmonitor.yaml`, both under the
         one switch, because a port declared with nothing bound to it is a permanently-down scrape
         target and `ChemclawTargetDown` would then report it forever. */}}
  - name: temporal-metrics
    containerPort: {{ .Values.monitoring.temporalSdkMetrics.port }}
{{- end }}
startupProbe:
  httpGet:
    path: /healthz
    port: metrics
  periodSeconds: {{ .Values.probes.worker.startup.periodSeconds }}
  failureThreshold: {{ .Values.probes.worker.startup.failureThreshold }}
  timeoutSeconds: {{ .Values.probes.worker.startup.timeoutSeconds }}
readinessProbe:
  httpGet:
    path: /readyz
    port: metrics
  initialDelaySeconds: 5
  periodSeconds: 10
livenessProbe:
  httpGet:
    path: /healthz
    port: metrics
  initialDelaySeconds: 10
  periodSeconds: 20
  failureThreshold: 6
{{- end -}}

{{- /* A worker's shutdown budget: long enough that the drain `durable/serve.py` performs can
       actually finish.

       Python installs no SIGTERM handler, so before that module existed a worker died mid-activity
       on every drain and Temporal re-ran the work only after its start-to-close timeout elapsed. A
       graceful shutdown fixes that in the process; this fixes it in the pod, because the default
       30 s grace period would SIGKILL through a 120 s drain and leave the code change buying
       nothing.

       Derived from the same ConfigMap key the worker reads, plus a margin for cancellation to
       propagate and the Postgres pool to close, so the two cannot disagree — the failure being
       avoided is a setting that looks configured and is overridden by a kubelet timer nobody
       thought to move. */ -}}
{{- /* `required` for the same reason `deployment-connectors.yaml`'s `replicas` carries one: `int
       nil` is `0`, so an absent key does not fail here — it renders a 30 s grace period against a
       120 s drain, which is the very SIGKILL this helper exists to prevent, wearing a number that
       looks deliberate. A derived value has to refuse when what it derives from is gone. */ -}}
{{- /* A connector server pod's shutdown ceiling: the longest synchronous tool call it can be
       holding, plus the endpoint drain in front of it.

       **This replaces a stated 120 s, and the 120 was wrong in the direction that loses work.**
       The comment beside it argued that the heavy science is not in process — which is true, and is
       precisely why the bound has to be large: what *is* in process is the HTTP call to
       `Chemclaw3-mcp`'s `servers/calc`, and this repository's client is allowed
       `calc_server_timeout_seconds` (900 s) for a composed primitive and
       `calc_atomic_timeout_seconds` (3600 s) for the two tools pinned to the `xtb` binary. At 120 s
       the kubelet SIGKILLed a running `optimize_geometry` on every rolling update, node drain and
       scale-down — and `cached_compute` stores a result only once the call *returns*, so the
       retry recomputed from zero rather than reading the D-011 cache.

       Derived from the same two ConfigMap keys the pods read, not restated, for the reason
       `chemclaw.workerGracePeriod` gives: a shutdown budget that has to remember what
       `CalculatorSettings` chose is one that stops agreeing with it silently.

       `calc_sampling_timeout_seconds` (14400 s) is deliberately outside this maximum. The two CREST
       searches it bounds are reachable only from `connectors/calc/activities.py` — a Temporal
       activity on a *worker* pod, which drains under `chemclaw.workerGracePeriod` and is retried by
       the broker if it does not — never from a connector server's synchronous tool surface.

       A grace period is a ceiling and not a wait: a connector pod with nothing in flight still
       exits after `connectorDrainSeconds`, so covering the worst case costs a rolling update
       nothing. `required` for the same reason the other two derived budgets carry one — `int nil`
       is `0`, and an absent key would render a grace period *shorter* than the default 30 s. */ -}}
{{- define "chemclaw.connectorGracePeriod" -}}
{{- $server := int (required "config.CHEMCLAW_CALC_SERVER_TIMEOUT_SECONDS must be set: a connector pod's terminationGracePeriodSeconds is derived from it" .Values.config.CHEMCLAW_CALC_SERVER_TIMEOUT_SECONDS) -}}
{{- $atomic := int (required "config.CHEMCLAW_CALC_ATOMIC_TIMEOUT_SECONDS must be set: a connector pod's terminationGracePeriodSeconds is derived from it" .Values.config.CHEMCLAW_CALC_ATOMIC_TIMEOUT_SECONDS) -}}
terminationGracePeriodSeconds: {{ add (max $server $atomic) (int .Values.connectorDrainSeconds) }}
{{- end -}}

{{- define "chemclaw.workerGracePeriod" -}}
terminationGracePeriodSeconds: {{ add (int (required "config.CHEMCLAW_WORKER_GRACEFUL_SHUTDOWN_SECONDS must be set: the worker terminationGracePeriodSeconds is derived from it" .Values.config.CHEMCLAW_WORKER_GRACEFUL_SHUTDOWN_SECONDS)) 30 }}
{{- end -}}

{{- /* Spread a multi-replica workload across nodes.

       `minReplicas: 2` says nothing about *where* those two land: the default scheduler is free to
       put both on one node, and then the second replica buys nothing against the failure it exists
       for. That matters more here than for a stateless service, because the Route pins a browser to
       one pod on purpose (D-121) — uploaded attachments, the harness todo list and the live
       `AgentSession` are in that pod's memory — so losing a node loses conversation state, not just
       capacity.

       `ScheduleAnyway`, not `DoNotSchedule`: a single-node dev or CI cluster must still be able to
       run the chart. The constraint is a preference the scheduler honours where it can, which is
       the honest strength of the claim — spreading is not something this chart can guarantee on
       infrastructure it does not own. */ -}}
{{- define "chemclaw.spreadAcrossNodes" -}}
topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: kubernetes.io/hostname
    whenUnsatisfiable: ScheduleAnyway
    labelSelector:
      matchLabels:
        {{- include "chemclaw.selectorLabels" . | nindent 8 }}
        app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- /* The common envFrom (the whole non-secret ConfigMap) + the mTLS volume mount. */ -}}
{{- define "chemclaw.envFrom" -}}
- configMapRef:
    name: {{ include "chemclaw.name" . }}-config
{{- end -}}

{{- /* The pod annotation that makes a ConfigMap change actually reach the pods.

       Non-secret config arrives only through `envFrom: configMapRef`, and environment is read once
       at process start. A `helm upgrade` that changes `.Values.config` therefore updated the
       ConfigMap and changed nothing running: `CHEMCLAW_LLM_BASE_URL`, `CHEMCLAW_ENTRA_REQUIRED`,
       `CHEMCLAW_BUDGET_ENABLED` and the rate limit all applied to *no* pod until something
       unrelated caused a restart. With the HPA on by default, the next scale-up then brought up
       pods that did read the new values — a fleet split across two configurations, with the
       operator's `helm upgrade` reporting success. (Two keys did force a rollout, by accident: the
       two the grace periods are derived from, which change the pod spec itself.)

       Hashing the rendered ConfigMap template makes the pod spec a function of the configuration,
       so the Deployment controller does the rollout for the same reason it does any other. The hash
       covers the whole file — the ServiceAccount and the optional placeholder Secret with it —
       which is deliberately wider than `.Values.config`: a change to either is also a change every
       pod should be restarted for.

       `$.Template.BasePath` and the root context are required: called from inside a `range`, `.` is
       the loop variable and the include would render nothing. */ -}}
{{- define "chemclaw.configChecksum" -}}
checksum/config: {{ include (print $.Template.BasePath "/config.yaml") . | sha256sum }}
{{- end -}}

{{- /* Where the synced graph is published — which is, and must be, exactly where the application
       reads it.

       `Settings.knowledge_path` is `note_repo_dir / knowledge_dir` and there is no second
       resolution: every reader (`kg.graph.load_notes`, the report retrievers, the note-index
       rebuild, `kg.validate`, the ELN sync, the memory synthesizers, the digest job) goes through
       that one property. So this is that expression, in the chart, over the same two values the
       ConfigMap hands the pods.

       It used to be an independent `knowledge.publishPath: /app/knowledge`, and the consequence was
       not a crash: the sync filled a directory nothing read while `knowledge_path` pointed at an
       empty (default install) or never-refreshed (configured install) tree, `rglob` over it yielded
       nothing and raised nothing, and the agent answered with zero knowledge-graph evidence. A path
       that only has to *agree* with another path eventually does not, so this one is derived rather
       than declared, and `tests/test_helm_chart.py` asserts the render equals what `Settings`
       resolves. */ -}}
{{- /* `required`, because the paragraph above is only true while both halves are present: an
       absent `CHEMCLAW_KNOWLEDGE_DIR` renders `<noteRepoPath>/` and re-creates the identical
       silent failure — the sync fills a directory nothing reads, `rglob` yields nothing and raises
       nothing, and the agent answers with zero knowledge-graph evidence. */ -}}
{{- define "chemclaw.knowledgePublishPath" -}}
{{ .Values.knowledge.noteRepoPath }}/{{ required "config.CHEMCLAW_KNOWLEDGE_DIR must be set: the knowledge publish path is derived from it, and an absent one publishes where no reader looks" .Values.config.CHEMCLAW_KNOWLEDGE_DIR }}
{{- end -}}

{{- /* Env the knowledge-sync init container and sidecar both need (DRY — they must agree). */ -}}
{{- define "chemclaw.knowledgeSyncEnv" -}}
- name: CHEMCLAW_KNOWLEDGE_REPO_URL
  value: {{ .Values.knowledge.sync.repoUrl | quote }}
- name: CHEMCLAW_KNOWLEDGE_SYNC_DIR
  value: {{ .Values.knowledge.sync.checkoutPath | quote }}
- name: CHEMCLAW_KNOWLEDGE_PUBLISH_DIR
  value: {{ include "chemclaw.knowledgePublishPath" . | quote }}
- name: CHEMCLAW_KNOWLEDGE_SYNC_INTERVAL_SECONDS
  value: {{ .Values.knowledge.sync.intervalSeconds | quote }}
{{- end -}}

{{- /* The two halves of a `restricted` Pod Security Admission profile, written once.

       Nothing in the chart asserted any of this. The image runs as a non-root UID
       (`Containerfile`), which is a different statement from the pod *declaring* that it must —
       and PSA reads the declaration, not the image. A namespace labelled
       `pod-security.kubernetes.io/enforce=restricted`, the default posture for a regulated
       OpenShift cluster, rejects every pod spec in this chart today. The image being fine is
       exactly what makes the omission easy to miss: it fails at admission, on deployment day, in
       someone else's cluster.

       `runAsNonRoot` is asserted without `runAsUser`: OpenShift assigns an arbitrary high UID from
       the namespace's range, and pinning one fights the SCC rather than satisfying it.

       `readOnlyRootFilesystem` is deliberately NOT here. It is not part of the restricted profile,
       and the calculation workers shell out to xtb/crest, which need writable scratch — asserting
       it would trade a real admission failure for a real runtime failure. It is a value
       (`securityContext.readOnlyRootFilesystem`) so a deployment that has provisioned the scratch
       mounts can turn it on deliberately. */ -}}
{{- define "chemclaw.podSecurityContext" -}}
runAsNonRoot: true
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "chemclaw.containerSecurityContext" -}}
allowPrivilegeEscalation: false
capabilities:
  drop:
    - ALL
readOnlyRootFilesystem: {{ .Values.securityContext.readOnlyRootFilesystem }}
{{- end -}}

{{- /* Init container: fill the knowledge tree BEFORE the app starts, so no pod ever serves a
       turn against an empty graph. Same image, so no second artifact to build or scan. */ -}}
{{- define "chemclaw.knowledgeInit" -}}
{{- if .Values.knowledge.sync.enabled }}
- name: knowledge-sync-init
  image: "{{ include "chemclaw.image" . }}"
  imagePullPolicy: {{ .Values.image.pullPolicy }}
  securityContext:
    {{- include "chemclaw.containerSecurityContext" . | nindent 4 }}
  command: ["/usr/local/bin/chemclaw-knowledge-sync", "once"]
  env:
    {{- include "chemclaw.knowledgeSyncEnv" . | nindent 4 }}
    {{- include "chemclaw.env" . | nindent 4 }}
  envFrom:
    {{- include "chemclaw.envFrom" . | nindent 4 }}
  resources:
    {{- toYaml .Values.resources.connector | nindent 4 }}
  volumeMounts:
    {{- include "chemclaw.knowledgeMounts" . | nindent 4 }}
{{- end }}
{{- end -}}

{{- /* Sidecar: refresh on a cadence so a merged note reaches a *live* pod without a redeploy.

       **With a liveness probe, because a wedged one used to be invisible.** `loop` catches a failing
       refresh deliberately — a dead git remote must not kill the pod — and the consequence was that
       an expired push credential left the container logging one WARNING per interval forever while
       serving a frozen corpus. Nothing measured it: no metric, no probe, no alert.
       `ChemclawKnowledgeNotesLost` covers notes going *out*; the graph coming *in* had nothing.

       The script now stamps a heartbeat on each *successful* refresh and `staleness` reads its age,
       so a stopped loop becomes a restarting container — a restart count and a `Warning` event an
       operator and `kube_pod_container_status_restarts_total` can both see. A restart does not
       repair a bad credential and is not meant to; what it buys is that the failure stops looking
       like health. Deliberately liveness and not readiness: a sidecar's readiness is the pod's, and
       a three-hour-old corpus is a better answer to a chemist than a connection error.

       The budget is three intervals plus the initial clone, so a single slow or failed tick is not a
       restart — the same "generous because a false restart costs more than a slow true one"
       reasoning as `chemclaw.workerProbes`. `failureThreshold: 3` over a one-interval period adds
       three more ticks on top of that before the kubelet acts. */ -}}
{{- define "chemclaw.knowledgeSidecar" -}}
{{- if .Values.knowledge.sync.enabled }}
- name: knowledge-sync
  image: "{{ include "chemclaw.image" . }}"
  imagePullPolicy: {{ .Values.image.pullPolicy }}
  securityContext:
    {{- include "chemclaw.containerSecurityContext" . | nindent 4 }}
  command: ["/usr/local/bin/chemclaw-knowledge-sync", "loop"]
  livenessProbe:
    exec:
      command:
        - /usr/local/bin/chemclaw-knowledge-sync
        - staleness
        - {{ mul (int .Values.knowledge.sync.intervalSeconds) 3 | quote }}
    initialDelaySeconds: {{ .Values.knowledge.sync.intervalSeconds }}
    periodSeconds: {{ .Values.knowledge.sync.intervalSeconds }}
    timeoutSeconds: 10
    failureThreshold: 3
  env:
    {{- include "chemclaw.knowledgeSyncEnv" . | nindent 4 }}
    {{- include "chemclaw.env" . | nindent 4 }}
  envFrom:
    {{- include "chemclaw.envFrom" . | nindent 4 }}
  resources:
    {{- toYaml .Values.resources.connector | nindent 4 }}
  volumeMounts:
    {{- include "chemclaw.knowledgeMounts" . | nindent 4 }}
{{- end }}
{{- end -}}

{{- /* The volumes a knowledge reader and the sync containers share.

       Two volumes, not three. The published tree is a directory *inside* the note-repo volume
       (`chemclaw.knowledgePublishPath`), because that is where `Settings.knowledge_path` resolves —
       so the separate `knowledge` emptyDir that used to be mounted at `/app/knowledge` is gone
       rather than repointed. It was also masking the corpus the image ships at that exact path,
       which is why `values.yaml`'s claim that an empty `repoUrl` "runs against whatever corpus the
       image shipped" was false; `knowledge-sync.sh` now seeds the publish directory from that
       corpus instead. */ -}}
{{- define "chemclaw.knowledgeMounts" -}}
{{ include "chemclaw.noteRepoMount" . }}
- name: knowledge-checkout
  mountPath: {{ .Values.knowledge.sync.checkoutPath }}
{{- end -}}

{{- /* Volumes + the mTLS secret, in one place so every pod spec stays identical. */ -}}
{{- define "chemclaw.volumes" -}}
{{- if .Values.secrets.temporalTls.enabled }}
{{- /* Required, not optional. Marked optional, an absent Secret surfaced as `FileNotFoundError`
       deep inside a Temporal connect — a message naming neither Temporal nor a Secret — from a
       post-install hook and every worker at once. Required, the kubelet reports
       `MountVolume.SetUp failed … secret "chemclaw-temporal-tls" not found` on the pod before a
       process starts. A deployment with no such Secret sets `secrets.temporalTls.enabled: false`
       and connects plaintext. */}}
- name: temporal-tls
  secret:
    secretName: {{ .Values.secrets.temporalTls.secretName }}
{{- end }}
- name: knowledge-checkout
  emptyDir: {}
{{- end -}}

{{- /* The PR-gate submitter's writable clone (gap DEP-2) — and, inside it at `knowledge_dir`, the
       tree every reader resolves. Every component that can call `propose_note` needs one: the front
       door (the `propose_knowledge_note` agent tool) and the background worker (job-result / BO /
       memory publishes), but NOT a connector's own worker — a bundle returns its note in the job
       envelope and core publishes it, so no connector process touches the note repo.

       One clone rather than a clone plus a published copy, because `Settings` offers one path for
       both: `knowledge_path` is `note_repo_dir / knowledge_dir`, and `kg/git_submitter.py` returns
       the checkout to the base branch after every submission *because* readers share it. The
       shallow replica at `sync.checkoutPath` survives as what the publish copies **from**, which is
       its stated reason for existing: a failed fetch must not be able to leave the directory the
       app reads half-written.

       This init container runs first — `git clone` refuses a non-empty destination and the publish
       directory is inside this one. */ -}}
{{- define "chemclaw.noteRepoInit" -}}
{{- if .Values.knowledge.sync.enabled }}
- name: note-repo-init
  image: "{{ include "chemclaw.image" . }}"
  imagePullPolicy: {{ .Values.image.pullPolicy }}
  securityContext:
    {{- include "chemclaw.containerSecurityContext" . | nindent 4 }}
  command: ["/usr/local/bin/chemclaw-knowledge-sync", "checkout"]
  env:
    {{- include "chemclaw.knowledgeSyncEnv" . | nindent 4 }}
    {{- include "chemclaw.env" . | nindent 4 }}
  envFrom:
    {{- include "chemclaw.envFrom" . | nindent 4 }}
  resources:
    {{- toYaml .Values.resources.connector | nindent 4 }}
  volumeMounts:
    {{- include "chemclaw.noteRepoMount" . | nindent 4 }}
{{- end }}
{{- end -}}

{{- define "chemclaw.noteRepoMount" -}}
- name: note-repo
  mountPath: {{ .Values.knowledge.noteRepoPath }}
{{- end -}}

{{- define "chemclaw.noteRepoVolume" -}}
- name: note-repo
  emptyDir: {}
{{- end -}}

{{- /* The mounted document share, read-only and on the background worker alone. `readOnly` is not
       decoration: this system never writes to a site's file share, and the mount is where that is
       made true rather than merely intended. */ -}}
{{- define "chemclaw.documentShareMount" -}}
{{- if .Values.documentShare.enabled }}
- name: document-share
  mountPath: {{ .Values.documentShare.mountPath }}
  readOnly: true
{{- end }}
{{- end -}}

{{- define "chemclaw.documentShareVolume" -}}
{{- if .Values.documentShare.enabled }}
- name: document-share
  persistentVolumeClaim:
    claimName: {{ .Values.documentShare.claimName }}
    readOnly: true
{{- end }}
{{- end -}}

{{- define "chemclaw.tlsMount" -}}
{{- if .Values.secrets.temporalTls.enabled }}
- name: temporal-tls
  mountPath: {{ .Values.secrets.temporalTls.mountPath }}
  readOnly: true
{{- end }}
{{- end -}}

{{- /* CHEMCLAW_CONNECTOR_URLS, computed from the SAME enabled set the connector Deployments come
       from, so the front door's address map cannot drift from the pods that exist. A bundle's
       manifest ships a loopback dev default; this is the deployment override that replaces it.

       A bundle with an explicit `url` is dialled there instead of at a Service this release
       renders, because for that bundle no such Service exists — `deployment-connectors.yaml` skips
       the app half precisely when this key is set. The two must stay conditioned on the same value:
       a computed Service address for a connector we do not run is the failure this exists to
       prevent, and it is silent — the front door dials a name that resolves to nothing and reports
       the capability as merely unreachable. */ -}}
{{- define "chemclaw.connectorUrls" -}}
{{- $urls := dict -}}
{{- range $name, $cfg := .Values.connectors -}}
{{- if and $cfg.enabled $cfg.server -}}
{{- $_ := set $urls $name (default (printf "http://%s-connector-%s:%v/mcp" (include "chemclaw.name" $) $name $.Values.connectorPort) $cfg.url) -}}
{{- end -}}
{{- end -}}
{{- toJson $urls -}}
{{- end -}}

{{- /* Which bundles the agent loads at all — `CHEMCLAW_CONNECTORS_ENABLED`, derived from the same
       block that renders the pods.

       **`enabled: false` used to remove a bundle's pods and leave its tools on the agent's
       surface.** `values.yaml` said this key was "in `config` below"; it was in none of the 33
       entries there, and `connectors_enabled` empty means *every discovered bundle* — "discovery is
       enablement until you say otherwise". So a disabled `calc` still advertised its jobs, the
       launcher still started the wrapper on the polled queue and its child on `connector-calc`,
       which nobody polls, and the chemist was told "running" until the job ceiling.

       Pathsep-joined (`:`), which is what `Settings.connectors_enabled_list` splits on, and in the
       map's key order — Helm ranges a map sorted by key and `registry.discovered()` sorts by name,
       so the derived order is the discovery order and the advertised tool order does not move.

       A release that enables no connector at all is refused rather than rendered: the empty string
       means "load everything" to the reader, so it is the one intent this variable cannot express,
       and rendering it would silently invert the operator's choice. */ -}}
{{- define "chemclaw.connectorsEnabled" -}}
{{- $names := list -}}
{{- range $name, $cfg := .Values.connectors -}}
{{- if $cfg.enabled -}}{{- $names = append $names $name -}}{{- end -}}
{{- end -}}
{{- /* **Not conditioned on `.Values.connectors` being present**, and that was the whole defect:
       the message used to offer "remove the connectors block entirely" as the connector-less
       release's remedy, and taking it skipped this guard — rendering the empty string, which the
       reader takes as *every* bundle, plus `CHEMCLAW_CONNECTOR_URLS: "{}"` so each one fell back
       to its manifest's loopback dev address. That is the "pods gone, tools advertised" regression
       this helper exists to close, reached through the door its own error message opened. There is
       no way to say "no connectors" here because `Settings` has no way to hear it. */ -}}
{{- if not $names -}}
{{- fail "connectors: this release enables no bundle. CHEMCLAW_CONNECTORS_ENABLED cannot say \"none\" — the empty string means every bundle the image ships — so at least one bundle must have `enabled: true`. Removing the `connectors` block does not state a connector-less release either: it renders that same empty string, and every bundle then falls back to its manifest's loopback dev address." -}}
{{- end -}}
{{- join ":" $names -}}
{{- end -}}

{{- /* How many processes in this release may open a Postgres pool — the multiplicand in
       `pg_pool_max_size × processes` that `core/config/store.py` has always stated in prose and
       nothing computed (D-2026-08-05-the-connection-budget-is-a-fleet-number).

       Derived from the SAME values that render the Deployments, for the same reason
       `chemclaw.connectorUrls` is: a hand-maintained count is a second declaration of the
       topology, and this chart has watched one of those go stale before. The front door counts at
       its HPA ceiling rather than its floor, exactly as CHEMCLAW_SERVICE_FLEET_REPLICAS does —
       the budget has to hold at the fleet's largest legal shape, not its smallest. Every worker and
       connector server pods `replicas` processes and pools once each (`chemclaw.core.db.pooling` in
       `durable/serve.py` and `connectors/server.py`).

       One front-door pod is one pooled process, with no uvicorn-worker factor, because `Settings`
       refuses CHEMCLAW_SERVICE_UVICORN_WORKERS above 1 outright (five per-process guarantees break
       across processes). Multiplying by a number that can only ever be 1 would be arithmetic
       nothing can exercise; if that guard is ever lifted, this is the second place to change and
       the validator's message is the first.

       The migration hook Job is deliberately NOT counted: it uses `connect()`, not the pool, and
       it has finished before any app container starts. */ -}}
{{- define "chemclaw.pooledProcesses" -}}
{{- $total := .Values.service.replicas | int -}}
{{- if .Values.service.autoscaling.enabled -}}
{{- $total = .Values.service.autoscaling.maxReplicas | int -}}
{{- end -}}
{{- $total = add $total (.Values.workers.background.replicas | int) -}}
{{- range $name, $cfg := .Values.connectors -}}
{{- if $cfg.enabled -}}
{{- /* `not $cfg.url` for the same reason the Deployment is guarded on it: an externally hosted
       connector pods nothing in this release and so opens no pool here. Counting it would spend
       the fleet's connection budget on processes that do not exist, which is the ceiling being
       wrong in the direction that silently throttles the pods that do. */ -}}
{{- /* Each half at its own count, because they are separate Deployments and (since 2026-08-26)
       separately scalable. Reading `replicas` for both was right only while one knob drove both;
       it is also what made a `url:` bundle with a worker contribute `nil | int` = 0 to the
       budget, since `replicas` was never required of one. */ -}}
{{- if and $cfg.server (not $cfg.url) -}}{{- $total = add $total ($cfg.serverReplicas | default $cfg.replicas | int) -}}{{- end -}}
{{- if $cfg.worker -}}{{- $total = add $total ($cfg.workerReplicas | default $cfg.replicas | int) -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- $total -}}
{{- end -}}
