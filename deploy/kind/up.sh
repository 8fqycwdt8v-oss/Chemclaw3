#!/usr/bin/env bash
# The whole ChemClaw3 system on a local kind cluster: the production images and the production
# chart, brought up by one command.
#
#   deploy/kind/up.sh [up|down|status|smoke]        (make kind-up / kind-down / kind-status / kind-smoke)
#
# `up` is idempotent and converges: it creates the cluster if absent, loads whatever images exist,
# applies the dependencies, `helm upgrade --install`s the chart with `values-kind.yaml`, waits for
# every Deployment and hook Job, and ends with the smoke. Re-running it after a failure picks up
# where the cluster is. Every wait is bounded and names what it was waiting for when it gives up.
#
# Knobs (environment):
#   CHEMCLAW_KIND_CORE_TAG   core image tag (default `kind`) — the chart, the hooks and the mock LLM
#   CHEMCLAW_KIND_TAG        tag of every other image: chemclaw/mcp-*, chemclaw/mock (default `kind`)
#   CHEMCLAW_KIND_AUTH       `devauth` (default). `oidc-mock` is refused until its prerequisites
#                            exist — see ../kind/README.md.
#   CHEMCLAW_KIND_LLM        `mock` (default): the scripted mock LLM. `live`: the host's gateway,
#                            from `chemclaw-live-env.sh` beside the checkouts (macOS Keychain), whose
#                            key goes straight into the cluster Secret and is never printed or written.
#   CHEMCLAW_KIND_SKIP_SMOKE `true` to stop after the rollout.
#   CHEMCLAW_MCP_REPO / CHEMCLAW_MOCK_REPO   sibling checkouts (default: beside this checkout).
set -euo pipefail

readonly KIND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Not readonly: `sibling_repo` reads it as a global, and the resolution below re-points it.
REPO_ROOT="$(cd "$KIND_DIR/../.." && pwd)"
readonly CLUSTER=chemclaw
readonly CTX="kind-$CLUSTER"
readonly NS=chemclaw
readonly RELEASE=chemclaw
readonly CORE_TAG="${CHEMCLAW_KIND_CORE_TAG:-kind}"
readonly TAG="${CHEMCLAW_KIND_TAG:-kind}"
readonly AUTH="${CHEMCLAW_KIND_AUTH:-devauth}"
readonly LLM="${CHEMCLAW_KIND_LLM:-mock}"
readonly FRONT_DOOR="http://127.0.0.1:18000"
readonly UI_URL="http://127.0.0.1:15173"
readonly TEMPORAL_UI_URL="http://127.0.0.1:18091"

log() { printf '\033[36m[kind]\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[33m[kind] %s\033[0m\n' "$*" >&2; }
die() { printf '\033[31m[kind] %s\033[0m\n' "$*" >&2; exit 1; }
k() { kubectl --context "$CTX" -n "$NS" "$@"; }

# The sibling checkouts, resolved the way the live lanes resolve them (`infra/live/siblings.sh`),
# from the *main* checkout: a worktree lives under `.claude/worktrees/`, beside no sibling.
main_checkout() {
  local common
  common="$(git -C "$REPO_ROOT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" \
    || { printf '%s' "$REPO_ROOT"; return; }
  (cd "$common/.." && pwd)
}
readonly MAIN_CHECKOUT="$(main_checkout)"
# shellcheck source=infra/live/siblings.sh
. "$REPO_ROOT/infra/live/siblings.sh"
readonly MCP_REPO="$(REPO_ROOT="$MAIN_CHECKOUT"; sibling_repo CHEMCLAW_MCP_REPO Chemclaw3-mcp)"

# Every fleet server this lane deploys, and the chart connector each one serves (blank: a backend
# core dials directly, not a bound connector — `calc_server_url`, `rxnlabel_server_url`).
readonly -a FLEET=(calc chem kinetics props pyexec rxnlabel rxnpredict safety suitability thermalsafety unitops)
connector_of() { case "$1" in calc | rxnlabel) printf '' ;; *) printf '%s' "$1" ;; esac; }

require_tools() {
  local tool
  for tool in kind kubectl helm docker openssl curl python3; do
    command -v "$tool" >/dev/null || die "$tool is not installed"
  done
  docker info >/dev/null 2>&1 || die "the Docker daemon is not answering"
  [ -d "$MCP_REPO/servers" ] || die "Chemclaw3-mcp not found at $MCP_REPO — set CHEMCLAW_MCP_REPO"
}

# ------------------------------------------------------------------------------------- cluster

ensure_cluster() {
  if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
    log "cluster $CLUSTER exists"
  else
    log "creating cluster $CLUSTER"
    kind create cluster --config "$KIND_DIR/kind-config.yaml" --wait 120s
  fi
  kubectl --context "$CTX" wait --for=condition=Ready node --all --timeout=120s >/dev/null \
    || die "the kind node did not become Ready in 120s"
}

# Whether the node already holds `image` — after `kind load`, the host tag may be removed to save
# disk, and the node's copy is then the only one.
node_has() {
  docker exec "$CLUSTER-control-plane" crictl inspecti "docker.io/$1" >/dev/null 2>&1
}

# Load one image if the host has it (kind skips an identical one); report whether the node now holds it.
load_image() {
  local image="$1"
  if docker image inspect "$image" >/dev/null 2>&1; then
    kind load docker-image --name "$CLUSTER" "$image" >/dev/null 2>&1 \
      || die "kind load docker-image $image failed (disk space? \`docker system df\`)"
  fi
  node_has "$image"
}

# What the node holds, as one space-delimited list rather than an associative array: macOS ships
# bash 3.2, which has none, and this script is run from a Mac. (`${arr[@]+"${arr[@]}"}` below is
# the same constraint: 3.2 calls an empty array unbound under `set -u`.)
AVAILABLE=" "
MISSING=()
# `load_images load` loads what the host has and records what the node holds; `load_images detect`
# only records — what `smoke` and `status` need, without touching the node.
load_images() {
  local mode="${1:-load}" image
  for image in "chemclaw/core:$CORE_TAG" "chemclaw/mock:$TAG" "$(ui_image)" \
    $(for s in "${FLEET[@]}"; do printf 'chemclaw/mcp-%s:%s ' "$s" "$TAG"; done); do
    if { [ "$mode" = load ] && load_image "$image"; } || node_has "$image"; then
      AVAILABLE="$AVAILABLE$image "
    else
      MISSING+=("$image")
    fi
  done
  [ "$mode" = load ] || return 0
  have "chemclaw/core:$CORE_TAG" \
    || die "chemclaw/core:$CORE_TAG is neither on the host nor in the node — build it first:
  docker build -f deploy/Containerfile -t chemclaw/core:$CORE_TAG ."
  if [ ${#MISSING[@]} -gt 0 ]; then
    warn "not available, deployed without: ${MISSING[*]}"
  fi
}
have() { case "$AVAILABLE" in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

# ------------------------------------------------------------------------------------- auth mode

ui_image() {
  case "$AUTH" in
    devauth) printf 'chemclaw/ui:%s-devauth' "$TAG" ;;
    *) printf 'chemclaw/ui:%s' "$TAG" ;;
  esac
}

check_auth_mode() {
  case "$AUTH" in
    devauth) ;;
    oidc-mock)
      die "CHEMCLAW_KIND_AUTH=oidc-mock is not runnable yet. Under CHEMCLAW_ENTRA_REQUIRED=true the
  core refuses a plaintext Postgres DSN and a plaintext Temporal channel to a non-loopback host
  (core/config: require_pg_tls, the temporal_tls check), so this mode needs, beyond the mock tenant
  (MOCK_ENTRA_ENABLED on mock-eln) and the UI's AUTH_MODE=msal against it:
    - Postgres serving TLS, and DSNs with sslmode=require or stronger;
    - Temporal frontend mTLS, and the chart's secrets.temporalTls Secret.
  deploy/kind/README.md lists what the mode will set once those exist."
      ;;
    *) die "CHEMCLAW_KIND_AUTH must be devauth or oidc-mock, got '$AUTH'" ;;
  esac
}

# The per-mode environment for the UI and the mock, one ConfigMap each, so the manifests name
# neither mode.
apply_auth_config() {
  k create configmap chemclaw-ui-auth --dry-run=client -o yaml \
    --from-literal=AUTH_MODE=dev --from-literal=ALLOW_INSECURE_AUTH=true | k apply -f - >/dev/null
  k create configmap chemclaw-mock-auth --dry-run=client -o yaml \
    --from-literal=MOCK_ENTRA_ENABLED=false | k apply -f - >/dev/null
}

# ------------------------------------------------------------------------------------- secrets

# A key of an existing Secret, decoded, or empty — so a re-run keeps every generated credential
# instead of rotating it under running pods. Never printed.
secret_value() {
  k get secret "$1" -o "jsonpath={.data.$2}" 2>/dev/null | base64 -d 2>/dev/null || true
}
existing_or_new() {
  local value
  value="$(secret_value "$1" "$2")"
  [ -n "$value" ] || value="$(openssl rand -hex 24)"
  printf '%s' "$value"
}

ensure_db_secret() {
  if k get secret chemclaw-kind-db >/dev/null 2>&1; then return; fi
  log "generating database passwords (Secret chemclaw-kind-db)"
  k create secret generic chemclaw-kind-db \
    --from-literal=POSTGRES_PASSWORD="$(openssl rand -hex 24)" \
    --from-literal=APP_PASSWORD="$(openssl rand -hex 24)" \
    --from-literal=TEMPORAL_PASSWORD="$(openssl rand -hex 24)" >/dev/null
}

# The model gateway's variables for the chart, set by `live_llm_env` in live mode.
LLM_SET=()
LLM_KEY=""
live_llm_env() {
  local env_file="${CHEMCLAW_LIVE_ENV_FILE:-$(dirname "$MAIN_CHECKOUT")/chemclaw-live-env.sh}"
  [ -f "$env_file" ] || die "CHEMCLAW_KIND_LLM=live needs $env_file (or CHEMCLAW_LIVE_ENV_FILE)"
  # Sourced into this process only: the key reaches the Secret below through a pipe, never argv,
  # a file or the log. The file prints nothing on success.
  # shellcheck source=/dev/null
  . "$env_file" || die "sourcing $env_file failed — is the Keychain item there?"
  [ -n "${CHEMCLAW_LLM_API_KEY:-}" ] && [ -n "${CHEMCLAW_LLM_BASE_URL:-}" ] \
    && [ -n "${CHEMCLAW_LLM_MODEL:-}" ] || die "$env_file did not set the gateway, model and key"
  LLM_KEY="$CHEMCLAW_LLM_API_KEY"
  unset CHEMCLAW_LLM_API_KEY
  LLM_SET=(--set-string "config.CHEMCLAW_LLM_BASE_URL=$CHEMCLAW_LLM_BASE_URL"
    --set-string "config.CHEMCLAW_LLM_MODEL=$CHEMCLAW_LLM_MODEL")
  if [ -n "${CHEMCLAW_LLM_CONTEXT_WINDOW_TOKENS:-}" ]; then
    LLM_SET+=(--set-string "config.CHEMCLAW_LLM_CONTEXT_WINDOW_TOKENS=$CHEMCLAW_LLM_CONTEXT_WINDOW_TOKENS")
  fi
  log "model gateway: $CHEMCLAW_LLM_BASE_URL (model: $CHEMCLAW_LLM_MODEL); key from the Keychain"
}

# `chemclaw-secrets`: everything the chart's `secrets.*` names, plus each fleet server's bearer
# (read by the server from the same key, by render-fleet.sh). Assembled as `KEY=value` lines on a
# pipe into `--from-env-file`, so no value is ever an argument or a file.
apply_app_secret() {
  local app_pw owner_pw name var
  app_pw="$(secret_value chemclaw-kind-db APP_PASSWORD)"
  owner_pw="$(secret_value chemclaw-kind-db POSTGRES_PASSWORD)"
  [ -n "$app_pw" ] && [ -n "$owner_pw" ] || die "chemclaw-kind-db is missing a password"
  {
    printf 'CHEMCLAW_POSTGRES_DSN=postgresql://chemclaw_app:%s@postgres:5432/chemclaw\n' "$app_pw"
    printf 'CHEMCLAW_POSTGRES_MIGRATION_DSN=postgresql://chemclaw:%s@postgres:5432/chemclaw\n' "$owner_pw"
    printf 'CHEMCLAW_LLM_API_KEY=%s\n' "$LLM_KEY"
    printf 'CHEMCLAW_KNOWLEDGE_REPO_TOKEN=\n'
    for var in CHEMCLAW_BO_MCP_TOKEN CHEMCLAW_CALC_MCP_TOKEN CHEMCLAW_MOLFP_MCP_TOKEN \
      CHEMCLAW_RXNFP_MCP_TOKEN; do
      printf '%s=%s\n' "$var" "$(existing_or_new chemclaw-secrets "$var")"
    done
    for name in "${FLEET[@]}"; do
      var="CHEMCLAW_$(printf '%s' "$name" | tr '[:lower:]' '[:upper:]')_TOKEN"
      printf '%s=%s\n' "$var" "$(existing_or_new chemclaw-secrets "$var")"
    done
  } | k create secret generic chemclaw-secrets --from-env-file=/dev/stdin --dry-run=client -o yaml \
    | k apply -f - >/dev/null
  log "Secret chemclaw-secrets applied (DSNs, connector bearers, LLM key: ${LLM})"
}

# ------------------------------------------------------------------------------------- dependencies

# A manifest with the lane's image tags in place of the defaults it names.
manifest() {
  sed -e "s#chemclaw/core:kind#chemclaw/core:$CORE_TAG#" \
    -e "s#chemclaw/mock:kind#chemclaw/mock:$TAG#" \
    -e "s#chemclaw/ui:kind-devauth#$(ui_image)#" "$KIND_DIR/manifests/$1"
}

wait_rollout() {
  local what="$1" timeout="$2"
  k rollout status "$what" --timeout="$timeout" >/dev/null \
    || die "$what did not become ready within $timeout:
$(k get pods -o wide 2>&1 | tail -n +1)
  inspect: kubectl --context $CTX -n $NS describe $what; kubectl --context $CTX -n $NS logs $what"
}

apply_dependencies() {
  log "applying the dependencies (Postgres, Temporal, mocks, fleet, front door, UI)"
  kubectl --context "$CTX" apply -f "$KIND_DIR/manifests/namespace.yaml" >/dev/null
  ensure_db_secret
  apply_auth_config
  apply_app_secret
  # The two bundles this image does not ship, from the files their owners keep.
  # The file named, not the directory: the fleet's `manifests/pyexec/connector.yaml` is a symlink
  # into `servers/pyexec/`, and `--from-file=<dir>` skips symlinks — it built an empty ConfigMap,
  # and every pod refused `pyexec` as an unknown connector.
  k create configmap chemclaw-connector-pyexec --dry-run=client -o yaml \
    --from-file=connector.yaml="$MCP_REPO/manifests/pyexec/connector.yaml" | k apply -f - >/dev/null
  k create configmap chemclaw-connector-mock-vendor --dry-run=client -o yaml \
    --from-file=connector.yaml="$REPO_ROOT/infra/live/e2e-full-stack/manifests/mock-vendor/connector.yaml" \
    | k apply -f - >/dev/null

  manifest postgres.yaml | k apply -f - >/dev/null
  manifest temporal.yaml | k apply -f - >/dev/null
  manifest mock-llm.yaml | k apply -f - >/dev/null
  manifest front-door.yaml | k apply -f - >/dev/null
  if have "chemclaw/mock:$TAG"; then manifest mock.yaml | k apply -f - >/dev/null; fi
  if have "$(ui_image)"; then manifest ui.yaml | k apply -f - >/dev/null; fi

  local servers=() name
  for name in "${FLEET[@]}"; do
    if have "chemclaw/mcp-$name:$TAG"; then servers+=("$name"); fi
  done
  if [ ${#servers[@]} -gt 0 ]; then
    "$KIND_DIR/render-fleet.sh" "$MCP_REPO" "$TAG" "${servers[@]}" | k apply -f - >/dev/null
  fi

  wait_rollout statefulset/postgres 180s
  wait_rollout deployment/temporal 300s
  wait_rollout deployment/mock-llm 180s
  if have "chemclaw/mock:$TAG"; then
    wait_rollout deployment/mock-eln 300s
    wait_rollout deployment/mock-vendor 120s
  fi
  for name in ${servers[@]+"${servers[@]}"}; do wait_rollout "deployment/chemclaw-mcp-$name" 600s; done
  log "dependencies ready (fleet: ${servers[*]+"${servers[*]}"})"
}

# ------------------------------------------------------------------------------------- the chart

# `--set connectors.<name>.enabled=false` for every bound bundle whose server could not be
# deployed: with `CHEMCLAW_CONNECTORS_REQUIRED=true` an unreachable bound bundle stops the front
# door, so a partial image set deploys the part that exists rather than none of it.
CHART_SET=()
disable_missing_connectors() {
  local name connector
  for name in "${FLEET[@]}"; do
    connector="$(connector_of "$name")"
    if [ -n "$connector" ] && ! have "chemclaw/mcp-$name:$TAG"; then
      CHART_SET+=(--set "connectors.$connector.enabled=false")
      warn "connector $connector disabled: chemclaw/mcp-$name:$TAG is not available"
    fi
  done
  if ! have "chemclaw/mock:$TAG"; then
    CHART_SET+=(--set "connectors.mock-vendor.enabled=false" --set documentShare.enabled=false)
    warn "connector mock-vendor and the ELN export mount disabled: chemclaw/mock:$TAG is not available"
  fi
}

# `--force-conflicts`: Helm 4 applies server-side, so a field someone changed by hand while
# debugging (`kubectl set resources`, `kubectl scale`) makes the next `up` fail on a field-manager
# conflict instead of converging. On this cluster the release is the source of truth.
install_chart() {
  disable_missing_connectors
  log "helm upgrade --install $RELEASE (core image chemclaw/core:$CORE_TAG) — runs the migrate hook first"
  helm --kube-context "$CTX" upgrade --install "$RELEASE" "$REPO_ROOT/deploy/helm/chemclaw" \
    --namespace "$NS" -f "$KIND_DIR/values-kind.yaml" \
    --set-string "image.tag=$CORE_TAG" ${LLM_SET[@]+"${LLM_SET[@]}"} ${CHART_SET[@]+"${CHART_SET[@]}"} \
    --wait --wait-for-jobs --timeout 20m --force-conflicts \
    || die "helm upgrade --install failed. Hook Jobs are deleted only on success, so a failed one is
  still there to read:
    kubectl --context $CTX -n $NS get jobs,pods
    kubectl --context $CTX -n $NS logs job/chemclaw-migrate   (or -convert, -schedules)"
  # The mock LLM validated its catalogue before the release existed; nothing to restart. The UI's
  # readiness follows the front door's, so it is waited on only now.
  if have "$(ui_image)"; then wait_rollout deployment/ui 180s; fi
  wait_rollout deployment/front-door 60s
  k wait --for=condition=Available deployment --all --timeout=300s >/dev/null \
    || die "not every Deployment is Available: $(k get deploy 2>&1)"
  log "release $RELEASE deployed"
}

# ------------------------------------------------------------------------------------- smoke

# One bounded HTTP GET against the host mapping; prints the status code.
http_code() { curl -s -o /dev/null -m 10 -w '%{http_code}' "$1" || true; }

wait_http_ok() {
  local name="$1" url="$2" attempts="${3:-60}" code=""
  for _ in $(seq 1 "$attempts"); do
    code="$(http_code "$url")"
    [ "$code" = 200 ] && { log "smoke: $name 200"; return; }
    sleep 2
  done
  die "smoke: $name answered ${code:-nothing} at $url, not 200"
}

# One turn through the front door: create a session, post a message carrying a mock-LLM behaviour
# marker, read the SSE stream to its end. Prints the stream.
turn() {
  local marker="$1" session
  session="$(curl -sf -m 20 -X POST "$FRONT_DOOR/sessions" -H 'content-type: application/json' -d '{}' \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["session_id"])')" \
    || die "smoke: POST /sessions failed"
  curl -sN -m 300 -X POST "$FRONT_DOOR/sessions/$session/messages" \
    -H 'content-type: application/json' -H 'accept: text/event-stream' \
    -d "{\"message\": \"[[$marker]] kind smoke\"}" || die "smoke: the $marker turn's stream failed"
}

smoke() {
  log "smoke: front door, UI, a mock-LLM turn, a durable job"
  wait_http_ok "front door /healthz" "$FRONT_DOOR/healthz"
  wait_http_ok "front door /readyz (connectors_required)" "$FRONT_DOOR/readyz"
  if have "$(ui_image)"; then wait_http_ok "UI /readyz" "$UI_URL/readyz"; fi
  wait_http_ok "Temporal UI" "$TEMPORAL_UI_URL/" 15

  if [ "$LLM" != mock ]; then
    log "smoke: CHEMCLAW_KIND_LLM=$LLM — the scripted turns need the mock LLM; skipping them"
    return
  fi
  local stream
  stream="$(turn a-cheap)"
  grep -q 'Two notes cover this coupling' <<<"$stream" \
    || die "smoke: the a-cheap turn did not end with its scripted answer. Stream tail:
$(tail -n 20 <<<"$stream")"
  grep -q 'find_notes' <<<"$stream" || die "smoke: the a-cheap turn made no find_notes call"
  log "smoke: mock-LLM turn answered (find_notes called, scripted answer streamed)"

  stream="$(turn d-collide)"
  local job_id
  job_id="$(python3 -c 'import re,sys; m=re.findall(r"\"job_id\": ?\"([^\"]+)\"", sys.stdin.read()); print(m[0] if m else "")' <<<"$stream")"
  [ -n "$job_id" ] || die "smoke: the d-collide turn launched no durable job. Stream tail:
$(tail -n 20 <<<"$stream")"
  log "smoke: durable job $job_id launched; waiting for it to complete (≤ 10 min)"
  local state="" body
  for _ in $(seq 1 120); do
    body="$(curl -sf -m 10 "$FRONT_DOOR/jobs/$job_id" || true)"
    state="$(python3 -c 'import json,sys
try: d=json.load(sys.stdin)
except Exception: print(""); raise SystemExit
print(str(d.get("status") or d.get("state") or "").lower())' <<<"$body")"
    case "$state" in
      completed) log "smoke: durable job $job_id completed"; return ;;
      failed | cancelled | terminated | timed_out | timedout)
        die "smoke: durable job $job_id ended $state: $body" ;;
    esac
    sleep 5
  done
  die "smoke: durable job $job_id did not complete in 10 min (last state: ${state:-unknown})"
}

# ------------------------------------------------------------------------------------- verbs

status() {
  kind get clusters 2>/dev/null | grep -qx "$CLUSTER" || { log "no cluster $CLUSTER"; return; }
  k get pods -o wide
  echo
  k get jobs 2>/dev/null || true
  echo
  helm --kube-context "$CTX" -n "$NS" status "$RELEASE" 2>/dev/null | sed -n '1,6p' || true
  printf '\n  front door  %s  (/healthz %s)\n' "$FRONT_DOOR" "$(http_code "$FRONT_DOOR/healthz")"
  printf '  UI          %s  (/readyz %s)\n' "$UI_URL" "$(http_code "$UI_URL/readyz")"
  printf '  Temporal UI %s  (%s)\n' "$TEMPORAL_UI_URL" "$(http_code "$TEMPORAL_UI_URL/")"
}

up() {
  require_tools
  check_auth_mode
  [ "$LLM" = mock ] || [ "$LLM" = live ] || die "CHEMCLAW_KIND_LLM must be mock or live, got '$LLM'"
  if [ "$LLM" = live ]; then live_llm_env; fi
  ensure_cluster
  load_images
  apply_dependencies
  install_chart
  case "${CHEMCLAW_KIND_SKIP_SMOKE:-false}" in
    true) log "smoke skipped (CHEMCLAW_KIND_SKIP_SMOKE)" ;;
    *) smoke ;;
  esac
  log "up. UI $UI_URL · front door $FRONT_DOOR · Temporal UI $TEMPORAL_UI_URL"
  if [ ${#MISSING[@]} -gt 0 ]; then warn "deployed without: ${MISSING[*]}"; fi
}

down() {
  if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
    kind delete cluster --name "$CLUSTER"
  else
    log "no cluster $CLUSTER"
  fi
}

case "${1:-up}" in
  up) up ;;
  down) down ;;
  status) status ;;
  smoke) require_tools; load_images detect; smoke ;;
  *) die "usage: up.sh [up|down|status|smoke]" ;;
esac
