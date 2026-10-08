#!/usr/bin/env bash
# Bring up the four-repo ChemClaw3 stack for a full end-to-end pass: this backend, the
# Chemclaw3-mcp tool fleet (every fleet bundle the front door binds, and calc and rxnlabel, via
# processes.sh), Chemclaw3_mock (the eln-json/eln-ord data sources, the mock-vendor MCP tool),
# and Chemclaw3_ui.
#
# Deliberately does not reimplement readiness polling for pieces that already have it:
# `infra/live/bootstrap.sh` brings up Postgres/Temporal and the note writer's repo, and
# `infra/live/processes.sh` brings up this repo's own connectors, Temporal workers and front
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

# One resolution of the sibling checkouts, shared with `../processes.sh`. See its header for why
# this is a file rather than a default in each script.
# shellcheck source=infra/live/siblings.sh
. "$HARNESS_DIR/../siblings.sh"
readonly MCP_REPO="$(sibling_repo CHEMCLAW_MCP_REPO Chemclaw3-mcp)"
readonly MOCK_REPO="$(sibling_repo CHEMCLAW_MOCK_REPO Chemclaw3_mock)"
readonly UI_REPO="$(sibling_repo CHEMCLAW_UI_REPO Chemclaw3_ui)"

# stderr, not stdout: `mock_venv_bin()` returns a path via stdout command substitution, and a
# log() that shared stdout corrupted it with ANSI-coded log text — the exact bug that made
# mock-eln's exec target unparseable. die() already had this right; log() did not.
log() { printf '\033[35m[e2e]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31m[e2e] %s\033[0m\n' "$*" >&2; exit 1; }

require_repo() {
  local path="$1" name="$2"
  [ -d "$path" ] || die "$name checkout not found at $path — set the env var or clone it there"
}

# ---------------------------------------------------------------------------- process helpers
# Same shape as infra/live/processes.sh's start/wait_for: no subshell around the launch (the
# recorded pid must be the real process, not a wrapper), readiness asked rather than assumed.

# Whether *this lane* has a live process recorded under `name` — this lane's own bookkeeping, and
# nothing about whether the address that process wants is free.
running() {
  local pidfile="$RUN_DIR/$1.pid"
  [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null
}

start() {
  local name="$1"; shift
  local pidfile="$RUN_DIR/$name.pid"
  if running "$name"; then
    log "$name already running (pid $(cat "$pidfile"))"
    return
  fi
  nohup "$@" >"$LIVE_DIR/e2e-$name.log" 2>&1 &
  echo $! >"$pidfile"
  log "$name started (pid $(cat "$pidfile"))"
}

wait_for() {
  local name="$1" url="$2" attempts="${3:-120}"
  for _ in $(seq 1 "$attempts"); do
    # Liveness first — a URL answering says something serves the address, never that this process
    # does, and with the checks the other way round a start that lost a race for a bound port was
    # reported ready off the incumbent. See the same comment in `infra/live/processes.sh` and
    # D-2026-08-27-one-lane-starts-the-fleet.
    if [ -e "$RUN_DIR/$name.pid" ] && ! running "$name"; then
      die "$name exited before becoming ready — see $LIVE_DIR/e2e-$name.log"
    fi
    if curl -fs -o /dev/null --max-time 2 "$url"; then
      log "$name ready"
      return
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

# `assert_credential_accepted` over every fleet bundle `processes.sh` started, **derived, never
# listed**. When `start_props` moved to `processes.sh` its check went with it and the list here kept
# only `chem` and `safety`, so `props`, `rxnpredict` and every later bundle went unchecked while the
# README said they were. The set is `processes.sh::fleet_bundle_names`' own test, read back from
# what it persisted: the URL map's keys are core's endpoint-declaring bundles, and a fleet manifest
# for the same name is what makes one the fleet's. The token is the variable `start_fleet_bundles`
# exports — same name, same `dev-token` default — which is the half the front door sends.
check_fleet_bundle_credentials() {
  local python="$1" env_file="$LIVE_DIR/run/connector-env.sh"
  [ -f "$env_file" ] || die "processes.sh returned without writing $env_file"
  local pairs name url var checked=0
  # Assigned rather than iterated, so a failure in either step stops the lane (see
  # `start_fleet_bundles` for what `for x in $(cmd)` does to a traceback under `set -e`).
  pairs="$( # shellcheck source=/dev/null
    . "$env_file" && "$python" -c 'import json, sys
for name, url in sorted(json.loads(sys.argv[1] or "{}").items()):
    print(name, url)' "${CHEMCLAW_CONNECTOR_URLS:-}")" \
    || die "could not read the connector URL map from $env_file"
  while read -r name url; do
    [ -n "$name" ] && [ -f "$MCP_REPO/manifests/$name/connector.yaml" ] || continue
    var="CHEMCLAW_$(printf '%s' "$name" | tr '[:lower:]' '[:upper:]')_TOKEN"
    assert_credential_accepted "$name" "$url" "${!var:-dev-token}"
    checked=$((checked + 1))
  done <<< "$pairs"
  [ "$checked" -gt 0 ] || die "no fleet bundle in $env_file's URL map to check a credential against"
}

# ---------------------------------------------------------------------------- Chemclaw3-mcp
# The servers this harness runs share one uv workspace at the repo root, so one resolved
# interpreter serves them all (same reasoning as processes.sh's python_bin()).
#
# **`chem` and `safety` are started by `infra/live/processes.sh`, not here**
# (D-2026-08-27-one-lane-starts-the-fleet). Chemclaw3 *dials* them — both bundles declare
# `http://127.0.0.1:885{8,9}/mcp`, and under `CHEMCLAW_CONNECTORS_REQUIRED=true` an unreachable one
# is a hard startup failure of the front door, not a degraded connector — so the script that starts
# the front door is the script that has to start them, and it does. This one started them too, and
# because a pidfile is a per-lane record of a machine-wide port, the two starts did not collide
# loudly: the second uvicorn died on the bound address while the readiness poll was answered by the
# first, leaving `processes.sh status` reporting DOWN over servers that were serving. What stays
# here is the *check* — `assert_credential_accepted` below, after processes.sh returns — because
# that is this lane's own lesson (D-2026-08-17) and a check is not a start.
#
# `calc` is started by `processes.sh`, not here, and it is NOT a connector and its manifest must stay
# off `CHEMCLAW_CONNECTORS_DIR` — it says so in a box. Chemclaw3 keeps its own `calc` bundle and
# its whole tool surface; what moved to the fleet is the *physics* behind them
# (D-2026-08-16-the-physics-leaves-the-cache-stays), which `connectors/calc/remote.py::calc_session`
# dials on a cache miss at `calc_server_url` (8860). "Not a connector" is not "not needed": with
# this server down, `/readyz` is entirely green — it probes connectors, and this is not one — and
# every calculator tool fails at call time with `CalcServerError: the calculation service is not
# answering`. That is how `predict_pka` failed on this harness's first real turn.
#
# This tree declares no `chem` or `safety` manifest: the fleet owns both, and a name found in two
# directories on `CHEMCLAW_CONNECTORS_DIR` is a startup error. What stays here for `safety` is
# `skills/safety-screening/SKILL.md` (architecture layer 3), found by directory name beside the
# fleet's manifest. This lane puts the fleet *checkout's* `manifests/` on the path, not the
# installed `chemclaw-contracts` package, so the servers it starts and the manifests it reads are
# one revision.

mcp_python_bin() { ( cd "$MCP_REPO" && uv sync --quiet && uv run python -c 'import sys; print(sys.executable)' ); }

# ---------------------------------------------------------------------------- Chemclaw3_mock
# Its own venv (start.sh/start-mcp.sh hard-code `.venv/bin/python`), created once, idempotently.

mock_venv_bin() {
  if [ ! -x "$MOCK_REPO/.venv/bin/python" ]; then
    log "creating Chemclaw3_mock's venv"
    ( cd "$MOCK_REPO" && python3 -m venv .venv && .venv/bin/pip install --quiet -e '.[dev]' )
  fi
  echo "$MOCK_REPO/.venv/bin/python"
}

start_mock_eln() {
  local python="$1"
  # bash -c ... exec, not a bare invocation: app/eln's real-dataset loader reads its CSVs by a
  # path relative to cwd (the same reason start.sh itself does `cd "$SCRIPT_DIR"` first), and
  # `exec` replaces the shell in place so the pid `start()` records is still the real process.
  MOCK_ELN_EXPORT_DIR="$MOCK_REPO/data/eln/exports" \
    MOCK_ORD_EXPORT_DIR="$MOCK_REPO/data/eln/exports/ord" \
    MOCK_ELN_SEED_ON_STARTUP=true \
    start mock-eln bash -c \
      "cd '$MOCK_REPO' && exec '$python' -m uvicorn app.main:app --host 0.0.0.0 --port 8090"
  wait_for mock-eln "http://127.0.0.1:8090/healthz"
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

# Whether the UI's installed tree is behind what its manifests ask for.
#
# `node_modules` existing is not that question: it survives every `git pull` that adds or bumps a
# dependency, so the lane skipped the install and the BFF died at import on a package the lockfile
# named and nothing had installed. npm writes `node_modules/.package-lock.json` as the record of
# what it last installed, so a lockfile (or `package.json`) newer than that record is the signal —
# and a missing record means no complete install ever finished.
ui_dependencies_stale() {
  local installed="$UI_REPO/node_modules/.package-lock.json"
  [ -f "$installed" ] || return 0
  [ "$UI_REPO/package-lock.json" -nt "$installed" ] || [ "$UI_REPO/package.json" -nt "$installed" ]
}

# `npm ci` when there is a lockfile — it installs exactly what the lockfile says and never rewrites
# it, which is the sibling checkout's file and not this lane's to edit — and `npm install` only
# when there is none to be exact about.
install_ui_dependencies() {
  log "installing Chemclaw3_ui dependencies"
  if [ -f "$UI_REPO/package-lock.json" ]; then
    ( cd "$UI_REPO" && npm ci --silent )
  else
    ( cd "$UI_REPO" && npm install --silent )
  fi
}

start_ui() {
  if ui_dependencies_stale; then install_ui_dependencies; fi
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

# ---------------------------------------------------------------------------- lane environment

# Every variable `up` composes for the backend, written where `processes.sh` reads it back.
#
# `processes.sh restart <name>` is the primitive the storm's chaos family uses and the command the
# end of `up` tells an operator to run — and it runs in a fresh shell holding none of the exports
# above. So a restarted front door came back without the fleet and harness manifest directories
# (no `mock-vendor`) and without the ELN/ORD sources. `processes.sh` sources this
# file at start with the caller's own environment winning (`source_unset_only`), and its `down`
# deletes it with the lane.
#
# The names are the ones `up` exports, in one list; `tests/test_live_lane_scripts.py` fails if
# `up` exports a `CHEMCLAW_*` variable that is in neither this list nor `LANE_ENV_PER_INVOCATION`.
# An unset one is skipped rather than written empty, which would override the reader's own. 0600:
# it carries the connector tokens.
readonly LANE_ENV_VARS=(
  CHEMCLAW_CONNECTORS_DIR CHEMCLAW_CONNECTORS_ENABLED
  CHEMCLAW_DATA_SOURCES CHEMCLAW_ELN_EXPORT_DIR CHEMCLAW_ORD_EXPORT_DIR
  CHEMCLAW_PROPS_TOKEN CHEMCLAW_RXNPREDICT_TOKEN CHEMCLAW_CHEM_TOKEN CHEMCLAW_SAFETY_TOKEN
  CHEMCLAW_CALC_TOKEN CHEMCLAW_PYEXEC_TOKEN
  CHEMCLAW_MCP_REPO
)
# **What is deliberately not persisted: the model gateway, and above all its key.** These were in
# the list above, so a restart "back to the mock" — `processes.sh restart api` from a shell naming
# no gateway — came back on the paid one, because the reader filled in what the caller had not
# set; and `CHEMCLAW_LLM_API_KEY` sat in a file on disk, under a Keychain-only setup that exists so
# it never does. Which gateway a process dials, and the credential that pays for it, is decided by
# the shell that starts it, every time: name it in the restarting shell to keep it, name nothing to
# get the mock. `processes.sh` also refuses to read these names back from an older file.
readonly LANE_ENV_PER_INVOCATION=(
  CHEMCLAW_LLM_BASE_URL CHEMCLAW_LLM_MODEL CHEMCLAW_LLM_API_KEY
)
persist_lane_env() {
  local file="$LIVE_DIR/run/lane-env.sh" var
  mkdir -p "$LIVE_DIR/run"
  # The umask is set *around* the redirection, not inside the command it redirects: in
  # `( umask 077; … ) >file` the file is opened before the body runs, so it is created 0644.
  ( umask 077
    for var in "${LANE_ENV_VARS[@]}"; do
      if [ -n "${!var:-}" ]; then printf 'export %s=%q\n' "$var" "${!var}"; fi
    done >"$file"
  )
  log "lane environment persisted to $file"
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
  # **The model destination, and why this no longer dies without a vendor key.** Every model call
  # goes to the one OpenAI-compatible gateway `CHEMCLAW_LLM_BASE_URL` names
  # (`D-2026-09-04-a-gateway-is-the-only-provider`); nothing in `src/` dials a vendor directly, so
  # a bare `ANTHROPIC_API_KEY` is no longer a credential this stack can use. Name a gateway and
  # this maps the environment's own key onto it; name nothing and the lane runs against
  # `chemclaw.cli.mock_llm`, which `infra/live/processes.sh` starts on that default address.
  #
  # The cost is stated rather than hidden: without a gateway in front of a real vendor, this lane
  # exercises every hop — front door, middleware chain, budget, audit, session store, connectors,
  # Temporal — against scripted completions rather than real ones. `make live-probes` is the run
  # that needs a real model behind the address.
  if [ -n "${CHEMCLAW_LLM_BASE_URL:-}" ]; then
    export CHEMCLAW_LLM_API_KEY="${CHEMCLAW_LLM_API_KEY:-$(printenv 'API-KEY' 2>/dev/null || true)}"
    # The refusal first: the log line below would otherwise print `model: unset` and the next
    # statement would kill the run *because* it is unset — a diagnostic that only ever appears
    # beside the fatal error explaining it.
    [ -n "${CHEMCLAW_LLM_MODEL:-}" ] || die "CHEMCLAW_LLM_BASE_URL is set but CHEMCLAW_LLM_MODEL is not"
    log "model gateway: $CHEMCLAW_LLM_BASE_URL (model: $CHEMCLAW_LLM_MODEL)"
  else
    log "no CHEMCLAW_LLM_BASE_URL — running against the local mock LLM on 127.0.0.1:8820."
    log "  set CHEMCLAW_LLM_BASE_URL + CHEMCLAW_LLM_MODEL to drive a real gateway instead."
  fi
  export CHEMCLAW_CONNECTORS_DIR="$own_connectors:$MCP_REPO/manifests:$HARNESS_DIR/manifests"
  export CHEMCLAW_DATA_SOURCES="graph,eln-json,eln-ord"
  export CHEMCLAW_ELN_EXPORT_DIR="$MOCK_REPO/data/eln/exports"
  export CHEMCLAW_ORD_EXPORT_DIR="$MOCK_REPO/data/eln/exports/ord"
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
  export CHEMCLAW_PYEXEC_TOKEN="${CHEMCLAW_PYEXEC_TOKEN:-dev-token}"

  # **Every bundle this lane can reach is bound, the five opt-in ones included.** `props`,
  # `kinetics`, `suitability`, `thermalsafety` and `unitops` declare `default_enabled: false`, so
  # with no enable-list the front door binds none of them — while `processes.sh` used to start all
  # five, so the full-stack lane ran five servers nothing called and read as having tested them.
  # This is the full-stack test, so it pays for them: the list is every bundle discovered on the
  # directory above, derived from the registry rather than written here, and an explicit list
  # overrides `default_enabled` (`registry.enabled`). `processes.sh` starts exactly the fleet
  # bundles the front door binds, so the two now agree by construction. The prefix this costs is
  # what `tests/test_context_floor.FLEET_PUBLISHED_ALLOWANCE` prices. Overridable, like the rest.
  local every_bundle
  every_bundle="$(cd "$REPO_ROOT" && uv run python -c 'import os
from chemclaw.connectors.registry import discovered
print(os.pathsep.join(sorted(discovered())))')" \
    || die "could not list the bundles discovered on $CHEMCLAW_CONNECTORS_DIR"
  export CHEMCLAW_CONNECTORS_ENABLED="${CHEMCLAW_CONNECTORS_ENABLED:-$every_bundle}"

  log "connectors dir: $CHEMCLAW_CONNECTORS_DIR"
  log "connectors enabled: $CHEMCLAW_CONNECTORS_ENABLED"

  # `chem` and `safety` come up inside processes.sh, which resolves the fleet checkout through the
  # same `sibling_repo` and therefore reaches the same answer. Exported anyway, so the child lane
  # is pinned to the path *this* one resolved rather than resolving a second time — one search, one
  # answer, whatever the two shells were started with.
  export CHEMCLAW_MCP_REPO="$MCP_REPO"

  # Persisted for every later `processes.sh` invocation — see `persist_lane_env`.
  persist_lane_env

  # **No fleet server is started here.** Every one the front door binds is
  # `processes.sh::start_fleet_bundles`', whose set is `fleet_bundle_names` (the endpoint-declaring
  # bundles the registry discovers that the fleet checkout also publishes). That set is deliberately
  # not listed here: an enumeration of it in this comment went stale twice, and each time the lane
  # started a server processes.sh also owned. The two scripts keep pidfiles in different run dirs
  # (`.live/e2e/run` here, `.live/run` there), so `running <name>` there is false while the port is
  # served and its collision guard kills the lane. One owner per server.
  log "starting the Chemclaw3-mcp fleet (calc, rxnlabel and the fleet bundles, via processes.sh)"
  local mcp_python; mcp_python="$(mcp_python_bin)"

  log "starting Chemclaw3_mock (ELN mock + mock-vendor MCP tool)"
  local mock_python; mock_python="$(mock_venv_bin)"
  start_mock_eln "$mock_python"
  start_mock_vendor "$mock_python"

  log "starting this repo's connectors, chem, safety, workers and front door"
  bash "$REPO_ROOT/infra/live/processes.sh" up

  # The two halves of a connector token are still set in two places — the exports above give the
  # *front door* what it sends, processes.sh gives the *server* what it verifies — so the check
  # D-2026-08-17 left behind still has to run. It runs here rather than inside the start, because
  # the start is no longer this lane's and a check is not a start: `/healthz` is unauthenticated,
  # so without it a mismatch shows up only as a degraded turn with nothing naming a credential.
  check_fleet_bundle_credentials "$mcp_python"
  # The calc backend is checked on exactly the same terms even though it is not a connector: the
  # credential has the same two halves, and a mismatch here is worse than a degraded turn — it is a
  # `CalcServerError` from a server whose `/healthz` is green, which is how this lane once
  # misdiagnosed a 401 all the way into Temporal.
  assert_credential_accepted calc "http://127.0.0.1:8860/mcp" "${CHEMCLAW_CALC_TOKEN:-dev-token}"
  # The reaction labeller, on the same terms and for the same reason: a backend rather than a
  # connector, dialled by the background worker's label drain, and a mismatch there surfaces only as
  # a drain that never labels anything — the state #520 found, with nothing naming a credential.
  assert_credential_accepted rxnlabel "http://127.0.0.1:8865/mcp" \
    "${CHEMCLAW_RXNLABEL_TOKEN:-dev-token}"

  log "starting Chemclaw3_ui (BFF + SPA)"
  start_ui

  backfill_corpus
  index_corpus

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
  # A short wait on purpose: this only has to *start* the drain. Every note costs a git commit
  # and a push (~1.8 s/record measured), so the full corpus takes hours and a bring-up
  # must not block on it. The workflow keeps running on the broker; `make live-data` reads how far
  # it got and is the place a shortfall is supposed to show up.
  if (cd "$REPO_ROOT" && uv run python -m chemclaw.cli.live_data --backfill-only \
        --timeout 120 >"$LIVE_DIR/e2e-corpus-backfill.log" 2>&1); then
    log "corpus backfill: $(grep -m1 '^Backfill:' "$LIVE_DIR/e2e-corpus-backfill.log" || echo done)"
  else
    log "WARNING: corpus backfill failed — see $LIVE_DIR/e2e-corpus-backfill.log."
    log "         the ORD half of the corpus is unreachable until it succeeds; \`make live-data\` retries it"
  fi
}

# Bring the corpus's derived reaction indexes current, as a deployment keeps them (#520).
#
# The backfill above lands records, their record-phase labels and their fingerprints. Two things a
# deployment does on top of that never happened here, and the structure-search tools said so on every
# run: no label drain ever ran (a deployment's `reaction-labels` Schedule, against the `rxnlabel`
# backend `processes.sh` now starts), so `substrate_precedent` reported 0 of 4,282 reactions
# labelled; and rows a previous lane wrote under an older fingerprint definition stayed in the
# database across `down`/`up`, so `similar_reactions` reported a partial index. `cli/live_index`
# runs the label drain, the operator's re-key and — when the re-key rebuilt every shelved row — the
# disposal of the superseded generation. See its module docstring.
#
# **Bounded, idempotent, skippable.** `CHEMCLAW_LIVE_INDEX_TIMEOUT` (seconds, default 600) bounds
# the whole step; the drain keeps running on the broker past it and a re-run rejoins it. Re-running
# `up` re-runs this and converges, so a bring-up that ran out of time is finished by the next one.
# `CHEMCLAW_LIVE_SKIP_INDEX=true` skips it, for a quick `up` that does not need precedent or
# similarity answers. Non-fatal, like the backfill: a bring-up that got every process up is not torn
# down over an index, and the tools' own `coverage` and `index_partial` say what state it is in.
#
# `CHEMCLAW_NOTE_REPO_DIR` is passed rather than exported: the re-key writes a compound note's
# successor through the note writer when a standardization bump moved its id, and that has to be the
# lane's own clone (`processes.sh` sets the same default for the processes it starts) — `up` exports
# no variable it does not persist (`LANE_ENV_VARS`), and this one is `processes.sh`'s to own.
index_corpus() {
  case "${CHEMCLAW_LIVE_SKIP_INDEX:-false}" in
    true|1|yes)
      log "index step skipped (CHEMCLAW_LIVE_SKIP_INDEX) — labels and fingerprint generations are as the database left them"
      return
      ;;
    false|0|no|"") ;;
    *) die "CHEMCLAW_LIVE_SKIP_INDEX must be true or false, got '$CHEMCLAW_LIVE_SKIP_INDEX'" ;;
  esac
  local timeout="${CHEMCLAW_LIVE_INDEX_TIMEOUT:-600}"
  [[ "$timeout" =~ ^[1-9][0-9]*$ ]] \
    || die "CHEMCLAW_LIVE_INDEX_TIMEOUT must be a positive integer (seconds), got '$timeout'"
  log "bringing the reaction indexes current: label drain + fingerprint re-key (≤ ${timeout}s; see cli/live_index)"
  local report="$LIVE_DIR/e2e-corpus-index.log"
  local status=0 line
  (cd "$REPO_ROOT" && CHEMCLAW_NOTE_REPO_DIR="${CHEMCLAW_NOTE_REPO_DIR:-$LIVE_DIR/knowledge-repo}" \
    uv run python -m chemclaw.cli.live_index --timeout "$timeout" >"$report" 2>&1) || status=$?
  # The step's own summary lines, whichever way it went: a failure in one index says nothing about
  # the other, and the line that names which one is the useful one. `|| true` because a report with
  # no summary line (an import error) is not a reason for `set -e` to end the bring-up.
  while IFS= read -r line; do log "  $line"; done < <(grep -E '^(Labels|Fingerprints):' "$report" || true)
  if [ "$status" -ne 0 ]; then
    log "WARNING: the index step reported a failure (exit $status) — see $report."
    log "         structure-search answers may still be degraded; re-running \`up\` retries it"
  fi
}

down() {
  # The precondition first: this announced "stopping Chemclaw3_ui" and *then* returned "nothing
  # running", so the one line an operator reads named work the next line declined to do.
  [ -d "$RUN_DIR" ] || { log "nothing running"; return; }
  log "stopping Chemclaw3_ui"
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
# covers the processes this script owns (mock-eln, mock-vendor, ui-bff);
# restarting a piece of this repo's own stack is infra/live/processes.sh's `restart` verb — and
# since D-2026-08-27-one-lane-starts-the-fleet that includes chem and safety, and since
# D-2026-08-28-the-durable-half-has-a-backend-too the calc and rxnlabel backends as well, and every other fleet
# bundle `processes.sh::fleet_bundle_names` derives (props, rxnpredict, …). They get a named arm
# below rather than falling through to "unknown process", because they *are* known: they are
# simply somebody else's to restart.
restart() {
  local name="$1" pidfile="$RUN_DIR/$1.pid"
  # The fleet bundles are derived rather than listed: a hand-kept `chem|safety|calc` arm is how
  # `restart props` came to die on a pidfile this lane stopped writing once processes.sh took props
  # over. A bundle the fleet publishes is processes.sh's, the same test `fleet_bundle_names` makes.
  if [ "$name" = calc ] || [ "$name" = rxnlabel ] \
      || [ -e "$MCP_REPO/manifests/$name/connector.yaml" ]; then
    die "$name is started by infra/live/processes.sh, which this lane calls — restart it there:
  bash infra/live/processes.sh restart $name"
  fi
  [ -e "$pidfile" ] || die "no $pidfile — is '$name' up?"
  local pid; pid="$(cat "$pidfile")"
  kill -9 "$pid" 2>/dev/null || true
  for _ in $(seq 1 50); do kill -0 "$pid" 2>/dev/null || break; sleep 0.2; done
  rm -f "$pidfile"
  log "$name killed (pid $pid)"
  case "$name" in
    mock-eln) start_mock_eln "$(mock_venv_bin)" ;;
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
