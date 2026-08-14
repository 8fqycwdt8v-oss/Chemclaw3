#!/usr/bin/env bash
# Bring up the four-repo ChemClaw3 stack for a full end-to-end pass: this backend, the
# Chemclaw3-mcp tool fleet (props, rxnpredict), Chemclaw3_mock (the HPC/Nextflow mock, the
# eln-json/eln-ord data sources, the mock-vendor MCP tool), and Chemclaw3_ui.
#
# Deliberately does not reimplement readiness polling for pieces that already have it:
# `infra/live/bootstrap.sh` brings up Postgres/Temporal and the PR-gate's note repo, and
# `infra/live/processes.sh` brings up this repo's own connectors, four Temporal workers and front
# door. Both are called as subprocesses. This script owns only what those two do not know about —
# the four external processes from the other three repos, the env that wires everything together,
# and the UI's BFF+SPA — using the same log/die/wait_for shape `processes.sh` already established.
#
# Usage: up.sh [up|down|status|restart <name>]
# Sibling checkout paths: CHEMCLAW_MCP_REPO, CHEMCLAW_MOCK_REPO, CHEMCLAW_UI_REPO.

set -euo pipefail

readonly HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd "$HARNESS_DIR/../../.." && pwd)"
readonly LIVE_DIR="${CHEMCLAW_LIVE_DIR:-$REPO_ROOT/.live}"
readonly RUN_DIR="$LIVE_DIR/e2e/run"

readonly MCP_REPO="${CHEMCLAW_MCP_REPO:-/workspace/8fqycwdt8v-oss/chemclaw3-mcp}"
readonly MOCK_REPO="${CHEMCLAW_MOCK_REPO:-/workspace/8fqycwdt8v-oss/chemclaw3_mock}"
readonly UI_REPO="${CHEMCLAW_UI_REPO:-/workspace/8fqycwdt8v-oss/chemclaw3_ui}"

# stderr, not stdout: `mock_venv_bin()` returns a path via stdout command substitution, and a
# log() that shared stdout corrupted it with ANSI-coded log text — the exact bug that made
# mock-hpc-eln's exec target unparseable. die() already had this right; log() did not.
log() { printf '\033[35m[e2e]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31m[e2e] %s\033[0m\n' "$*" >&2; exit 1; }

require_repo() {
  local path="$1" name="$2"
  [ -d "$path" ] || die "$name checkout not found at $path — set the env var or clone it there"
}

# ---------------------------------------------------------------------------- process helpers
# Same shape as infra/live/processes.sh's start/wait_for: no subshell around the launch (the
# recorded pid must be the real process, not a wrapper), readiness asked rather than assumed.

start() {
  local name="$1"; shift
  local pidfile="$RUN_DIR/$name.pid"
  if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    log "$name already running (pid $(cat "$pidfile"))"
    return
  fi
  nohup "$@" >"$LIVE_DIR/e2e-$name.log" 2>&1 &
  echo $! >"$pidfile"
  log "$name started (pid $(cat "$pidfile"))"
}

wait_for() {
  local name="$1" url="$2" attempts="${3:-120}"
  local pidfile="$RUN_DIR/$name.pid"
  for _ in $(seq 1 "$attempts"); do
    if curl -fs -o /dev/null --max-time 2 "$url"; then
      log "$name ready"
      return
    fi
    if [ -f "$pidfile" ] && ! kill -0 "$(cat "$pidfile")" 2>/dev/null; then
      die "$name exited before becoming ready — see $LIVE_DIR/e2e-$name.log"
    fi
    sleep 1
  done
  die "$name did not become ready at $url — see $LIVE_DIR/e2e-$name.log"
}

# ---------------------------------------------------------------------------- Chemclaw3-mcp
# `props` and `rxnpredict` share one uv workspace at the repo root, so one resolved interpreter
# serves both (same reasoning as processes.sh's python_bin()).

mcp_python_bin() { ( cd "$MCP_REPO" && uv sync --quiet && uv run python -c 'import sys; print(sys.executable)' ); }

start_props() {
  local python="$1"
  # No --app-dir: `python` is the shared workspace venv's interpreter, which already has
  # `chemclaw_mcp_props` on its path via uv's editable workspace install.
  CHEMCLAW_PROPS_TOKEN="${CHEMCLAW_PROPS_TOKEN:-dev-token}" \
    start props "$python" -m uvicorn chemclaw_mcp_props.app:app --host 127.0.0.1 --port 8850
  wait_for props "http://127.0.0.1:8850/healthz"
}

start_rxnpredict() {
  local python="$1"
  # fake_a/fake_c: a deterministic tool surface with no model weights and no checkpoint download —
  # exactly what CI-shaped hardware wants (no GPU, no HuggingFace egress). See
  # engine/base_doubles.py::register_requested for how the env vars below reach the registry.
  CHEMCLAW_RXNPREDICT_TOKEN="${CHEMCLAW_RXNPREDICT_TOKEN:-dev-token}" \
    CHEMCLAW_RXNPREDICT_ENABLED_FORWARD_MODELS="${CHEMCLAW_RXNPREDICT_ENABLED_FORWARD_MODELS:-fake_a}" \
    CHEMCLAW_RXNPREDICT_ENABLED_CONDITIONS_MODELS="${CHEMCLAW_RXNPREDICT_ENABLED_CONDITIONS_MODELS:-fake_c}" \
    start rxnpredict "$python" -m uvicorn chemclaw_mcp_rxnpredict.app:app --host 127.0.0.1 --port 8857
  wait_for rxnpredict "http://127.0.0.1:8857/healthz"
}

# ---------------------------------------------------------------------------- Chemclaw3_mock
# Its own venv (start.sh/start-mcp.sh hard-code `.venv/bin/python`), created once, idempotently.

mock_venv_bin() {
  if [ ! -x "$MOCK_REPO/.venv/bin/python" ]; then
    log "creating Chemclaw3_mock's venv"
    ( cd "$MOCK_REPO" && python3 -m venv .venv && .venv/bin/pip install --quiet -e '.[dev]' )
  fi
  echo "$MOCK_REPO/.venv/bin/python"
}

start_mock_hpc_eln() {
  local python="$1"
  # bash -c ... exec, not a bare invocation: app/eln's real-dataset loader reads its CSVs by a
  # path relative to cwd (the same reason start.sh itself does `cd "$SCRIPT_DIR"` first), and
  # `exec` replaces the shell in place so the pid `start()` records is still the real process.
  MOCK_ELN_EXPORT_DIR="$MOCK_REPO/data/eln/exports" \
    MOCK_ORD_EXPORT_DIR="$MOCK_REPO/data/eln/exports/ord" \
    MOCK_HPC_API_TOKEN="${CHEMCLAW_HPC_API_TOKEN:-mock-hpc-token}" \
    MOCK_HPC_ENFORCE_AUTH=true \
    MOCK_HPC_POLLS_UNTIL_DONE="${MOCK_HPC_POLLS_UNTIL_DONE:-2}" \
    MOCK_ELN_SEED_ON_STARTUP=true \
    start mock-hpc-eln bash -c \
      "cd '$MOCK_REPO' && exec '$python' -m uvicorn app.main:app --host 0.0.0.0 --port 8090"
  wait_for mock-hpc-eln "http://127.0.0.1:8090/healthz"
}

start_mock_vendor() {
  local python="$1"
  MOCK_MCP_VENDOR_HOST=0.0.0.0 MOCK_MCP_VENDOR_PORT=8091 \
    start mock-vendor bash -c "cd '$MOCK_REPO' && exec '$python' -m app.mcp_tools.vendor_server"
  # No REST /healthz on the MCP transport itself; a TCP-reachable /mcp answering (even a 4xx for a
  # bare GET, which streamable-http gives an unauthenticated non-POST request) is evidence the
  # ASGI app is up — the same "reachable" bar the connector manifest's own probe uses when a
  # bundle exposes nothing dedicated to poll.
  local pidfile="$RUN_DIR/mock-vendor.pid"
  for _ in $(seq 1 60); do
    local code
    code="$(curl -s -o /dev/null --max-time 2 -w '%{http_code}' "http://127.0.0.1:8091/mcp" || true)"
    [ -n "$code" ] && [ "$code" != "000" ] && { log "mock-vendor ready ($code)"; return; }
    if [ -f "$pidfile" ] && ! kill -0 "$(cat "$pidfile")" 2>/dev/null; then
      die "mock-vendor exited before becoming ready — see $LIVE_DIR/e2e-mock-vendor.log"
    fi
    sleep 1
  done
  die "mock-vendor did not become ready on 8091 — see $LIVE_DIR/e2e-mock-vendor.log"
}

# ---------------------------------------------------------------------------- Chemclaw3_ui

start_ui() {
  if [ ! -d "$UI_REPO/node_modules" ]; then
    log "installing Chemclaw3_ui dependencies"
    ( cd "$UI_REPO" && npm install --silent )
  fi
  CHEMCLAW_API_URL="http://127.0.0.1:${CHEMCLAW_LIVE_API_PORT:-8000}" \
    AUTH_MODE=dev \
    start ui-bff bash -c "cd '$UI_REPO' && exec npm run dev"
  wait_for ui-bff "http://127.0.0.1:5173"
}

# ---------------------------------------------------------------------------- entrypoint

up() {
  require_repo "$MCP_REPO" "Chemclaw3-mcp"
  require_repo "$MOCK_REPO" "Chemclaw3_mock"
  require_repo "$UI_REPO" "Chemclaw3_ui"
  mkdir -p "$RUN_DIR"

  log "bringing up infra (Postgres/pgvector + Temporal + note repo)"
  bash "$REPO_ROOT/infra/live/bootstrap.sh" up

  # bootstrap.sh's own last line says "Next: make db-migrate && make live-up" — a step that has
  # been missed by hand before (this script's own first live run hit "relation session_owners
  # does not exist" from skipping it). Both commands are idempotent, so running them
  # unconditionally on every `up` is correct rather than merely convenient.
  log "applying database migrations"
  ( cd "$REPO_ROOT" && uv run python -m chemclaw.core.migrate \
      && uv run python -m chemclaw.agent.message_migration )

  # The env this backend's front door and workers need — composed once, here, and exported before
  # infra/live/processes.sh runs so it inherits every one of these (it only sets defaults for the
  # keys it already knows about; none of the keys below are among them).
  local own_connectors
  own_connectors="$(cd "$REPO_ROOT" && uv run python -c \
    'import chemclaw.connectors, pathlib; print(pathlib.Path(chemclaw.connectors.__file__).parent)')"
  export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-$(printenv 'API-KEY' 2>/dev/null || true)}"
  [ -n "$ANTHROPIC_API_KEY" ] || die "no ANTHROPIC_API_KEY and no 'API-KEY' env var to map it from"
  export CHEMCLAW_CONNECTORS_DIR="$own_connectors:$MCP_REPO/manifests:$HARNESS_DIR/manifests"
  export CHEMCLAW_DATA_SOURCES="graph,eln-json,eln-ord"
  export CHEMCLAW_ELN_EXPORT_DIR="$MOCK_REPO/data/eln/exports"
  export CHEMCLAW_ORD_EXPORT_DIR="$MOCK_REPO/data/eln/exports/ord"
  export CHEMCLAW_HPC_LAUNCH_INTERFACE=nextflow
  export CHEMCLAW_HPC_API_BASE_URL="http://localhost:8090"
  export CHEMCLAW_HPC_API_TOKEN="${CHEMCLAW_HPC_API_TOKEN:-mock-hpc-token}"
  export CHEMCLAW_HPC_ARTIFACT_STORE_URL="http://localhost:8090/artifacts"
  export CHEMCLAW_HPC_PIPELINE_NAME="qm-pipeline"
  export CHEMCLAW_HPC_PIPELINE_VERSION="mock-1"
  export CHEMCLAW_PROPS_TOKEN="${CHEMCLAW_PROPS_TOKEN:-dev-token}"
  export CHEMCLAW_RXNPREDICT_TOKEN="${CHEMCLAW_RXNPREDICT_TOKEN:-dev-token}"

  log "connectors dir: $CHEMCLAW_CONNECTORS_DIR"

  log "starting the Chemclaw3-mcp fleet (props, rxnpredict)"
  local mcp_python; mcp_python="$(mcp_python_bin)"
  start_props "$mcp_python"
  start_rxnpredict "$mcp_python"

  log "starting Chemclaw3_mock (HPC/ELN mock + mock-vendor MCP tool)"
  local mock_python; mock_python="$(mock_venv_bin)"
  start_mock_hpc_eln "$mock_python"
  start_mock_vendor "$mock_python"

  log "starting this repo's connectors, workers and front door"
  bash "$REPO_ROOT/infra/live/processes.sh" up

  log "starting Chemclaw3_ui (BFF + SPA)"
  start_ui

  log "full stack up. UI: http://127.0.0.1:5173 · front door: http://127.0.0.1:${CHEMCLAW_LIVE_API_PORT:-8000} · logs: $LIVE_DIR"
}

down() {
  log "stopping Chemclaw3_ui"
  [ -d "$RUN_DIR" ] || { log "nothing running"; return; }
  for pidfile in "$RUN_DIR"/*.pid; do
    [ -e "$pidfile" ] || continue
    local name pid
    name="$(basename "$pidfile" .pid)"
    pid="$(cat "$pidfile")"
    if kill -0 "$pid" 2>/dev/null; then
      # UI dev server forks (vite + the BFF): kill the process group, not just the recorded pid.
      kill -- "-$(ps -o pgid= "$pid" | tr -d ' ')" 2>/dev/null || kill "$pid" 2>/dev/null || true
      log "$name stopped (pid $pid)"
    fi
    rm -f "$pidfile"
  done
  log "stopping this repo's connectors/workers/front door"
  bash "$REPO_ROOT/infra/live/processes.sh" down
}

status() {
  [ -d "$RUN_DIR" ] || { log "nothing running (external processes)"; }
  for pidfile in "$RUN_DIR"/*.pid; do
    [ -e "$pidfile" ] || continue
    local name pid
    name="$(basename "$pidfile" .pid)"
    pid="$(cat "$pidfile")"
    if kill -0 "$pid" 2>/dev/null; then printf '  %-16s up   (pid %s)\n' "$name" "$pid"
    else printf '  %-16s DOWN\n' "$name"; fi
  done
  bash "$REPO_ROOT/infra/live/processes.sh" status
}

# Stop one named external process and bring it back — the shape the chaos round needs. Only
# covers the processes this script owns (props, rxnpredict, mock-hpc-eln, mock-vendor, ui-bff);
# restarting a piece of this repo's own stack is infra/live/processes.sh's `restart` verb.
restart() {
  local name="$1" pidfile="$RUN_DIR/$1.pid"
  [ -e "$pidfile" ] || die "no $pidfile — is '$name' up?"
  local pid; pid="$(cat "$pidfile")"
  kill -9 "$pid" 2>/dev/null || true
  for _ in $(seq 1 50); do kill -0 "$pid" 2>/dev/null || break; sleep 0.2; done
  rm -f "$pidfile"
  log "$name killed (pid $pid)"
  case "$name" in
    props) start_props "$(mcp_python_bin)" ;;
    rxnpredict) start_rxnpredict "$(mcp_python_bin)" ;;
    mock-hpc-eln) start_mock_hpc_eln "$(mock_venv_bin)" ;;
    mock-vendor) start_mock_vendor "$(mock_venv_bin)" ;;
    ui-bff) start_ui ;;
    *) die "restart: unknown process '$name'" ;;
  esac
}

case "${1:-up}" in
  up) up ;;
  down) down ;;
  status) status ;;
  restart) [ $# -ge 2 ] || die "usage: up.sh restart <name>"; restart "$2" ;;
  *) die "usage: up.sh [up|down|status|restart <name>]" ;;
esac
