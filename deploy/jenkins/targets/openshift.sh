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
#
# Everything below `main` is a function, and `main` runs only when this file is executed rather than
# sourced. That is what lets `tests/test_deploy_chart.py` drive the posture helpers against real
# values files instead of grepping this script for a string — the defect they were shipped with
# (reading "the key is mentioned" as "a posture is stated") is one only an execution can see.
set -euo pipefail

DRY_RUN="${DRY_RUN:-true}"
HELM_TIMEOUT="${HELM_TIMEOUT:-15m}"

log() { printf '\033[36m[openshift]\033[0m %s\n' "$*" >&2; }

# **A key that appears is not a posture that is stated**, and reading the first as the second is how
# both helpers below shipped. They matched `^\s*(windows|unboundedGrowthAccepted):` — so
# `unboundedGrowthAccepted: false`, an operator writing down what they do *not* want, was read as a
# statement, the `--set` was suppressed, and the deploy died inside `templates/config.yaml` with a
# Go-template `fail` instead of the sentence these functions exist to print. Same for
# `allowAnyDestination: false`.
#
# The chart's guards take two shapes and so does this. An **accept** key states a posture only by
# being true. A **list** key (`egressDestinations`, `retention.windows`) states one by having
# contents — inline, or as an indented block on the lines beneath it, which is why this is `awk` and
# not a pattern: no single-line match can see a block's body. Comment lines and trailing comments
# are dropped first, because a key inside a `#` line is documentation, not configuration.
#
# This reads YAML with a scanner rather than a parser, deliberately — a Jenkins agent has `jq`,
# `helm` and `oc`, and adding a YAML dependency to state a posture is a worse trade than a scanner
# whose failure mode is the chart's own `fail`, which still stands behind it.
#
# `awk` into a variable and never `awk | grep -q`: under this script's `pipefail` a `grep -q` that
# matches exits first, `awk` dies of EPIPE, and the pipeline reports the *match* as a failure —
# size-dependent, so it passes on a small fixture and fails on a real values file. The `Makefile`
# carries the same warning at the place it was learned.
states_posture() {
  local values_file="$1" accept_key="$2" list_key="$3" verdict
  [ -n "${values_file}" ] && [ -r "${values_file}" ] || return 1
  verdict="$(awk -v accept="${accept_key}" -v list="${list_key}" '
    { sub(/[[:space:]]+#.*$/, "") }                       # a trailing comment is not a value
    /^[[:space:]]*(#|$)/ { next }                         # nor is a comment line, nor a blank one
    # An open block whose first body line is indented deeper than its key: contents, so: stated.
    open_at >= 0 {
      match($0, /^[[:space:]]*/)
      if (RLENGTH > open_at) { print "yes"; exit }
      open_at = -1
    }
    $0 ~ "^[[:space:]]*" accept ":[[:space:]]*(true|yes|on)[[:space:]]*$" { print "yes"; exit }
    $0 ~ "^[[:space:]]*" list ":" {
      value = $0; sub("^[[:space:]]*" list ":[[:space:]]*", "", value)
      if (value != "" && value !~ /^(null|~|\[[[:space:]]*\]|\{[[:space:]]*\})$/) { print "yes"; exit }
      match($0, /^[[:space:]]*/); open_at = RLENGTH    # empty inline: a block may follow
    }
    BEGIN { open_at = -1 }
  ' "${values_file}")"
  [ "${verdict}" = "yes" ]
}

# One body for two postures, because they are the same argument twice and were the same code twice:
# honour a values file that states the posture, otherwise require the operator to say out loud that
# the permissive default is what they meant, otherwise refuse. The `--set` is emitted only when the
# file does *not* state it — the chart's guards are exclusive-or ("Neither is set, or both are"), so
# adding a flag beside a stated posture is its own failure.
posture_flags() {
  local values_file="$1" accept_key="$2" list_key="$3" opted_in="$4" set_flag="$5" refusal="$6"
  if states_posture "${values_file}" "${accept_key}" "${list_key}"; then
    return 0
  fi
  if [ "${opted_in}" = "true" ]; then
    printf -- '%s' "${set_flag}"
    return 0
  fi
  echo "${refusal}" >&2
  return 1
}

# Every pod's egress posture must be stated by the release, because an unstated one used to render
# `to: []` — which a NetworkPolicy reads as *every* destination
# (D-2026-08-26-a-knob-that-renders-nothing-is-not-a-knob). The chart now refuses to render without
# one, and this refuses to guess which one you meant.
egress_flags() {
  posture_flags "$1" allowAnyDestination egressDestinations \
    "${ALLOW_ANY_EGRESS_DESTINATION:-false}" \
    '--set networkPolicy.allowAnyDestination=true' \
    "the release states no egress posture: put networkPolicy.egressDestinations in the values file, or set ALLOW_ANY_EGRESS_DESTINATION=true to say the old any-destination default is what you want. The chart will not render without one."
}

# The sibling posture, and the reason a helper for it exists at all: the chart has *two* refusals
# and this script only knew about one, so every `helm upgrade --install` from here died in
# `templates/config.yaml` with "retention: set exactly one of ...".
retention_flags() {
  posture_flags "$1" unboundedGrowthAccepted windows \
    "${ACCEPT_UNBOUNDED_GROWTH:-false}" \
    '--set retention.unboundedGrowthAccepted=true' \
    "the release states no retention posture: put retention.windows in the values file (the CHEMCLAW_RETENTION_* day windows this deployment keeps history for), or set ACCEPT_UNBOUNDED_GROWTH=true to say the durable tables may grow forever. The chart will not render without one."
}

# **A release installed before `templates/config.yaml` moved two objects out of Helm hooks cannot be
# upgraded until they are adopted, and Helm cannot do it itself.**
#
# On the previous chart `chemclaw-config` and the runtime ServiceAccount were `pre-install,
# pre-upgrade` hooks with `hook-delete-policy: before-hook-creation`, so they persist in the
# namespace between releases — and Helm creates hook resources with a plain `Create`, no metadata
# visitor, so they carry no `meta.helm.sh/release-name`/`-namespace`. The current chart claims those
# same two names as *tracked* resources, and `helm upgrade`'s ownership check refuses to import an
# object without them: "exists and cannot be imported into the current release". That is at prepare
# time, before a hook runs, so nothing is half-applied — measured against k3s v1.29.9, and `--dry-run`
# refuses identically, which is why `DRY_RUN=true` is the default here.
#
# Adopting is a one-time act and this does it, because a manual step in the middle of an automated
# release is a step that gets skipped at 3am. It is not a blanket "take over whatever collides":
# only an object that is *provably this release's own leftover hook* qualifies — carrying this
# release's `app.kubernetes.io/instance`, `managed-by: Helm`, a `helm.sh/hook` annotation, and no
# owner. Anything else keeps colliding and helm says so, which is the right answer for an object
# somebody else made. Idempotent: once adopted the selector still matches and the guard skips it.
#
# `oc annotate` mutates, so `DRY_RUN=true` reports and changes nothing — the first (dry) run tells
# the operator exactly what the real one will do. The same two commands are in
# `docs/guides/runbook.md` and `deploy/README.md` for the hand-run `helm upgrade` path.
adopt_leftover_hook_objects() {
  local release="$1" kind name hook owner
  while IFS='|' read -r kind name hook owner; do
    [ -n "${kind}" ] && [ -n "${hook}" ] && [ -z "${owner}" ] || continue
    if [ "${DRY_RUN}" = "true" ]; then
      log "${kind}/${name}: would adopt into release ${release} (the previous chart made it a hook)"
      continue
    fi
    log "${kind}/${name}: adopting into release ${release} (the previous chart made it a hook)"
    # `</dev/null` is load-bearing: without it `oc` inherits the loop's stdin — the process
    # substitution below — drains it, and the loop ends after the first object. Measured against a
    # live API server: the ConfigMap was adopted, the ServiceAccount was not, and `helm upgrade`
    # then failed on the second one with the same message the whole function exists to prevent.
    "${KUBECTL}" annotate --overwrite --namespace "${NAMESPACE}" "${kind}/${name}" \
      "meta.helm.sh/release-name=${release}" "meta.helm.sh/release-namespace=${NAMESPACE}" \
      >/dev/null </dev/null
  done < <("${KUBECTL}" get configmap,serviceaccount --namespace "${NAMESPACE}" \
             --selector "app.kubernetes.io/instance=${release},app.kubernetes.io/managed-by=Helm" \
             --output 'jsonpath={range .items[*]}{.kind}|{.metadata.name}|{.metadata.annotations.helm\.sh/hook}|{.metadata.annotations.meta\.helm\.sh/release-name}{"\n"}{end}' \
             2>/dev/null || true)
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

  adopt_leftover_hook_objects "${release}"

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

main() {
  DESCRIPTOR="${1:?usage: openshift.sh <release-descriptor.json>}"
  NAMESPACE="${NAMESPACE:?NAMESPACE must name the target namespace}"

  command -v jq   >/dev/null || { echo "jq is required to read the descriptor" >&2; exit 1; }
  command -v helm >/dev/null || { echo "helm is required" >&2; exit 1; }
  KUBECTL="$(command -v oc || command -v kubectl)" || { echo "need oc or kubectl" >&2; exit 1; }

  log "environment $(jq -r '.environment' "${DESCRIPTOR}") -> namespace ${NAMESPACE} (dry_run=${DRY_RUN})"

  # Deliberate order, and it is not alphabetical: the tool fleet and the mock come up before the
  # core that dials them. Under `CHEMCLAW_CONNECTORS_REQUIRED=true` an unreachable connector is a
  # hard startup failure of the front door rather than a degraded one, so deploying core first
  # turns a rollout into a crash-loop that reads as a core defect.
  local name kind
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
}

# Executed, not sourced: `${BASH_SOURCE[0]}` is this file either way, `$0` is only equal to it when
# this file is the command. Sourcing therefore loads the helpers and deploys nothing, which is what
# the posture tests need and what a `NAMESPACE`-less shell must not be punished for.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  main "$@"
fi
