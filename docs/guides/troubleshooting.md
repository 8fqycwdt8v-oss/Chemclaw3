# Troubleshooting

Symptom first. Find what you see in the index, read the entry, follow the link for the full
procedure. Every quoted message is the literal text the code emits; `<…>` marks a value it fills
in. Routine operation and the meaning of each process are in [`operations.md`](operations.md);
alert-by-alert entries are in [runbook § (x-c)](runbook.md#x-c-when-an-alert-fires).

| You see | Go to |
| --- | --- |
| A pod in `CrashLoopBackOff`, a `ValidationError for Settings`, a `SECURITY:` line at boot | [§2](#2-a-pod-will-not-boot) |
| `helm template`/`upgrade` prints `Error: … execution error at (chemclaw/templates/…)` | [§3](#3-helm-refuses-to-render) |
| `chemclaw-migrate` failed, release `pending-upgrade`, `exists and cannot be imported` | [§4](#4-migrations-and-releases) |
| 401 / 403 / 413 / 429 / 503 from the front door | [§5](#5-the-front-door-refuses-a-request) |
| A chat never answers, `turn_timeout`, `at_capacity`, an empty answer | [§6](#6-turns-hang-time-out-or-answer-empty) |
| The stream stops mid-answer behind a proxy | [§7](#7-the-sse-stream-drops) |
| `llm_timeout`, `model.call_failed`, every turn `internal` | [§8](#8-model-gateway-errors) |
| A tool fails, `connector … is unreachable`, MCP 401/421/503 | [§9](#9-tool-calls-and-connectors) |
| Jobs never start or never finish, worker `/readyz` 503 | [§10](#10-temporal-workers-and-durable-jobs) |
| `[calc-at-capacity]`, calculations queueing | [§11](#11-calculation-capacity) |
| `server at capacity; retry shortly` with an idle database, `PoolTimeout` | [§12](#12-database-pool-saturation) |
| The agent does not know a note that was merged | [§13](#13-knowledge-corpus-stale) |
| `egress refused`, `chemclaw-netguard-preload:` | [§14](#14-egress-refused) |
| `budget exhausted`, `spend_cap_reached`, `loop_cap_reached` | [§15](#15-budget-and-spend-cap-refusals) |
| The UI shows `upstream unavailable`, a sign-in loop, or "budget exhausted" | [§16](#16-the-ui-cannot-reach-the-backend-or-sign-in-loops) |

---

## 1. Reading logs and following one request

**Where.** Every process logs to stderr; `kubectl -n <ns> logs deploy/<deployment> [-c <container>]`.
The chart sets `CHEMCLAW_LOG_JSON=true`: one JSON object per line with `time`, `level`, `logger`,
`source`, `message`, `correlation_id`, `actor`, `session_id`, and structured extras under
`fields` (`fields.event` is the line's name: `http.request`, `turn.finished`,
`model.call_failed`, `activity.finished`, …). Without JSON the default format is
`<time> <LEVEL> <logger> [<correlation_id>/<session_id>]: <message>`. More detail:
`CHEMCLAW_LOG_LEVEL=DEBUG` and roll the pod.

**The join key is the correlation id.**

| Hop | Where the id comes from | Where it lands |
| --- | --- | --- |
| Browser → UI BFF (`Chemclaw3_ui`) | the BFF mints one per request (32 hex) | BFF access line `correlation_id`; sent upstream as `X-Chemclaw-Correlation-Id` |
| BFF/ingress → front door | `_request_correlation_id` adopts an inbound `X-Chemclaw-Correlation-Id` matching `[A-Za-z0-9_-]{8,64}`, else mints a `uuid4().hex` | every log line's `correlation_id`; the response header `X-Chemclaw-Correlation-Id`; `audit_events.correlation_id`; a 500 body's `correlation_id`; a failed turn's SSE `error` event |
| Front door → connector / MCP server | `turn_headers()` sends `X-Chemclaw-Actor`, `X-Chemclaw-Session`, `X-Chemclaw-Correlation-Id`, `X-Chemclaw-Dry-Run` and W3C `traceparent` | `Chemclaw3-mcp` servers: `[correlation/session]` in their log format. In-repo connector servers: the `connector <name> request: path=… actor=… session=… dry_run=…` line (actor and session only) |
| Front door → Temporal → worker | carried in the job input (`ConnectorJobInput.correlation_id`) and re-bound by the worker interceptor | worker log lines' `correlation_id`; `job_records` |
| Watching an already-running turn | — | response header `X-Chemclaw-Turn-Correlation-Id` names the *turn's* id, distinct from the watch request's own |

**Follow one request:**

```bash
CID=<id from the UI error, the response header, or the SSE error event>
for d in chemclaw-service chemclaw-background-worker $(kubectl -n <ns> get deploy -o name | grep -E 'connector|interactive' | cut -d/ -f2); do
  kubectl -n <ns> logs deploy/$d --since=2h 2>/dev/null | grep -F "$CID" | sed "s/^/[$d] /"
done
```

```sql
-- every tool call of that turn, with outcome and latency
SELECT ts, actor, tool, outcome, latency_ms, left(detail, 200)
FROM audit_events WHERE correlation_id = '<CID>' ORDER BY ts;
```

`make explain SESSION=<session-id>` reconstructs why a session's tools ran. The Temporal UI (event
history of the workflow) is the record for a durable job. With `CHEMCLAW_OTEL_ENABLED`, the same
turn is one trace: `chemclaw.turn` → `chemclaw.tool` → the connector's spans.

On an in-repo connector pod (`chemclaw-connector-<name>`), lines emitted inside a tool carry
`correlation_id` `-`; join there by session id and time.

---

## 2. A pod will not boot

**Presents as** `CrashLoopBackOff`; the previous container log (`kubectl logs --previous`) ends in
one of the messages below. Settings are validated when `chemclaw.core.config` is imported, so a
`Settings` refusal is a `pydantic_core._pydantic_core.ValidationError: 1 validation error for
Settings … Value error, <message>` before anything else runs.

| Message (start) | Process | Cause → fix |
| --- | --- | --- |
| `SECURITY: entra_required is False but the service binds a non-loopback interface (<host>)` | front door | Set `CHEMCLAW_ENTRA_REQUIRED=true` with `CHEMCLAW_ENTRA_TENANT_ID` and `CHEMCLAW_ENTRA_AUDIENCE`; local dev only: bind loopback or `CHEMCLAW_SERVICE_ALLOW_INSECURE=true`. |
| `SECURITY: this Temporal worker would run with CHEMCLAW_ENTRA_REQUIRED=false` | every worker | Same posture for workers; local dev opt-out is `CHEMCLAW_WORKER_ALLOW_UNAUTHENTICATED=true`. |
| `SECURITY: this process makes model calls while CHEMCLAW_LLM_BASE_URL names a loopback address` | front door, mcp-face, workers, CLI | Point `CHEMCLAW_LLM_BASE_URL` at the gateway, or `CHEMCLAW_LLM_ALLOW_LOOPBACK_GATEWAY=true` for a sidecar/mock. |
| `the LLM gateway requires <fields> to be set — an empty base URL is not 'no destination'` | any | `CHEMCLAW_LLM_BASE_URL` (and model) blanked. |
| `SECURITY: a proxy is configured in this process's environment (<proxy>) and would carry traffic to …` | any | Undeclared `HTTP(S)_PROXY`/`ALL_PROXY`. Add the proxy host to `CHEMCLAW_EGRESS_ALLOW`, add destinations to `NO_PROXY`, or unset it. Under `entra_required` an ambient proxy is refused too (`… and entra_required=true — the deployment that believes it is in the enforced posture`); `NO_PROXY=*` or declare it. |
| `entra_audience must be set when entra_required` / `entra_tenant_id or entra_issuer must be set …` / `entra_tenant_id or entra_jwks_url must be set …` | any | Half-configured Entra. |
| `entra_expensive_actions needs entra_privileged_roles` | any | Set `CHEMCLAW_ENTRA_PRIVILEGED_ROLES` or drop `CHEMCLAW_ENTRA_EXPENSIVE_ACTIONS`. |
| `CHEMCLAW_ENTRA_CA_BUNDLE=<path> is not a usable CA bundle` | front door | File missing or no PEM inside; mount the tenant CA (`trustedCA.configMap`/`.secret` with `trustedCA.entra: true`, and check `trustedCA.key` names the key holding the PEM). |
| `entra_required=true with a non-loopback temporal_address (<addr>) and no temporal_tls_cert / temporal_tls_ca / temporal_api_key` | any | Enable `secrets.temporalTls` (Secret `chemclaw-temporal-tls` with `tls.crt`, `tls.key`, `ca.crt`) or set `CHEMCLAW_TEMPORAL_API_KEY`. |
| `MountVolume.SetUp failed … secret "chemclaw-temporal-tls" not found` (pod event) | any | `secrets.temporalTls.enabled: true` but the Secret is absent. |
| `entra_required=true with a non-loopback <DSN> and sslmode=<mode>` / `… that names no host and no sslmode` / `… is not a connection string libpq can parse` | any | Add `sslmode=require` (or `verify-full&sslrootcert=…`) and a host to each of `CHEMCLAW_POSTGRES_DSN`, `CHEMCLAW_POSTGRES_MIGRATION_DSN`, `CHEMCLAW_SESSION_STORE_DSN`. |
| `this deployment may admit <N> concurrent turns (<R> replicas × <W> uvicorn worker(s) × <C> per process) against …` | any | Raise `CHEMCLAW_SERVICE_FLEET_MAX_CONCURRENT_TURNS` (to what the gateway serves) or lower replicas / `CHEMCLAW_SERVICE_MAX_CONCURRENT_TURNS`. |
| `this deployment may open <N> Postgres connections on …` | any | Raise `CHEMCLAW_PG_FLEET_MAX_CONNECTIONS` with `postgres.maxConnections`, or lower `CHEMCLAW_PG_POOL_MAX_SIZE`. |
| `this deployment may dispatch <N> concurrent calculations …` | any | `CHEMCLAW_WORKER_MAX_CONCURRENT_ACTIVITIES` × calc workers exceeds `CHEMCLAW_CALC_BACKEND_MAX_CONCURRENT_REQUESTS`. |
| `service_uvicorn_workers>1 silently breaks five per-process guarantees` | front door | Keep one uvicorn worker per pod; scale with replicas. |
| `service_max_connections (<n>) is below what this process's own caps can occupy` | front door | Raise `CHEMCLAW_SERVICE_MAX_CONNECTIONS`. |
| `harness_autonomy='plan_only' with harness_enabled=False enforces nothing` | any | Set `CHEMCLAW_HARNESS_ENABLED=true`. |
| `budget_enabled=true with every cap at 0 (unlimited) guards nothing` | any | Set a `budget_max_*` cap or disable budgets. |
| `embedding_dim=<n> disagrees with the note_index vector column` | any | Revert `CHEMCLAW_EMBEDDING_DIM` or change schema and setting together. |
| `log_level=<x> is not a level logging accepts` | any | Fix `CHEMCLAW_LOG_LEVEL`. |
| `temporal_metrics_port=<n> is also worker_metrics_port` | workers | Give the SDK exporter its own port. |
| `… does not cover the job it bounds` / `… does not fit the ceiling that kills the run` / `… does not fit inside the budget it reports within` | any | A timeout was raised without the ceiling above it; the message names both settings. |
| `service_cors_origins may not contain '*'` | front door | Name origins explicitly. |
| `data source names must be unique in data_sources` | any | Duplicate in `CHEMCLAW_DATA_SOURCES`. |
| `unknown CHEMCLAW_COMPONENT=<x>` | any | Wrong component in a hand-written manifest. |

Not a boot failure, but often mistaken for one: **every expensive job is refused** and the pod is
healthy → `CHEMCLAW_ENTRA_PRIVILEGED_ROLES` is empty
([deploy/README](../../deploy/README.md#the-setting-that-does-not-block-boot-and-closes-every-expensive-job)).
A stale `CHEMCLAW_ENTRA_CLIENT_ID` in a ConfigMap is silently ignored (it is refused only in a
dotenv file).

**Deeper:** [runbook § "Exposing the front door"](runbook.md#exposing-the-front-door-the-two-settings-that-decide-whether-it-boots),
[deploy/README § "A non-production cluster that runs the enforced posture"](../../deploy/README.md#a-non-production-cluster-that-runs-the-enforced-posture).

---

## 3. Helm refuses to render

**Presents as** `Error: execution error at (chemclaw/templates/<file>:<line>): <message>` on
`helm template`, `helm upgrade` or `make helm-validate`. Nothing reaches the cluster.

| Message (start) | Fix |
| --- | --- |
| `networkPolicy: set exactly one of egressDestinations … or allowAnyDestination: true` | State the egress posture. |
| `networkPolicy.allowAnyDestination must be a boolean, not the string …` | Unquote the boolean. |
| `retention: set exactly one of retention.windows … or retention.unboundedGrowthAccepted: true` | State the retention posture. |
| `retention.windows: <key> is not a retention setting` | Use a `CHEMCLAW_RETENTION_*_DAYS` key. |
| `retention.windows: CHEMCLAW_RETENTION_ENABLED is derived from this block` | Remove it from the map. |
| `retention: this release states retention windows, which bound the conversation and trace tables and not artifact_blobs` | Add `retention.artifactStore` or `retention.artifactGrowthAccepted: true`. |
| `retention: this release states retention windows and must say how long artefacts are kept` | Set `CHEMCLAW_RETENTION_SESSION_EXHIBITS_DAYS` or `retention.exhibitsGrowthAccepted: true`. |
| `retention.*GrowthAccepted / unboundedGrowthAccepted must be a boolean, not the string …` | Unquote. |
| `retention.artifactStore: <key> is not an artifact-eviction bound` | Use `CHEMCLAW_ARTIFACT_STORE_MAX_BYTES` / `CHEMCLAW_ARTIFACT_EVICT_IDLE_DAYS`. |
| `temporal.namespace is not set, and this chart ships no default for it` | One namespace per release, registered on the broker. |
| `config.CHEMCLAW_TEMPORAL_NAMESPACE is derived from temporal.namespace` | Remove it from `config:`. |
| `config.CHEMCLAW_SESSION_STORE is <x>; a chart release must set "postgres"` | Set it to `postgres`. |
| `connectors: this release enables no bundle` | Enable at least one connector. |
| `<key> must be at least 1: it renders CHEMCLAW_SERVICE_FLEET_REPLICAS …` | Replica count 0 is not a scale-down; use `kubectl scale`. |
| `rollout.maxSurgePods must be a whole number of pods` / `… is <n>; a negative surge …` | Integer ≥ 0. |
| `monitoring.alertmanager.enabled is true but …` (receivers empty, defaultReceiver empty or undeclared, alerts not rendered) | Declare receivers and a default; enable `monitoring.alerts`. |
| `mcpFace.route.enabled is true but mcpFace.ingressNamespaces is empty` | Name the namespaces allowed in. |
| `workers.background.documentParseMemoryBytes must be a positive whole byte count` | Bytes, not `320Mi`. |
| `config.CHEMCLAW_<X> must be set: … is derived from it` (turn timeout, health timeout, graceful shutdown, calc timeouts, knowledge dir, max concurrent turns) | Restore the key in `config:`. |
| `connectors.<name>: server pods need serverReplicas or replicas` / `worker pods need workerReplicas or replicas` / `….interactive.replicas is required` / `….interactive.maxConcurrentActivities is required` / `….interactive.maxReplicas is required when keda.enabled` | Fill the connector entry. |
| `make helm-validate`: binary `not installed` | Install `helm`, `kubeconform`, `promtool` ([runbook](runbook.md#make-helm-validate-says-a-binary-is-not-installed---see-docsguidesrunbookmd)). |

---

## 4. Migrations and releases

| Presents as | Cause | Fix |
| --- | --- | --- |
| `chemclaw-migrate` fails after ~5 s: `canceling statement due to lock timeout` | another session holds a lock on the table | find it in `pg_stat_activity`; do **not** raise `CHEMCLAW_PG_MIGRATION_LOCK_TIMEOUT_SECONDS` |
| fails waiting on the advisory lock: `another migrator held the migration lock for the whole <n>s budget (CHEMCLAW_PG_MIGRATION_LOCK_WAIT_SECONDS)` | overlapping deploy, or a dead migrator (released on disconnect) | wait and re-run |
| `migration <file> was edited after being applied …; add a new migration file instead` | an applied file changed | revert the edit; add a new numbered file |
| `no <ledger file> in '<dir>' …` / `applied migrations: (none)` on a fresh DB | `CHEMCLAW_SQL_MIGRATIONS_DIR` resolves wrong (image path `/app/infra/sql`) | fix the path |
| release stuck `pending-upgrade` | hook Job retried to `migrateJob.activeDeadlineSeconds` | `kubectl logs job/chemclaw-migrate`, fix, then `helm rollback chemclaw` or upgrade again |
| `UPGRADE FAILED: … exists and cannot be imported into the current release` | ConfigMap/ServiceAccount created by the old chart's hooks | adopt them (two commands in the runbook) |
| front door `/readyz` 503 `"schema behind image"` | the image's newest migration is not applied | run the migrate Job; check it did not fail |
| WARNING `migrate.database_ahead` | image older than the schema (a rollback) | expected after rollback; see what each stranded migration costs |
| `permission denied for table <t>` after an upgrade or rollback | grants not reconciled | `make db-grants` from the running image |
| SQL `InvalidColumnReference` / `ON CONFLICT` failures after a rollback | a rollback-breaking migration | the per-migration table in the runbook |

**Deeper:** [runbook § (xi)](runbook.md#xi-a-migration-that-will-not-apply-and-a-release-stuck-in-pending-upgrade),
[§ "Roll back a release"](runbook.md#roll-back-a-release),
[§ "helm upgrade refuses"](runbook.md#helm-upgrade-refuses-exists-and-cannot-be-imported-into-the-current-release).

---

## 5. The front door refuses a request

| Status | Body / header | Cause | Diagnose → fix |
| --- | --- | --- | --- |
| 401 | `missing bearer token` | no `Authorization: Bearer` | log `request to <route> carried no bearer token` (INFO); `chemclaw_auth_failures_total{reason="missing"}`. Client/BFF not attaching the token. |
| 401 | `invalid or expired token` | audience, issuer, signature or expiry | log `token validation failed: <reason>` (INFO) names it; `reason="invalid"`. Check `CHEMCLAW_ENTRA_AUDIENCE` vs the token's `aud`, a v1 token (issuer `sts.windows.net`), clock skew. |
| 503 | `identity provider unavailable` | JWKS fetch failed | WARNING `identity provider unavailable: …`; `reason="provider_unavailable"`. Egress to the tenant, `CHEMCLAW_ENTRA_CA_BUNDLE`. |
| 503 | `service misconfigured; refusing to serve` | `entra_required` off and the request arrived over the network | WARNING `SECURITY: refusing a request that arrived on <addr> while entra_required is False`; `reason="network_exposed"`. Turn Entra on. |
| 403 | `only the session's owner may <act>` and other ownership/role sentences | authorization | `authz.refused` log event; `chemclaw_authz_refusals_total{resource}`. Role assignment ([runbook § (xv)](runbook.md#xv-onboard-entitle-and-offboard-a-person)). |
| 403 | `cancelling a durable job is an operator action …` | caller lacks a privileged role | `CHEMCLAW_ENTRA_PRIVILEGED_ROLES` |
| 413 | `{"detail":…}` before any log of the body | body over `CHEMCLAW_SERVICE_MAX_REQUEST_BYTES` | WARNING `refused a request body over the <n> byte limit with 413`; `chemclaw_requests_too_large_total`. Keep it above `CHEMCLAW_ATTACHMENT_MAX_BYTES`. |
| 429 + `Retry-After` | `too many requests` | per-principal rate limit | `chemclaw_requests_rate_limited_total`; `CHEMCLAW_SERVICE_RATE_LIMIT_PER_MINUTE` / `_BURST` (per process) |
| 429 + `Retry-After` | `too many concurrent turns for this user; wait for one to finish` | per-actor turn cap | `chemclaw_turns_refused_actor_cap_total` (not the rate-limit counter) |
| 429 | `<session\|user> <turn\|token> budget exhausted (<n> …)` / `This conversation has reached its size limit …` | budgets | [§15](#15-budget-and-spend-cap-refusals) |
| 429 | `too many concurrent event streams; close one and retry` | stream caps | `chemclaw_event_streams_rejected_total`; `CHEMCLAW_SERVICE_MAX_EVENT_STREAMS_PER_USER` / `_TOTAL` |
| 429 | `this server holds as many waiting messages as it can; retry shortly` | session queue full | `chemclaw_turn_queue_refused_total`; `CHEMCLAW_SERVICE_TURN_QUEUE_MAX` |
| 503 | `server at capacity; retry shortly` | Postgres checkout failed, or readiness just found it down | WARNING `shedding <METHOD> <path>: <exc>`; `chemclaw_db_unavailable_total` → [§12](#12-database-pool-saturation) |
| 503 | a sentence naming the subsystem (e.g. `the durable execution backend (Temporal) is unreachable …`) | Temporal or document index unreachable | WARNING `shedding …` with the cause; `chemclaw_subsystem_unavailable_total` → [§10](#10-temporal-workers-and-durable-jobs) |
| 503, no log line at all | — | uvicorn `--limit-concurrency` (`CHEMCLAW_SERVICE_MAX_CONNECTIONS`) | raise it only if SSE streams genuinely need it |
| 500 | `The request could not be completed due to an internal error.` + `correlation_id` | unhandled exception | ERROR `unhandled error serving <METHOD> <path> (correlation <id>)` with the traceback → [§1](#1-reading-logs-and-following-one-request) |

**Deeper:** [runbook § (xii)](runbook.md#xii-a-caller-is-being-refused-429--413-or-should-be-and-is-not).

---

## 6. Turns hang, time out or answer empty

A turn is `POST /sessions/{id}/messages`, answered as an SSE stream. Its status line is written
before admission, so **every turn-level refusal is an HTTP 200 with an `error` event**:
`{"type":"error","code":…,"retryable":…,"message":…,"correlation_id":…}`.

| `code` | Means | Look at |
| --- | --- | --- |
| `at_capacity` (retryable) | no admission permit within `CHEMCLAW_SERVICE_TURN_ADMISSION_TIMEOUT_SECONDS` (5 s) | `chemclaw_turns_shed_total`, `chemclaw_turns_in_flight` / `chemclaw_turn_capacity`, HPA → alerts `ChemclawTurnsShed`, `ChemclawFrontDoorAtItsPermitCeiling` |
| `turn_timeout` | the turn exceeded `CHEMCLAW_SERVICE_TURN_TIMEOUT_SECONDS` (600 s in the chart) | `chemclaw_turn_timeouts_total`; latency breakdown below |
| `llm_timeout` (retryable) | model provider stalled, timed out, 429 or 5xx | [§8](#8-model-gateway-errors) |
| `storage_unavailable` (retryable) | database unreachable mid-turn | [§12](#12-database-pool-saturation) |
| `context_length` | thread no longer fits the model window | start a new session; check `chemclaw_context_compactions_total`, `chemclaw_context_unreducible_total` |
| `spend_cap_reached`, `loop_cap_reached` | a cap stopped the turn | [§15](#15-budget-and-spend-cap-refusals) |
| `empty_answer` (retryable) | the turn finished with nothing to read | `chemclaw_turn_empty_answers_total`; `make explain SESSION=<id>` |
| `queue_cancelled` | a message waiting behind another turn was withdrawn | `GET /sessions/{id}/queue` |
| `internal` | unclassified failure | ERROR `turn failed for session <id>` / `turn stream failed for session <id>` with the traceback |

**Slow, not failing.** Break p95 down in this order (all `histogram_quantile(0.95, …_bucket)`):
`chemclaw_tool_duration_seconds` by `tool`, then `chemclaw_model_call_duration_seconds`, then
`chemclaw_evidence_source_seconds` by `source`. A turn waiting in a session's queue holds no
permit; one waiting on a queued tool shows the tool in flight
([§11](#11-calculation-capacity)).

**A turn that "never ends" in the UI** but finished server-side: `turn.finished` log line present
→ the stream dropped ([§7](#7-the-sse-stream-drops)); the answer is in the conversation history.

**Deeper:** runbook [`ChemclawTurnLatencyHigh`](runbook.md#chemclawturnlatencyhigh),
[`ChemclawTurnsTimingOut`](runbook.md#chemclawturnstimingout),
[`ChemclawTurnsAnsweringEmpty`](runbook.md#chemclawturnsansweringempty),
[`ChemclawTurnsShedHeavily`](runbook.md#chemclawturnsshedheavily).

---

## 7. The SSE stream drops

**Presents as** an answer that stops mid-stream, a browser reconnect loop, or a `stream_lagged`
error event; the server's `turn.finished` line shows the turn completed.

| Cause | Diagnose | Fix |
| --- | --- | --- |
| An intermediary reaps idle connections | the drop happens after a long tool call; front door sends a keepalive comment every `CHEMCLAW_SERVICE_SSE_PING_SECONDS` (15 s) | set every proxy/LB idle timeout above that (OpenShift router: `haproxy.router.openshift.io/timeout` on the Route) |
| A proxy buffers `text/event-stream` | nothing arrives until the turn ends | disable response buffering for the API path in the proxy/CDN |
| The client stopped reading | `chemclaw_turn_send_timeouts_total`, `chemclaw_event_stream_send_timeouts_total` | client side; the turn is detached and continues (`chemclaw_turns_detached_total`) |
| Reader too slow for the event rate | SSE `stream_lagged`; `chemclaw_turn_readers_lagged_total` | re-attach to `GET /sessions/{id}/turn/stream` |
| Pod replaced mid-turn | 410 `turn_interrupted` on re-attach; `chemclaw_turns_finished_total{outcome="interrupted"}` | expected on a rollout beyond the drain window; ask again |
| Request landed on another replica | attachments missing, re-attach 404 `no turn is running for this session` | the Route must keep its affinity cookie (`haproxy.router.openshift.io/disable_cookies: "false"`); a proxy in front must pass it |

The UI BFF has its own upstream pools; `no upstream connection available` (503) means its pool is
exhausted, not the backend ([§16](#16-the-ui-cannot-reach-the-backend-or-sign-in-loops)).

---

## 8. Model gateway errors

Every model call goes to `CHEMCLAW_LLM_BASE_URL` (OpenAI-compatible) with `CHEMCLAW_LLM_API_KEY`.

| Presents as | Cause | Diagnose → fix |
| --- | --- | --- |
| SSE `llm_timeout`; log `model.call_failed … the model gateway failed after <ms> ms (timeout\|rate_limited\|transport: <Exc>)` | gateway slow, overloaded, 429, 5xx, refused socket | `chemclaw_model_calls_total{outcome}`, `chemclaw_model_call_duration_seconds`. Retries: `CHEMCLAW_LLM_MAX_RETRIES` (3), per call `CHEMCLAW_LLM_TIMEOUT_SECONDS` (60). A fallback route (`CHEMCLAW_LLM_FALLBACK_BASE_URL`) shows on `chemclaw_model_fallbacks_total`. |
| every turn `internal`; `model.call_failed … (error: AuthenticationError)` | gateway answers 401/403 — bad or missing key | rotate `secrets.keys.llmApiKey` and restart ([operations § 5.3](operations.md#53-rotating-secrets)). There is no credential preflight. |
| `model.call_failed … (context_length: …)` | thread too long | user starts a new session |
| `egress refused: outbound connection to '<host>'` on the first turn | gateway host not derived into the allowlist | [§14](#14-egress-refused) |
| tokens metered as zero, `usage_unreadable: <n> usage content(s) carried no token count` | gateway changed its usage keys | alert `ChemclawUsageUnreadable`; budgets do not bind until fixed |

---

## 9. Tool calls and connectors

| Presents as | Cause | Diagnose → fix |
| --- | --- | --- |
| WARNING `connector <name> is unreachable (<leaf error>); its tools are unavailable this turn` | pod down, `/mcp` broken, or missing token (`MissingConnectorCredential: connector '<name>' needs a bearer token in $<VAR>, which is unset or empty`) | `chemclaw_connectors_unreachable_total{connector}`; `kubectl get pods -l app.kubernetes.io/component=connector-<name>` → [`ChemclawConnectorsDegradingTurns`](runbook.md#chemclawconnectorsdegradingturns) |
| `connector <name> was found unreachable within the last <n>s; not dialling it this turn` | recent failure is cached | wait, or fix the cause above |
| `chemclaw_connectors_unhealthy` > 0, `/readyz` `connectors_unhealthy` > 0 | `/healthz` sweep failed | it only sees `/healthz`; a green value does not prove `/mcp` works |
| MCP server log `server <name> refused an unauthenticated request to /mcp` (core sees 401) | token differs between core and server, or unset on the server (fails closed) | the manifest's `token_env` must hold the same value on both sides; restart both after rotation. The fleet Deployments read it from `chemclaw-secrets`, so a missing key shows as the server pod in `CreateContainerConfigError` rather than as a 401 |
| core sees **421** from a `Chemclaw3-mcp` server, `/healthz` green | DNS-rebinding guard: the `Host` the caller sends is not allowed | add the Service `name:port` to the server's `MCP_ALLOWED_HOSTS` |
| core sees **503** + `Retry-After` from an MCP server | its session ceiling (`MCP_MAX_SESSIONS`) is full | scale the server, or raise its memory and ceiling |
| MCP server `/healthz` 503 with a `reason` | corpus/backend failed to load | server log; the image's data |
| tool result `isError` with `Error executing tool <name>: …` | the server raised | `chemclaw_tool_calls_total{tool,outcome="error"}` → [`ChemclawToolCallsFailing`](runbook.md#chemclawtoolcallsfailing) |
| `chemclaw_tool_refusals_total{reason}` rising | governance (role gate, dry run, unapproved plan) — not a fault | role config |
| WARNING `queue unreachable; calling <connector>.<tool> directly` | Temporal unreachable from the front door | `chemclaw_queued_tool_calls_direct_total` → [§10](#10-temporal-workers-and-durable-jobs) |

---

## 10. Temporal workers and durable jobs

**Startup line to look for** (it names exactly what the worker connected to):
`background worker connected: address=<addr> namespace=<ns> queue=background-jobs …`,
`<name> connector worker connected: queue=connector-<name> …`,
`<name> interactive worker connected: queue=connector-<name>-interactive tools=… concurrency=<n>`.

| Presents as | Cause | Diagnose → fix |
| --- | --- | --- |
| worker exits 1 at start, crash-loops | broker unreachable at startup, TLS material wrong | previous log; `CHEMCLAW_TEMPORAL_ADDRESS`, `secrets.temporalTls` |
| worker `/readyz` 503, pod `Running` | no broker contact for 3 × `jobs_in_flight_refresh_seconds` | probe the broker before restarting anything; `chemclaw_degraded_total{subsystem="jobs_in_flight"}` |
| jobs accepted, never start | worker on a different namespace/queue, or no worker for that queue | compare startup line with `temporal.namespace`; `temporal task-queue describe --task-queue <q> --namespace <ns>` shows pollers; [`ChemclawWorkerNotPolling`](runbook.md#chemclawworkernotpolling) cannot see a queue nobody polls |
| no `background-jobs` processing at all | background worker gone (`Recreate` strategy, single replica) | [`ChemclawNoBackgroundWorkerIsScraped`](runbook.md#chemclawnobackgroundworkerisscraped); `kubectl describe deploy chemclaw-background-worker` |
| chemist told durable jobs are unavailable | front door cannot reach Temporal | `chemclaw_durable_unreachable_total`; [`ChemclawDurableUnreachable`](runbook.md#chemclawdurableunreachable) |
| job `RUNNING` forever / retrying | activity failing every attempt, or result over `CHEMCLAW_ACTIVITY_RESULT_MAX_BYTES` (`ActivityResultTooLarge`) | Temporal UI event history; `chemclaw_activity_failures_total{activity}` → [`ChemclawActivityRetryStorm`](runbook.md#chemclawactivityretrystorm) |
| nondeterminism error after a deploy | workflow code changed under in-flight runs | [`workflow-versioning.md`](workflow-versioning.md) |
| periodic job not running | Schedule never created, pruned, or failing | `GET /schedules` (`note`, `last_outcome`); re-run the upgrade to re-apply; log `deleted stale schedule <id> (no longer planned)` |
| a finished job never reached the chat | push-back dropped | [`ChemclawPushBackDropped`](runbook.md#chemclawpushbackdropped) |

Cancel a runaway job: `DELETE /jobs/{job_id}` (privileged role).
**Deeper:** [runbook § (x)](runbook.md#x-find-out-what-a-worker-is-doing-or-why-it-stopped).

---

## 11. Calculation capacity

| Presents as | Cause | Diagnose → fix |
| --- | --- | --- |
| tool error beginning `[calc-at-capacity]` (or `[<server>-at-capacity]`) | the backend pod's admission slots are full | `chemclaw_calc_backend_at_capacity_total{tool}`; durable jobs retry with backoff. Add `servers/calc` replicas; do not raise per-pod ceilings. → [`ChemclawCalculationBackendRefusingForCapacity`](runbook.md#chemclawcalculationbackendrefusingforcapacity) |
| tool error beginning `[calc-time-budget]` | the inline wall clock stopped the calculation on a busy pod | not retried; run as a durable job or when the pod is quieter |
| a queued tool returns a job id instead of an answer | the call waited past the turn's patience on `connector-<name>-interactive` | expected under load; the result arrives as a job. Scale `connectors.<name>.interactive` (KEDA if enabled). |
| CREST search holds a pod for hours | a search is charged every slot of its pod | expected; separate searches from optimisations by scaling |
| thrashing, heartbeat timeouts | more sessions than `CHEMCLAW_CALC_BACKEND_MAX_CONCURRENT_REQUESTS` | `chemclaw_calc_requests_in_flight` by pod → [`ChemclawCalcBackendOverCommitted`](runbook.md#chemclawcalcbackendovercommitted) |

---

## 12. Database pool saturation

| Presents as | Cause | Diagnose → fix |
| --- | --- | --- |
| 503 `server at capacity; retry shortly`, WARNING `shedding … PoolTimeout` | callers waited `CHEMCLAW_PG_POOL_TIMEOUT_SECONDS` (10 s) for a connection | `chemclaw_pg_pool_requests_waiting`, `chemclaw_pg_pool_available`, `chemclaw_db_query_duration_seconds` → [`ChemclawPgPoolSaturated`](runbook.md#chemclawpgpoolsaturated) |
| `ConnectionError: Postgres unreachable at <redacted DSN>: <cause>` | database down or DSN wrong | front door `/readyz` 503 `"database unreachable"` → [`ChemclawDatabaseUnavailable`](runbook.md#chemclawdatabaseunavailable) |
| connect failures against an idle database | fleet asks for more than `max_connections` | [`ChemclawFleetAboveItsConnectionCeiling`](runbook.md#chemclawfleetaboveitsconnectionceiling) |
| `db.slow` / `db.failed` log events; `chemclaw_db_query_failures_total{kind}` | slow queries / schema disagreement | `pg_stat_activity` for lock holders |
| audit rows missing: `audit_sink_failure` / `audit_buffer_full` markers | the sink cannot write / cannot keep up | [`ChemclawAuditTrailIncomplete`](runbook.md#chemclawaudittrailincomplete), [`ChemclawAuditTrailShedding`](runbook.md#chemclawaudittrailshedding) |

Raise `CHEMCLAW_PG_POOL_MAX_SIZE` and `postgres.maxConnections` together; `Settings` refuses a pair
that disagrees.

---

## 13. Knowledge corpus stale

| Presents as | Cause | Diagnose → fix |
| --- | --- | --- |
| a merged note is not cited | sidecar refresh failing | `kubectl logs <pod> -c knowledge-sync`: `WARNING could not fetch <branch> into <repo> — serving the previous snapshot`, `WARNING refresh failed; serving the previous snapshot`; `chemclaw_knowledge_sync_age_seconds` |
| `chemclaw_knowledge_sync_age_seconds` = `-1` | the tree holds no note | `kubectl logs <pod> -c knowledge-sync-init`; `knowledge.sync.repoUrl`, token |
| sidecar restarting | liveness `staleness` failed: `ERROR last successful refresh was <n>s ago, over the <max>s budget` | remote / credential |
| `WARNING <repo> holds a commit origin/<branch> does not — a note whose push failed` | a note write could not push | the next write replays it; [`ChemclawKnowledgeNotesLost`](runbook.md#chemclawknowledgenoteslost) |
| `WARNING a note write holds <lock> — refreshing on the next tick` | normal contention | none |
| `note_repo_dir '.' resolves to <path> — the checkout this process is running from` | `CHEMCLAW_NOTE_REPO_DIR` unset outside Helm | set it to a dedicated clone |
| the agent cites nothing from the graph, no error | readers resolve an empty `note_repo_dir/knowledge_dir` | `CHEMCLAW_NOTE_REPO_DIR`, `CHEMCLAW_KNOWLEDGE_DIR` |
| ELN data stale | sync not running or wedged | `chemclaw_ingest_cursor_lag_seconds{source}` → [`ChemclawIngestCursorStalled`](runbook.md#chemclawingestcursorstalled) |

**Deeper:** [`ChemclawKnowledgeCorpusStale`](runbook.md#chemclawknowledgecorpusstale).

---

## 14. Egress refused

| Layer | Log line | Counter |
| --- | --- | --- |
| in-process guard (`chemclaw.core.netguard`) | ERROR `egress refused: outbound connection to '<host>' is not on the allowlist` | `chemclaw_egress_refused_total` |
| compiled interposer (`LD_PRELOAD`) | stderr `chemclaw-netguard-preload: …` naming destination, port, verb | `chemclaw_egress_preload_refused_connect`, `chemclaw_egress_preload_refused_resolve` |
| NetworkPolicy | connection timeouts, no log | — |
| `Chemclaw3-mcp` servers | ERROR `egress refused: host='<host>'` | the fleet's own egress counter |

**Fix.** If the destination is legitimate (a new connector, a remote git note repo, a collector),
add its bare host to `CHEMCLAW_EGRESS_ALLOW` (no scheme, no port) **and** to
`networkPolicy.egressDestinations`, then roll the pods. If it is not, the refusal is the control
working — record it. A disarmed guard shows as `chemclaw_egress_guard_armed` /
`chemclaw_egress_preload_armed` at 0.
**Deeper:** [`ChemclawEgressRefused`](runbook.md#chemclawegressrefused),
[`ChemclawEgressPreloadRefused`](runbook.md#chemclawegresspreloadrefused).

---

## 15. Budget and spend-cap refusals

| Presents as | Bound | Diagnose → fix |
| --- | --- | --- |
| 429 `session turn budget exhausted (<n> turns)` / `session token budget exhausted (<n> tokens)` | per-session caps | `chemclaw_turns_refused_budget_total`; start a new session or raise `CHEMCLAW_BUDGET_MAX_TURNS_PER_SESSION` / `CHEMCLAW_BUDGET_MAX_TOKENS_PER_SESSION` |
| 429 `user turn budget exhausted …` / `user token budget exhausted …` | per-user caps over `CHEMCLAW_BUDGET_WINDOW_HOURS` | WARNING `user tokens budget N% spent (<used> of <cap>) for <oid> …` names who; [`ChemclawBudgetNearingItsCap`](runbook.md#chemclawbudgetnearingitscap) |
| SSE `budget_exhausted` mid-stream | the binding check inside the stream | same counters |
| 429 `This conversation has reached its size limit (<n> MiB stored, against <m> MiB)` | `CHEMCLAW_SESSION_MAX_THREAD_BYTES` | `chemclaw_turns_refused_thread_size_total`; new session |
| SSE `spend_cap_reached` | `CHEMCLAW_AGENT_MAX_TURN_BILLED_TOKENS` | WARNING `the turn for session <id> hit its <n> billed-token cap after <m> tokens`; `chemclaw_turn_spend_caps_total` |
| SSE `loop_cap_reached` | `CHEMCLAW_HARNESS_MAX_LOOP_ITERATIONS` | `chemclaw_turn_loop_caps_total` |

Counters are per pod unless `CHEMCLAW_SESSION_STORE=postgres`; `chemclaw_degraded_total{subsystem="budget_window"}`
says the durable half was configured and unreachable.
**Deeper:** [`ChemclawTurnsHittingACap`](runbook.md#chemclawturnshittingacap),
[`ChemclawTurnsRefusedByBudget`](runbook.md#chemclawturnsrefusedbybudget).

---

## 16. The UI cannot reach the backend, or sign-in loops

The UI (`Chemclaw3_ui`) is a BFF that proxies `/api/*` to the backend's service root, named by the
BFF's own API-URL variable (`Chemclaw3_ui` `.env.example`; a URL with a path is refused).

| Presents as | Cause | Diagnose → fix |
| --- | --- | --- |
| BFF exits 1 with `config:` lines | the BFF's own startup validation (`AUTH_MODE`, an API URL with a path, missing `ENTRA_*`) | fix the BFF env (`Chemclaw3_ui` README "What the BFF refuses to start with") |
| 502 `{"detail":"upstream unavailable","code":…}`; BFF log `upstream error` with `correlation_id` | backend unreachable from the BFF | BFF → `chemclaw-service:8080` reachability; `networkPolicy.serviceIngress`; front door `/readyz` |
| 503 `no upstream connection available` | the BFF's upstream pool is saturated | BFF log `upstream pool saturated`; BFF pool sizing |
| every API call 401 after a successful login | token is for the wrong audience or v1 | API scope must be the backend API's (`aud` = `CHEMCLAW_ENTRA_AUDIENCE`); API app registration `accessTokenAcceptedVersion: 2`; backend log `token validation failed: <reason>` |
| redirect loop at login | SPA redirect URI not registered, or CSP blocks the authority | Entra SPA platform redirect URIs; BFF CSP opens `login.microsoftonline.com` or `ENTRA_AUTHORITY` |
| "random logout" after about an hour | silent refresh iframe blocked | a proxy overwriting the BFF's CSP |
| UI says "budget exhausted" for a 429 | something stripped `Retry-After` — the UI treats a 429 without it as terminal | proxies must pass `Retry-After` |
| 503 `service misconfigured; refusing to serve` | backend running with `entra_required` off | [§2](#2-a-pod-will-not-boot) |
| conversation list empty | backend not on `CHEMCLAW_SESSION_STORE=postgres` | set it |
| CORS errors in the browser console | UI served from a different origin than the API | `CHEMCLAW_SERVICE_CORS_ORIGINS` (never `*`), or serve same-origin |

The BFF mints the correlation id the backend adopts — quote it from the browser's network tab
(response header `X-Chemclaw-Correlation-Id`) and follow [§1](#1-reading-logs-and-following-one-request).
