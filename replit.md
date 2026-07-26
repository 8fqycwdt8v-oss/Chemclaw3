# Chemclaw3

A chemistry-native AI agent for pharmaceutical/chemical process R&D. Combines a FastAPI chat interface, Microsoft Agent Framework (MAF) for conversation orchestration, Temporal for durable long-running chemistry jobs, Agent Skills, and a PR-gated Markdown knowledge graph.

## Run & Operate

### Chemclaw services (Python/FastAPI)
- `bash services/chemclaw/start.sh` — FastAPI front-door chat service (port 8000)
- `bash services/chemclaw/start-temporal.sh` — Temporal dev server (gRPC: 7233, UI: 8233)
- `services/chemclaw/.bin/temporal server start-dev` — raw Temporal binary

### Support infrastructure (TypeScript pnpm workspace)
- `pnpm --filter @workspace/api-server run dev` — existing Express API server (port 8080)
- `pnpm run typecheck` — full typecheck across all packages

### Workflows (managed by Replit)
- **Chemclaw API Service** — runs `start.sh` on port 8000 (webview)
- **Chemclaw Temporal Dev Server** — runs `start-temporal.sh` (console)
- **artifacts/api-server: API Server** — existing Node.js API on 8080

## Stack

- **Frontend**: FastAPI + SSE (`service/`) + static chat UI (`service/static/`)
- **Agent**: Microsoft Agent Framework (MAF) — `agents/`
- **Durable jobs**: Temporal SDK (`workflows/`, `workers/`)
- **Chemistry**: xTB via tblite, RDKit, BoFire (BO campaigns), DRFP fingerprints
- **LLM**: Anthropic Claude via Replit AI Integration (no API key required)
- **DB**: PostgreSQL + pgvector (Replit managed) — sessions, audit trail, calculation cache, fingerprints
- **Python env**: `.venv` inside `services/chemclaw/` (Python 3.11, pip-managed)

## Where things live

- `services/chemclaw/` — Chemclaw3 Python project (cloned from GitHub)
- `services/chemclaw/service/app.py` — FastAPI application factory
- `services/chemclaw/agents/chemclaw_agent.py` — MAF agent builder
- `services/chemclaw/chemclaw/config.py` — all env-driven config (`CHEMCLAW_*` prefix)
- `services/chemclaw/agents/llm_provider.py` — LLM provider seam
- `services/chemclaw/start.sh` — startup script (bridges Replit AI Integration env vars → Anthropic SDK)
- `services/chemclaw/infra/sql/` — database migrations (all applied)
- `lib/api-spec/openapi.yaml` — OpenAPI spec for the pnpm workspace API

## Environment variables

| Variable | Value / Source |
|---|---|
| `CHEMCLAW_LLM_PROVIDER` | `anthropic` |
| `CHEMCLAW_AGENT_MODEL` | `claude-sonnet-4-6` |
| `CHEMCLAW_ENTRA_REQUIRED` | `false` (dev mode, no Entra tenant yet) |
| `CHEMCLAW_SERVICE_ALLOW_INSECURE` | `true` (allows binding 0.0.0.0 without Entra) |
| `CHEMCLAW_SERVICE_HOST` | `0.0.0.0` |
| `CHEMCLAW_SERVICE_PORT` | `8000` |
| `CHEMCLAW_TEMPORAL_ADDRESS` | `localhost:7233` |
| `CHEMCLAW_POSTGRES_DSN` | set at runtime from `$DATABASE_URL` in `start.sh` |
| `ANTHROPIC_API_KEY` | set at runtime from `$AI_INTEGRATIONS_ANTHROPIC_API_KEY` in `start.sh` |
| `ANTHROPIC_BASE_URL` | set at runtime from `$AI_INTEGRATIONS_ANTHROPIC_BASE_URL` in `start.sh` |

## API endpoints

| Route | Description |
|---|---|
| `GET /healthz` | Liveness probe |
| `GET /readyz` | Readiness probe |
| `POST /sessions` | Create a new conversation session |
| `POST /sessions/{id}/messages` | Send a turn (SSE stream of events) |
| `GET /` | Static chat UI |

## Architecture decisions

- **Dev mode**: `CHEMCLAW_ENTRA_REQUIRED=false` + `CHEMCLAW_SERVICE_ALLOW_INSECURE=true`. Every request runs under a shared dev principal. Set `CHEMCLAW_ENTRA_REQUIRED=true` and provide Entra tenant/client/audience when moving to production.
- **Temporal**: Running as a local in-memory dev server (CLI binary at `.bin/temporal`). Replace `CHEMCLAW_TEMPORAL_ADDRESS` with your Temporal Cloud namespace or self-hosted cluster URL for production.
- **LLM bridging**: `start.sh` maps `AI_INTEGRATIONS_ANTHROPIC_*` → `ANTHROPIC_*` so the Anthropic SDK picks them up without modifying source code.
- **CPU-only PyTorch**: Installed CPU wheels for torch (via pip) to avoid CUDA packages that can't write to the Nix store. Re-install GPU torch if you move to a GPU-enabled host.
- **pgvector**: Extension enabled in Replit's PostgreSQL. All 14 migrations applied (schema_migrations, calculation_results, fingerprints, sessions, audit trail, note index, etc.).

## Connecting production services (when ready)

1. **Entra**: Set `CHEMCLAW_ENTRA_TENANT_ID`, `CHEMCLAW_ENTRA_CLIENT_ID`, `CHEMCLAW_ENTRA_AUDIENCE`, then flip `CHEMCLAW_ENTRA_REQUIRED=true`.
2. **Temporal Cloud**: Point `CHEMCLAW_TEMPORAL_ADDRESS` at your namespace + set `CHEMCLAW_TEMPORAL_NAMESPACE`.
3. **HPC workers**: Run `python -m workers.hpc_worker` and `python -m workers.background_worker` (need Temporal + Postgres).

## Gotchas

- Install Python packages with `PIP_USER=0 services/chemclaw/.venv/bin/pip install ...` (not system pip — Nix store is read-only).
- Do not use `uv run` in the service workflow — use `.venv/bin/python` directly (Replit's `.pythonlibs` path confuses uv's env detection).
- Port 8080 is taken by the api-server artifact. Chemclaw uses 8000.
- Temporal in-memory store loses all workflow history on restart. Run `make up` (docker-compose) for persistent Temporal when you have Docker available.
