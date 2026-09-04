#!/usr/bin/env bash
# Apply a release descriptor to an OpenShift/Kubernetes namespace.
#
# The descriptor (see ../README.md) names components and the **digest** of the bytes each one is,
# never a tag. That is the whole reason this script takes a file rather than arguments: four
# repositories publish independently, and "which four builds are in this environment" has to be one
# reviewable object rather than four pipeline runs somebody correlates by timestamp.
#
# Two kinds of component, because only one of the four repositories ships a chart:
#
#   helm       — `Chemclaw3`, whose `deploy/helm/chemclaw` renders every component role it runs.
#                `helm upgrade --install` also runs the pre-deploy migrate Job (a Helm hook), so
#                the DDL completes before any app container starts. Never run migrations by hand.
#   deployment — `Chemclaw3_ui` and each `Chemclaw3-mcp` server, which have an image and a
#                NetworkPolicy but no chart. `oc set image` against a Deployment an operator
#                already created is the honest minimum: it changes the bytes and nothing else.
#                A chart for those two is `docs/planning/BACKLOG.md` work, not something to fake here.
#
# DRY_RUN=true (the default) renders and diffs without touching the cluster. A pipeline that
# defaults to mutating a namespace is one nobody can safely run for the first time.
set -euo pipefail

DESCRIPTOR="${1:?usage: openshift.sh <release-descriptor.json>}"
DRY_RUN="${DRY_RUN:-true}"
NAMESPACE="${NAMESPACE:?NAMESPACE must name the target namespace}"
HELM_TIMEOUT="${HELM_TIMEOUT:-15m}"

command -v jq   >/dev/null || { echo "jq is required to read the descriptor" >&2; exit 1; }
command -v helm >/dev/null || { echo "helm is required" >&2; exit 1; }
KUBECTL="$(command -v oc || command -v kubectl)" || { echo "need oc or kubectl" >&2; exit 1; }

log() { printf '\033[36m[openshift]\033[0m %s\n' "$*" >&2; }

# Every pod's egress posture must be stated by the release, because an unstated one used to render
# `to: []` — which a NetworkPolicy reads as *every* destination
# (D-2026-08-26-a-knob-that-renders-nothing-is-not-a-knob). The chart now refuses to render without
# one, and this refuses to guess which one you meant.
egress_flags() {
  local values_file="$1"
  if [ -n "${values_file}" ] && grep -qE '^\s*(egressDestinations|allowAnyDestination):' "${values_file}"; then
    return 0
  fi
  if [ "${ALLOW_ANY_EGRESS_DESTINATION:-false}" = "true" ]; then
    printf -- '--set networkPolicy.allowAnyDestination=true'
    return 0
  fi
  echo "the release states no egress posture: put networkPolicy.egressDestinations in the values" \
       "file, or set ALLOW_ANY_EGRESS_DESTINATION=true to say the old any-destination default is" \
       "what you want. The chart will not render without one." >&2
  return 1
}

# The sibling posture, and the reason this function exists at all: the chart has *two* refusals and
# this script only knew about one, so every `helm upgrade --install` from here died in
# `templates/config.yaml` with "retention: set exactly one of ...". Same shape as `egress_flags`
# deliberately — honour a values file that states either key, otherwise require the operator to say
# out loud that unbounded growth is what they meant — because the two guards make the same argument
# and an operator who has met one should recognise the other.
retention_flags() {
  local values_file="$1"
  if [ -n "${values_file}" ] && grep -qE '^\s*(windows|unboundedGrowthAccepted):' "${values_file}"; then
    return 0
  fi
  if [ "${ACCEPT_UNBOUNDED_GROWTH:-false}" = "true" ]; then
    printf -- '--set retention.unboundedGrowthAccepted=true'
    return 0
  fi
  echo "the release states no retention posture: put retention.windows in the values file (the" \
       "CHEMCLAW_RETENTION_* day windows this deployment keeps history for), or set" \
       "ACCEPT_UNBOUNDED_GROWTH=true to say the durable tables may grow forever." \
       "The chart will not render without one." >&2
  return 1
}

apply_helm() {
  local name chart release image digest values extra
  name="$1"
  chart="$(jq -r --arg n "${name}" '.components[$n].chart' "${DESCRIPTOR}")"
  release="$(jq -r --arg n "${name}" '.components[$n].release // $n' "${DESCRIPTOR}")"
  image="$(jq -r --arg n "${name}" '.components[$n].image' "${DESCRIPTOR}")"
  digest="$(jq -r --arg n "${name}" '.components[$n].digest' "${DESCRIPTOR}")"
  values="$(jq -r --arg n "${name}" '.components[$n].values // ""' "${DESCRIPTOR}")"
  # One assignment each, before the `read`, and that is load-bearing twice over. `read -r -a x
  # <<<"$(f)"` takes its exit status from `read`, so a refusing posture check printed its message and
  # the script carried on to a helm error naming a template instead of the missing posture; a plain
  # assignment propagates the substitution's status under `set -e`. And *two* substitutions in one
  # assignment would take only the last one's status, so a refused egress posture beside an accepted
  # retention posture would pass — which is the same silent-fallthrough one line up.
  local egress retention
  egress="$(egress_flags "${values}")"
  retention="$(retention_flags "${values}")"
  read -r -a extra <<<"${egress} ${retention}"

  local args=(upgrade --install "${release}" "${chart}"
              --namespace "${NAMESPACE}"
              --set "image.repository=${image}"
              --set "image.digest=${digest}"
              --timeout "${HELM_TIMEOUT}")
  [ -n "${values}" ] && args+=(--values "${values}")
  [ "${#extra[@]}" -gt 0 ] && args+=("${extra[@]}")

  if [ "${DRY_RUN}" = "true" ]; then
    log "${release}: dry run (no cluster changes)"
    helm "${args[@]}" --dry-run
  else
    log "${release}: upgrading to ${digest}"
    # `--wait` and deliberately **not** `--atomic`, which this chart forbids at the point of the
    # annotation that makes it wrong (`templates/migrate-job.yaml`, and the ledger row of
    # D-2026-08-27-a-conversion-that-cannot-be-rolled-back-is-not-a-pre-upgrade-step).
    #
    # `chemclaw-convert` is a `post-upgrade` hook precisely so a failed stored-message backfill does
    # not roll a healthy release back: the pass rewrites `session_messages` rows into a shape the
    # *previous* release's reader raises on, and Helm neither undoes a data conversion nor re-runs
    # the hook. `--atomic` rolls back on any failed hook, so a backfill that merely runs past its
    # `activeDeadlineSeconds` would take the whole release with it and leave converted rows behind a
    # reader that cannot read them. Without it, Helm still reports the failed Job — the release
    # stays up, both message shapes stay readable, and the pass is resumable.
    #
    # The rollout itself is still waited on, so a release that never becomes ready fails the
    # pipeline. The front door's terminationGracePeriod is derived from the turn timeout, so a
    # rolling update is minutes by design — hence the generous default timeout above.
    helm "${args[@]}" --wait
  fi
}

apply_deployment() {
  local name deployment container image digest
  name="$1"
  deployment="$(jq -r --arg n "${name}" '.components[$n].deployment' "${DESCRIPTOR}")"
  container="$(jq -r --arg n "${name}" '.components[$n].container // .components[$n].deployment' "${DESCRIPTOR}")"
  image="$(jq -r --arg n "${name}" '.components[$n].image' "${DESCRIPTOR}")"
  digest="$(jq -r --arg n "${name}" '.components[$n].digest' "${DESCRIPTOR}")"

  if [ "${DRY_RUN}" = "true" ]; then
    log "${deployment}: would set ${container}=${image}@${digest}"
    "${KUBECTL}" set image "deployment/${deployment}" "${container}=${image}@${digest}" \
      --namespace "${NAMESPACE}" --dry-run=server -o name
    return
  fi
  log "${deployment}: setting ${container}=${image}@${digest}"
  "${KUBECTL}" set image "deployment/${deployment}" "${container}=${image}@${digest}" \
    --namespace "${NAMESPACE}"
  "${KUBECTL}" rollout status "deployment/${deployment}" --namespace "${NAMESPACE}" --timeout=10m
}

log "environment $(jq -r '.environment' "${DESCRIPTOR}") -> namespace ${NAMESPACE} (dry_run=${DRY_RUN})"

# Deliberate order, and it is not alphabetical: the tool fleet and the mock come up before the core
# that dials them. Under `CHEMCLAW_CONNECTORS_REQUIRED=true` an unreachable connector is a hard
# startup failure of the front door rather than a degraded one, so deploying core first turns a
# rollout into a crash-loop that reads as a core defect.
for name in $(jq -r '.order[]? // (.components | keys[])' "${DESCRIPTOR}"); do
  kind="$(jq -r --arg n "${name}" '.components[$n].kind' "${DESCRIPTOR}")"
  case "${kind}" in
    helm)       apply_helm "${name}" ;;
    deployment) apply_deployment "${name}" ;;
    null)       echo "component '${name}' is named in .order but not in .components" >&2; exit 1 ;;
    *)          echo "component '${name}' has unknown kind '${kind}'" >&2; exit 1 ;;
  esac
done

log "done"
