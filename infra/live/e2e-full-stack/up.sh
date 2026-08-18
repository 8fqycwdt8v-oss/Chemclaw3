#!/usr/bin/env bash
# Bring up the four-repo ChemClaw3 stack for a full end-to-end pass: this backend, the
# Chemclaw3-mcp tool fleet (props, rxnpredict, chem, safety, calc), Chemclaw3_mock (the HPC/Nextflow mock, the
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

# Assert the server accepts the bearer this lane will actually send it.
#
# `wait_for` above proves the process is up, and that is all it proves: `/healthz` is
# unauthenticated on every server in this fleet, so a token mismatch leaves the connector reading
# `healthy` while every `/mcp` call is refused. That is not hypothetical — this lane spent a whole
# storm run in it, and the misdiagnosis went all the way to Temporal: 401s surfaced as
# `CalcServerError: the calculation service is not answering`, four storm checks failed, and the
# only honest evidence was a `401 Unauthorized` line in the server's own log.
#
# `D-2026-08-17-a-harness-that-starts-two-of-five-servers...` names this blind spot — "/readyz says
# nothing about whether the caller holds the credential that backend verifies" — and this is the
# check that closes it for the lane. Any status but 401/403 counts as accepted: a bare POST is not
# a valid MCP `initialize`, so 400 and 406 are the *expected* healthy answers here. We are asking
# one question only, and it is not "does this call work".
assert_credential_accepted() {
  local name="$1" url="$2" token="$3"
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -X POST \
    -H "Authorization: Bearer $token" -H 'content-type: application/json' \
    -d '{}' "$url" || echo 000)"
  case "$code" in
    401|403)
      die "$name is running but refused this lane's credential (HTTP $code at $url). The server
      verifies a different value than the one exported here — check that the same token reaches
      both halves. A restarted process that predates this invocation keeps its old environment,
      which is the usual cause."
      ;;
    000) die "$name did not answer $url at all while checking its credential" ;;
    *) log "$name credential accepted (HTTP $code)" ;;
  esac
}

# ---------------------------------------------------------------------------- Chemclaw3-mcp
# The four servers this harness runs share one uv workspace at the repo root, so one resolved
# interpreter serves them all (same reasoning as processes.sh's python_bin()).
#
# `chem` and `safety` are here because Chemclaw3 *dials* them: both bundles declare
# `http://127.0.0.1:885{8,9}/mcp`, and under `CHEMCLAW_CONNECTORS_REQUIRED=true` an unreachable
# one is a hard startup failure of the front door, not a degraded connector. They were absent
# from this script while the fleet grew to five servers, which is exactly how that failed —
# `ConnectorsUnavailable: ... chem, safety`, with nothing in the harness saying who should have
# started them.
#
# The fifth server, `calc`, is started too, but it is NOT a connector and its manifest must stay
# off `CHEMCLAW_CONNECTORS_DIR` — it says so in a box. Chemclaw3 keeps its own `calc` bundle and
# all fifteen tools; what moved to the fleet is the *physics* behind them
# (D-2026-08-16-the-physics-leaves-the-cache-stays), which `connectors/calc/remote.py::calc_session`
# dials on a cache miss at `calc_server_url` (8860). "Not a connector" is not "not needed": with
# this server down, `/readyz` is entirely green — it probes connectors, and this is not one — and
# every calculator tool fails at call time with `CalcServerError: the calculation service is not
# answering`. That is how `predict_pka` failed on this harness's first real turn.
#
# Note the collision, because it is intentional: `chem` and `safety` exist *both* in this repo's
# `src/chemclaw/connectors/` and in the fleet's `manifests/`, same names, same tools. First
# directory on `CHEMCLAW_CONNECTORS_DIR` wins (`connectors/registry.py::_bundle_dirs`) and this
# script lists Chemclaw3's own first, so the in-tree bundles answer. That is the right way round
# for an end-to-end pass: the in-tree `safety` bundle ships `skills/safety-screening/SKILL.md`,
# and a skill is architecture layer 3 — the fleet's manifest declares none. Either way both
# bundles name the same URLs, so these two servers must run regardless of which manifest wins.

mcp_python_bin() { ( cd "$MCP_REPO" && uv sync --quiet && uv run python -c 'import sys; print(sys.executable)' ); }

start_props() {
  local python="$1"
  # No --app-dir: `python` is the shared workspace venv's interpreter, which already has
  # `chemclaw_mcp_props` on its path via uv's editable workspace install.
  CHEMCLAW_PROPS_TOKEN="${CHEMCLAW_PROPS_TOKEN:-dev-token}" \
    start props "$python" -m uvicorn chemclaw_mcp_props.app:app --host 127.0.0.1 --port 8850
  wait_for props "http://127.0.0.1:8850/healthz"
  assert_credential_accepted props "http://127.0.0.1:8850/mcp" "${CHEMCLAW_PROPS_TOKEN:-dev-token}"
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
  assert_credential_accepted rxnpredict "http://127.0.0.1:8857/mcp" "${CHEMCLAW_RXNPREDICT_TOKEN:-dev-token}"
}

start_chem() {
  local python="$1"
  CHEMCLAW_CHEM_TOKEN="${CHEMCLAW_CHEM_TOKEN:-dev-token}" \
    start chem "$python" -m uvicorn chemclaw_mcp_chem.app:app --host 127.0.0.1 --port 8858
  wait_for chem "http://127.0.0.1:8858/healthz"
  assert_credential_accepted chem "http://127.0.0.1:8858/mcp" "${CHEMCLAW_CHEM_TOKEN:-dev-token}"
}

start_safety() {
  local python="$1"
  CHEMCLAW_SAFETY_TOKEN="${CHEMCLAW_SAFETY_TOKEN:-dev-token}" \
    start safety "$python" -m uvicorn chemclaw_mcp_safety.app:app --host 127.0.0.1 --port 8859
  wait_for safety "http://127.0.0.1:8859/healthz"
  assert_credential_accepted safety "http://127.0.0.1:8859/mcp" "${CHEMCLAW_SAFETY_TOKEN:-dev-token}"
}

# Not a connector — see the fleet comment above. `calc_server_url` defaults to 8860.
start_calc() {
  local python="$1"
  CHEMCLAW_CALC_TOKEN="${CHEMCLAW_CALC_TOKEN:-dev-token}" \
    start calc "$python" -m uvicorn chemclaw_mcp_calc.app:app --host 127.0.0.1 --port 8860
  wait_for calc "http://127.0.0.1:8860/healthz"
  assert_credential_accepted calc "http://127.0.0.1:8860/mcp" "${CHEMCLAW_CALC_TOKEN:-dev-token}"
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
  # Both halves, in dependency order. `npm run dev` starts two processes and only one of them is
  # Vite; polling 5173 alone reported "ui-bff ready" while the BFF was dead at import, and every
  # /api call the browser made came back 502 from Vite's proxy. The BFF is the one this harness is
  # actually wiring to the front door, so it is the one whose own /healthz has to answer.
  wait_for ui-bff "http://127.0.0.1:${BFF_PORT:-8787}/healthz"
  wait_for ui-spa "http://127.0.0.1:5173"
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
  # Both halves of each token matter and they are set in two different places: the `start_*`
  # function gives the *server* the value it verifies, and this export gives the *front door* the
  # value it sends. Setting only the first is a specific and quiet failure — `/healthz` is
  # unauthenticated, so the connector reports `healthy` while every `/mcp` call it makes is
  # rejected, and the turn degrades with no clue why.
  export CHEMCLAW_PROPS_TOKEN="${CHEMCLAW_PROPS_TOKEN:-dev-token}"
  export CHEMCLAW_RXNPREDICT_TOKEN="${CHEMCLAW_RXNPREDICT_TOKEN:-dev-token}"
  export CHEMCLAW_CHEM_TOKEN="${CHEMCLAW_CHEM_TOKEN:-dev-token}"
  export CHEMCLAW_SAFETY_TOKEN="${CHEMCLAW_SAFETY_TOKEN:-dev-token}"
  export CHEMCLAW_CALC_TOKEN="${CHEMCLAW_CALC_TOKEN:-dev-token}"

  log "connectors dir: $CHEMCLAW_CONNECTORS_DIR"

  log "starting the Chemclaw3-mcp fleet (props, rxnpredict, chem, safety, calc)"
  local mcp_python; mcp_python="$(mcp_python_bin)"
  start_props "$mcp_python"
  start_rxnpredict "$mcp_python"
  start_chem "$mcp_python"
  start_safety "$mcp_python"
  start_calc "$mcp_python"

  log "starting Chemclaw3_mock (HPC/ELN mock + mock-vendor MCP tool)"
  local mock_python; mock_python="$(mock_venv_bin)"
  start_mock_hpc_eln "$mock_python"
  start_mock_vendor "$mock_python"

  log "starting this repo's connectors, workers and front door"
  bash "$REPO_ROOT/infra/live/processes.sh" up

  log "starting Chemclaw3_ui (BFF + SPA)"
  start_ui

  backfill_corpus

  log "full stack up. UI: http://127.0.0.1:5173 · front door: http://127.0.0.1:${CHEMCLAW_LIVE_API_PORT:-8000} · logs: $LIVE_DIR"
}

# Make the seeded ELN/ORD corpus reachable at all.
#
# **Without this the ORD half of the mock's data is permanently invisible, and nothing says so.**
# All ~10,000 ORD exports share one mtime — the moment the repo was cloned — and carry older
# payload timestamps. The incremental sync's cursor passes that instant on its first scheduled
# firing, and from then on no run can ever qualify them again. Chemclaw3 detects this exactly
# right and loudly (`ingest/eln/adapter.py::warn_late_arrivals` aggregates one WARNING naming the
# remedy); the gap was that this harness never took the remedy. The 2026-08-17 four-repo run
# therefore graded the whole `grounded` probe suite — whose header names ORD record ids, operators
# and counts — against a corpus holding **none** of it, while `/readyz` was green throughout.
#
# Runs the real `ElnSyncWorkflow` on the real broker from the epoch, via `cli/live_data`, and
# reports what arrived. Non-fatal: a bring-up that got every process up should not be torn down
# over an ingest, and the lane's own checks are where a bad corpus is supposed to go red.
backfill_corpus() {
  log "backfilling the seeded ELN/ORD corpus from the epoch (see cli/live_data)"
  # A short wait on purpose: this only has to *start* the drain. Every proposal costs a PR-gate
  # git branch and commit (~1.8 s/record measured), so the full corpus takes hours and a bring-up
  # must not block on it. The workflow keeps running on the broker; `make live-data` reads how far
  # it got and is the place a shortfall is supposed to show up.
  if (cd "$REPO_ROOT" && uv run python -m chemclaw.cli.live_data --backfill --corpus-only \
        --timeout 120 >"$LIVE_DIR/e2e-corpus-backfill.log" 2>&1); then
    log "corpus backfill: $(grep -m1 '^Backfill:' "$LIVE_DIR/e2e-corpus-backfill.log" || echo done)"
  else
    log "WARNING: corpus backfill failed — see $LIVE_DIR/e2e-corpus-backfill.log."
    log "         the ORD half of the corpus is unreachable until it succeeds; \`make live-data\` retries it"
  fi
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
# covers the processes this script owns (props, rxnpredict, chem, safety, calc, mock-hpc-eln,
# mock-vendor, ui-bff);
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
    chem) start_chem "$(mcp_python_bin)" ;;
    safety) start_safety "$(mcp_python_bin)" ;;
    calc) start_calc "$(mcp_python_bin)" ;;
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
