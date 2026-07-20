# Chemclaw developer entrypoints. These are the ONLY invocations to use —
# CLAUDE.md and CI both go through them, so behavior stays identical everywhere.
# `uv run` executes inside the project venv without a manual activate step.

.PHONY: install lint type test check db-migrate kg-validate eval up down

install:  ## Sync the venv with runtime + dev dependencies.
	uv sync

lint:  ## Ruff lint + format check (no writes; use `uv run ruff format` to fix).
	uv run ruff check .
	uv run ruff format --check .

type:  ## Static type check, strict (all first-party packages).
	uv run mypy chemclaw agents bo calc evals kg workflows workers tests

test:  ## Run the test suite.
	uv run pytest

check: lint type test  ## The full gate CLAUDE.md requires green before a step is "done".

db-migrate:  ## Apply infra/sql migrations to the configured database.
	uv run python -m calc.migrate

kg-validate:  ## Validate the knowledge graph (schema, duplicate ids, broken links).
	uv run python -m kg.validate

eval:  ## Score the versioned eval case-set and print the citable report (Phase 2b).
	uv run python -m evals.harness

up:  ## Start the local dev stack (Temporal dev server + Postgres/pgvector).
	docker compose -f infra/docker-compose.yml up -d

down:  ## Stop the local dev stack.
	docker compose -f infra/docker-compose.yml down
