# Chemclaw3

AI agent for pharmaceutical/chemical process R&D: MAF conversation orchestration,
Temporal durable jobs, Agent Skills, and a PR-gated Markdown knowledge graph.
Architecture and build order live in `docs/architektur.md` and
`docs/implementation-plan.md`.

## Quickstart

```sh
uv sync                 # install runtime + dev dependencies
cp .env.example .env    # optional — defaults match the dev stack
make up                 # Temporal dev cluster + Postgres/pgvector (docker-compose)
make db-migrate         # apply infra/sql migrations
make check              # lint + mypy --strict + tests
```

Useful targets: `make eval` (score the versioned metric case-set),
`make eln-validate` (validate ELN exports), `make kg-validate` (knowledge-graph
schema + link check). See the `Makefile` for the full list.

Every environment value comes from `chemclaw/config.py` (see `.env.example`);
there is no second config source.

## Running the assistant

```sh
# The front-door chat service (FastAPI + SSE). Browse to the served page, start a
# session, watch a plan + tool use, get a cited answer.
uvicorn service.app:create_app --factory --port 8080

# Durable workers (separate processes; need Temporal + Postgres from `make up`).
python -m workers.hpc_worker          # hpc-jobs queue (QM/Nextflow)
python -m workers.background_worker   # background-jobs (ELN sync, reports, memory)
```

The LLM provider is config-selected (`CHEMCLAW_LLM_PROVIDER`): an internal
OpenAI-compatible endpoint in production (one generic credential, not Entra), or
Anthropic for local dev. Set `CHEMCLAW_HARNESS_ENABLED=true` for the autonomous
plan→approve→execute harness. Entra identity is enforced when
`CHEMCLAW_ENTRA_REQUIRED=true` (off in dev).

## Deployment

`deploy/` holds the OpenShift delivery: one rootless multi-target image
(`deploy/Containerfile`, role chosen by `CHEMCLAW_COMPONENT`) and a Helm chart
(`deploy/helm/chemclaw/`). See `deploy/README.md` for the topology (front-door
Route behind OIDC, the two Temporal workers, MCP servers, workload identity
federation, and the three plain secrets). The build order and per-phase status
live in `docs/implementation-tickets.md`.
