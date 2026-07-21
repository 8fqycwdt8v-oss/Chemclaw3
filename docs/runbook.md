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
  entry and per skipped broken export file; `DEBUG` adds calculation cache hit-vs-compute (the
  "why did this recompute?" answer).
- **A stuck/failed job:** open the Temporal UI (:8080) → the workflow → event history; cross-check
  the worker's stderr logs. A worker not picking up jobs is usually the wrong queue/namespace —
  the startup log line shows exactly what it connected to.
- **Database down:** connections fail fast with `ConnectionError: Postgres unreachable at
  <host>: <cause>` (password redacted). It is a retryable infra fault, so Temporal retries the
  activity; fix the DSN/host and it recovers.
- **OpenTelemetry (optional):** set `CHEMCLAW_OTEL_ENABLED=true` and point
  `OTEL_EXPORTER_OTLP_ENDPOINT` at a collector. Requires the OpenTelemetry SDK + OTLP exporter
  extras installed; enabling without them raises a directive error.

## Talk to the agent from a terminal (testing)

The production ingress is Teams/Copilot with Entra-ID SSO (architektur.md §7). For local
testing there is a CLI: `make chat` (or `uv run chemclaw --admin`). It needs `ANTHROPIC_API_KEY`
in the environment — the chat client preflights it and fails with a clear message otherwise.

- **Admin mode is required.** Entra auth is unimplemented (Phase 6), so the CLI runs only with
  `--admin`: it bypasses auth, advertises every skill, and stamps the audit trail with
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
must match `CHEMCLAW_ECFP_BITS` / `CHEMCLAW_DRFP_BITS` (see `config.py`). There is no
applied-migrations record yet — re-run `make db-migrate` to be sure a fresh DB is current.

## (iii) Add / switch an ELN source

Both ingestion adapters (`json` free-text export, `ord` native ORD) are registered in
`eln/registry.py`. The durable sync ingests from one source — set `CHEMCLAW_ELN_SYNC_ADAPTER`
(`json` | `ord`) and its export directory (`CHEMCLAW_ELN_EXPORT_DIR` / `CHEMCLAW_ORD_EXPORT_DIR`).
A *new* ELN source is one new adapter class satisfying the `ElnAdapter` contract plus one entry
in `ELN_ADAPTERS`; the memory jobs then read it automatically (they ingest every registered
adapter). Validate an export with `make eln-validate`.

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

## (vi) Change a fingerprint definition (ECFP radius/bits or DRFP bits)

`CHEMCLAW_ECFP_RADIUS`/`_ECFP_BITS`/`_DRFP_BITS` define the fingerprints. A **width** change
(`*_BITS`) also needs a matching `bit(N)` schema change (`infra/sql/002,003`) or inserts fail
loudly. Every fingerprint row records the *definition* it was indexed under, and similarity
search returns only rows matching the store's current definition — so after any definition
change, previously-indexed rows fall out of search (safe: no wrong scores, just missing hits)
until you **re-index** them (re-run the ELN sync / re-add molecules). If search comes back
empty after a config change, that is the tell: the index predates the new definition and needs
rebuilding.
