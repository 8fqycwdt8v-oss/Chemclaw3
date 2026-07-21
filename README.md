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

## Security

Pre-**Phase 6**: there is **no authentication or authorization** yet (by design — see
`docs/architektur.md` §7–§8). Safe for single-tenant / trusted-network / dev use; do **not**
expose the agent to multiple or untrusted users before Phase 6. Full posture and deployment
guidance in [`SECURITY.md`](SECURITY.md).
