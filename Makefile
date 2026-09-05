# Chemclaw developer entrypoints. These are the ONLY invocations to use —
# CLAUDE.md and CI both go through them, so behavior stays identical everywhere.
# `uv run` executes inside the project venv without a manual activate step.

# Kubernetes API version the rendered chart is validated against. OpenShift 4.16 ships Kubernetes
# 1.29; override (`make helm-validate KUBE_VERSION=1.30.0`) when the target cluster moves.
KUBE_VERSION ?= 1.29.0

# The case-set version `eval-baseline-check` declares it is scoring. It must equal the
# `case_set_version` recorded in `data/evals/baseline.json`, or the check refuses to compare:
# aggregates over two different case-sets are different quantities, and a delta between them looks
# like a result while meaning nothing. Bump this together with a `make eval-baseline` refresh
# whenever the case-set itself changes — the mismatch is the tripwire that says you forgot.
# Bumped when the case set itself changes, because a baseline is only comparable to the set it was
# recorded on — `eval-baseline-check` refuses to compare two versions rather than reporting a drift
# between different quantities. 2026-08-25 added `autonomy-turn-cost`.
EVAL_CASE_SET_VERSION ?= autonomy-2026-08-25

# The two patterns that classify `deps-audit`'s output. Named here rather than inlined in the
# recipe so `tests/test_deploy_chart.py` can assert the classification against the same strings
# the target uses, instead of a second copy that can drift from it. There are no scratch-path
# variables beside them any more: the recipe classifies what the command *said*, holding it in a
# shell variable, and writes its one scratch file with `mktemp` (see the target).
# A real finding. Checked first and never excused, so an advisory whose text mentions a connection
# failure cannot be read as one.
AUDIT_FOUND := Found [0-9]+ known vulnerabilit
# The advisory database (or `pip-audit` itself) could not be reached. Both observed forms:
# `uvx` failing to fetch the tool, and `pip-audit` dying inside `requests`.
AUDIT_UNREACHABLE := ConnectionError|Failed to fetch|Max retries exceeded|Temporary failure in name resolution|Name or service not known|Network is unreachable

# Turn a rendered chart on stdin into the one `groups:` document `promtool check rules` reads.
#
# `promtool` wants a bare rule file; a `PrometheusRule` wraps its groups in `spec:`, and a dashboard
# hides its queries in a JSON string inside a ConfigMap. Both are unwrapped here so one `promtool`
# invocation covers every PromQL expression this chart ships — the alerts *and* the panels.
#
# Alert rules are passed through whole rather than reduced to their `expr`: `promtool` checks the
# `for:`/`labels:`/`annotations:` shape too, and a template that emitted a malformed annotation
# would otherwise pass. Panels have no rule shape, so each becomes a synthetic recording rule whose
# name carries the dashboard and panel it came from, which is what makes a failure locatable.
#
# Inlined as a variable rather than a file under `deploy/`, because `src/` is all the code
# (`tests/test_repo_map.py::test_no_import_package_sits_beside_data`) and this is a gate's argument,
# not a program anything imports.
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

# Enforce exit-on-error and pipefail for all recipes: a failing command in a pipeline does not
# pass silently when followed by a successful command. This is critical for the helm-validate
# target: if `helm template` fails and emits empty output, `kubeconform` would otherwise see no
# documents, print a clean summary, and exit 0 — masking a broken chart. Without this, CI would
# report the chart valid when it is not. (.SHELLFLAGS applies to all recipes; assignment is
# necessary because Make has no built-in way to set them).
SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c

.DEFAULT_GOAL := help

.PHONY: help install lint type test cov check ci chat db-migrate db-grants schedules-apply kg-validate proposals-reconcile synthesize eval eval-strict eval-baseline eval-baseline-check eln-validate skill-validate connector-validate datasource-validate sink-validate channel-validate sink-schema template-validate connectors prose-validate helm-validate explain user-erase reindex reindex-full up down phoenix-up phoenix-down phoenix-publish deps-audit live-infra live-infra-down live-up live-down live-status live-jobs live-probes live-template-args live-verifier-margin trajectory-census live-data live-plan-gate live-degradation live-storm live-soak live-soak-report leak-probe mutants mutant-results mutant-stats

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

leak-probe:  ## Drive real turns in one process and report what each one retains (needs `make live-up`).
	uv run python -m chemclaw.cli.leak_probe $(ARGS)

mutants:  ## Mutation-test the invariant-bearing modules (see [tool.mutmut]; slow, run deliberately).
	uv run mutmut run $(ARGS)

mutant-results:  ## Show the survivors from the last `make mutants` run.
	uv run mutmut results

mutant-stats:  ## Write the last run's per-category counts to mutants/mutmut-cicd-stats.json.
	@# The machine-readable half of `mutant-results`, and the one the weekly workflow gates on.
	@# `mutmut results` prints one line per non-killed mutant for a human; this writes the counts,
	@# so `.github/workflows/mutants.yml` can decide on a number rather than by grepping prose.
	uv run mutmut export-cicd-stats

check: lint type test  ## The fast inner-loop gate: lint + type + test (no coverage floor).

# `deps-audit` is in this list and in `.github/workflows/ci.yml` for the same reason: it was in
# neither. It ran only from `image.yml`, which triggers on `main` and on pull requests — so every
# branch push, and the whole documented pre-push gate, went green against a lockfile with known
# CVEs, and CLAUDE.md's "a green `make` locally means a green CI" was false for the supply chain
# alone. Last in the list rather than first: a dependency finding is a real failure but not one
# that should mask a broken test, and it is the one gate whose fix lives in `uv.lock` rather than
# in the diff under review.
ci: lint type cov kg-validate eval-strict eval-baseline-check eln-validate skill-validate connector-validate datasource-validate sink-validate channel-validate template-validate prose-validate helm-validate deps-audit  ## The full pre-push gate: lint + type + coverage + all validators + the dependency audit (what CI runs).

chat:  ## Chat with the agent from the terminal (admin/testing; needs CHEMCLAW_LLM_BASE_URL up).
	uv run chemclaw --admin

db-migrate:  ## Apply infra/sql migrations to the configured database.
	uv run python -m chemclaw.core.migrate
	@# The stored-message conversion is a second command, not a step inside the first: the kernel
	@# imports no other subpackage, and the converter lives in layer 1 (tests/test_layering.py).
	uv run python -m chemclaw.agent.message_migration

db-grants:  ## Reconcile the runtime role's privileges (run after db-migrate, on every deploy).
	@# Not part of `db-migrate`: the migrations are applied once per file and tracked, while the
	@# grants must be re-applied whenever the schema grows or the runtime role appears. Separate
	@# targets keep that difference visible; the chart's hook Job runs both, in this order.
	uv run python -m chemclaw.core.grants

schedules-apply:  ## Create/update the Temporal Schedules for the periodic background jobs.
	uv run python -m chemclaw.cli.schedules

kg-validate:  ## Validate the knowledge graph (schema, duplicate ids, broken links, citations).
	uv run python -m chemclaw.cli.validate_kg

proposals-reconcile:  ## Report merged proposal rows whose note the corpus does not hold (D-2026-08-27).
	uv run python -m chemclaw.cli.reconcile_proposals

synthesize:  ## Start a memory-synthesis job: KIND=campaign|playbook|optimization|observation-promotion [FRESH=1].
	@test -n "$(KIND)" || { echo "usage: make synthesize KIND=<kind> [FRESH=1]"; exit 64; }
	uv run python -m chemclaw.cli.synthesize $(KIND) $(if $(filter 1,$(FRESH)),--fresh,)

eval:  ## Score the versioned eval case-set and print the citable report (Phase 2b).
	uv run python -m chemclaw.evals.harness

eval-strict:  ## Score the case-set and FAIL on a science regression (what CI gates on).
	uv run python -m chemclaw.evals.harness --strict

eval-baseline-check:  ## Score the case-set against data/evals/baseline.json and FAIL on a worsening drift.
	uv run python -m chemclaw.evals.harness --case-set-version $(EVAL_CASE_SET_VERSION) --baseline

eval-baseline:  ## Regenerate data/evals/baseline.json from a scoring run (after a reviewed change).
# The version is passed, and it has to be: `refresh_baseline` defaults to "unversioned" while
# `eval-baseline-check` asks for $(EVAL_CASE_SET_VERSION), so the two targets used to disagree and a
# regenerated baseline failed the very check it was regenerated for. Found by running them in
# sequence, which is what adding a case makes you do.
	uv run python -m chemclaw.cli.refresh_baseline --case-set-version $(EVAL_CASE_SET_VERSION)

eln-validate:  ## Validate every enabled ingest source's reactions (RDKit structure + mass balance).
	@# The validator asks the registry what is attached, so the shipped gate has to say which
	@# sources it covers. Both file-drop adapters, which is what CI has always checked — a
	@# deployment runs the same command against its own CHEMCLAW_DATA_SOURCES.
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


sink-schema:  ## Print the DDL + registry seed a results database needs (apply it yourself).
	uv run python -m chemclaw.cli.sink_schema --all

template-validate:  ## Validate the step templates (steps, references, tools/jobs/profiles named).
	uv run python -m chemclaw.cli.validate_templates

connectors:  ## Run every enabled local connector's FastAPI app in one dev process.
	uv run python -m chemclaw.cli.connectors_dev

prose-validate:  ## Check the agent's prose only names tools that exist (gap IDEA-7).
	uv run python -m chemclaw.cli.validate_prose_contract

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
	@command -v promtool >/dev/null || { echo "promtool not installed - see docs/guides/runbook.md"; exit 1; }
	@# `--set networkPolicy.allowAnyDestination=true` because the chart refuses to render until a
	@# release states where its pods may talk (`templates/networkpolicy.yaml`), and
	@# `--set retention.unboundedGrowthAccepted=true` because it refuses until a release states what
	@# happens to the durable tables' history (`templates/config.yaml`). A validation render has
	@# neither destinations nor windows to enumerate, so it takes both escape hatches explicitly —
	@# the same sentences an operator has to write, which is why the flags are visible here.
	@#
	@# Twice, and the second render is the point: every switch this chart ships **off** was
	@# validated by nobody. `mcpFace.enabled` rendered a Deployment mounting a volume the pod did
	@# not declare and `monitoring.temporalSdkMetrics.enabled` rendered a container port name one
	@# character over the Kubernetes limit — both behind flags no gate had ever set, so the first
	@# thing that saw either was an operator's `helm upgrade`. The union render rather than one per
	@# flag: the flags are independent, so turning them all on covers each of them and costs one
	@# kubeconform invocation instead of three. (Neither of those two defects is one kubeconform can
	@# *see* — both are cross-field invariants no OpenAPI schema expresses. They are caught by
	@# `tests/test_deploy_chart.py`'s rendered-chart assertions, which walk the same variant set.
	@# What this arm adds is that a template behind an off-by-default flag is at least rendered and
	@# schema-checked at all.)
	@#
	@# **This list is a literal and the claim above it is not self-maintaining**, which is why
	@# `tests/test_deploy_chart.py::test_the_union_render_covers_every_switch_this_chart_ships_off`
	@# derives the real set from `values.yaml` and fails on this line the day a switch is added. It
	@# shipped covering three of six; `secrets.create` and `mcpFace.route.enabled` were rendered by
	@# nothing in `tests/`, this file or `.github/`. The two `--set`s after `alertmanager.enabled`
	@# are its prerequisites, not extra coverage: that template refuses to render with no receivers.
	@set -e; \
	  for flags in "" "--set mcpFace.enabled=true --set mcpFace.route.enabled=true --set documentShare.enabled=true --set monitoring.temporalSdkMetrics.enabled=true --set secrets.create=true --set monitoring.alertmanager.enabled=true --set-json monitoring.alertmanager.receivers=[{\"name\":\"chemclaw-oncall\"}] --set monitoring.alertmanager.defaultReceiver=chemclaw-oncall"; do \
	    helm template chemclaw deploy/helm/chemclaw \
	      --set networkPolicy.allowAnyDestination=true \
	      --set retention.unboundedGrowthAccepted=true $$flags \
	    | kubeconform -strict -summary -ignore-missing-schemas -kubernetes-version $(KUBE_VERSION) \
	        -schema-location default -schema-location \
	        'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'; \
	  done
	@# The externally-hosted connector (D-2026-08-09-a-connector-we-do-not-run), rendered because
	@# no shipped bundle sets `url` and so the default render above never takes that branch. Every
	@# other check on it reads the template *text*, which cannot see a `{{- if }}` nesting mistake;
	@# this is the only place the behaviour itself is exercised. `molfp` is an arbitrary choice —
	@# any bundle with an `endpoint:` proves the same three things.
	@# Matched with `case`, never `printf | grep -q` — under this file's `.SHELLFLAGS` (`-o
	@# pipefail`, line 16) that pipeline reports a *match* as a failure: `grep -q` exits the moment
	@# it matches, `printf` then dies of EPIPE, and pipefail takes the pipeline's status from it.
	@# Worse, it is size-dependent, so it passed against a small stub here and failed in CI on the
	@# real render — the output has to be long enough for grep to leave before printf finishes.
	@# `case` reads the variable in-process: no subprocess, no pipe, nothing to race.
	@set -e; \
	  render=$$(helm template chemclaw deploy/helm/chemclaw \
	    --set networkPolicy.allowAnyDestination=true \
	    --set retention.unboundedGrowthAccepted=true \
	    --set connectors.molfp.url=https://model.invalid/mcp); \
	  case "$$render" in *chemclaw-connector-molfp*) \
	    echo "FAIL: an externally hosted connector still gets a Deployment/Service"; exit 1;; esac; \
	  case "$$render" in *https://model.invalid/mcp*) ;; *) \
	    echo "FAIL: an externally hosted connector is missing from CHEMCLAW_CONNECTOR_URLS"; exit 1;; esac; \
	  case "$$render" in *chemclaw-connector-rxnfp*) ;; *) \
	    echo "FAIL: overriding one connector removed another's pods"; exit 1;; esac; \
	  echo "external-connector render OK: no pods, dialled at the given URL, siblings untouched"
	@# The PromQL, which nothing checked. `kubeconform` validates that `expr` is a *string*, not
	@# that the string parses — so a syntax error in a rule is accepted here, accepted by the API
	@# server, and then rejected by Prometheus at rule-group load, taking the **whole group** with
	@# it. That failure is silent from the cluster's side: the object exists and is `Valid` by every
	@# check this repo ran, and the alerts in it simply never evaluate.
	@#
	@# Both renders, because a rule behind a flag is a rule nothing else parses: the shipped
	@# defaults, and the one with the Temporal SDK exporter on, which is the only shape that renders
	@# `ChemclawWorkerNotPolling`. The dashboards go through the same check for the same reason at
	@# one remove — over a hundred panel queries that no other gate reads, where a mistyped one is a
	@# blank panel rather than an error.
	@set -e; \
	  work=$$(mktemp -d); trap 'rm -rf "$$work"' EXIT; \
	  printf '%s\n' "$$PROMQL_FROM_RENDER" > "$$work/extract.py"; \
	  for flag in "" "--set monitoring.temporalSdkMetrics.enabled=true"; do \
	    helm template chemclaw deploy/helm/chemclaw \
	      --set networkPolicy.allowAnyDestination=true \
	      --set retention.unboundedGrowthAccepted=true $$flag \
	      > "$$work/render.yaml"; \
	    uv run python "$$work/extract.py" < "$$work/render.yaml" > "$$work/rules.yaml"; \
	    promtool check rules "$$work/rules.yaml"; \
	  done

upstream-check:  ## Re-check every upstream shape this repo borrows (run on any langchain/langgraph/deepagents bump).
	@# The whole point of `tests/test_upstream_surface.py` is that a dependency bump becomes one
	@# conversation instead of six surprises, and that only works if somebody runs it *at* the bump.
	@# It is in the suite too, so this is a shortcut rather than a second gate — but a named target
	@# is what a bump checklist can point at. Two of its assertions check an *absence* (the MCP call
	@# timeout, the unreadable run counter), so a failure here can mean "upstream fixed it, go and
	@# delete our workaround" as easily as "upstream broke us".
	uv run pytest tests/test_upstream_surface.py -q
	@uv run python -c "import importlib.metadata as m; print('resolved: ' + ', '.join(f\"{p}=={m.version(p)}\" for p in ('langchain','langchain-core','langgraph','langgraph-checkpoint','deepagents','langchain-mcp-adapters')))"

deps-audit:  ## Check the locked dependency closure for known vulnerabilities (supply chain).
	@# Against the *lockfile* rather than the environment: the exact versions the image installs,
	@# not whatever happens to be resolved in a developer's venv. `--no-deps` because the export is
	@# already the fully-resolved set — re-resolving would audit a different closure than ships.
	@# `.github/workflows/image.yml` runs exactly this, blocking, so a finding is a red build
	@# rather than a report nobody opens.
	@#
	@# **A found vulnerability and an unreachable advisory database are different events, and
	@# `pip-audit` gives them the same exit code (1).** So the output is classified rather than the
	@# status trusted. This target joining `make ci` is what forced the question: `make ci` is the
	@# documented pre-push gate, a laptop on a train has no network, and failing it there teaches
	@# people to skip the gate. Measured under `unshare -rn`: `uvx` cannot fetch `pip-audit` itself
	@# (make error 2) or `pip-audit` runs and dies on `requests.exceptions.ConnectionError` (make
	@# error 1) — the same 1 a real finding exits with.
	@#
	@# The answer is asymmetric on purpose. Offline, unreachable is reported and tolerated: the
	@# developer keeps a usable gate and loses only the check that has no local answer anyway. In
	@# CI, where the network is a given, unreachable is a **failure** — a silent skip there is a
	@# supply-chain hole that reads as a green build forever, which is exactly the shape this
	@# target was added to close. `CI` is the signal because every runner sets it and nothing else
	@# has to be kept in sync.
	@#
	@# A real finding is never mistaken for an outage: `Found N known vulnerabilities` is checked
	@# first and fails unconditionally, so a connection string appearing in an advisory's text
	@# cannot buy an exemption.
	@#
	@# **The classified bytes are the ones the command produced, held in a variable.** They used to
	@# be read back from a log file the run piped into with `tee`, and that is a different question:
	@# `tee`'s own failure was never examined, so a `tee` that could not write left the greps reading
	@# whatever was already at that fixed, world-writable path. Measured — a real finding, a genuinely
	@# failing `tee` (read-only mount), and a stale log holding a connection error — the target
	@# printed "SKIPPED ... unreachable" and exited 0 on a vulnerable lockfile. Capturing removes the
	@# whole class: there is no second copy of the output that can disagree with the first. The one
	@# scratch file left is the export, and it is an `mktemp` rather than a fixed name, because a
	@# predictable path in a shared /tmp is a symlink someone else can plant.
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

explain:  ## Reconstruct why a session's tools ran: SESSION=<id> (D-166).
	@test -n "$(SESSION)" || { echo "usage: make explain SESSION=<session-id>"; exit 64; }
	uv run python -m chemclaw.cli.explain $(SESSION)

user-erase:  ## Offboard a person's conversational data: ACTOR=<oid> [APPLY=1]. Dry run by default.
	@test -n "$(ACTOR)" || { echo "usage: make user-erase ACTOR=<entra-oid> [APPLY=1]"; exit 64; }
	@# `APPLY` is compared to the literal `1`, not tested for non-emptiness. `$(if $(APPLY),...)` is
	@# a *non-empty* test, so `APPLY=0` and `APPLY=false` both read as true — and this is the one
	@# irreversible target in the file, where "I explicitly said no" must not commit a deletion.
	@# Anything other than `1` is a dry run, and an unrecognised value says so rather than guessing.
	@case "$(APPLY)" in \
	  ""|1) ;; \
	  *) echo "user-erase: APPLY=$(APPLY) is not 1 — running as a dry run. Use APPLY=1 to commit." ;; \
	esac
	uv run python -m chemclaw.cli.erase_actor $(ACTOR) $(if $(filter 1,$(APPLY)),--apply,)

reindex:  ## Incrementally rebuild the derived note index — only notes changed since last run.
	uv run python -m chemclaw.retrieval.vector_index

reindex-full:  ## Full note-index rebuild, ignoring stored fingerprints (recovery only).
	uv run python -m chemclaw.retrieval.vector_index --full

share-estimate:  ## Cost a mounted document share before indexing it (reads nothing). SHARE=<source>
	uv run python -m chemclaw.cli.sync_share $(SHARE) --dry-run

share-sync:  ## Crawl a mounted document share into the document index now. SHARE=<source>
	uv run python -m chemclaw.cli.sync_share $(SHARE)

up:  ## Start the local dev stack (Temporal dev server + Postgres/pgvector).
	docker compose -f infra/docker-compose.yml up -d

down:  ## Stop the local dev stack.
	docker compose -f infra/docker-compose.yml down

# The eval lane's reader (AG-13). Separate from `up`/`down` because it is opened deliberately to
# ask a question about a run, not needed to run anything. `phoenix-publish` takes DIR (a transcript
# directory) and NAME (what the experiment is called), and calls no model — the transcripts are the
# record and this reads them.
phoenix-up:  ## Start Phoenix, the eval lane's trace + experiment backend (UI on :6006).
	docker compose -f infra/docker-compose.observability.yml up -d

phoenix-down:  ## Stop Phoenix.
	docker compose -f infra/docker-compose.observability.yml down

phoenix-publish:  ## Publish an archived probe run to Phoenix. DIR=<transcripts> [NAME=<experiment>]
	uv run python -m chemclaw.cli.phoenix_publish $(DIR) $(if $(NAME),--name $(NAME),)

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

live-e2e-full-stack:  ## Full four-repo pass: this backend + Chemclaw3-mcp + Chemclaw3_mock + Chemclaw3_ui.
	bash infra/live/e2e-full-stack/up.sh up

live-e2e-full-stack-down:  ## Stop the four-repo pass.
	bash infra/live/e2e-full-stack/up.sh down

live-e2e-full-stack-status:  ## Show which four-repo-pass processes are running.
	bash infra/live/e2e-full-stack/up.sh status

live-jobs:  ## Run a real durable job end to end (Temporal + connector worker + Postgres; no LLM).
	uv run python -m chemclaw.cli.live_jobs

live-probes:  ## Ask the running front door the live probe set (needs a real model gateway).
	uv run python -m chemclaw.cli.live_probes $(ARGS)

# The half of `template-validate` that needs a session. `make template-validate` reads a tool's
# parameters out of this tree and cannot answer for a bundle we declare and do not run — seven
# shipped steps, reported by name as `unchecked_arguments` and unchecked. This opens the real
# connectors and checks the same arguments against what each running server advertises. Live-lane,
# never `ci`: `ci` must stay offline, and the row that asked for this proposed `connector-validate`,
# which is inside `ci` and would have answered `[]` for exactly those bundles.
# Exit 3 (not 1) means it could not reach something — reported, never counted as checked.
live-template-args:  ## Check every template's tool arguments against the running connector servers.
	uv run python -m chemclaw.cli.validate_template_args_live $(ARGS)

live-verifier-margin:  ## Re-roll the raw judge and measure its margin at the threshold (needs a model credential).
	uv run python -m chemclaw.cli.verifier_margin $(ARGS)

trajectory-census:  ## Count recurring tool-call trajectories over the stored sessions (the distiller's trigger).
	uv run python -m chemclaw.cli.trajectory_census $(ARGS)

# The corpus half of the same question `live-probes` asks of the model: not "did a tool answer"
# but "is the number in the answer the number in the paper". Checks every published measurement
# against what actually arrived, value by value. No model, for the reason `live-jobs` gives: a
# graded answer cannot separate a corpus that never held the data from a model that did not look
# for it. `ARGS="--corpus-only"` drops the Postgres half and needs no infrastructure at all.
#
# **It does not backfill by default, deliberately.** Making the seeded corpus reachable is a
# once-per-bring-up job that `infra/live/e2e-full-stack/up.sh` starts, and it takes over two hours
# (a PR-gate proposal costs ~1.8 s and there are 4,251 records). Re-running it on every check would
# re-walk all of them for no new rows. `ARGS="--backfill"` when you need it back.
live-data:  ## Check the seeded corpus against the published factor tables, value by value.
	uv run python -m chemclaw.cli.live_data $(ARGS)

# The two M12 re-validation suites. Separate targets rather than one, because each needs the
# stack configured a *different* way and no single invocation can hold both: the plan gate needs
# `CHEMCLAW_HARNESS_AUTONOMY=plan_only`, and the ordering check needs the durable broker
# deliberately stopped. Each exits non-zero on a failed check or on one it could not take.
#
# There was a third, `live-routing`. It measured the specialist team's routing accuracy, and
# D-2026-08-15 deleted the team, the challenge panel and that measurement together. The target
# outlived its suite and failed at argparse — `invalid choice: 'routing'` — so it is gone too.

# The one measurement that asks whether the tools are worth what they cost, by asking the same
# questions twice. The control arm is a *profile*, so it is the front door that needs
# `data/evals/profiles` on its profile path, not this client — `infra/live/processes.sh` puts it
# there, and the suite checks the front door accepted the profile before it spends anything,
# because a run whose control arm quietly fell back to the default agent would produce a report
# comparing one agent with itself.
live-ab:  ## Ask the probe corpus with and without tools and compare (needs a real model gateway).
	uv run python -m chemclaw.cli.live_probes --suite ab $(ARGS)

live-plan-gate:  ## M12: plan -> approve -> execute -> re-gate, live (needs harness_autonomy=plan_only).
	uv run python -m chemclaw.cli.live_probes --suite plan-gate $(ARGS)

live-degradation:  ## M12: capability_degraded must precede the first token (run with Temporal stopped).
	uv run python -m chemclaw.cli.live_probes --suite degradation $(ARGS)

live-storm:  ## Stress, chaos and adversarial pass against the live stack — mock model, no LLM calls.
	uv run python -m chemclaw.cli.live_storm $(ARGS)

live-soak:  ## Repeat the storm for hours and fit what drifts; checkpointed, so it survives a restart.
	bash infra/live/soak.sh $(ARGS)

live-soak-report:  ## Fit every series in the soak record so far.
	bash infra/live/soak.sh report
