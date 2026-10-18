{{- /* The Cluster's name, which is also its Services' prefix (`<name>-rw`) and the server name its WAL
       is archived under. */ -}}
{{- define "chemclaw-postgres.name" -}}
{{- default .Release.Name .Values.name -}}
{{- end -}}

{{- /* The PromQL selector for this cluster's instance pods (`<name>-<serial>`, as the operator names
       them), so a restored cluster's alerts never read the original's pods or the reverse. */ -}}
{{- define "chemclaw-postgres.instances" -}}
namespace="{{ .Release.Namespace }}",pod=~"{{ include "chemclaw-postgres.name" . }}-[0-9]+"
{{- end -}}

{{- define "chemclaw-postgres.labels" -}}
app.kubernetes.io/name: chemclaw-postgres
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- /* Kept on uninstall, rollback and when a template stops rendering: the primary's volumes are the
       only copy of the data since the last archived segment, and a Helm delete is not a decision to
       lose them. Removing a database is a deliberate `oc delete`. */ -}}
{{- define "chemclaw-postgres.keep" -}}
helm.sh/resource-policy: keep
{{- end -}}

{{- /* Every refusal the chart makes, in one place, so a render either names what is missing or is whole. */ -}}
{{- define "chemclaw-postgres.validate" -}}
{{- $b := .Values.backup }}
{{- if not .Values.imageName }}
{{- fail "imageName is not set: the operator's stock images carry no pgvector, and the migrations run `CREATE EXTENSION vector` (bit(2048) HNSW needs pgvector 0.7 or later). Name a PostgreSQL 16 image that has it." }}
{{- end }}
{{- if not (hasPrefix "s3://" ($b.destinationPath | default "")) }}
{{- fail "backup.destinationPath must be an s3:// URL (a bucket and prefix of an S3 or S3-compatible store). Another provider's ObjectStore is a site edit of this template." }}
{{- end }}
{{- if or (le (int .Values.targets.rpoSeconds) 0) (le (int .Values.targets.rtoSeconds) 0) }}
{{- fail "targets.rpoSeconds and targets.rtoSeconds are not set. They are the recovery point and time this site accepts for a lost cluster; the chart ships no default because a default is a guarantee nobody agreed to. Set both, in seconds." }}
{{- end }}
{{- if hasKey (.Values.parameters | default dict) "archive_timeout" }}
{{- fail "parameters.archive_timeout is refused: set archiveTimeoutSeconds, which the RPO check reads. A second place to say the same thing is a way for the target to be met on paper." }}
{{- end }}
{{- if not (regexMatch "^[1-9][0-9]*$" (toString .Values.archiveTimeoutSeconds)) }}
{{- fail (printf "archiveTimeoutSeconds %v must be a whole number of seconds, at least 1: it renders archive_timeout, and 0 means WAL is never switched on a timer, so the RPO check below would be met on paper." .Values.archiveTimeoutSeconds) }}
{{- end }}
{{- if lt (int .Values.targets.rpoSeconds) (int .Values.archiveTimeoutSeconds) }}
{{- fail (printf "targets.rpoSeconds (%v) is below archiveTimeoutSeconds (%v): WAL reaches the object store at most that often when the database is quiet, so a smaller target cannot be met. Raise the target or lower archiveTimeoutSeconds." .Values.targets.rpoSeconds .Values.archiveTimeoutSeconds) }}
{{- end }}
{{- if not (regexMatch "^[1-9][0-9]*[dwm]$" ($b.retentionPolicy | default "")) }}
{{- fail (printf "backup.retentionPolicy %q is not a Barman retention (a positive count and d, w or m: 30d, 4w, 3m)." $b.retentionPolicy) }}
{{- end }}
{{- if not (has ($b.encryption | default "") (list "" "AES256" "aws:kms")) }}
{{- fail (printf "backup.encryption %q must be empty, AES256 or aws:kms (server-side encryption of the archive and the base backups)." $b.encryption) }}
{{- end }}
{{- if lt (int .Values.instances) 1 }}
{{- fail "instances must be at least 1." }}
{{- end }}
{{- if and .Values.temporalDatabases.enabled (not .Values.temporalDatabases.passwordSecret) }}
{{- fail "temporalDatabases.enabled is true but passwordSecret is empty: the Temporal role's login comes from a kubernetes.io/basic-auth Secret (username and password) the operator creates." }}
{{- end }}
{{- if and .Values.tls.serverTLSSecret (not .Values.tls.serverCASecret) }}
{{- fail "tls.serverTLSSecret needs tls.serverCASecret beside it: the operator's admission webhook refuses a server certificate without the CA that signs it." }}
{{- end }}
{{- if and .Values.import.enabled .Values.recovery.enabled }}
{{- fail "import.enabled and recovery.enabled are both true: a cluster is bootstrapped from a live database or from a backup, not both." }}
{{- end }}
{{- if .Values.import.enabled }}
{{- if or (not .Values.import.source.host) (not .Values.import.source.user) (not .Values.import.source.passwordSecret.name) }}
{{- fail "import.enabled needs import.source.host, user and passwordSecret.name: where the existing database is and how to log in to it." }}
{{- end }}
{{- if not (has .Values.import.type (list "microservice" "monolith")) }}
{{- fail (printf "import.type %q must be microservice (one database) or monolith (several, and roles)." .Values.import.type) }}
{{- end }}
{{- if and (eq .Values.import.type "microservice") .Values.import.roles }}
{{- fail "import.roles is refused with import.type microservice (the operator's admission webhook rejects it): use import.type monolith to carry roles over, or create the roles in the new cluster." }}
{{- end }}
{{- if and (eq .Values.import.type "microservice") (gt (len (.Values.import.databases | default (list))) 1) }}
{{- fail "import.databases names more than one database with import.type microservice (the operator's admission webhook rejects it): a microservice import copies exactly one. Use import.type monolith for several." }}
{{- end }}
{{- if and (eq .Values.import.type "monolith") (not .Values.import.databases) }}
{{- fail "import.type monolith needs import.databases: name every database to carry over (the application's, and Temporal's if they live there)." }}
{{- end }}
{{- end }}
{{- if .Values.monitoring.alerts.enabled }}
{{- range $key := list "archiveFailingSeconds" "archiveBacklogSeconds" "baseBackupMaxAgeSeconds" "firstBackupGraceSeconds" "metricsAbsentSeconds" }}
{{- if not (regexMatch "^[1-9][0-9]*$" (toString (index $.Values.monitoring.alerts $key))) }}
{{- fail (printf "monitoring.alerts.%s %v must be a whole number of seconds, at least 1: it is how long a backup alert waits, and 0 or less would page on the first scrape or never." $key (index $.Values.monitoring.alerts $key)) }}
{{- end }}
{{- end }}
{{- end }}
{{- if and .Values.recovery.rehearsal (not .Values.recovery.enabled) }}
{{- fail "recovery.rehearsal is true but recovery.enabled is not: a rehearsal is a restore that does not archive." }}
{{- end }}
{{- if .Values.recovery.enabled }}
{{- if not .Values.recovery.sourceClusterName }}
{{- fail "recovery.enabled needs recovery.sourceClusterName: the name of the cluster whose archive to restore." }}
{{- end }}
{{- if eq .Values.recovery.sourceClusterName (include "chemclaw-postgres.name" .) }}
{{- fail (printf "recovery.sourceClusterName is %q, this cluster's own name: a restored cluster archives under its name, so it would write into the archive it was restored from. Give this release a different `name`." .Values.recovery.sourceClusterName) }}
{{- end }}
{{- end }}
{{- end -}}
