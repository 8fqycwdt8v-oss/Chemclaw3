#!/usr/bin/env bash
# Apply a release descriptor to a Databricks workspace.
#
# **What Databricks is to this system, stated plainly, because the pipeline is only honest if this
# is.** Databricks appears in ChemClaw3 in four different roles, and only two of them are things a
# release *deploys*:
#
#   1. The ELN / warehouse       — `src/chemclaw/ingest/sources/eln-databricks/datasource.yaml`, a
#                                  binding over a SQL warehouse. Deployed by *nobody*: it is the
#                                  site's existing lakehouse. What a release owes it is a preflight
#                                  (does the warehouse this environment names still exist?) and the
#                                  two credentials the binding reads by name.
#   2. The LLM endpoint          — Mosaic AI Model Serving is OpenAI-compatible, which is what the
#                                  F0 provider seam expects. Same deal: preflight, not deploy.
#   3. Heavy compute             — Databricks Jobs running the semiempirical calculation server.
#                                  A bundle deploys these.
#   4. Workspace assets          — bundles, apps, jobs. This is the half that is a deployment.
#
# So this target deploys 3 and 4 and *checks* 1 and 2. It does not host Postgres, Temporal or the
# Temporal worker fleet, and pretending otherwise in a script would be the most expensive kind of
# wrong. A deployment that runs the services on OpenShift and depends on Databricks for the three
# above is the normal shape, and `openshift.sh` is that half.
#
# Nothing here invents a bundle. `.components[<name>].bundle` must point at a real directory holding
# a `databricks.yml`; there is none in this repository yet, so this target refuses rather than
# guesses. That refusal is the honest state of the Databricks half and is recorded as such in
# `docs/decisions/` and `docs/planning/DEFERRED.md`.
set -euo pipefail

DESCRIPTOR="${1:?usage: databricks.sh <release-descriptor.json>}"
DRY_RUN="${DRY_RUN:-true}"
: "${DATABRICKS_HOST:?DATABRICKS_HOST must name the workspace}"
: "${DATABRICKS_TOKEN:?DATABRICKS_TOKEN must be bound from a Jenkins credential}"

command -v jq >/dev/null || { echo "jq is required to read the descriptor" >&2; exit 1; }
command -v databricks >/dev/null || { echo "the databricks CLI is required" >&2; exit 1; }

log() { printf '\033[36m[databricks]\033[0m %s\n' "$*" >&2; }
TARGET="$(jq -r '.environment' "${DESCRIPTOR}")"

# The dependencies a release consumes but does not create. Checked before anything is deployed,
# because the failure they cause otherwise is silent in the worst way: a front door that starts,
# passes both probes, and fails at the first turn — `/readyz` probes connectors and knows nothing
# about a model-serving endpoint.
preflight() {
  local endpoint warehouse
  endpoint="$(jq -r '.databricks.servingEndpoint // ""' "${DESCRIPTOR}")"
  warehouse="$(jq -r '.databricks.sqlWarehouseId // ""' "${DESCRIPTOR}")"

  if [ -n "${endpoint}" ]; then
    log "preflight: serving endpoint ${endpoint}"
    databricks serving-endpoints get "${endpoint}" >/dev/null \
      || { echo "serving endpoint '${endpoint}' is not reachable — CHEMCLAW_LLM_BASE_URL for this environment points at it" >&2; return 1; }
  fi
  if [ -n "${warehouse}" ]; then
    log "preflight: SQL warehouse ${warehouse}"
    databricks warehouses get "${warehouse}" >/dev/null \
      || { echo "SQL warehouse '${warehouse}' is not reachable — the eln-databricks binding names it" >&2; return 1; }
  fi
}

apply_bundle() {
  local name bundle
  name="$1"
  bundle="$(jq -r --arg n "${name}" '.components[$n].bundle // ""' "${DESCRIPTOR}")"
  [ -n "${bundle}" ] || { echo "component '${name}' is kind 'bundle' but names no .bundle directory" >&2; return 1; }
  [ -f "${bundle}/databricks.yml" ] || {
    echo "no databricks.yml under '${bundle}'. This repository ships no asset bundle yet: a"
    echo "Databricks deployment of the compute half needs one (jobs, and the serving endpoint if"
    echo "the environment owns it). Point .bundle at the repository that holds it." >&2
    return 1
  }

  # `validate` IS the dry run — `bundle deploy` has no such flag — and it is worth running in both
  # modes: it resolves variables and the target, which is where a wrong environment name surfaces.
  log "${name}: validating bundle in ${bundle} (target ${TARGET})"
  (cd "${bundle}" && databricks bundle validate --target "${TARGET}")
  if [ "${DRY_RUN}" = "true" ]; then
    log "${name}: dry run — validated, not deployed"
    return
  fi
  log "${name}: deploying bundle (target ${TARGET})"
  (cd "${bundle}" && databricks bundle deploy --target "${TARGET}")
}

apply_app() {
  local name app source
  name="$1"
  app="$(jq -r --arg n "${name}" '.components[$n].app' "${DESCRIPTOR}")"
  source="$(jq -r --arg n "${name}" '.components[$n].sourceCodePath' "${DESCRIPTOR}")"
  if [ "${DRY_RUN}" = "true" ]; then
    log "${name}: would deploy app '${app}' from ${source}"
    databricks apps get "${app}" >/dev/null || log "${name}: app '${app}' does not exist yet"
    return
  fi
  log "${name}: deploying app '${app}' from ${source}"
  databricks apps deploy "${app}" --source-code-path "${source}"
}

preflight

for name in $(jq -r '.order[]? // (.components | keys[])' "${DESCRIPTOR}"); do
  kind="$(jq -r --arg n "${name}" '.components[$n].kind' "${DESCRIPTOR}")"
  case "${kind}" in
    bundle) apply_bundle "${name}" ;;
    app)    apply_app "${name}" ;;
    # A helm/deployment component in a Databricks release is not an error worth failing on: a real
    # environment is usually split (services on OpenShift, compute and data on Databricks), and both
    # targets read the same descriptor. Each skips what the other owns, loudly.
    helm|deployment) log "${name}: kind '${kind}' belongs to the OpenShift target — skipped" ;;
    null)   echo "component '${name}' is named in .order but not in .components" >&2; exit 1 ;;
    *)      echo "component '${name}' has unknown kind '${kind}'" >&2; exit 1 ;;
  esac
done

log "done"
