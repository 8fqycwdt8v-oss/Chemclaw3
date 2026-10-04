# Operating a running deployment

Routine operation of a ChemClaw release on OpenShift: what healthy looks like, what to check on a
cadence, what runs on its own, and the admin tasks an operator does by hand. Symptom-first
diagnosis is [`troubleshooting.md`](troubleshooting.md); the procedures this page points at live in
[`runbook.md`](runbook.md) and [`deploy/README.md`](../../deploy/README.md).

Everything configurable is a field of the one `Settings` object (`src/chemclaw/core/config/`),
set as `CHEMCLAW_<FIELD>` through the chart's `config:` block (a ConfigMap) or `secrets.*` (a
Secret). Defaults quoted below are the code's; the chart overrides some of them, and
`deploy/helm/chemclaw/values.yaml` is where to read what a release actually ships.

---

## 1. The processes

One image, dispatched by `deploy/entrypoint.sh` on the component name. Deployment names are fixed
by `chemclaw.name` (`chemclaw`), not by the Helm release name.

| Role | Deployment | Probes (port) | Task queue |
| --- | --- | --- | --- |
| Front door | `chemclaw-service` | `/healthz`, `/readyz`, `/metrics` on `service.port` (8080) | — (starts workflows) |
| Background worker | `chemclaw-background-worker` | `/healthz`, `/readyz`, `/metrics` on `workerMetricsPort` (9000) | `background-jobs` |
| Connector server | `chemclaw-connector-<name>` | `/healthz`, `/livez`, `/metrics` on `connectorPort` (no `/readyz`; startup and readiness use `/healthz`, liveness `/livez`) | — (serves `/mcp`) |
| Connector worker | `chemclaw-connector-worker-<name>` | as background worker (9000) | `connector-<name>` |
| Interactive worker | `chemclaw-interactive-worker-<name>` | as background worker (9000) | `connector-<name>-interactive` |
| Read-only MCP face | `chemclaw-mcp-face` (off by default) | `/healthz` (startup, readiness), `/livez` (liveness), `/metrics` | — |

Hook Jobs: `chemclaw-migrate` (`pre-install,pre-upgrade,pre-rollback`), `chemclaw-convert`
(`post-upgrade`), `chemclaw-schedules` (`post-install,post-upgrade`).

Containers beside the app on the front door, mcp-face and background worker pods: `note-repo-init`
and `knowledge-sync-init` (init containers) and the `knowledge-sync` sidecar, all running
`deploy/knowledge-sync.sh` (see §4.3).

---

## 2. What a healthy system looks like

### 2.1 Probes

```bash
# Front door (any pod)
kubectl -n <ns> exec deploy/chemclaw-service -- curl -s localhost:8080/readyz
#   {"status":"ready","connectors_unhealthy":0}

# A worker
kubectl -n <ns> port-forward deploy/chemclaw-background-worker 9000:9000
curl -s localhost:9000/readyz        # 200 = worker running AND broker heard from recently
```

| Endpoint | Healthy | Unhealthy reads |
| --- | --- | --- |
| front door `/readyz` | 200, `"status":"ready"` | 503 `"database unreachable"` or `"schema behind image"` (session store `postgres` only). `connectors_unhealthy` is reported, never gating. |
| worker `/readyz` | 200 | 503: worker not running, or no broker contact for `jobs_in_flight_refresh_seconds` × 3 (90 s at the default 30 s). Not a liveness signal. |
| `/healthz` (all) | 200 `{"status":"ok"}` | No answer = wedged event loop; the kubelet restarts the pod (on a connector server or the MCP face, `/livez` is the route it restarts on and `/healthz` only takes the pod out of its Service). |
| `GET /schedules` (front door, authenticated) | every planned Schedule listed with `last_outcome` `COMPLETED` | a `note` saying the Schedule does not exist, `last_outcome` `FAILED`/`TIMED_OUT`, climbing `skipped_overlap` |

`/schedules` returns one `ScheduleHealth` per planned Schedule (`src/chemclaw/durable/schedules.py`):
`schedule_id`, `interval_seconds`, `paused`, `last_run`, `runs_total`, `skipped_overlap`,
`running_now`, `last_outcome`, `note`. `last_outcome` is the only field that separates a dead job
from a quiet one.

### 2.2 Dashboards

Five dashboards ship in `deploy/helm/chemclaw/dashboards/`, rendered into a ConfigMap by
`templates/configmap-dashboards.yaml`. Where they display is a choice — see
[runbook § (x-b)](runbook.md#x-b-make-the-monitoring-stack-actually-collect-this) step 3.

| Dashboard | Look here for |
| --- | --- |
| Chemclaw turns (`chemclaw-turns.json`) | turn rate, outcomes, p50/p95/p99, tokens, in-flight vs capacity |
| Chemclaw tools and model (`chemclaw-tools-and-model.json`) | p95 by tool, refusals by reason, model-call outcomes |
| Chemclaw durable jobs (`chemclaw-durable.json`) | job success ratio, p95 by connector, Temporal slots/pollers |
| Chemclaw front door (`chemclaw-front-door.json`) | per-route rate, error ratio, p95, pre-handler refusals |
| Chemclaw data and storage (`chemclaw-data.json`) | ingest lag, evidence per source, cache hit ratio, outbox, Postgres pool |

### 2.3 The signals that matter

Metrics are per process (one registry per pod), declared in `src/chemclaw/core/metrics.py`. A
counter at zero on one pod may be counting on another: a job launched from the front door counts
there; its activity counts on the worker.

| Question | Healthy reading | Series |
| --- | --- | --- |
| Is anything gone? | every target `up` | `up` (alert `ChemclawTargetDown`) |
| Are turns finishing? | `errored`/`timed_out` a small fraction | `chemclaw_turns_finished_total{outcome}` |
| Is the front door full? | ratio below 0.9 | `chemclaw_turns_in_flight` / `chemclaw_turn_capacity` |
| Is load being shed? | flat | `chemclaw_turns_shed_total`, `chemclaw_turns_refused_actor_cap_total` |
| Is the model gateway healthy? | `outcome="ok"` dominant | `chemclaw_model_calls_total{outcome}`, `chemclaw_model_call_duration_seconds` |
| Are tools failing? | low `outcome="error"` per tool | `chemclaw_tool_calls_total{tool,outcome}` |
| Are connectors up? | 0 | `chemclaw_connectors_unhealthy`, `chemclaw_connectors_unreachable_total{connector}` |
| Are durable jobs succeeding? | few `failed` | `chemclaw_jobs_finished_total{connector,outcome}`, `chemclaw_activity_failures_total` |
| Is the database keeping up? | `requests_waiting` 0 | `chemclaw_pg_pool_available`, `chemclaw_pg_pool_requests_waiting` |
| Is the audit trail complete? | flat | `chemclaw_audit_sink_failures_total`, `chemclaw_audit_events_shed_total` |
| Is something quietly degraded? | flat | `chemclaw_degraded_total{subsystem}` |
| Is ingest current? | below your threshold | `chemclaw_ingest_cursor_lag_seconds{source}` |
| Is the knowledge tree current? | below your threshold, never `-1` | `chemclaw_knowledge_sync_age_seconds` |
| Is spend in line? | under `monitoring.alerts.tokensPerHourWarning` | `chemclaw_tokens_total{profile}` |
| Is egress clean? | 0 refusals, guards armed (1) | `chemclaw_egress_refused_total`, `chemclaw_egress_guard_armed`, `chemclaw_egress_preload_armed` |

Every alert in `templates/prometheusrule.yaml` carries a `runbook_url` to its own entry under
[runbook § (x-c)](runbook.md#x-c-when-an-alert-fires). On a stock OpenShift cluster none of this is
collected until user-workload monitoring is switched on — do
[runbook § (x-b)](runbook.md#x-b-make-the-monitoring-stack-actually-collect-this) first.

---

## 3. Routine checks

### Daily

| Check | How |
| --- | --- |
| Nothing firing that nobody owns | Alertmanager / console → Observe → Alerting, filter `namespace="<ns>"` |
| Every pod scraped | `absent(up{namespace="<ns>"})` empty; console → Observe → Targets all `Up` |
| Turn outcomes | `sum by (outcome) (increase(chemclaw_turns_finished_total[24h]))` |
| Shedding | `sum(increase(chemclaw_turns_shed_total[24h]))` (a shed is an HTTP 200 with an `at_capacity` SSE error — only this counter sees it) |
| Schedules ran | `curl -s -H "Authorization: Bearer $TOKEN" https://<host>/schedules \| jq '.[] \| {schedule_id,last_run,last_outcome,note}'` |
| Durable failures | `sum by (connector, outcome) (increase(chemclaw_jobs_finished_total[24h]))` |
| Silent degradations | `sum by (subsystem) (increase(chemclaw_degraded_total[24h])) > 0` |
| Crash loops | `kubectl -n <ns> get pods` — restarts column; `kubectl -n <ns> get events --field-selector type=Warning` |

### Weekly

| Check | How |
| --- | --- |
| Latency trend | `histogram_quantile(0.95, sum by (le) (rate(chemclaw_turn_duration_seconds_bucket[7d])))` and the same per tool on `chemclaw_tool_duration_seconds_bucket` |
| Token burn by profile | `sum by (profile) (increase(chemclaw_tokens_total[7d]))` against the gateway's own invoice |
| Caps biting | `sum by (outcome) (increase(chemclaw_turns_finished_total{outcome=~"spend_capped\|loop_capped"}[7d]))` — [runbook `ChemclawTurnsHittingACap`](runbook.md#chemclawturnshittingacap) |
| Budget near misses | `increase(chemclaw_budget_warnings_total[7d])`, then the `budget … spent` WARNING lines for the scope |
| Auth refusals | `sum by (reason) (increase(chemclaw_auth_failures_total[7d]))` |
| Group-claim overage | `increase(chemclaw_group_claim_overage_total[7d])` — users whose group entitlements cannot be read |
| Cache effectiveness | `sum by (outcome) (increase(chemclaw_calc_cache_total[7d]))` |
| Ingest health | `sum by (source, outcome) (increase(chemclaw_ingest_records_total[7d]))`; re-ingest rejects per [runbook § (v)](runbook.md#v-re-ingest-a-rejected-eln-entry-after-fixing-the-source-record) |
| Result outbox | `chemclaw_outbox_pending`, `chemclaw_outbox_dead_lettered` per sink (only with `CHEMCLAW_RESULT_SINKS` set) |
| Schedule fit | `skipped_overlap` rising on `/schedules` = the job no longer fits its interval |

### Monthly

| Check | How |
| --- | --- |
| Table growth | `topk(10, chemclaw_table_bytes)` — published by every retention pass; absent means the sweep is not running |
| Pool headroom | `max(chemclaw_pg_pool_size / chemclaw_pg_pool_max_size)`; compare the fleet sum with `postgres.maxConnections` |
| Fleet ceilings still true | `sum(chemclaw_turn_capacity)` vs `chemclaw_fleet_turn_ceiling`; HPA `maxReplicas` vs `CHEMCLAW_SERVICE_FLEET_MAX_CONCURRENT_TURNS` |
| Restore drill | restore Postgres to a scratch instance and run `make db-migrate` against it ([runbook § (xiii)](runbook.md#xiii-restore-a-store--and-what-a-restore-does-to-the-audit-trail)) |
| Image freshness / CVEs | rebuild and redeploy by digest ([runbook § (xiv)](runbook.md#xiv-cut-a-release-pin-the-image-to-bytes)) |
| Role set still matches the directory | `CHEMCLAW_ENTRA_PRIVILEGED_ROLES`, `CHEMCLAW_TOOL_ROLE_GATES`, `CHEMCLAW_SKILL_ROLE_GATES` against Entra app roles ([runbook § (xv)](runbook.md#xv-onboard-entitle-and-offboard-a-person)) |
| Egress allowlist is minimal | `CHEMCLAW_EGRESS_ALLOW` and `networkPolicy.egressDestinations` — every entry still needed |

---

## 4. What runs on its own

### 4.1 Temporal Schedules

Created and reconciled by `make schedules-apply` (`python -m chemclaw.cli.schedules`), which the
chart runs as the `chemclaw-schedules` hook Job after every install and upgrade. The plan is
`planned_schedules()` in `src/chemclaw/durable/schedules.py`: a Schedule exists only where its
condition holds, and the applier **deletes** any Schedule in `OWNED_SCHEDULE_IDS` that is no longer
planned. All fire on `background-jobs` with overlap policy SKIP, a deterministic per-id phase offset
(`schedule_jitter_fraction`, 0.2 of the interval) and a per-run ceiling of
`schedule_run_timeout_seconds` (86 400 s).

| Schedule id | Workflow | Planned when | Cadence setting (default) |
| --- | --- | --- | --- |
| `eln-sync` | `ElnSyncWorkflow` | an ingest source is enabled in `CHEMCLAW_DATA_SOURCES` | `eln_sync_schedule_minutes` (60) |
| `eval-drift` | `EvalDriftWorkflow` | `eval_drift_enabled` | `eval_drift_schedule_minutes` (1440) |
| `note-reindex` | `NoteReindexWorkflow` | a hybrid retrieval leg reads the index (`note_reindex_effective`) | `note_reindex_schedule_minutes` (60) |
| `document-sync` | `DocumentShareSyncWorkflow` | a document share source is enabled | `document_sync_schedule_minutes` (360) |
| `reaction-labels` | `ReactionLabelWorkflow` | an ingest source or a `corpus:` binding exists | `label_sync_schedule_minutes` (60) |
| `reaction-corpus` | `ReactionCorpusWorkflow` | a source declares a `corpus:` binding | `corpus_sync_schedule_minutes` (1440) |
| `commitment-mirror` | `CommitmentSyncWorkflow` | a source declares a `commitments:` half | `commitment_sync_schedule_minutes` (1440) |
| `digest` | `DigestWorkflow` | `digest_enabled` | `digest_schedule_minutes` (1440) |
| `agent-check-in` | `CheckInWorkflow` | `check_in_enabled` | `check_in_schedule_minutes` (1440) |
| `retention` | `RetentionWorkflow` | `retention_enabled` **and** at least one `retention_*_days` > 0 | `retention_schedule_minutes` (1440) |
| `exhibit-pushes` | `ExhibitPushPruneWorkflow` | `session_store = postgres` | every `exhibit_push_retention_hours` (24 h) |
| `artifact-eviction` | `ArtifactEvictionWorkflow` | `artifact_store_max_bytes` or `artifact_evict_idle_days` set | `artifact_eviction_schedule_minutes` (1440) |
| `orphaned-waits` | `OrphanedWaitsWorkflow` | always | `awaiting_orphan_sweep_minutes` (60) |
| `result-publish` | `PublishResultsWorkflow` | a sink is enabled in `CHEMCLAW_RESULT_SINKS` | `result_publish_schedule_minutes` (15) |
| `observations` | `ObservationSynthesisWorkflow` | `observations_enabled` | `observation_schedule_minutes` (1440) |

Knowledge mining (campaign, playbook, optimization synthesis) has **no** Schedule; start it on
demand with `make synthesize KIND=…`.

Do not run `make schedules-apply` by hand against a shared broker with a different config than the
release: it prunes what *its* config does not plan. Each release needs its own
`temporal.namespace` (the chart refuses to render without one).

### 4.2 Retention

The `retention` Schedule sweeps the tables named by the `retention_*_days` windows
(`retention_session_events_days`, `retention_session_messages_days`,
`retention_session_exhibits_days`, `retention_tool_results_days`,
`retention_result_publications_days`, `retention_checkpoints_days`, all 0 = keep). The chart sets
them from `retention.windows` and derives `CHEMCLAW_RETENTION_ENABLED`; it refuses to render unless
`retention.windows` or `retention.unboundedGrowthAccepted: true` is stated. `audit_events` is never
pruned. A sweep that has stopped running raises `ChemclawRetentionNotSweeping` (absence of
`chemclaw_table_bytes`).

Uploaded attachments (`session_attachments`) have no window of their own: they are swept on
`retention_session_messages_days`, dated by upload, and go with their session on a delete or an
erasure. Each session holds at most `CHEMCLAW_ATTACHMENT_MAX_PER_SESSION` files and
`CHEMCLAW_ATTACHMENT_STORE_MAX_BYTES` of parsed text; past either the oldest file's text is cleared
and its name kept, so the agent can say it was dropped. What the table costs is
`SELECT pg_size_pretty(pg_total_relation_size('session_attachments'))` on the session database.

### 4.3 Knowledge sync

`deploy/knowledge-sync.sh` keeps each pod's copy of the knowledge repo current:

| Container | Mode | Does |
| --- | --- | --- |
| `note-repo-init` (init) | `checkout` | clones `knowledge.sync.repoUrl` on the base branch into `knowledge.noteRepoPath` (the note writer's clone) |
| `knowledge-sync-init` (init) | `once` | refreshes and publishes before the app starts; fails the pod rather than serve an empty tree |
| `knowledge-sync` (sidecar) | `loop` | refreshes every `knowledge.sync.intervalSeconds` (300); a failed refresh logs `WARNING refresh failed; serving the previous snapshot` and keeps going |
| — (sidecar liveness) | `staleness <s>` | fails when the last successful refresh is older than the budget |

The repo credential is `secrets.keys.knowledgeRepoToken`, handed to git through the image's
`chemclaw-git-askpass` helper — by `knowledge-sync.sh` in the sync containers and by
`deploy/entrypoint.sh` in the front door and background worker, whose note writer pushes. Required
only once `knowledge.sync.repoUrl` is set. Freshness is `chemclaw_knowledge_sync_age_seconds` (`-1` = the tree holds no note); its
alert is off until `monitoring.alerts.knowledgeCorpusStaleSeconds` is non-zero.

### 4.4 Hook Jobs on every release

| Order | Job | Runs | On failure |
| --- | --- | --- | --- |
| 1 | `chemclaw-migrate` (`pre-install,pre-upgrade,pre-rollback`) | migrations, store setup, grants | release blocked, nothing half-applied; [runbook § (xi)](runbook.md#xi-a-migration-that-will-not-apply-and-a-release-stuck-in-pending-upgrade) |
| 2 | Deployments roll | | |
| 3 | `chemclaw-convert` (`post-upgrade`) | stored-message conversion (`chemclaw.agent.message_migration`) | not reversible by rollback; never deploy with `--atomic` |
| 4 | `chemclaw-schedules` (`post-install,post-upgrade`) | `apply_schedules` | Schedules stay as they were; re-run the upgrade |

---

## 5. Routine admin tasks

### 5.1 People

Identity is Entra only; there is no user table. Onboard = assign the app role (or group) in
Entra, nothing to restart. Revoke = remove it; takes effect within the tenant's token lifetime.
Erase = `make user-erase ACTOR=<oid>` (dry run), then `APPLY=1`. Full procedure:
[runbook § (xv)](runbook.md#xv-onboard-entitle-and-offboard-a-person).

`CHEMCLAW_ENTRA_PRIVILEGED_ROLES` ships empty, which refuses every expensive job to everyone —
see [deploy/README § "The setting that does not block boot"](../../deploy/README.md#the-setting-that-does-not-block-boot-and-closes-every-expensive-job).

### 5.2 Capability and data

| Task | Procedure |
| --- | --- |
| Add a skill | [runbook § (i)](runbook.md#i-add-a-skill); validate with `make skill-validate` |
| Add/repoint a database | [runbook § (ii)](runbook.md#ii-add-or-repoint-a-database) |
| Add/switch a data source | [runbook § (iii)](runbook.md#iii-add--switch-a-data-source-an-eln-a-warehouse-a-retrieval-index); `make datasource-validate` |
| Add a connector (in-image) | [runbook § (iv)](runbook.md#iv-add-a-capability--a-tool-a-durable-job-and-their-skills-a-connector); `make connector-validate` |
| Attach a connector this image does not ship | [deploy/README § "Attaching a connector bundle"](../../deploy/README.md#attaching-a-connector-bundle-this-image-does-not-ship) |
| Add a profile / template | [runbook § (iv-b)](runbook.md#iv-b-add-a-specialized-agent-a-profile), [§ (iv-c)](runbook.md#iv-c-add-a-fixed-procedure-a-template) |
| Attach a results database | [runbook § (xvi)](runbook.md#xvi-attach-an-external-results-database); `make sink-schema` prints the DDL |
| First crawl of a document share | `make share-estimate SHARE=<source>`, then `make share-sync SHARE=<source>` |
| Rebuild the note index | `make reindex` (incremental) / `make reindex-full` (recovery) |

A change to the chart's `config:` block rolls every pod on `helm upgrade` (each Deployment carries a
`checksum/config` annotation over the rendered ConfigMap).

### 5.3 Rotating secrets

Secrets arrive as environment variables (`secretKeyRef` from `secrets.name`, default
`chemclaw-secrets`) or, for Temporal mTLS, as files from `secrets.temporalTls.secretName`. Neither is
checksummed into the pods, so **a rotated Secret takes effect only after the consuming pods
restart**:

```bash
kubectl -n <ns> rollout restart deploy -l app.kubernetes.io/instance=<release>
```

| Secret key (`values.yaml`) | Env var | Consumers to restart |
| --- | --- | --- |
| `secrets.keys.llmApiKey` | `CHEMCLAW_LLM_API_KEY` | every process that makes model calls: front door, mcp-face, background worker, connector workers |
| `secrets.keys.postgresDsn` | `CHEMCLAW_POSTGRES_DSN` | all Deployments (each holds a pool) |
| `secrets.optionalKeys.sessionStoreDsn` | `CHEMCLAW_SESSION_STORE_DSN` | all Deployments |
| `secrets.migrationKeys.postgresMigrationDsn` | `CHEMCLAW_POSTGRES_MIGRATION_DSN` | none — read by `chemclaw-migrate` on the next release |
| `secrets.keys.knowledgeRepoToken` | (the knowledge repo's token) | the `knowledge-sync` containers and the note writer's push (front door, background worker) — restart those pods |
| `secrets.optionalKeys.temporalApiKey` / `temporalTls` Secret | `CHEMCLAW_TEMPORAL_API_KEY` / TLS files | every worker, the front door, and KEDA's `TriggerAuthentication` reads the TLS Secret directly |
| connector bearer tokens (`secrets.optionalKeys.*Token`) | the manifest's `token_env` | **both sides**: the MCP server pods (`Chemclaw3-mcp`) and every core pod that dials them |
| `secrets.optionalKeys.llmFallbackApiKey` | `CHEMCLAW_LLM_FALLBACK_API_KEY` | as `llmApiKey` |
| `secrets.optionalKeys.framingEnvelopeSecret`, `mcpFaceToken`, `vectorStoreApiKey` | as named in `values.yaml` | front door / mcp-face |

A connector bearer has no overlap window: server and client hold one token each, so rotate both
Secrets, then restart the server pods and the core pods together. Calls fail with 401 (logged as
`connector … is unreachable`) for the gap.

### 5.4 Scaling

| What | Knob | Notes |
| --- | --- | --- |
| Front door | `service.autoscaling.{minReplicas,maxReplicas}` (2–6) | HPA on `chemclaw_turns_in_flight` at `occupancy.targetPercent` (60) of permits, CPU as fallback. The occupancy metric needs a custom-metrics API (prometheus-adapter/KEDA); without it `kubectl describe hpa chemclaw-service` shows `FailedGetPodsMetric`. Any replica can follow or stop a turn running on another (`GET …/turn/stream`, `POST …/turn/stop`): it asks the holding pod through Postgres, so scaling in or out does not cost a chemist the live view or the Stop button. |
| Cross-replica turn relay | `CHEMCLAW_SERVICE_TURN_RELAY_POLL_SECONDS` (0.25), `CHEMCLAW_SERVICE_TURN_RELAY_LEASE_SECONDS` (10) | how fast a Stop sent to another replica lands and how coarsely a turn followed from another replica arrives; a holding pod polls only while it holds a turn. A holder that does not answer within the lease is a 503 on the asking replica; `chemclaw_turn_relay_poll_failures_total` counts the holder's failed polls (D-2026-10-04-a-running-turn-is-reached-through-postgres-from-any-replica). |
| Front-door fleet ceiling | `CHEMCLAW_SERVICE_FLEET_MAX_CONCURRENT_TURNS` | must be ≥ `maxReplicas × CHEMCLAW_SERVICE_MAX_CONCURRENT_TURNS`, or every pod refuses to start. Raise with the gateway's throughput budget. |
| Per-process turns | `CHEMCLAW_SERVICE_MAX_CONCURRENT_TURNS` (12), `CHEMCLAW_SERVICE_MAX_CONCURRENT_TURNS_PER_ACTOR` | |
| Background worker | `workers.background.replicas` | **keep at 1**: the note-writer lock is host-local. Strategy `Recreate`. |
| Connector server / worker | `connectors.<name>.serverReplicas` / `workerReplicas` (fallback `replicas`) | |
| Activities per worker | `CHEMCLAW_WORKER_MAX_CONCURRENT_ACTIVITIES` (8) | multiplies into the calc backend's load; `Settings` checks the product against `CHEMCLAW_CALC_BACKEND_MAX_CONCURRENT_REQUESTS` when set |
| Interactive workers | `connectors.<name>.interactive.{replicas,maxReplicas,maxConcurrentActivities}` | autoscaled by KEDA on `connector-<name>-interactive` backlog when `keda.enabled` |
| Calculation backend | `Chemclaw3-mcp` `servers/calc` replicas | add replicas rather than raise per-pod ceilings |
| Postgres connections | `CHEMCLAW_PG_POOL_MAX_SIZE` and `postgres.maxConnections` together | `Settings` refuses a pair that disagrees |

### 5.5 Budgets and caps

| Bound | Setting (code default; chart) | Refusal |
| --- | --- | --- |
| Per session / per user turns and tokens | `CHEMCLAW_BUDGET_ENABLED` (false; chart `true`), `CHEMCLAW_BUDGET_MAX_TURNS_PER_SESSION` (100), `CHEMCLAW_BUDGET_MAX_TOKENS_PER_SESSION` (2 000 000), `CHEMCLAW_BUDGET_MAX_TURNS_PER_USER` (1000), `CHEMCLAW_BUDGET_MAX_TOKENS_PER_USER` (20 000 000) | 429 `session turn budget exhausted (N turns)` / `user token budget exhausted (N tokens)` |
| Budget window | `CHEMCLAW_BUDGET_WINDOW_HOURS` (24, rolling per principal) | |
| Early warning | `CHEMCLAW_BUDGET_WARN_FRACTION` (0.8) | WARNING `<scope> <unit> budget N% spent …`; `chemclaw_budget_warnings_total` |
| Per-turn spend | `CHEMCLAW_AGENT_MAX_TURN_BILLED_TOKENS` (chart 3 000 000) | SSE `spend_cap_reached` |
| Per-turn model calls | `CHEMCLAW_HARNESS_MAX_LOOP_ITERATIONS` (25) | SSE `loop_cap_reached` |
| Thread size | `CHEMCLAW_SESSION_MAX_THREAD_BYTES` (1.5 MiB) | 429 `This conversation has reached its size limit …` |
| Request rate | `CHEMCLAW_SERVICE_RATE_LIMIT_PER_MINUTE` / `_BURST` (chart 120 / 30) | 429 `too many requests` + `Retry-After` |

The durable half of the budget engages only with `CHEMCLAW_SESSION_STORE=postgres`; elsewhere the
counters are per pod. Tuning: [runbook § (viii)](runbook.md#viii-answer-is-prompt-caching-paying-off),
[§ (xii)](runbook.md#xii-a-caller-is-being-refused-429--413-or-should-be-and-is-not).

---

## 6. Backups and restore

| Store | Back up | Restore |
| --- | --- | --- |
| Postgres (audit trail, sessions, cache, job records, note index) | point-in-time, by your provider | restore → `make db-migrate` → `make db-grants` → record the discarded audit window. [runbook § (xiii)](runbook.md#xiii-restore-a-store--and-what-a-restore-does-to-the-audit-trail) |
| Temporal | its own persistence | in-flight runs are lost; finished results survive in `job_records` and the calculation cache |
| Knowledge git repo | any clone is a backup (every pod has one) | re-point `knowledge.sync.repoUrl` |

A logical restore without the `schema_migrations` ledger needs the pre-steps in
[runbook § "Replay the migrations"](runbook.md#replay-the-migrations-against-a-database-that-already-has-the-schema).

---

## 7. Upgrades

1. **Read the diff for workflow changes.** Anything touching a `@workflow.defn` body follows
   [`workflow-versioning.md`](workflow-versioning.md): gate with `workflow.patched()`, or pause the
   Schedules (`temporal schedule toggle --pause --schedule-id <id> -n <ns>`), drain open runs, deploy,
   resume.
2. **Deploy by digest** ([runbook § (xiv)](runbook.md#xiv-cut-a-release-pin-the-image-to-bytes)).
   `helm upgrade` runs the hooks in §4.4 order. Do not pass `--atomic`.
3. **Watch the migrate Job**: `kubectl -n <ns> logs job/chemclaw-migrate -f`. It fails fast on a
   lock it cannot take (`canceling statement due to lock timeout`) and is bounded by
   `migrateJob.activeDeadlineSeconds` (900 s).
4. **Drain is automatic and slow on purpose.** Front-door grace = `CHEMCLAW_SERVICE_TURN_TIMEOUT_SECONDS`
   + `service.drainSeconds` (600 + 15 s); workers = `CHEMCLAW_WORKER_GRACEFUL_SHUTDOWN_SECONDS` + 30 s
   (150 s). Workers log `worker.draining` / `worker.drained`.
5. **Verify**: `/readyz` 200 on every pod; `/schedules` shows the plan; no `migrate.database_ahead`
   WARNING (that line means the image is *behind* the schema — a rollback).

Rollback: [runbook § "Roll back a release"](runbook.md#roll-back-a-release). A release installed
before the ConfigMap became a tracked resource needs the one-time adoption in
[runbook § "helm upgrade refuses"](runbook.md#helm-upgrade-refuses-exists-and-cannot-be-imported-into-the-current-release).

---

## 8. Logs

| Aspect | Fact |
| --- | --- |
| Where | stderr of each container (`logging.basicConfig` default); `kubectl -n <ns> logs deploy/<name> [-c knowledge-sync]` |
| Level | `CHEMCLAW_LOG_LEVEL` (INFO); an invalid level refuses startup. DEBUG adds cache hit-vs-compute. |
| Text format | `CHEMCLAW_LOG_FORMAT`, default `%(asctime)s %(levelname)s %(name)s [%(correlation_id)s/%(session_id)s]: %(message)s` |
| JSON | `CHEMCLAW_LOG_JSON` (false in code, `true` in the chart): keys `time` (UTC ISO-8601), `level`, `logger`, `source`, `process`, `thread`, `message`, `correlation_id`, `actor`, `session_id`, plus `fields` (structured extras, including `fields.event`) and `exception` |
| Structured events | `fields.event` names the line: `http.request`, `turn.started`, `turn.finished`, `turn.interrupted`, `model.call_failed`, `activity.started`, `activity.finished`, `authz.refused`, `db.failed`, `db.slow`, `migrate.*`, `kg.write.*`, `worker.draining`, `worker.drained`, `publish.attempt_failed`, … (`log_event` call sites in `src/`) |
| Access log | one `http.request` line per request: `METHOD /route-template STATUS in N.Nms`, with actor and session where known |
| Connector pods | `chemclaw-connector-<name>` lines carry the caller's `X-Chemclaw-Correlation-Id`/`-Session`/`-Actor` as `correlation_id`/`session_id`/`actor` — the `connector <name> request: …` line and every line inside a tool; header-supplied, so log attribution only, and swept by the redaction filter |
| Redaction | `SecretRedactingFilter` scrubs DSN passwords, bearer tokens and API keys from every line; audit-trail tool arguments are deliberately *not* redacted (`SECURITY.md`) |
| Tool audit | every tool call is a row in `audit_events` (keyed on `correlation_id`) when the Postgres sink is on, and a log line regardless |
| Traces | optional: `CHEMCLAW_OTEL_ENABLED` + `CHEMCLAW_OTEL_ENDPOINT`; `CHEMCLAW_OTEL_LLM_SPANS` adds a span per model call; content stays out unless `CHEMCLAW_OTEL_INCLUDE_SENSITIVE_DATA` |

Following one request across processes: [troubleshooting § 1](troubleshooting.md#1-reading-logs-and-following-one-request).

---

## 9. Operator commands

### Make targets (operations)

`make help` lists all. The ones an operator uses against a deployment:

| Target | Does |
| --- | --- |
| `make db-migrate` | apply `infra/sql` migrations, then the stored-message conversion |
| `make db-grants` | reconcile the runtime role's privileges (after `db-migrate`, every deploy) |
| `make schedules-apply` | create/update/prune the Temporal Schedules (the chart's hook does this) |
| `make explain SESSION=<id>` | reconstruct why a session's tools ran |
| `make user-erase ACTOR=<oid> [APPLY=1]` | offboard a person's conversational data; dry run by default |
| `make synthesize KIND=campaign\|playbook\|optimization\|observation-promotion [FRESH=1]` | start a memory-synthesis job on demand |
| `make reindex` / `make reindex-full` | rebuild the derived note index |
| `make share-estimate SHARE=<source>` / `make share-sync SHARE=<source>` | cost / crawl a mounted document share |
| `make rekey-compounds [APPLY=1 [DISPOSE=1]]` | carry compound notes and fingerprints across a standardization bump |
| `make sink-schema` | print the DDL and registry seed a results database needs |
| `make kg-validate` | validate the knowledge graph |
| `make skill-validate`, `connector-validate`, `datasource-validate`, `sink-validate`, `channel-validate`, `template-validate`, `eln-validate`, `prose-validate` | validate declarations against the live surface |
| `make helm-validate` | render the chart and schema-check it (needs `helm`, `kubeconform`, `promtool`) |
| `make eval-baseline-check` | score the eval case-set against `data/evals/baseline.json` |
| `make chat` | admin terminal chat (`--admin`, bypasses auth; needs a gateway) |
| `make kind-up` / `kind-smoke` / `kind-down` | the whole system on a local kind cluster |
| `make live-infra`, `live-up`, `live-status`, `live-down` | the local live lane |

### CLIs (`python -m chemclaw.cli.<name>`)

| Module | Does |
| --- | --- |
| `schedules` | apply the Temporal Schedules (`make schedules-apply`) |
| `explain` | reconstruct why a session's tool calls happened |
| `erase_actor` | offboard a person (`--finish <session-id>…` clears sessions a concurrent turn left orphaned) |
| `sync_share` | cost, then crawl, a mounted document share |
| `backfill_publications` | queue results computed before a results store was attached |
| `backfill_corpus` | write knowledge notes from a directory of existing documents |
| `rekey_compounds` / `rekey_campaigns` | carry records across a standardization or campaign-id change |
| `synthesize` | start a memory-synthesis job without a model in the loop |
| `sink_schema` | print the DDL a results database needs |
| `egress_preload` | print the egress posture the entrypoint arms the compiled guard with |
| `trajectory_census`, `distill`, `propose_profile` | mine stored sessions for skill/profile proposals (dry by default) |
| `refresh_baseline` | regenerate `data/evals/baseline.json` from a real scoring run |
| `soak_report` | fit the series of a soak record |
| `chat` | the admin REPL (`chemclaw --admin`) |
| `validate_*` | the validators behind the `*-validate` targets |
| `live_*`, `mock_llm`, `leak_probe`, `retrieval_arms`, `verifier_margin`, `hypothesis_recovery` | live-lane and measurement tools; not for production |

Module-level entry points outside `cli/`: `python -m chemclaw.core.migrate`,
`python -m chemclaw.core.grants`, `python -m chemclaw.agent.message_migration`,
`python -m chemclaw.retrieval.vector_index`.
