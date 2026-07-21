# Chemclaw developer entrypoints. These are the ONLY invocations to use —
# CLAUDE.md and CI both go through them, so behavior stays identical everywhere.
# `uv run` executes inside the project venv without a manual activate step.

.PHONY: install lint type test cov check chat db-migrate schedules-apply kg-validate eval eln-validate skill-validate up down

install:  ## Sync the venv with runtime + dev dependencies.
	uv sync

lint:  ## Ruff lint + format check (no writes; use `uv run ruff format` to fix).
	uv run ruff check .
	uv run ruff format --check .

type:  ## Static type check, strict (all first-party packages).
	uv run mypy chemclaw agents bo calc eln evals kg mcp_servers memory report scripts workflows workers tests

test:  ## Run the test suite.
	uv run pytest

cov:  ## Run the test suite with coverage (first-party packages; report missing lines).
	uv run pytest --cov --cov-report=term-missing

check: lint type test  ## The full gate CLAUDE.md requires green before a step is "done".

chat:  ## Chat with the agent from the terminal (admin/testing mode; needs ANTHROPIC_API_KEY).
	uv run chemclaw --admin

db-migrate:  ## Apply infra/sql migrations to the configured database.
	uv run python -m calc.migrate

schedules-apply:  ## Create/update the Temporal Schedules for the periodic background jobs.
	uv run python -m scripts.schedules

kg-validate:  ## Validate the knowledge graph (schema, duplicate ids, broken links).
	uv run python -m kg.validate

eval:  ## Score the versioned eval case-set and print the citable report (Phase 2b).
	uv run python -m evals.harness

eln-validate:  ## Validate the ELN export's reactions (RDKit structure + mass balance).
	uv run python -m eln.validate

skill-validate:  ## Validate SKILL.md frontmatter (name/description present, name matches dir).
	uv run python -m scripts.validate_skills

up:  ## Start the local dev stack (Temporal dev server + Postgres/pgvector).
	docker compose -f infra/docker-compose.yml up -d

down:  ## Stop the local dev stack.
	docker compose -f infra/docker-compose.yml down
