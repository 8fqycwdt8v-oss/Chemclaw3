# Chemclaw developer entrypoints. These are the ONLY invocations to use —
# CLAUDE.md and CI both go through them, so behavior stays identical everywhere.
# `uv run` executes inside the project venv without a manual activate step.

# Kubernetes API version the rendered chart is validated against. OpenShift 4.16 ships Kubernetes
# 1.29; override (`make helm-validate KUBE_VERSION=1.30.0`) when the target cluster moves.
KUBE_VERSION ?= 1.29.0

# Enforce exit-on-error and pipefail for all recipes: a failing command in a pipeline does not
# pass silently when followed by a successful command. This is critical for the helm-validate
# target: if `helm template` fails and emits empty output, `kubeconform` would otherwise see no
# documents, print a clean summary, and exit 0 — masking a broken chart. Without this, CI would
# report the chart valid when it is not. (.SHELLFLAGS applies to all recipes; assignment is
# necessary because Make has no built-in way to set them).
SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c

.DEFAULT_GOAL := help

.PHONY: help install lint type test cov check ci chat db-migrate schedules-apply kg-validate eval eval-strict eval-baseline eln-validate skill-validate connector-validate datasource-validate template-validate connectors prose-validate safety-validate helm-validate audit-verify explain reindex reindex-full up down deps-audit live-infra live-infra-down live-up live-down live-status live-jobs live-probes live-storm live-soak live-soak-report mutants mutant-results

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

type:  ## Static type check, strict (the whole package, plus examples and tests).
	uv run mypy src examples tests

test:  ## Run the test suite.
	uv run pytest

cov:  ## Run the test suite with coverage (first-party packages; report missing lines).
	uv run pytest --cov --cov-report=term-missing

mutants:  ## Mutation-test the invariant-bearing modules (see [tool.mutmut]; slow, run deliberately).
	uv run mutmut run $(ARGS)

mutant-results:  ## Show the survivors from the last `make mutants` run.
	uv run mutmut results

check: lint type test  ## The fast inner-loop gate: lint + type + test (no coverage floor).

ci: lint type cov kg-validate eval-strict eln-validate skill-validate connector-validate datasource-validate template-validate prose-validate safety-validate helm-validate  ## The full pre-push gate: lint + type + coverage + all validators (what CI runs).

chat:  ## Chat with the agent from the terminal (admin/testing mode; needs ANTHROPIC_API_KEY).
	uv run chemclaw --admin

db-migrate:  ## Apply infra/sql migrations to the configured database.
	uv run python -m chemclaw.science.calc.migrate

schedules-apply:  ## Create/update the Temporal Schedules for the periodic background jobs.
	uv run python -m chemclaw.cli.schedules

kg-validate:  ## Validate the knowledge graph (schema, duplicate ids, broken links).
	uv run python -m chemclaw.kg.validate

eval:  ## Score the versioned eval case-set and print the citable report (Phase 2b).
	uv run python -m chemclaw.evals.harness

eval-strict:  ## Score the case-set and FAIL on a science regression (what CI gates on).
	uv run python -m chemclaw.evals.harness --strict

eval-baseline:  ## Regenerate data/evals/baseline.json from a scoring run (after a reviewed change).
	uv run python -m chemclaw.cli.refresh_baseline

eln-validate:  ## Validate the ELN export's reactions (RDKit structure + mass balance).
	uv run python -m chemclaw.ingest.eln.validate

skill-validate:  ## Validate SKILL.md frontmatter (name/description present, name matches dir).
	uv run python -m chemclaw.cli.validate_skills

connector-validate:  ## Validate the connector bundles (manifests, declarations, tool surface, jobs).
	uv run python -m chemclaw.cli.validate_connectors

datasource-validate:  ## Validate the data-source manifests (halves resolve, config binds, names exist).
	uv run python -m chemclaw.cli.validate_datasources

template-validate:  ## Validate the step templates (steps, references, tools/jobs/profiles named).
	uv run python -m chemclaw.cli.validate_templates

connectors:  ## Run every enabled local connector's FastAPI app in one dev process.
	uv run python -m chemclaw.cli.connectors_dev

prose-validate:  ## Check the agent's prose only names tools that exist (gap IDEA-7).
	uv run python -m chemclaw.cli.validate_prose_contract

safety-validate:  ## Force-compile the safety rule/alert tables (catches a bad table at deploy, not on first use).
	uv run python -m chemclaw.cli.validate_safety

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
	@command -v helm >/dev/null || { echo "helm not installed - see docs/guides/runbook.md"; exit 1; }
	@command -v kubeconform >/dev/null || { echo "kubeconform not installed - see docs/guides/runbook.md"; exit 1; }
	helm template chemclaw deploy/helm/chemclaw \
	  | kubeconform -strict -summary -ignore-missing-schemas -kubernetes-version $(KUBE_VERSION) \
	      -schema-location default -schema-location \
	      'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'

audit-verify:  ## Verify the tamper-evident hash chain over the GxP audit trail (F10-G1).
	uv run python -m chemclaw.cli.verify_audit_chain

deps-audit:  ## Check the locked dependency closure for known vulnerabilities (supply chain).
	@# Against the *lockfile* rather than the environment: the exact versions the image installs,
	@# not whatever happens to be resolved in a developer's venv. `--no-deps` because the export is
	@# already the fully-resolved set — re-resolving would audit a different closure than ships.
	@# `.github/workflows/image.yml` runs exactly this, blocking, so a finding is a red build
	@# rather than a report nobody opens.
	uv export --no-hashes --no-dev --format requirements-txt > /tmp/chemclaw-requirements.txt
	uvx pip-audit --no-deps --disable-pip -r /tmp/chemclaw-requirements.txt

explain:  ## Reconstruct why a session's tools ran: SESSION=<id> (D-166).
	@test -n "$(SESSION)" || { echo "usage: make explain SESSION=<session-id>"; exit 64; }
	uv run python -m chemclaw.cli.explain $(SESSION)

reindex:  ## Incrementally rebuild the derived note index — only notes changed since last run.
	uv run python -m chemclaw.retrieval.vector_index

reindex-full:  ## Full note-index rebuild, ignoring stored fingerprints (recovery only).
	uv run python -m chemclaw.retrieval.vector_index --full

up:  ## Start the local dev stack (Temporal dev server + Postgres/pgvector).
	docker compose -f infra/docker-compose.yml up -d

down:  ## Stop the local dev stack.
	docker compose -f infra/docker-compose.yml down

# The live lane. Four targets rather than one, because the stages answer different questions and
# only the last needs a model credential: `live-infra` provides what `make up` provides where there
# is no Docker daemon, `live-up` starts the six processes the README has always listed by hand,
# `live-jobs` proves the durable path (Temporal + workers + Postgres, no LLM), and `live-probes`
# adds the model on top. Run against a deployment, never on a diff — none of these is in `make ci`.

live-infra:  ## Start Postgres/pgvector + Temporal for the live lane (uses Docker when available).
	bash infra/live/bootstrap.sh up

live-infra-down:  ## Stop the live lane's Postgres and Temporal.
	bash infra/live/bootstrap.sh down

live-up:  ## Start the live processes: connectors, the four Temporal workers, the front door.
	bash infra/live/processes.sh up

live-down:  ## Stop the live processes.
	bash infra/live/processes.sh down

live-status:  ## Show which live processes are running.
	bash infra/live/processes.sh status

live-jobs:  ## Run a real durable job end to end (Temporal + connector worker + Postgres; no LLM).
	uv run python -m chemclaw.cli.live_jobs

live-probes:  ## Ask the running front door the live probe set (needs ANTHROPIC_API_KEY).
	uv run python -m chemclaw.cli.live_probes $(ARGS)

live-storm:  ## Stress, chaos and adversarial pass against the live stack — mock model, no LLM calls.
	uv run python -m chemclaw.cli.live_storm $(ARGS)

live-soak:  ## Repeat the storm for hours and fit what drifts; checkpointed, so it survives a restart.
	bash infra/live/soak.sh $(ARGS)

live-soak-report:  ## Fit every series in the soak record so far.
	bash infra/live/soak.sh report
