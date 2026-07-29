# `infra/` — local dev infrastructure

**Responsibility:** the developer-facing stack definition. `docker-compose.yml`
brings up a self-hosted Temporal dev server (frontend + web UI on :8080) and a
pgvector-enabled Postgres, started via `make up` / stopped via `make down`.

Ports and credentials mirror `.env.example` and `chemclaw/config.py`, so a fresh
checkout connects with no extra setup. This is a **dev** topology only — not a
production deployment (plan step 0.5).
