# Operations runbook (admin)

How a system/admin configures and troubleshoots Chemclaw. Everything environment-dependent
comes from the one config source (`chemclaw/config.py`, every field mirrored in `.env.example`,
overridable as `CHEMCLAW_<FIELD>`); this runbook covers the four recurring admin tasks.

## Prerequisites

- Local dev stack: `make up` starts Temporal (dev server + UI) and Postgres/pgvector;
  `make down` stops it. The **Temporal Web UI is at http://localhost:8080** — the first place
  to look at a running/failed job's event history. Frontend gRPC is `localhost:7233`.
- The full gate before calling any change done: `make check` (ruff + `mypy --strict` + pytest).

## Logging & troubleshooting

- **Verbosity is one switch.** Set `CHEMCLAW_LOG_LEVEL=DEBUG` (default `INFO`) and restart the
  affected worker. `configure_logging()` runs at each worker's entrypoint; no code change.
- **What gets logged:** each worker logs its connected address/namespace/queue and registered
  workflows on startup; every agent tool call is audited (name, arguments, outcome, latency —
  `agents/audit.py`); the ELN sync logs `ingested/rejected` counts plus a WARNING per rejected
  entry, per skipped broken export file, and one aggregated WARNING naming export files that
  arrived too late to be ingested (recovery: section (v)); `DEBUG` adds calculation cache
  hit-vs-compute (the "why did this recompute?" answer).
- **Changing workflow code:** a control-flow change deployed while a run is in flight fails that run
  on replay. Follow `docs/workflow-versioning.md` (patch-gate or drain) for any release touching a
  `@workflow.defn` body.
- **A stuck/failed job:** open the Temporal UI (:8080) → the workflow → event history; cross-check
  the worker's stderr logs. A worker not picking up jobs is usually the wrong queue/namespace —
  the startup log line shows exactly what it connected to.
- **Database down:** connections fail fast with `ConnectionError: Postgres unreachable at
  <host>: <cause>` (password redacted). It is a retryable infra fault, so Temporal retries the
  activity; fix the DSN/host and it recovers.
- **OpenTelemetry (optional):** set `CHEMCLAW_OTEL_ENABLED=true` and point
  `OTEL_EXPORTER_OTLP_ENDPOINT` at a collector. Requires the OpenTelemetry SDK + OTLP exporter
  extras installed; enabling without them raises a directive error.

## Exposing the front door (the two settings that decide whether it boots)

- **`CHEMCLAW_ENTRA_REQUIRED=true` for anything reachable beyond localhost.** With it off, every
  request runs as the shared dev principal with all authorization gates open, so the service
  **refuses to start** on a non-loopback bind (the `0.0.0.0` default) with `SECURITY:
  entra_required is False but the service binds a non-loopback interface …`. That message means the
  guard worked, not that the deployment is broken: set `CHEMCLAW_ENTRA_REQUIRED=true` (plus
  `CHEMCLAW_ENTRA_TENANT_ID` and `CHEMCLAW_ENTRA_AUDIENCE`, which are validated together at
  startup), or bind loopback for local dev. `CHEMCLAW_SERVICE_ALLOW_INSECURE=true` is the conscious
  opt-out and boots with a loud warning instead.
- **`CHEMCLAW_ENTRA_CLIENT_ID` was removed.** Settings is `extra="forbid"`, so a stale export of it
  aborts startup with a validation error naming the field — unset it in any inherited environment.
- **Note submission is serialized per host.** Keep the background worker at one replica (see
  `deploy/helm/chemclaw/values.yaml`); the PR-gate's checkout lock is host-local, so a second
  replica needs the distributed lock still open in `BACKLOG.md`.

## Talk to the agent from a terminal (testing)

The production ingress is Teams/Copilot with Entra-ID SSO (architektur.md §7). For local
testing there is a CLI: `make chat` (or `uv run chemclaw --admin`). It needs `ANTHROPIC_API_KEY`
in the environment — the chat client preflights it and fails with a clear message otherwise.

- **Admin mode is required.** Entra auth is enforced at the *front door* (F4), and this CLI has no
  browser OIDC token to validate, so it runs only with `--admin`: it bypasses auth, advertises every skill, and stamps the audit trail with
  `CHEMCLAW_CLI_ADMIN_ACTOR` (default `admin@localhost`). Without `--admin` it refuses and exits
  non-zero — "no authentication" stays a conscious choice, not a default.
- **One-shot vs. REPL:** `uv run chemclaw --admin -m "which solvent next for …?"` asks one
  question, prints the answer to stdout, and exits (scriptable); with no `-m` it is an
  interactive chat (the thread accumulates; `exit`/Ctrl-D to quit).
- **Attribute the run:** `--actor alice@lab` overrides the audit actor. `--audit-postgres`
  persists the tool-audit trail to Postgres (default is log-only).

## (i) Add a skill

Drop a `skills/<name>/SKILL.md` (front-matter schema + template in `skills/README.md`) and
restart the agent — discovery is automatic. To add a second skills directory (e.g. team-private
skills), set `CHEMCLAW_SKILLS_DIR` to an OS-path-separator list, like `PATH`
(`skills:/opt/team-skills`).

## (ii) Add or repoint a database

Set `CHEMCLAW_POSTGRES_DSN` and run `make db-migrate` (applies `infra/sql/*.sql` in filename
order; each migration is idempotent, so re-running is safe). A new capability's table is a new
hand-written `infra/sql/00N_*.sql`. Note the bit-width coupling: a `bit(N)` fingerprint column
must match `CHEMCLAW_ECFP_BITS` / `CHEMCLAW_DRFP_BITS` (see `config.py`). Applied migrations are
recorded in the `schema_migrations` ledger with a checksum (D-034), so re-running is safe and an
edited already-applied file is flagged as drift rather than silently skipped.

## (iii) Add / switch an ELN source

Sources live on the generic seam (`sources/registry.py`): `eln-json` (free-text export) and
`eln-ord` (native ORD) are the ingest sources, `graph` the retrieve source. Set which are active
with `CHEMCLAW_DATA_SOURCES` (a comma list, e.g. `graph,eln-json,eln-ord`) plus each ingest
source's export directory (`CHEMCLAW_ELN_EXPORT_DIR` / `CHEMCLAW_ORD_EXPORT_DIR`). The durable sync
ingests **every** active ingest source, each with its own high-water cursor (keyed by registry
name in `sync_cursors`), so sources advance independently; the memory jobs read the same active set.
A *new* ELN source is one new adapter class satisfying the `ElnAdapter` contract plus one
`DATA_SOURCES` entry in `sources/registry.py` and its key in `CHEMCLAW_DATA_SOURCES`. Validate an
export with `make eln-validate`.

## (iv) Add a capability/tool the agent can call

The agent reaches the fingerprint search over the **MCP protocol**: each capability is a server
listed in `CHEMCLAW_MCP_SERVERS` (default `mcp-molfp`, `mcp-rxnfp` in `chemclaw/config.py`), and
`build_agent` attaches it as an `MCPStdioTool` subprocess. **Adding a capability is a config
entry**, not agent code:

1. Write (or reuse) a FastMCP server exposing the tools (see `mcp_servers/molfp/server.py`).
2. Add `{name, command, args, allowed_tools}` to `CHEMCLAW_MCP_SERVERS` — set `allowed_tools`
   to the read/search tools the agent may call (keep index/write tools off the chat agent;
   those writes go through the PR-gate).
3. Servers are launched from the repo root (`command`/`args`, e.g. `python -m ...`); ensure the
   process's working directory is the checkout so `-m mcp_servers...` resolves.

Some agent tools are still in-process plain functions (calculators, graph, BO) — those are a
thin wrapper module under `agents/` plus one line in the `build_agent` `tools=[...]` list.
Troubleshooting: a server that fails to start surfaces in the worker/agent logs; verify it runs
standalone with `python -m mcp_servers.<name>.server` and that Postgres is reachable (tool
*discovery* needs no DB, but *invoking* a search does).

## (v) Re-ingest a rejected ELN entry (after fixing the source record)

The durable sync rejects an entry that fails validation (bad structure, mass-balance
mismatch) and **advances past it** — a rejection is deterministic bad data, so re-fetching
it unchanged would only re-reject it. Each rejection is reported in the run's
`IngestSummary.rejected` (visible in the Temporal workflow result) and logged as a `WARNING`
carrying the entry id, the reason, **and the entry's timestamp**.

To re-ingest one after correcting its source record upstream: start the `ElnSyncWorkflow`
with `since` set to just before that entry's timestamp (from the rejection log/summary). The
sync re-fetches from there and re-ingests everything after it; ingestion is idempotent
(id-keyed fingerprint upserts + a stable note branch), so the already-ingested entries in
that window are harmless no-ops and only the corrected entry newly succeeds. There is no
automatic re-drive by design (KISS) — re-ingestion is a deliberate, admin-triggered action.

The **same procedure backfills a late-arriving export**: a file dropped into the export directory
after the sync's overlap window, carrying an older payload timestamp, is filtered out permanently
and reported as `… export file(s) arrived after the sync cursor but carry an older timestamp …`.
Start the sync with `since` set before those entries' timestamps to pull them in.

## (vi) Change a fingerprint definition (ECFP radius/bits or DRFP bits)

`CHEMCLAW_ECFP_RADIUS`/`_ECFP_BITS`/`_DRFP_BITS` define the fingerprints. A **width** change
(`*_BITS`) also needs a matching `bit(N)` schema change (`infra/sql/002,003`) or inserts fail
loudly. Every fingerprint row records the *definition* it was indexed under, and similarity
search returns only rows matching the store's current definition — so after any definition
change, previously-indexed rows fall out of search (safe: no wrong scores, just missing hits)
until you **re-index** them (re-run the ELN sync / re-add molecules). If search comes back
empty after a config change, that is the tell: the index predates the new definition and needs
rebuilding.

## (vii) Read eval-drift alerts

The scheduled `EvalDriftWorkflow` re-scores the committed eval case-set and raises one alert per
metric that moved past the relative noise band (`CHEMCLAW_EVAL_DRIFT_EPSILON`). Two surfaces, both
intentional:

- **The background worker's log** is where you meet a regression: `eval drift: metric 'f1' scored
  … vs baseline … (delta …)`, or `… disappeared from the run …` when a metric stopped being scored
  at all (that is *not* a score of 0.0 — usually a removed or erroring case).
- **The durable record** is the `system-eval-drift` channel in `session_events`; delivery is
  must-deliver, so a failed write fails the workflow run rather than dropping the alert. **No UI
  consumes this channel by design** — read the backlog with
  `SELECT created_at, payload FROM session_events WHERE session_id = 'system-eval-drift' ORDER BY
  created_at DESC LIMIT 20;`.

Over the committed (deterministic) case-set this is a *deployment-consistency tripwire*: it fires
when the deployed code, cases, and `evals/baseline.json` are inconsistent. After a deliberate
metric change, refresh the committed baseline — otherwise every scheduled run re-alerts.
