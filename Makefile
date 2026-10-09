# Chemclaw developer entrypoints. CI, Jenkins and CLAUDE.md all go through these targets.

# Kubernetes version the chart is validated against (OpenShift 4.16 ships 1.29).
KUBE_VERSION ?= 1.29.0

# Must equal `case_set_version` in data/evals/baseline.json; bump with `make eval-baseline`.
EVAL_CASE_SET_VERSION ?= live-cost-2026-09-14

# pytest-xdist workers for `test` and `cov`. The gate is serial (0); 4 is a local opt-in, and a
# failure seen only in parallel is re-run serially. Not `auto`: each worker draws its own Postgres
# pools, so tests/conftest.py caps each worker's pool instead.
PYTEST_WORKERS ?= 0
PYTEST_XDIST := $(if $(filter-out 0,$(PYTEST_WORKERS)),-n $(PYTEST_WORKERS),)

# How deps-audit classifies pip-audit output; tests/test_deploy_chart.py asserts against these.
AUDIT_FOUND := Found [0-9]+ known vulnerabilit
AUDIT_UNREACHABLE := ConnectionError|Failed to fetch|Max retries exceeded|Temporary failure in name resolution|Name or service not known|Network is unreachable

# Rendered chart on stdin -> one `groups:` document for `promtool check rules` (alerts and
# dashboard panels). A variable, not a file, because `src/` is all the code.
export PROMQL_FROM_RENDER
define PROMQL_FROM_RENDER
import json, re, sys, yaml
rules = []
for doc in yaml.safe_load_all(sys.stdin):
    if not doc:
        continue
    if doc.get("kind") == "PrometheusRule":
        rules += [r for g in doc["spec"]["groups"] for r in g["rules"]]
    elif doc.get("kind") == "ConfigMap" and doc["metadata"]["name"].endswith("-dashboards"):
        for key, body in sorted(doc.get("data", {}).items()):
            board = re.sub(r"[^a-z0-9]+", "_", key.lower())
            for panel in json.loads(body)["panels"]:
                for i, target in enumerate(panel.get("targets", [])):
                    rules.append({"record": "panel:%s:%d:%d" % (board, panel["id"], i),
                                  "expr": target["expr"]})
if not rules:
    sys.exit("no PromQL found in the render - the extraction is broken, not the chart")
yaml.safe_dump({"groups": [{"name": "chart", "rules": rules}]}, sys.stdout, sort_keys=False)
endef

# pipefail: a failed `helm template` must not reach kubeconform as an empty, "valid" render.
SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c

.DEFAULT_GOAL := help

.PHONY: help install lint type test cov check ci deps-audit upstream-check architecture-baseline \
  mutants mutant-results mutant-stats kg-validate eval eval-strict eval-baseline-check \
  eval-baseline eln-validate skill-validate connector-validate datasource-validate sink-validate \
  channel-validate template-validate prose-validate helm-validate kind-validate up down db-migrate \
  db-grants schedules-apply connectors chat phoenix-up phoenix-down kind-up kind-down kind-status \
  kind-smoke synthesize reindex reindex-full share-estimate share-sync rekey-compounds user-erase \
  openapi sink-schema trajectory-census distill propose-profile live-infra live-infra-down live-up \
  live-down live-status live-e2e-full-stack live-e2e-full-stack-down live-e2e-full-stack-status \
  live-jobs live-probes live-ab live-delegation live-plan-gate live-degradation live-turn-cost \
  live-benchmark live-template-args live-verifier-margin live-data live-storm live-soak \
  live-soak-report live-leak-probe live-replicas \
  retrieval-arms hypothesis-recovery phoenix-publish explain model-text model-text-eval

help:  ## List every target, grouped by section.
	@awk 'BEGIN {FS = ":.*?## "} /^##@ / {printf "\n\033[1m%s\033[0m\n", substr($$0, 5)} \
		/^[a-z][a-z0-9-]*:.*?## / {printf "  \033[36m%-26s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

##@ Gate

install:  ## Sync the venv with runtime + dev dependencies.
	uv sync

lint:  ## Ruff lint + format check (no writes; use `uv run ruff format` to fix).
	uv run ruff check .
	uv run ruff format --check .

type:  ## Static type check, strict (the whole package, plus examples and tests).
	uv run mypy src examples tests

test:  ## Run the test suite (serial; `PYTEST_WORKERS=4` for ~2x, re-run a parallel-only failure serially).
	uv run pytest $(PYTEST_XDIST)

cov:  ## Run the test suite with coverage (first-party packages; report missing lines).
	uv run pytest --cov --cov-report=term-missing $(PYTEST_XDIST)

check: lint type test  ## The fast inner-loop gate: lint + type + test (no coverage floor).

# deps-audit runs last: a supply-chain finding must not mask a broken test.
ci: lint type cov kg-validate eval-strict eval-baseline-check eln-validate skill-validate connector-validate datasource-validate sink-validate channel-validate template-validate prose-validate helm-validate kind-validate live-replicas deps-audit  ## The full pre-push gate: lint + type + coverage + all validators + the dependency audit (what CI runs).

deps-audit:  ## Check the locked dependency closure for known vulnerabilities (supply chain).
	@# Audits the lockfile export, not the venv; classifies output because pip-audit exits 1 for both a
	@# finding and an unreachable database. Unreachable is tolerated locally and fails in CI.
	@scratch=$$(mktemp -d); trap 'rm -rf "$$scratch"' EXIT; \
	uv export --no-hashes --no-dev --format requirements-txt > "$$scratch/requirements.txt"; \
	report=$$(uvx pip-audit --no-deps --disable-pip -r "$$scratch/requirements.txt" 2>&1) && rc=0 || rc=$$?; \
	printf '%s\n' "$$report"; \
	if [ $$rc -ne 0 ]; then \
	  if grep -qE '$(AUDIT_FOUND)' <<<"$$report"; then exit $$rc; fi; \
	  if ! grep -qE '$(AUDIT_UNREACHABLE)' <<<"$$report"; then exit $$rc; fi; \
	  if [ -n "$${CI:-}" ]; then \
	    echo "deps-audit: the advisory database is unreachable and this is CI — the supply-chain"; \
	    echo "deps-audit: check cannot be skipped where the network is a given. Failing."; \
	    exit 1; \
	  fi; \
	  echo "deps-audit: SKIPPED - the advisory database is unreachable and CI is unset."; \
	  echo "deps-audit: the lockfile was NOT audited. Re-run with a network before you push."; \
	fi

upstream-check:  ## Re-check every upstream shape this repo borrows (run on any langchain/langgraph/deepagents bump).
	uv run pytest tests/test_upstream_surface.py -q
	@uv run python -c "import importlib.metadata as m; print('resolved: ' + ', '.join(f\"{p}=={m.version(p)}\" for p in ('langchain','langchain-core','langgraph','langgraph-checkpoint','deepagents','langchain-mcp-adapters')))"

architecture-baseline:  ## Measure the architecture programme's baseline (import, build, prose, sizes) to docs/planning/.
	uv run python -m chemclaw.cli.architecture_baseline

mutants:  ## Mutation-test the invariant-bearing modules (see [tool.mutmut]; slow, run deliberately).
	uv run mutmut run $(ARGS)

mutant-results:  ## Show the survivors from the last `make mutants` run.
	@# No run and no survivors print alike, so require evidence of a run first.
	@test -e .mutmut-cache -o -d mutants || \
		{ echo 'no mutation run found — run `make mutants` first (this is not "no survivors")'; exit 1; }
	uv run mutmut results

mutant-stats:  ## Write the last run's per-category counts to mutants/mutmut-cicd-stats.json.
	uv run mutmut export-cicd-stats

##@ Validators

kg-validate:  ## Validate the knowledge graph (schema, duplicate ids, broken links, citations).
	uv run python -m chemclaw.cli.validate_kg

eval:  ## Score the versioned eval case-set and print the citable report (Phase 2b).
	uv run python -m chemclaw.evals.harness

eval-strict:  ## Score the case-set and FAIL on a science regression (what CI gates on).
	uv run python -m chemclaw.evals.harness --strict

eval-baseline-check:  ## Score the case-set against data/evals/baseline.json and FAIL on a worsening drift.
	uv run python -m chemclaw.evals.harness --case-set-version $(EVAL_CASE_SET_VERSION) --baseline

eval-baseline:  ## Regenerate data/evals/baseline.json from a scoring run (after a reviewed change).
	uv run python -m chemclaw.cli.refresh_baseline --case-set-version $(EVAL_CASE_SET_VERSION)

eln-validate:  ## Validate every enabled ingest source's reactions (RDKit structure + mass balance).
	@# CI checks the two file-drop adapters; a deployment runs this against its own sources.
	CHEMCLAW_DATA_SOURCES=eln-json,eln-ord uv run python -m chemclaw.ingest.eln.validate

skill-validate:  ## Validate SKILL.md frontmatter (name/description present, name matches dir).
	uv run python -m chemclaw.cli.validate_skills

connector-validate:  ## Validate the connector bundles (manifests, declarations, tool surface, jobs).
	uv run python -m chemclaw.cli.validate_connectors

datasource-validate:  ## Validate the data-source manifests (halves resolve, config binds, names exist).
	uv run python -m chemclaw.cli.validate_datasources

sink-validate:  ## Validate the result-sink manifests (drivers resolve, config binds, names exist).
	uv run python -m chemclaw.cli.validate_sinks

channel-validate:  ## Validate every delivery-channel manifest against its driver's signature.
	uv run python -m chemclaw.cli.validate_channels

template-validate:  ## Validate the step templates (steps, references, tools/jobs/profiles named).
	uv run python -m chemclaw.cli.validate_templates

prose-validate:  ## Check the agent's prose only names tools that exist (gap IDEA-7).
	uv run python -m chemclaw.cli.validate_prose_contract

model-text:  ## Regenerate schema/model-text/inventory.json: every string a model reads, its owner and token cost.
	uv run python -m chemclaw.cli.model_text_inventory

model-text-eval:  ## Ship-or-not for a model-text batch: offline gate, then control vs candidate (ARGS=--dry-run for the plumbing).
	uv run python -m chemclaw.cli.model_text_eval $(ARGS)

helm-validate:  ## Render the Helm chart and validate it against the Kubernetes schemas.
	@# -ignore-missing-schemas: no schema exists for OpenShift's Route; tests/test_deploy_chart.py pins
	@# the rendered kinds. The second render turns on every off-by-default switch (derived and checked
	@# by tests/test_deploy_chart.py::test_the_union_render_covers_every_switch_this_chart_ships_off).
	@command -v helm >/dev/null || { echo "helm not installed - see docs/guides/runbook.md"; exit 1; }
	@command -v kubeconform >/dev/null || { echo "kubeconform not installed - see docs/guides/runbook.md"; exit 1; }
	@command -v promtool >/dev/null || { echo "promtool not installed - see docs/guides/runbook.md"; exit 1; }
	@set -e; \
	  for flags in "" "--set mcpFace.enabled=true --set mcpFace.route.enabled=true --set-json mcpFace.ingressNamespaces=[{\"network.openshift.io/policy-group\":\"ingress\"}] --set documentShare.enabled=true --set documentShare.accessMode=ReadWriteMany --set monitoring.temporalSdkMetrics.enabled=true --set secrets.create=true --set monitoring.alertmanager.enabled=true --set-json monitoring.alertmanager.receivers=[{\"name\":\"chemclaw-oncall\"}] --set monitoring.alertmanager.defaultReceiver=chemclaw-oncall --set keda.enabled=true"; do \
	    helm template chemclaw deploy/helm/chemclaw \
	      --set networkPolicy.allowAnyDestination=true \
	      --set retention.unboundedGrowthAccepted=true \
	      --set temporal.namespace=chemclaw $$flags \
	    | kubeconform -strict -summary -ignore-missing-schemas -kubernetes-version $(KUBE_VERSION) \
	        -schema-location default -schema-location \
	        'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'; \
	  done
	@# An externally hosted connector gets no pods and is dialled at its URL; `case`, not grep -q,
	@# because grep -q under pipefail reports a match as a failure.
	@set -e; \
	  render=$$(helm template chemclaw deploy/helm/chemclaw \
	    --set networkPolicy.allowAnyDestination=true \
	    --set retention.unboundedGrowthAccepted=true \
	    --set temporal.namespace=chemclaw \
	    --set connectors.molfp.url=https://model.invalid/mcp); \
	  case "$$render" in *chemclaw-connector-molfp*) \
	    echo "FAIL: an externally hosted connector still gets a Deployment/Service"; exit 1;; esac; \
	  case "$$render" in *https://model.invalid/mcp*) ;; *) \
	    echo "FAIL: an externally hosted connector is missing from CHEMCLAW_CONNECTOR_URLS"; exit 1;; esac; \
	  case "$$render" in *chemclaw-connector-rxnfp*) ;; *) \
	    echo "FAIL: overriding one connector removed another's pods"; exit 1;; esac; \
	  echo "external-connector render OK: no pods, dialled at the given URL, siblings untouched"
	@# promtool parses every alert and dashboard query; kubeconform only sees a string. The third render
	@# states retention windows, the only shape that renders ChemclawRetentionNotSweeping.
	@set -e; \
	  work=$$(mktemp -d); trap 'rm -rf "$$work"' EXIT; \
	  printf '%s\n' "$$PROMQL_FROM_RENDER" > "$$work/extract.py"; \
	  for flag in "" "--set monitoring.temporalSdkMetrics.enabled=true"; do \
	    helm template chemclaw deploy/helm/chemclaw \
	      --set networkPolicy.allowAnyDestination=true \
	      --set retention.unboundedGrowthAccepted=true \
	      --set temporal.namespace=chemclaw $$flag \
	      > "$$work/render.yaml"; \
	    uv run python "$$work/extract.py" < "$$work/render.yaml" > "$$work/rules.yaml"; \
	    promtool check rules "$$work/rules.yaml"; \
	  done; \
	  helm template chemclaw deploy/helm/chemclaw \
	    --set networkPolicy.allowAnyDestination=true \
	    --set retention.windows.CHEMCLAW_RETENTION_SESSION_MESSAGES_DAYS=365 \
	    --set retention.windows.CHEMCLAW_RETENTION_SESSION_EXHIBITS_DAYS=365 \
	    --set retention.artifactGrowthAccepted=true \
	    --set temporal.namespace=chemclaw \
	    > "$$work/render.yaml"; \
	  uv run python "$$work/extract.py" < "$$work/render.yaml" > "$$work/rules.yaml"; \
	  promtool check rules "$$work/rules.yaml"

kind-validate:  ## Offline: render the chart with deploy/kind/values-kind.yaml + the fleet, schema-check all of it.
	@# Strict, without -ignore-missing-schemas: a kind cluster has no CRD the default schemas lack.
	@command -v helm >/dev/null || { echo "helm not installed - see docs/guides/runbook.md"; exit 1; }
	@command -v kubeconform >/dev/null || { echo "kubeconform not installed - see docs/guides/runbook.md"; exit 1; }
	@command -v kubectl >/dev/null || { echo "kubectl not installed (render-fleet.sh uses kubectl kustomize)"; exit 1; }
	helm template chemclaw deploy/helm/chemclaw --namespace chemclaw -f deploy/kind/values-kind.yaml \
	  | kubeconform -strict -summary -kubernetes-version $(KUBE_VERSION)
	helm template chemclaw deploy/helm/chemclaw --namespace chemclaw -f deploy/kind/values-kind.yaml \
	  -f deploy/kind/values-kind-oidc-mock.yaml \
	  | kubeconform -strict -summary -kubernetes-version $(KUBE_VERSION)
	kubeconform -strict -summary -kubernetes-version $(KUBE_VERSION) deploy/kind/manifests
	@set -e; mcp="$${CHEMCLAW_MCP_REPO:-.sibling/Chemclaw3-mcp}"; \
	  [ -d "$$mcp/servers" ] || { echo "kind-validate: no Chemclaw3-mcp checkout at $$mcp — set CHEMCLAW_MCP_REPO"; exit 1; }; \
	  bash deploy/kind/render-fleet.sh "$$mcp" kind \
	    | kubeconform -strict -summary -kubernetes-version $(KUBE_VERSION)

##@ Dev stack

up:  ## Start the local dev stack (Temporal dev server + Postgres/pgvector).
	docker compose -f infra/docker-compose.yml up -d

down:  ## Stop the local dev stack.
	docker compose -f infra/docker-compose.yml down

db-migrate:  ## Apply infra/sql migrations to the configured database.
	uv run python -m chemclaw.core.migrate
	@# A second command: the kernel imports no other subpackage, and the converter is layer 1.
	uv run python -m chemclaw.agent.message_migration

db-grants:  ## Reconcile the runtime role's privileges (run after db-migrate, on every deploy).
	@# Separate from db-migrate: migrations apply once, grants re-apply whenever the schema grows.
	uv run python -m chemclaw.core.grants

schedules-apply:  ## Create/update the Temporal Schedules for the periodic background jobs.
	uv run python -m chemclaw.cli.schedules

connectors:  ## Run every enabled local connector's FastAPI app in one dev process.
	uv run python -m chemclaw.cli.connectors_dev

chat:  ## Chat with the agent from the terminal (admin/testing; needs CHEMCLAW_LLM_BASE_URL up).
	@# The local lane's gateway is the loopback mock, which every model-calling process refuses unless allowed.
	CHEMCLAW_LLM_ALLOW_LOOPBACK_GATEWAY=$${CHEMCLAW_LLM_ALLOW_LOOPBACK_GATEWAY:-true} uv run chemclaw --admin

phoenix-up:  ## Start Phoenix, the eval lane's trace + experiment backend (UI on :6006).
	docker compose -f infra/docker-compose.observability.yml up -d

phoenix-down:  ## Stop Phoenix.
	docker compose -f infra/docker-compose.observability.yml down

kind-up:  ## The whole system on a local kind cluster: production images + chart, then a smoke (deploy/kind/).
	bash deploy/kind/up.sh up

kind-down:  ## Delete the local kind cluster and everything in it.
	bash deploy/kind/up.sh down

kind-status:  ## Pods, jobs, the release and the host URLs of the local kind cluster.
	bash deploy/kind/up.sh status

kind-smoke:  ## Re-run the kind cluster's smoke: /healthz, /readyz, UI, a mock-LLM turn, a durable job.
	bash deploy/kind/up.sh smoke

##@ Data jobs

synthesize:  ## Start a memory-synthesis job: KIND=campaign|playbook|optimization|observation-promotion [FRESH=1].
	@test -n "$(KIND)" || { echo "usage: make synthesize KIND=<kind> [FRESH=1]"; exit 64; }
	uv run python -m chemclaw.cli.synthesize $(KIND) $(if $(filter 1,$(FRESH)),--fresh,)

reindex:  ## Incrementally rebuild the derived note index — only notes changed since last run.
	uv run python -m chemclaw.retrieval.vector_index

reindex-full:  ## Full note-index rebuild, ignoring stored fingerprints (recovery only).
	uv run python -m chemclaw.retrieval.vector_index --full

share-estimate:  ## Cost a mounted document share before indexing it (reads nothing). SHARE=<source>
	@test -n "$(SHARE)" || { echo "usage: make share-estimate SHARE=<source>"; exit 64; }
	uv run python -m chemclaw.cli.sync_share $(SHARE) --dry-run

share-sync:  ## Crawl a mounted document share into the document index now. SHARE=<source>
	@test -n "$(SHARE)" || { echo "usage: make share-sync SHARE=<source>"; exit 64; }
	uv run python -m chemclaw.cli.sync_share $(SHARE)

# APPLY compares to the literal 1, so APPLY=0 or APPLY=false never writes.
rekey-compounds:  ## Carry compound notes and fingerprint rows across a standardization bump [APPLY=1 [DISPOSE=1]]. Preview by default.
	@case "$(APPLY)" in \
	  ""|1) ;; \
	  *) echo "rekey-compounds: APPLY=$(APPLY) is not 1 — previewing. Use APPLY=1 to write." ;; \
	esac
	uv run python -m chemclaw.cli.rekey_compounds $(if $(filter 1,$(APPLY)),--apply,) $(if $(filter 1,$(DISPOSE)),--dispose-superseded,)

user-erase:  ## Offboard a person's conversational data: ACTOR=<oid> [APPLY=1]. Dry run by default.
	@test -n "$(ACTOR)" || { echo "usage: make user-erase ACTOR=<entra-oid> [APPLY=1]"; exit 64; }
	@case "$(APPLY)" in \
	  ""|1) ;; \
	  *) echo "user-erase: APPLY=$(APPLY) is not 1 — running as a dry run. Use APPLY=1 to commit." ;; \
	esac
	uv run python -m chemclaw.cli.erase_actor $(ACTOR) $(if $(filter 1,$(APPLY)),--apply,)

openapi:  ## Regenerate schema/api/openapi.json, the published API contract (offline; review the diff).
	uv run python -m chemclaw.cli.openapi

sink-schema:  ## Print the DDL + registry seed a results database needs (apply it yourself).
	uv run python -m chemclaw.cli.sink_schema --all

trajectory-census:  ## Count recurring tool-call trajectories over the stored sessions (the distiller's trigger).
	uv run python -m chemclaw.cli.trajectory_census $(ARGS)

distill:  ## Distil recurring trajectories into skill proposals (dry; ARGS="--propose" to file).
	uv run python -m chemclaw.cli.distill $(ARGS)

propose-profile:  ## Propose an agent profile from observed tool co-occurrence (dry; ARGS="--propose").
	uv run python -m chemclaw.cli.propose_profile $(ARGS)

##@ Live lane

# Run against a running stack, never in `ci`: they need a front door, a broker or a model gateway.

live-infra:  ## Start Postgres/pgvector + Temporal for the live lane (uses Docker when available).
	bash infra/live/bootstrap.sh up

live-infra-down:  ## Stop the Postgres and Temporal this lane created (never a stack it adopted).
	bash infra/live/bootstrap.sh down

live-up:  ## Start the live processes: connectors, the Temporal workers, the front door.
	bash infra/live/processes.sh up

live-down:  ## Stop the live processes.
	bash infra/live/processes.sh down

live-status:  ## Show which live processes are running.
	bash infra/live/processes.sh status

live-e2e-full-stack:  ## Full four-repo pass: this backend + Chemclaw3-mcp + Chemclaw3_mock + Chemclaw3_ui.
	bash infra/live/e2e-full-stack/up.sh up

live-e2e-full-stack-down:  ## Stop the four-repo pass.
	bash infra/live/e2e-full-stack/up.sh down

live-e2e-full-stack-status:  ## Show which four-repo-pass processes are running.
	bash infra/live/e2e-full-stack/up.sh status

live-jobs:  ## Run a real durable job end to end (Temporal + connector worker + Postgres; no LLM).
	uv run python -m chemclaw.cli.live_jobs

live-probes:  ## Ask the running front door the live probe set (exit 3 unreached, 2 ungraded).
	uv run python -m chemclaw.cli.live_probes $(ARGS)

live-ab:  ## Ask the probe corpus against the prompt-swapping control arm and compare (real gateway).
	uv run python -m chemclaw.cli.live_probes --suite ab $(ARGS)

live-delegation:  ## The delegation experiment: drive every arm and compare (real gateway).
	uv run python -m chemclaw.cli.live_probes --suite delegation $(ARGS)

live-plan-gate:  ## M12: plan -> approve -> execute -> re-gate, live (needs harness_autonomy=plan_only).
	uv run python -m chemclaw.cli.live_probes --suite plan-gate $(ARGS)

live-degradation:  ## M12: capability_degraded must precede the first token (run with Temporal stopped).
	uv run python -m chemclaw.cli.live_probes --suite degradation $(ARGS)

live-turn-cost:  ## Score `turn_cost_ratio` over turns this system really ran (exit 3 unreached).
	uv run python -m chemclaw.cli.live_turn_cost $(ARGS)

live-benchmark:  ## Score this system on the vendored ChemBench subset (exit 3 unreached).
	uv run python -m chemclaw.cli.live_benchmark $(ARGS)

live-template-args:  ## Check every template's tool arguments against the running connector servers.
	uv run python -m chemclaw.cli.validate_template_args_live $(ARGS)

live-verifier-margin:  ## Re-roll the raw judge and measure its margin at the threshold (needs a model credential).
	uv run python -m chemclaw.cli.verifier_margin $(ARGS)

live-data:  ## Check the seeded corpus against the published factor tables, value by value.
	uv run python -m chemclaw.cli.live_data $(ARGS)

live-storm:  ## Stress, chaos and adversarial pass against the live stack — mock model, no LLM calls.
	uv run python -m chemclaw.cli.live_storm $(ARGS)

live-soak:  ## Repeat the storm for hours and fit what drifts; checkpointed, so it survives a restart.
	bash infra/live/soak.sh $(ARGS)

live-soak-report:  ## Fit every series in the soak record so far.
	bash infra/live/soak.sh report

# Under `make ci` the lane may skip, named and counted, where its prerequisites are absent; asked for
# directly, or by CI's `replicas` job, it fails instead (infra/live/replicas.sh).
live-replicas: export LIVE_REPLICAS_OPTIONAL := $(if $(filter ci,$(MAKECMDGOALS)),1,)
live-replicas:  ## Three front doors + two background and two calc workers on one database: limits, resume, single-flight (no model key).
	bash infra/live/replicas.sh $(ARGS)

live-leak-probe:  ## Drive real turns in one process and report what each one retains (needs `make live-up`).
	uv run python -m chemclaw.cli.leak_probe $(ARGS)

retrieval-arms:  ## Score retrieval configurations against the labelled gold set (needs `make up`).
	uv run python -m chemclaw.cli.retrieval_arms $(ARGS)

hypothesis-recovery:  ## Reproduce the ADR's tournament-recovery table against a null control.
	uv run python -m chemclaw.cli.hypothesis_recovery $(ARGS)

phoenix-publish:  ## Publish an archived probe run to Phoenix. DIR=<transcripts> [NAME=<experiment>]
	@test -n "$(DIR)" || { echo "usage: make phoenix-publish DIR=<transcripts> [NAME=<experiment>]"; exit 64; }
	uv run python -m chemclaw.cli.phoenix_publish $(DIR) $(if $(NAME),--name $(NAME),)

##@ Tools

explain:  ## Reconstruct why a session's tools ran: SESSION=<id> (D-166).
	@test -n "$(SESSION)" || { echo "usage: make explain SESSION=<session-id>"; exit 64; }
	uv run python -m chemclaw.cli.explain $(SESSION)
