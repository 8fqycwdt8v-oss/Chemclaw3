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

{{- /* Env shared by every component: the ConfigMap (non-secret) + the three plain secret keys. */ -}}
{{- define "chemclaw.env" -}}
- name: CHEMCLAW_TEMPORAL_TLS_CERT
  value: "{{ .Values.secrets.temporalTls.mountPath }}/tls.crt"
- name: CHEMCLAW_TEMPORAL_TLS_KEY
  value: "{{ .Values.secrets.temporalTls.mountPath }}/tls.key"
- name: CHEMCLAW_TEMPORAL_TLS_CA
  value: "{{ .Values.secrets.temporalTls.mountPath }}/ca.crt"
{{- range $configKey, $secretEnv := .Values.secrets.keys }}
- name: {{ $secretEnv }}
  valueFrom:
    secretKeyRef:
      name: {{ $.Values.secrets.name }}
      key: {{ $secretEnv }}
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

{{- /* The common envFrom (the whole non-secret ConfigMap) + the mTLS volume mount. */ -}}
{{- define "chemclaw.envFrom" -}}
- configMapRef:
    name: {{ include "chemclaw.name" . }}-config
{{- end -}}

{{- /* Env the knowledge-sync init container and sidecar both need (DRY — they must agree). */ -}}
{{- define "chemclaw.knowledgeSyncEnv" -}}
- name: CHEMCLAW_KNOWLEDGE_REPO_URL
  value: {{ .Values.knowledge.sync.repoUrl | quote }}
- name: CHEMCLAW_KNOWLEDGE_SYNC_DIR
  value: {{ .Values.knowledge.sync.checkoutPath | quote }}
- name: CHEMCLAW_KNOWLEDGE_PUBLISH_DIR
  value: {{ .Values.knowledge.publishPath | quote }}
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
  image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
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
  image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
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

{{- /* The knowledge volume mounts every component shares (published tree + the sync checkout). */ -}}
{{- define "chemclaw.knowledgeMounts" -}}
- name: knowledge
  mountPath: {{ .Values.knowledge.publishPath }}
- name: knowledge-checkout
  mountPath: {{ .Values.knowledge.sync.checkoutPath }}
{{- end -}}

{{- /* Volumes + the mTLS secret, in one place so every pod spec stays identical. */ -}}
{{- define "chemclaw.volumes" -}}
- name: temporal-tls
  secret:
    secretName: {{ .Values.secrets.temporalTls.secretName }}
    optional: true
- name: knowledge
  emptyDir: {}
- name: knowledge-checkout
  emptyDir: {}
{{- end -}}

{{- /* The PR-gate submitter's own writable clone (gap DEP-2). Every component that can call
       `propose_note` needs one — that is the front door (the `propose_knowledge_note` agent tool)
       and the background worker (job-result / BO / memory publishes), but NOT a connector's own
       worker: a bundle returns its note in the job envelope and core publishes it, so no
       connector process ever touches the note repo. Deliberately a different directory
       from the read replica: `git checkout -B note/<id>` switches a whole working tree. */ -}}
{{- define "chemclaw.noteRepoInit" -}}
{{- if .Values.knowledge.sync.enabled }}
- name: note-repo-init
  image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
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
- name: temporal-tls
  mountPath: {{ .Values.secrets.temporalTls.mountPath }}
  readOnly: true
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
