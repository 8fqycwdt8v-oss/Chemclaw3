#!/usr/bin/env bash
# The live lane's caching gateway (D-2026-10-24-the-live-lane-caches-through-a-local-gateway).
#
# Anthropic's OpenAI-compatible endpoint does not cache prompts, and every ChemClaw turn resends a
# prefix of about 50k tokens per model call. This starts LiteLLM on loopback in front of the native
# Messages API with cache breakpoints (`litellm.yaml`), so the lane keeps dialling one
# OpenAI-compatible gateway (D-2026-09-04-a-gateway-is-the-only-provider) and pays for the prefix
# once per five minutes instead of once per call.
#
# The vendor key is read from ANTHROPIC_API_KEY (or the variable literally named `API-KEY`) and
# only ever lives in the gateway's environment. ChemClaw authenticates to the gateway with a local
# token minted here (0600 under .live/run/), never with the vendor key.
#
# Usage: up.sh [up|down|env]    `env` prints the CHEMCLAW_LLM_* exports for the lane's shell.

set -euo pipefail

readonly HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd "$HARNESS_DIR/../../.." && pwd)"
readonly LIVE_DIR="${CHEMCLAW_LIVE_DIR:-$REPO_ROOT/.live}"
readonly RUN_DIR="$LIVE_DIR/run"
readonly PIDFILE="$RUN_DIR/gateway.pid"
readonly TOKENFILE="$RUN_DIR/gateway.token"
readonly PORT="${CHEMCLAW_LIVE_GATEWAY_PORT:-4000}"
readonly MODEL="${CHEMCLAW_LIVE_GATEWAY_MODEL:-claude-sonnet-5-5}"
# Pinned: this process holds the vendor key, so an unreviewed release must not reach it.
readonly LITELLM_SPEC="litellm[proxy]==1.106.2"

log() { printf '\033[36m[gateway]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31m[gateway] %s\033[0m\n' "$*" >&2; exit 1; }

running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

vendor_key() {
  local key="${ANTHROPIC_API_KEY:-$(printenv 'API-KEY' 2>/dev/null || true)}"
  [ -n "$key" ] || die "no vendor key: set ANTHROPIC_API_KEY (or API-KEY) in this shell"
  printf '%s' "$key"
}

up() {
  mkdir -p "$RUN_DIR"
  if running; then log "already running (pid $(cat "$PIDFILE"))"; return; fi
  local key
  key="$(vendor_key)"
  command -v uv >/dev/null 2>&1 || die "uv not found"
  # The umask wraps the redirection, so the file is created 0600 rather than chmod-ed after.
  ( umask 077; python3 -c 'import secrets; print("sk-lane-" + secrets.token_urlsafe(24))' >"$TOKENFILE" )
  ANTHROPIC_API_KEY="$key" LITELLM_MASTER_KEY="$(cat "$TOKENFILE")" LITELLM_TELEMETRY=False \
    nohup uv tool run --python 3.12 --from "$LITELLM_SPEC" litellm \
      --config "$HARNESS_DIR/litellm.yaml" --host 127.0.0.1 --port "$PORT" \
      >>"$LIVE_DIR/gateway.log" 2>&1 &
  echo $! >"$PIDFILE"
  for _ in $(seq 1 120); do
    running || die "gateway exited before becoming ready — see $LIVE_DIR/gateway.log"
    if curl -fs -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/health/liveliness"; then
      log "ready on http://127.0.0.1:$PORT/v1 (pid $(cat "$PIDFILE")); next: eval \"\$(bash $0 env)\""
      return
    fi
    sleep 1
  done
  die "gateway not ready after 120 s — see $LIVE_DIR/gateway.log"
}

down() {
  if running; then kill "$(cat "$PIDFILE")" && log "stopped"; else log "not running"; fi
  rm -f "$PIDFILE" "$TOKENFILE"
}

env_exports() {
  [ -f "$TOKENFILE" ] || die "no gateway token — run \`$0 up\` first"
  printf 'export CHEMCLAW_LLM_BASE_URL=%q\n' "http://127.0.0.1:$PORT/v1"
  printf 'export CHEMCLAW_LLM_MODEL=%q\n' "$MODEL"
  printf 'export CHEMCLAW_LLM_API_KEY=%q\n' "$(cat "$TOKENFILE")"
  printf 'export CHEMCLAW_LLM_ALLOW_LOOPBACK_GATEWAY=true\n'
}

case "${1:-up}" in
  up) up ;;
  down) down ;;
  env) env_exports ;;
  *) die "usage: $0 [up|down|env]" ;;
esac
