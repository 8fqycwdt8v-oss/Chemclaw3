# Chemclaw3

AI agent for pharmaceutical/chemical process R&D: LangGraph conversation orchestration,
Temporal durable jobs, Agent Skills, and a PR-gated Markdown knowledge graph.

**`ARCHITECTURE.md` is the map** — the four layers and what every directory in this
repository is for. Read it before going looking for something. The original design and
build order live in `docs/reference/architektur.md` and `docs/archive/plans/implementation-plan.md`; both are
historical (see `CLAUDE.md`).

## Quickstart

Prerequisites: Python `>=3.11`, [`uv`](https://docs.astral.sh/uv/), and Docker (with the
`docker compose` CLI) for `make up`.

```sh
uv sync                 # install runtime + dev dependencies
uv run pre-commit install  # ruff check/format + mypy --strict on every commit
cp .env.example .env    # optional — defaults match the dev stack
make up                 # Temporal dev cluster + Postgres/pgvector (docker-compose)
make db-migrate         # apply infra/sql migrations
make check              # fast inner loop: lint + mypy --strict + tests
```

`make check` is the inner loop, not the gate: it skips coverage and the eight validators
(`kg-validate`, `eln-validate`, `skill-validate`, `connector-validate`, `datasource-validate`,
`template-validate`, `prose-validate`, `helm-validate`). Run `make ci` before
pushing — it is exactly what CI runs and is what `pre-commit` does not cover.

`make up` binds Postgres on `5432`, the Temporal frontend gRPC on `7233`, and the Temporal Web UI
on `8081` (see `infra/docker-compose.yml`).

Useful targets: `make eval` (score the versioned metric case-set),
`make eln-validate` (validate ELN exports), `make kg-validate` (knowledge-graph
schema + link check). See the `Makefile` for the full list.

Every environment value comes from `src/chemclaw/core/config/` (see `.env.example`);
there is no second config source.

## Running the assistant

```sh
# The front-door chat service (FastAPI + SSE). Browse to the served page, start a
# session, watch a plan + tool use, get a cited answer.
uvicorn chemclaw.api.app:create_app --factory --host 127.0.0.1 --port 8000

# Durable workers (separate processes; need Temporal + Postgres from `make up`).
python -m chemclaw.durable.background_worker    # background-jobs (ELN sync, reports, memory)
python -m chemclaw.connectors.qm.worker         # connector-qm (the durable QM/DFT job via Nextflow)
python -m chemclaw.connectors.calc.worker       # connector-calc (the expensive xTB calculations)
python -m chemclaw.connectors.bo.worker         # connector-bo (optimization campaigns)
```

`make live-up` starts all of these together, readiness-polled, and `make live-jobs` then runs a
real durable job end to end — see "Live-test the whole stack" in `docs/guides/runbook.md`. Port
`8000` is not arbitrary: it is what `CHEMCLAW_LIVE_PROBE_BASE_URL` defaults to, so the probe
runner reaches the front door with no override.

The LLM provider is config-selected (`CHEMCLAW_LLM_PROVIDER`): an internal
OpenAI-compatible endpoint in production (one generic credential, not Entra), or
Anthropic for local dev. Set `CHEMCLAW_HARNESS_ENABLED=true` for the autonomous
plan→approve→execute harness. Entra identity is enforced when
`CHEMCLAW_ENTRA_REQUIRED=true` (off in dev).

## Deployment

`deploy/` holds the OpenShift delivery: one rootless multi-target image
(`deploy/Containerfile`, role chosen by `CHEMCLAW_COMPONENT`) and a Helm chart
(`deploy/helm/chemclaw/`). See `deploy/README.md` for the topology (front-door
Route behind OIDC, the background worker plus one Temporal worker per connector
bundle that owns durable work, the connector servers, workload identity
federation, and the plain secrets `values.yaml` declares). The build order and
per-phase status live in `docs/archive/plans/implementation-tickets.md`.

## Security

`SECURITY.md` describes the enforced posture (Entra OIDC at the front door, the
`require_actor` reject-if-absent rule, the single `authorize_trigger` gate, role-scoped
skills, the audit trail and PR-gate), the `entra_required` enforcement switch, and the
live-infrastructure edges still open. Run shared/exposed deployments only with
`entra_required=true`.
