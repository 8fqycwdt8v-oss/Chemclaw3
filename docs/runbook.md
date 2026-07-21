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

Today the agent's tools are a Python list in `agents/chemclaw_agent.py` (`build_agent`): a new
capability is a thin wrapper module under `agents/` plus one line in that `tools=[...]` list.
The standalone FastMCP servers in `mcp_servers/` are imported in-process, not attached over the
MCP protocol — attaching them as MCP-client tools is a deliberate future migration (see
`BACKLOG.md`), not required to add a capability now.
