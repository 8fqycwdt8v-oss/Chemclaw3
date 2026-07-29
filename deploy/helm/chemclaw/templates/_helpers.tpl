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

{{- /* Init container: fill the knowledge tree BEFORE the app starts, so no pod ever serves a
       turn against an empty graph. Same image, so no second artifact to build or scan. */ -}}
{{- define "chemclaw.knowledgeInit" -}}
{{- if .Values.knowledge.sync.enabled }}
- name: knowledge-sync-init
  image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
  imagePullPolicy: {{ .Values.image.pullPolicy }}
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
