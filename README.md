# Chemclaw3

AI agent for pharmaceutical/chemical process R&D: LangGraph conversation orchestration,
Temporal durable jobs, Agent Skills, and a Markdown knowledge graph.

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

`make check` is the inner loop, not the gate: it skips the coverage floor, the evals, the
dependency audit and the validators (`kg-validate`, `eln-validate`, `skill-validate`,
`connector-validate`, `datasource-validate`, `sink-validate`, `channel-validate`,
`template-validate`, `prose-validate`, `helm-validate`, `kind-validate`;
`tests/test_repo_map.py` derives the list from the `ci` target). Run `make ci` before pushing — it
is exactly what CI runs and is what `pre-commit` does not cover. `make help` lists every target.

Postgres-backed tests skip when no database is reachable, and the run's closing summary says how
many did — a green `make check` without `make up` is not evidence about the durable layer.

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
# session, watch a plan + tool use, get a cited answer. Three variables, each a
# different fact: `--host` is the socket, `CHEMCLAW_SERVICE_HOST` is what the app is
# told about it (with no identity provider configured the app refuses a request that
# arrives on anything but loopback — `src/chemclaw/api/middleware.py`), and
# `CHEMCLAW_LLM_ALLOW_LOOPBACK_GATEWAY` is the *gateway* posture stated out loud.
# The third is not extra ceremony: the shipped `CHEMCLAW_LLM_BASE_URL` is the local
# mock on loopback, and since D-2026-09-12 every process that makes a model call
# refuses to boot pointed at it **in every posture** — the loopback bind used to skip
# the check, which made a deployment's exposed-gateway typo invisible. Saying "yes, I
# mean the dev mock" is what replaced it (`src/chemclaw/core/llm_gateway.py`).
CHEMCLAW_SERVICE_HOST=127.0.0.1 CHEMCLAW_LLM_ALLOW_LOOPBACK_GATEWAY=true \
  uvicorn chemclaw.api.app:create_app --factory --host 127.0.0.1 --port 8000

# Durable workers (separate processes; need Temporal + Postgres from `make up`). The
# background worker takes agent turns inside an activity, so it asks the same gateway
# question; the connector workers reach no model and do not. Every worker binds no
# request surface, so the loopback-bind exemption above means nothing to it: with no
# identity provider configured it refuses to boot until the unauthenticated posture is
# stated (`src/chemclaw/durable/serve.py`). Each worker also serves `/healthz`, `/readyz`
# and `/metrics` on `CHEMCLAW_WORKER_METRICS_PORT` (default 9000), so two workers on one
# machine need distinct ports (or 0 to disable the surface).
export CHEMCLAW_WORKER_ALLOW_UNAUTHENTICATED=true
CHEMCLAW_LLM_ALLOW_LOOPBACK_GATEWAY=true CHEMCLAW_WORKER_METRICS_PORT=9000 \
  python -m chemclaw.durable.background_worker  # background-jobs (ELN sync, reports, memory)
CHEMCLAW_WORKER_METRICS_PORT=9001 python -m chemclaw.connectors.calc.worker     # connector-calc
CHEMCLAW_WORKER_METRICS_PORT=9002 python -m chemclaw.connectors.bo.worker       # connector-bo
CHEMCLAW_WORKER_METRICS_PORT=9004 python -m chemclaw.connectors.results.worker  # connector-results (result publication)
```

`make live-up` starts all of these together, readiness-polled, and `make live-jobs` then runs a
real durable job end to end — see "Live-test the whole stack" in `docs/guides/runbook.md`. Port
`8000` is not arbitrary: it is what `CHEMCLAW_LIVE_PROBE_BASE_URL` defaults to, so the probe
runner reaches the front door with no override.

Every model call goes to one OpenAI-compatible gateway, `CHEMCLAW_LLM_BASE_URL`
(one generic credential, not Entra). There is no provider selection — which vendor
answers behind that address is the gateway's business. The default is the local
mock (`python -m chemclaw.cli.mock_llm`), so a fresh checkout needs no credential.
The plan→approve→execute harness is on by default (`CHEMCLAW_HARNESS_ENABLED`), starting in
`plan_only` mode so a plan is approved before anything runs (`CHEMCLAW_HARNESS_AUTONOMY`).
Entra identity is enforced when `CHEMCLAW_ENTRA_REQUIRED=true` — off in the code default for
local dev, on in the shipped Helm chart.

## Documentation for operators

| Task | Read |
| --- | --- |
| Install the system (this repo, the MCP fleet, the UI) | [`docs/guides/deployment.md`](docs/guides/deployment.md) |
| Run it day to day | [`docs/guides/operations.md`](docs/guides/operations.md) |
| Something is wrong | [`docs/guides/troubleshooting.md`](docs/guides/troubleshooting.md) |
| Every procedure and alert in depth | [`docs/guides/runbook.md`](docs/guides/runbook.md) |
| Chart reference, values, delivery | [`deploy/README.md`](deploy/README.md) |

## Deployment

`deploy/` holds the OpenShift delivery: one rootless multi-target image
(`deploy/Containerfile`, role chosen by `CHEMCLAW_COMPONENT`) and a Helm chart
(`deploy/helm/chemclaw/`). See `deploy/README.md` for the topology (front-door
Route behind OIDC, the background worker plus one Temporal worker per connector
bundle that owns durable work, the connector servers, the migration and schedule
hook Jobs, and the plain secrets `values.yaml` declares). Every outbound credential
is a mounted secret: no component exchanges a workload-identity or On-Behalf-Of token.
The operational procedures — bring-up, upgrades, live lanes, troubleshooting — are
in `docs/guides/runbook.md`; `make kind-up` runs the same image and chart on a local
kind cluster (`deploy/kind/README.md`).

## Security

`SECURITY.md` describes the enforced posture (Entra OIDC at the front door, the
`require_actor` reject-if-absent rule, the single `authorize_trigger` gate, role-scoped
skills, the audit trail and note provenance), the `entra_required` enforcement switch, and the
live-infrastructure edges still open. Run shared/exposed deployments only with
`entra_required=true`.
