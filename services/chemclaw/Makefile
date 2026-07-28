# Chemclaw developer entrypoints. These are the ONLY invocations to use —
# CLAUDE.md and CI both go through them, so behavior stays identical everywhere.
# `uv run` executes inside the project venv without a manual activate step.

# Kubernetes API version the rendered chart is validated against. OpenShift 4.16 ships Kubernetes
# 1.29; override (`make helm-validate KUBE_VERSION=1.30.0`) when the target cluster moves.
KUBE_VERSION ?= 1.29.0

.DEFAULT_GOAL := help

.PHONY: help install lint type test cov check chat db-migrate schedules-apply kg-validate eval eln-validate skill-validate connector-validate datasource-validate template-validate connectors prose-validate helm-validate audit-verify reindex up down

help:  ## List every target with its one-line description (the default).
	@# Reads the `## ` comments beside each target, so a new target documents itself the day it is
	@# written and this list cannot drift from what the Makefile actually offers.
	@grep -hE '^[a-z][a-z0-9-]*:.*?## ' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

install:  ## Sync the venv with runtime + dev dependencies.
	uv sync

lint:  ## Ruff lint + format check (no writes; use `uv run ruff format` to fix).
	uv run ruff check .
	uv run ruff format --check .

type:  ## Static type check, strict (every first-party package; see tests/test_packaging.py).
	uv run mypy agents bo calc chemclaw connectors eln evals examples kg mcp_servers memory report safety scripts service sources templates tests workers workflows

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

connector-validate:  ## Validate the connector bundles (manifests, declarations, tool surface, jobs).
	uv run python -m scripts.validate_connectors

datasource-validate:  ## Validate the data-source manifests (halves resolve, config binds, names exist).
	uv run python -m scripts.validate_datasources

template-validate:  ## Validate the step templates (steps, references, tools/jobs/profiles named).
	uv run python -m scripts.validate_templates

connectors:  ## Run every enabled local connector's FastAPI app in one dev process.
	uv run python -m scripts.connectors_dev

prose-validate:  ## Check the agent's prose only names tools that exist (gap IDEA-7).
	uv run python -m scripts.validate_prose_contract

helm-validate:  ## Render the Helm chart and validate it against the Kubernetes schemas.
	@# `-ignore-missing-schemas` is required, not a relaxation of convenience: the chart renders an
	@# OpenShift `route.openshift.io/v1 Route`, and no JSON schema for it exists in kubeconform's
	@# defaults or in the datreeio CRDs catalog (both paths return 404). Without the flag this
	@# target can never pass — which is why it had never been seen to: the only workflow that ran
	@# it was stranded where GitHub Actions does not read (D-117).
	@#
	@# The flag skips a kind rather than failing it, so `tests/test_deploy_chart.py` pins exactly
	@# which kinds the chart renders. A new unvalidated CRD is then a deliberate edit to that test,
	@# not something that slips through as "skipped".
	@command -v helm >/dev/null || { echo "helm not installed - see docs/runbook.md"; exit 1; }
	@command -v kubeconform >/dev/null || { echo "kubeconform not installed - see docs/runbook.md"; exit 1; }
	helm template chemclaw deploy/helm/chemclaw \
	  | kubeconform -strict -summary -ignore-missing-schemas -kubernetes-version $(KUBE_VERSION) \
	      -schema-location default -schema-location \
	      'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'

audit-verify:  ## Verify the tamper-evident hash chain over the GxP audit trail (F10-G1).
	uv run python -m scripts.verify_audit_chain

reindex:  ## Rebuild the derived note index (dense + lexical) for hybrid retrieval (F10-A).
	uv run python -m report.vector_index

up:  ## Start the local dev stack (Temporal dev server + Postgres/pgvector).
	docker compose -f infra/docker-compose.yml up -d

down:  ## Stop the local dev stack.
	docker compose -f infra/docker-compose.yml down
