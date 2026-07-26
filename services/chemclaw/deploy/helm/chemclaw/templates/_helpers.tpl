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
