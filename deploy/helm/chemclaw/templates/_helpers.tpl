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

{{- /* The port a worker serves its probes and its scrape on. Env, so `chemclaw.core.worker_http`
       binds the same number the container port and the PodMonitor name — one value, no third place
       for it to drift. Worker-only: the front door and the connector servers already have an HTTP
       surface on `service_port` and must not start a second one. */ -}}
{{- define "chemclaw.workerMetricsEnv" -}}
- name: CHEMCLAW_WORKER_METRICS_PORT
  value: {{ .Values.workerMetricsPort | quote }}
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
       while, and a false restart mid-job is more expensive than a slow true one. */ -}}
{{- define "chemclaw.workerProbes" -}}
ports:
  - name: metrics
    containerPort: {{ .Values.workerMetricsPort }}
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
{{- define "chemclaw.workerGracePeriod" -}}
terminationGracePeriodSeconds: {{ add (int .Values.config.CHEMCLAW_WORKER_GRACEFUL_SHUTDOWN_SECONDS) 30 }}
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
{{- define "chemclaw.knowledgePublishPath" -}}
{{ .Values.knowledge.noteRepoPath }}/{{ .Values.config.CHEMCLAW_KNOWLEDGE_DIR }}
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

{{- /* Sidecar: refresh on a cadence so a merged note reaches a *live* pod without a redeploy. */ -}}
{{- define "chemclaw.knowledgeSidecar" -}}
{{- if .Values.knowledge.sync.enabled }}
- name: knowledge-sync
  image: "{{ include "chemclaw.image" . }}"
  imagePullPolicy: {{ .Values.image.pullPolicy }}
  securityContext:
    {{- include "chemclaw.containerSecurityContext" . | nindent 4 }}
  command: ["/usr/local/bin/chemclaw-knowledge-sync", "loop"]
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

{{- define "chemclaw.tlsMount" -}}
{{- if .Values.secrets.temporalTls.enabled }}
- name: temporal-tls
  mountPath: {{ .Values.secrets.temporalTls.mountPath }}
  readOnly: true
{{- end }}
{{- end -}}

{{- /* CHEMCLAW_CONNECTOR_URLS, computed from the SAME enabled set the connector Deployments come
       from, so the front door's address map cannot drift from the pods that exist. A bundle's
       manifest ships a loopback dev default; this is the deployment override that replaces it. */ -}}
{{- define "chemclaw.connectorUrls" -}}
{{- $urls := dict -}}
{{- range $name, $cfg := .Values.connectors -}}
{{- if and $cfg.enabled $cfg.server -}}
{{- $_ := set $urls $name (printf "http://%s-connector-%s:%v/mcp" (include "chemclaw.name" $) $name $.Values.connectorPort) -}}
{{- end -}}
{{- end -}}
{{- toJson $urls -}}
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
{{- if $cfg.server -}}{{- $total = add $total ($cfg.replicas | int) -}}{{- end -}}
{{- if $cfg.worker -}}{{- $total = add $total ($cfg.replicas | int) -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- $total -}}
{{- end -}}
