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
#   CHEMCLAW_KIND_AUTH       `devauth` (default): sign-in off, the UI in AUTH_MODE=dev.
#                            `oidc-mock`: sign-in enforced against the mock tenant over https, the UI
#                            in AUTH_MODE=msal, Postgres TLS and Temporal mTLS (what core requires
#                            beside CHEMCLAW_ENTRA_REQUIRED=true). See README.md, "Modes".
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
# The mock tenant as the browser reaches it, and therefore the `iss` every token carries: MSAL.js
# requires an https authority, and the issuer is compared as a string, so the one value the browser,
# the mock and core all agree on is the host-mapped address (kind-config.yaml, 18443).
readonly TENANT_URL="https://127.0.0.1:18443/entra/mock-tenant"

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
    # The browser reaches the tenant on a host port. A cluster created before kind-config.yaml
    # mapped 18443 has no such port (a mapping is fixed at creation); `tenant_forward` stands in
    # for it once the mock is up.
    oidc-mock) ;;
    *) die "CHEMCLAW_KIND_AUTH must be devauth or oidc-mock, got '$AUTH'" ;;
  esac
}

# A ConfigMap from `KEY=value` lines on stdin; prints `changed` when its data differs from what
# the cluster holds, so the pods that read it through `envFrom` (which never hot-reloads) can be
# restarted exactly when a mode switch changed their environment.
apply_env_configmap() {
  local name="$1" before after
  before="$(k get configmap "$name" -o jsonpath='{.data}' 2>/dev/null || true)"
  k create configmap "$name" --from-env-file=/dev/stdin --dry-run=client -o yaml | k apply -f - >/dev/null
  after="$(k get configmap "$name" -o jsonpath='{.data}')"
  [ "$before" = "$after" ] || printf 'changed'
}

# The per-mode environment for the UI, the mock and Temporal, one ConfigMap each, so no manifest
# names a mode. The UI's REVIEWER_ROLES is the same role values-kind-oidc-mock.yaml makes
# privileged (CHEMCLAW_ENTRA_PRIVILEGED_ROLES): the BFF is told it separately, and left empty the UI
# offers nobody the privileged actions core would accept from them.
AUTH_CHANGED=""
apply_auth_config() {
  local ui mock temporal
  case "$AUTH" in
    devauth)
      ui="AUTH_MODE=dev
ALLOW_INSECURE_AUTH=true"
      mock="MOCK_ENTRA_ENABLED=false"
      temporal=""
      ;;
    oidc-mock)
      ui="AUTH_MODE=msal
ENTRA_TENANT_ID=mock-tenant
ENTRA_CLIENT_ID=mock-spa-client
API_SCOPE=api://chemclaw/Chat.Access
ENTRA_AUTHORITY=$TENANT_URL
REVIEWER_ROLES=chemist"
      mock="MOCK_ENTRA_ENABLED=true
MOCK_ENTRA_ISSUER=$TENANT_URL/v2.0
MOCK_ENTRA_AUDIENCE=api://chemclaw
MOCK_ENTRA_SPA_CLIENT_ID=mock-spa-client
MOCK_ENTRA_REDIRECT_URIS=http://127.0.0.1:15173/auth/callback,http://localhost:15173/auth/callback
MOCK_SSL_CERTFILE=/tls/tls.crt
MOCK_SSL_KEYFILE=/tls/tls.key"
      # The server's own frontend and internode TLS (auto-setup's config template), client
      # certificates required, and — under the same names — the client half the bundled `temporal`
      # CLI (namespace registration) and the Temporal UI read.
      temporal="TEMPORAL_TLS_SERVER_CA_CERT=/tls/ca.crt
TEMPORAL_TLS_SERVER_CERT=/tls/tls.crt
TEMPORAL_TLS_SERVER_KEY=/tls/tls.key
TEMPORAL_TLS_FRONTEND_CERT=/tls/tls.crt
TEMPORAL_TLS_FRONTEND_KEY=/tls/tls.key
TEMPORAL_TLS_CLIENT1_CA_CERT=/tls/ca.crt
TEMPORAL_TLS_CLIENT2_CA_CERT=/tls/ca.crt
TEMPORAL_TLS_REQUIRE_CLIENT_AUTH=true
TEMPORAL_TLS_INTERNODE_SERVER_NAME=temporal-frontend
TEMPORAL_TLS_FRONTEND_SERVER_NAME=temporal-frontend
TEMPORAL_TLS_CA=/tls/ca.crt
TEMPORAL_TLS_CERT=/tls/client.crt
TEMPORAL_TLS_KEY=/tls/client.key
TEMPORAL_TLS_SERVER_NAME=temporal-frontend
TEMPORAL_TLS_ENABLE_HOST_VERIFICATION=true"
      ;;
  esac
  AUTH_CHANGED="$AUTH_CHANGED$(printf '%s\n' "$ui" | apply_env_configmap chemclaw-ui-auth)"
  AUTH_CHANGED="$AUTH_CHANGED$(printf '%s\n' "$mock" | apply_env_configmap chemclaw-mock-auth)"
  AUTH_CHANGED="$AUTH_CHANGED$(printf '%s\n' "$temporal" | apply_env_configmap chemclaw-temporal-tls-env)"
}

# The mock tenant on 127.0.0.1:18443 for a cluster that has no host mapping for it.
#
# A fresh cluster gets the port from kind-config.yaml (30443 → 18443). One created before that
# mapping existed cannot gain it — Docker fixes a container's ports at creation — and recreating it
# means reloading every image, so instead a supervised `kubectl port-forward` serves the same
# address: a loop that restarts the forward whenever it exits (a pod restart ends one), recorded
# in a pidfile so `down` and a switch back to devauth stop it. Nothing here runs on a cluster that
# has the mapping.
readonly FORWARD_PIDFILE="${TMPDIR:-/tmp}/chemclaw-kind-tenant-forward.pid"
stop_tenant_forward() {
  [ -f "$FORWARD_PIDFILE" ] || return 0
  local pid
  pid="$(cat "$FORWARD_PIDFILE")"
  # The loop first, so it cannot start another forward; then the forward it was running.
  kill "$pid" 2>/dev/null || true
  pkill -f "port-forward --context $CTX -n $NS svc/mock-eln-public" 2>/dev/null || true
  rm -f "$FORWARD_PIDFILE"
}
tenant_forward() {
  if [ "$AUTH" != oidc-mock ]; then stop_tenant_forward; return 0; fi
  if docker port "$CLUSTER-control-plane" 30443/tcp >/dev/null 2>&1; then return 0; fi
  if [ -f "$FORWARD_PIDFILE" ] && kill -0 "$(cat "$FORWARD_PIDFILE")" 2>/dev/null; then return 0; fi
  log "this cluster predates the 18443 mapping — serving the mock tenant there by a supervised port-forward"
  nohup bash -c "while true; do kubectl port-forward --context $CTX -n $NS svc/mock-eln-public \
    --address 127.0.0.1 18443:8090 >/dev/null 2>&1; sleep 2; done" >/dev/null 2>&1 &
  echo $! >"$FORWARD_PIDFILE"
}

# ------------------------------------------------------------------------------------- tls

# One CA per cluster, and the leaf certificates the in-cluster TLS endpoints serve: Postgres, the
# Temporal frontend (plus the client certificate every Temporal caller presents), and the mock
# tenant. Issued once and kept as Secrets; the CA's private key is never stored — it exists in a
# temporary directory for the length of this function, so re-issuing means a new CA, which is
# what `make kind-down` gives a fresh cluster anyway. `chemclaw-kind-ca` holds the CA certificate
# alone: the smoke's `--cacert`, and what a browser trusts to sign in without a warning.
# The CA's key lives here while `ensure_tls` runs, and goes with the process whichever way it exits.
TLS_WORKDIR=""
trap '[ -z "$TLS_WORKDIR" ] || rm -rf "$TLS_WORKDIR"' EXIT
TLS_SECRETS=(chemclaw-kind-ca postgres-tls temporal-tls chemclaw-temporal-tls mock-eln-tls)
ensure_tls() {
  local secret missing=""
  for secret in "${TLS_SECRETS[@]}"; do
    k get secret "$secret" >/dev/null 2>&1 || missing="$missing $secret"
  done
  [ -n "$missing" ] || return 0
  log "issuing this cluster's CA and TLS certificates (missing:$missing)"
  local dir
  dir="$(mktemp -d)"
  TLS_WORKDIR="$dir"
  ( umask 077
    cat >"$dir/openssl.cnf" <<'CNF'
[req]
distinguished_name = dn
[dn]
[ca]
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
CNF
    openssl req -x509 -new -nodes -newkey rsa:2048 -sha256 -days 825 -subj "/CN=chemclaw-kind-ca" \
      -config "$dir/openssl.cnf" -extensions ca -keyout "$dir/ca.key" -out "$dir/ca.crt" 2>/dev/null
    issue() {
      local name="$1" sans="$2"
      printf '[leaf]\nbasicConstraints = CA:FALSE\nkeyUsage = critical,digitalSignature,keyEncipherment\nextendedKeyUsage = serverAuth,clientAuth\nsubjectKeyIdentifier = hash\nauthorityKeyIdentifier = keyid\nsubjectAltName = %s\n' \
        "$sans" >"$dir/$name.ext"
      openssl req -new -nodes -newkey rsa:2048 -sha256 -subj "/CN=$name" -config "$dir/openssl.cnf" \
        -keyout "$dir/$name.key" -out "$dir/$name.csr" 2>/dev/null
      openssl x509 -req -sha256 -days 825 -in "$dir/$name.csr" -CA "$dir/ca.crt" -CAkey "$dir/ca.key" \
        -CAcreateserial -extfile "$dir/$name.ext" -extensions leaf -out "$dir/$name.crt" 2>/dev/null
    }
    issue postgres "DNS:postgres,DNS:postgres.$NS.svc"
    issue temporal "DNS:temporal-frontend,DNS:temporal-frontend.$NS.svc,DNS:localhost,IP:127.0.0.1"
    issue temporal-client "DNS:chemclaw-temporal-client"
    issue mock-eln "DNS:mock-eln,DNS:mock-eln.$NS.svc,DNS:localhost,IP:127.0.0.1"
  ) || die "issuing the cluster's certificates failed (openssl: $(command -v openssl))"
  apply_secret() { k create secret generic "$@" --dry-run=client -o yaml | k apply -f - >/dev/null; }
  apply_secret chemclaw-kind-ca --from-file=ca.crt="$dir/ca.crt"
  apply_secret postgres-tls --from-file=tls.crt="$dir/postgres.crt" --from-file=tls.key="$dir/postgres.key"
  apply_secret temporal-tls --from-file=ca.crt="$dir/ca.crt" \
    --from-file=tls.crt="$dir/temporal.crt" --from-file=tls.key="$dir/temporal.key" \
    --from-file=client.crt="$dir/temporal-client.crt" --from-file=client.key="$dir/temporal-client.key"
  # The chart's own Secret, in the shape deploy/README.md documents: the client pair and the CA.
  apply_secret chemclaw-temporal-tls --from-file=ca.crt="$dir/ca.crt" \
    --from-file=tls.crt="$dir/temporal-client.crt" --from-file=tls.key="$dir/temporal-client.key"
  apply_secret mock-eln-tls --from-file=tls.crt="$dir/mock-eln.crt" --from-file=tls.key="$dir/mock-eln.key"
  AUTH_CHANGED="${AUTH_CHANGED}changed"
  rm -rf "$dir"; TLS_WORKDIR=""
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
  # Postgres serves TLS in every mode; oidc-mock *requires* it, because core refuses a non-loopback
  # DSN below `sslmode=require` once sign-in is enforced (`require_pg_tls`).
  local tls=""
  [ "$AUTH" = oidc-mock ] && tls="?sslmode=require"
  {
    printf 'CHEMCLAW_POSTGRES_DSN=postgresql://chemclaw_app:%s@postgres:5432/chemclaw%s\n' "$app_pw" "$tls"
    printf 'CHEMCLAW_POSTGRES_MIGRATION_DSN=postgresql://chemclaw:%s@postgres:5432/chemclaw%s\n' "$owner_pw" "$tls"
    printf 'CHEMCLAW_LLM_API_KEY=%s\n' "$LLM_KEY"
    printf 'CHEMCLAW_KNOWLEDGE_REPO_TOKEN=\n'
    # The core bundles' own bearers, and the prompt-injection envelope's tag: without a shared one
    # every replica and every restart frames retrieved content under its own random nonce (the
    # front door warns as much at start).
    for var in CHEMCLAW_BO_MCP_TOKEN CHEMCLAW_CALC_MCP_TOKEN CHEMCLAW_MOLFP_MCP_TOKEN \
      CHEMCLAW_RXNFP_MCP_TOKEN CHEMCLAW_FRAMING_ENVELOPE_SECRET; do
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
  ensure_tls
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

  # `envFrom` and mounted certificates are read at container start, so a mode switch or a new CA
  # restarts exactly the pods that read them.
  if [ -n "$AUTH_CHANGED" ]; then
    log "auth mode or certificates changed — restarting Postgres, Temporal, the mock and the UI"
    k rollout restart statefulset/postgres deployment/temporal deployment/temporal-ui >/dev/null
    if have "chemclaw/mock:$TAG"; then k rollout restart deployment/mock-eln >/dev/null; fi
    if have "$(ui_image)" && k get deployment/ui >/dev/null 2>&1; then
      k rollout restart deployment/ui >/dev/null
    fi
  fi
  wait_rollout statefulset/postgres 180s
  wait_rollout deployment/temporal 300s
  wait_rollout deployment/mock-llm 180s
  if have "chemclaw/mock:$TAG"; then
    wait_rollout deployment/mock-eln 300s
    wait_rollout deployment/mock-vendor 120s
  fi
  for name in ${servers[@]+"${servers[@]}"}; do wait_rollout "deployment/chemclaw-mcp-$name" 600s; done
  tenant_forward
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
  AUTH_VALUES=()
  if [ "$AUTH" = oidc-mock ]; then AUTH_VALUES=(-f "$KIND_DIR/values-kind-oidc-mock.yaml"); fi
  log "helm upgrade --install $RELEASE (core image chemclaw/core:$CORE_TAG) — runs the migrate hook first"
  # **Twice at most, and the second attempt is the convergence, not a retry of luck.** A fresh `up`
  # starts some two dozen Python processes on one node at once; a pod that needs longer than its
  # Deployment's 600 s progress deadline gets the Deployment marked `ProgressDeadlineExceeded`, and
  # Helm 4's readiness check fails the release on that *status* at once — while the pod goes on to
  # become ready a few minutes later. The post-install hooks (convert, schedules) run only after a
  # successful wait, so stopping there leaves them unrun. So: on a failed wait, give every
  # Deployment a bounded chance to become Available, then upgrade again — which changes nothing,
  # finds everything ready, and runs the hooks.
  local attempt
  for attempt in 1 2; do
    if helm --kube-context "$CTX" upgrade --install "$RELEASE" "$REPO_ROOT/deploy/helm/chemclaw" \
      --namespace "$NS" -f "$KIND_DIR/values-kind.yaml" ${AUTH_VALUES[@]+"${AUTH_VALUES[@]}"} \
      --set-string "image.tag=$CORE_TAG" ${LLM_SET[@]+"${LLM_SET[@]}"} ${CHART_SET[@]+"${CHART_SET[@]}"} \
      --wait --wait-for-jobs --timeout 20m --force-conflicts; then
      break
    fi
    [ "$attempt" = 1 ] || die "helm upgrade --install failed twice. Hook Jobs are deleted only on
  success, so a failed one is still there to read:
    kubectl --context $CTX -n $NS get jobs,pods
    kubectl --context $CTX -n $NS logs job/chemclaw-migrate   (or -convert, -schedules)"
    warn "helm's readiness wait failed; giving every Deployment up to 15 min to become Available, then upgrading again"
    k wait --for=condition=Available deployment --all --timeout=900s >/dev/null \
      || die "not every Deployment became Available:
$(k get deploy 2>&1)"
  done
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

# The caller's credential for the API: none in devauth (the front door mints its dev principal), a
# token from the mock tenant's mint in oidc-mock — for `alice`, who holds `chemist`, the role
# values-kind-oidc-mock.yaml names privileged, so the durable job is hers to launch.
AUTH_HEADER=(-H "x-kind-smoke: 1")
smoke_credential() {
  [ "$AUTH" = oidc-mock ] || return 0
  local ca token
  ca="$(mktemp)"
  k get secret chemclaw-kind-ca -o jsonpath='{.data.ca\.crt}' | base64 -d >"$ca"
  token="$(curl -sf -m 20 --cacert "$ca" -X POST "$TENANT_URL/oauth2/v2.0/token" \
    -H 'content-type: application/json' \
    -d '{"oid":"00000000-0000-0000-0000-00000000a11c","upn":"alice@mock-tenant.test","roles":["chemist","reviewer"]}' \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')" \
    || { rm -f "$ca"; die "smoke: the mock tenant at $TENANT_URL minted no token"; }
  rm -f "$ca"
  AUTH_HEADER=(-H "Authorization: Bearer $token")
  # And the half that makes the other half mean something: no token, no session.
  local anonymous
  anonymous="$(curl -s -o /dev/null -m 10 -w '%{http_code}' -X POST "$FRONT_DOOR/sessions" \
    -H 'content-type: application/json' -d '{}' || true)"
  [ "$anonymous" = 401 ] || die "smoke: an anonymous POST /sessions answered $anonymous, not 401"
  log "smoke: sign-in enforced (anonymous 401) and a mock-tenant token minted for alice"
}

# One turn through the front door: create a session, post a message carrying a mock-LLM behaviour
# marker, read the SSE stream to its end. Prints the stream.
turn() {
  local marker="$1" session
  session="$(curl -sf -m 20 -X POST "$FRONT_DOOR/sessions" "${AUTH_HEADER[@]}" \
    -H 'content-type: application/json' -d '{}' \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["session_id"])')" \
    || die "smoke: POST /sessions failed"
  curl -sN -m 300 -X POST "$FRONT_DOOR/sessions/$session/messages" "${AUTH_HEADER[@]}" \
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
  smoke_credential
  local stream
  stream="$(turn a-cheap)"
  grep -q 'Two notes cover this coupling' <<<"$stream" \
    || die "smoke: the a-cheap turn did not end with its scripted answer. Stream tail:
$(tail -n 20 <<<"$stream")"
  grep -q 'find_notes' <<<"$stream" || die "smoke: the a-cheap turn made no find_notes call"
  # `/readyz` asks each connector's `/healthz`, which is a plain route; a connector whose MCP
  # transport refuses the front door (the 421 a loopback-only Host allow-list gives) is healthy
  # there and absent from the turn. The turn's own `capability_degraded` event is what names it.
  if grep -q '"type":"capability_degraded"' <<<"$stream"; then
    die "smoke: the turn could not open every bound connector: $(grep -o '"connectors":\[[^]]*\]' <<<"$stream" | head -1)
  see the front door's log for each one's reason"
  fi
  log "smoke: mock-LLM turn answered (find_notes called, scripted answer streamed)"

  stream="$(turn d-collide)"
  local job_id
  job_id="$(python3 -c 'import re,sys; m=re.findall(r"\"job_id\": ?\"([^\"]+)\"", sys.stdin.read()); print(m[0] if m else "")' <<<"$stream")"
  if [ -z "$job_id" ]; then
    # **A persisted result is never recomputed (D-011)**: once one run of this payload completed,
    # the next turn is answered from the cache and launches nothing. That is the durable layer
    # working, so the evidence moves to the record of the run that did complete.
    grep -q '"tool":"compute_reaction_energy"' <<<"$stream" \
      || die "smoke: the d-collide turn neither launched a job nor called compute_reaction_energy. Stream tail:
$(tail -n 20 <<<"$stream")"
    local done_before
    done_before="$(curl -sf -m 10 "${AUTH_HEADER[@]}" "$FRONT_DOOR/jobs" | python3 -c 'import json,sys
print(next((j["job_id"] for j in json.load(sys.stdin)
            if j.get("job") == "compute_reaction_energy" and j.get("state") == "completed"), ""))' || true)"
    [ -n "$done_before" ] || die "smoke: compute_reaction_energy answered from no completed job"
    log "smoke: durable result served from the cache of completed job $done_before (D-011)"
    return
  fi
  log "smoke: durable job $job_id launched; waiting for it to complete (≤ 10 min)"
  local state="" body
  for _ in $(seq 1 120); do
    body="$(curl -sf -m 10 "${AUTH_HEADER[@]}" "$FRONT_DOOR/jobs/$job_id" || true)"
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
  stop_tenant_forward
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
