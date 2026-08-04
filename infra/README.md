# `infra/` — local dev infrastructure

**Responsibility:** the developer-facing stack definition. `docker-compose.yml`
brings up a self-hosted Temporal dev server (frontend + web UI on :8081) and a
pgvector-enabled Postgres, started via `make up` / stopped via `make down`.

Ports and credentials mirror `.env.example` and `chemclaw/config.py`, so a fresh
checkout connects with no extra setup. This is a **dev** topology only — not a
production deployment (plan step 0.5).

`live/` holds the two scripts the live-test lane is made of: `bootstrap.sh`
provides what `make up` provides on a machine with no Docker daemon (it defers to
the compose file whenever one is reachable), and `processes.sh` starts and stops
the connectors, the four Temporal workers and the front door with readiness polls
rather than sleeps. Both are reached through `make live-*`; the procedure is in
`docs/guides/runbook.md`.

`sql/` is the migration set `make db-migrate` applies, in filename order, against
a ledger with per-file checksums.
