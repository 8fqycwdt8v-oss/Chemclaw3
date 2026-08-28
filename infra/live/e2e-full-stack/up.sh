#!/usr/bin/env bash
# Bring up the four-repo ChemClaw3 stack for a full end-to-end pass: this backend, the
# Chemclaw3-mcp tool fleet (props, rxnpredict, calc here; chem and safety via processes.sh),
# Chemclaw3_mock (the eln-json/eln-ord data sources, the mock-vendor MCP tool), and Chemclaw3_ui.
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
#
# **Two things this lane used to conflate, and they are unrelated.** It required a real Anthropic
# credential *and* all three sibling checkouts, in one `die` each, so a session holding neither
# could not run any of it — although the pieces that need neither are most of the lane. They are
# now separate knobs, because "which model" and "which checkouts" are separate questions:
#
#   * **Model.** `CHEMCLAW_LLM_PROVIDER=openai_compatible` (plus `CHEMCLAW_LLM_BASE_URL` and
#     `CHEMCLAW_LLM_MODEL`) runs the lane against `chemclaw.cli.mock_llm`, which
#     `infra/live/processes.sh` already starts on the exact base URL `http://127.0.0.1:8820/v1`.
#     No credential is involved. Anything else keeps the original requirement verbatim.
#   * **Chemclaw3_mock.** Absent, the lane still comes up — without `mock-eln`, `mock-vendor`, the
#     `eln-json`/`eln-ord` data sources and the corpus backfill. It says so by name on the way up,
#     because the alternative is a green bring-up over a corpus that is not there: the 2026-08-17
#     run graded the whole `grounded` suite against an empty ORD corpus while `/readyz` was green,
#     which is the same failure one step earlier.
#
# A degraded bring-up is never silent and never partial-by-accident: `CHEMCLAW_CONNECTORS_REQUIRED`
# stays `true` (processes.sh pins it), so a connector this lane *does* declare and cannot reach is
# still a hard startup failure.

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
# mock-eln's exec target unparseable. die() already had this right; log() did not.
log() { printf '\033[35m[e2e]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31m[e2e] %s\033[0m\n' "$*" >&2; exit 1; }

require_repo() {
  local path="$1" name="$2"
  [ -d "$path" ] || die "$name checkout not found at $path — set the env var or clone it there"
}

# Whether the lane is pointed at an OpenAI-compatible endpoint rather than at Anthropic.
#
# The same predicate `infra/live/processes.sh::llm_configured` uses for its half of the decision,
# written here as its own function because this lane asks a narrower question: not "is a model
# configured" but "is a model configured that needs no credential from us". Both readings of an
# unset `CHEMCLAW_LLM_PROVIDER` are the shipped default, `anthropic`.
mock_model_lane() { [ "${CHEMCLAW_LLM_PROVIDER:-anthropic}" = "openai_compatible" ]; }

# Resolve, and require, whatever the configured provider actually needs.
#
# Split from the single unconditional `die` this used to be. The Anthropic arm is unchanged, down
# to the message. The openai_compatible arm requires the two settings `core/config/llm.py`'s
# `_llm_provider_config` validator requires, and checks them *here* rather than letting the front
# door fail its own validation three subprocesses later — a bring-up that dies in `processes.sh`
# reports a uvicorn traceback, and this reports the two variable names.
resolve_model_credential() {
  if mock_model_lane; then
    [ -n "${CHEMCLAW_LLM_BASE_URL:-}" ] && [ -n "${CHEMCLAW_LLM_MODEL:-}" ] || die \
      "CHEMCLAW_LLM_PROVIDER=openai_compatible needs CHEMCLAW_LLM_BASE_URL and CHEMCLAW_LLM_MODEL.
For the scripted mock, the base URL must be exactly http://127.0.0.1:8820/v1 — processes.sh
string-compares it to decide whether to start chemclaw.cli.mock_llm, so a trailing slash or
'localhost' brings the front door up pointed at nothing."
    log "model: $CHEMCLAW_LLM_MODEL at $CHEMCLAW_LLM_BASE_URL (no credential needed)"
    return
  fi
  export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-$(printenv 'API-KEY' 2>/dev/null || true)}"
  [ -n "$ANTHROPIC_API_KEY" ] || die "no ANTHROPIC_API_KEY and no 'API-KEY' env var to map it from"
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
# `calc` is started here, and it is NOT a connector and its manifest must stay
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

# Which published manifests this script is responsible for starting.
#
# **Derived from the directory that is mounted, not written down.** `D-2026-08-17-a-harness-that-
# starts-two-of-five-servers-is-a-harness-that-tests-two` fixed the count of the day by adding the
# missing servers by name — and the fleet then grew `pyexec`, whose manifest is published, mounted
# on `CHEMCLAW_CONNECTORS_DIR` by the line above, and started by nobody. Measured: the front door
# refused to boot with `connectors_required is set but these connectors are unreachable: pyexec`.
# It failed closed, which is the posture working; it also means the lane could not come up at all
# until somebody edited a list. A list that must be edited whenever the other repository gains a
# server is a list that will be out of date again, so the answer is to stop keeping one.
#
# `chem` and `safety` are excluded because `infra/live/processes.sh` starts them and must
# (D-2026-08-27-one-lane-starts-the-fleet); everything else in `manifests/` is this script's.
# `calc` is not in this list at all and cannot be: it lives in `manifests-internal/`, is a backend
# rather than a connector, and has its own start below.
fleet_connectors_to_start() {
  local dir
  for dir in "$MCP_REPO"/manifests/*/; do
    [ -f "$dir/connector.yaml" ] || continue
    local name; name="$(basename "$dir")"
    case "$name" in chem|safety) continue ;; esac
    printf '%s\n' "$name"
  done
}

# The port a manifest declares, read from the manifest. Same shape as `processes.sh::fleet_port`,
# and deliberately the same source: the address the server binds and the address Chemclaw3 dials
# have to be one number, and a second copy of it here is how they would stop being.
fleet_port() {
  "$1" - "$MCP_REPO/manifests/$2/connector.yaml" <<'PY'
import re, sys
url = re.search(r"url:\s*(\S+)", open(sys.argv[1]).read()).group(1)
print(re.search(r":(\d+)/mcp", url).group(1))
PY
}

# Start one fleet connector by name, at the port and under the token its own manifest declares.
#
# No --app-dir: `python` is the shared workspace venv's interpreter, which already has every
# `chemclaw_mcp_<name>` on its path via uv's editable workspace install.
#
# `rxnpredict` is the one server needing more than the pattern, and it gets it here rather than in
# a function of its own: fake_a/fake_c give it a deterministic tool surface with no model weights
# and no checkpoint download — exactly what CI-shaped hardware wants (no GPU, no HuggingFace
# egress). See engine/base_doubles.py::register_requested for how those env vars reach the registry.
start_fleet_connector() {
  local python="$1" name="$2" port token var extra=()
  port="$(fleet_port "$python" "$name")" \
    || die "no port in $MCP_REPO/manifests/$name/connector.yaml"
  var="CHEMCLAW_$(printf '%s' "$name" | tr '[:lower:]' '[:upper:]')_TOKEN"
  token="${!var:-dev-token}"
  export "$var=$token"
  if [ "$name" = rxnpredict ]; then
    extra=(
      "CHEMCLAW_RXNPREDICT_ENABLED_FORWARD_MODELS=${CHEMCLAW_RXNPREDICT_ENABLED_FORWARD_MODELS:-fake_a}"
      "CHEMCLAW_RXNPREDICT_ENABLED_CONDITIONS_MODELS=${CHEMCLAW_RXNPREDICT_ENABLED_CONDITIONS_MODELS:-fake_c}"
    )
  fi
  start "$name" env "${extra[@]}" "$python" -m uvicorn "chemclaw_mcp_$name.app:app" \
    --host 127.0.0.1 --port "$port"
  wait_for "$name" "http://127.0.0.1:$port/healthz"
  assert_credential_accepted "$name" "http://127.0.0.1:$port/mcp" "$token"
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
  require_repo "$UI_REPO" "Chemclaw3_ui"
  # Chemclaw3_mock is the one checkout this lane can do without, so its absence is a posture rather
  # than an error — declared once, here, and read by every arm below instead of each re-testing the
  # directory. What it costs is named on the way up rather than left to be discovered as an empty
  # corpus or a connector that is simply not in the list.
  local mock_available=false
  if [ -d "$MOCK_REPO" ]; then
    mock_available=true
  else
    log "Chemclaw3_mock is not at $MOCK_REPO — running the three-repo posture. NOT RUN, and not"
    log "  skipped-green: mock-eln (:8090), mock-vendor (:8091, search_building_blocks/get_price),"
    log "  the eln-json and eln-ord data sources, the seeded ELN/ORD corpus and its backfill, and"
    log "  the Entra mock tenant. Any check over those measures nothing. Set CHEMCLAW_MOCK_REPO."
  fi
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
  resolve_model_credential
  # `$HARNESS_DIR/manifests` holds exactly one bundle — `mock-vendor`, which names
  # http://127.0.0.1:8091/mcp. With the mock checkout absent nothing serves that address, and
  # `CHEMCLAW_CONNECTORS_REQUIRED=true` (pinned by processes.sh, and deliberately not relaxed here)
  # makes an unreachable declared connector a hard startup failure of the front door. So the
  # manifest directory is dropped with the server rather than left declared and dead.
  export CHEMCLAW_CONNECTORS_DIR="$own_connectors:$MCP_REPO/manifests"
  if [ "$mock_available" = true ]; then
    export CHEMCLAW_CONNECTORS_DIR="$CHEMCLAW_CONNECTORS_DIR:$HARNESS_DIR/manifests"
    export CHEMCLAW_DATA_SOURCES="graph,eln-json,eln-ord"
    export CHEMCLAW_ELN_EXPORT_DIR="$MOCK_REPO/data/eln/exports"
    export CHEMCLAW_ORD_EXPORT_DIR="$MOCK_REPO/data/eln/exports/ord"
  else
    # `graph` alone. The two ELN adapters read their export directories off disk rather than over
    # HTTP, so leaving them enabled against a path that does not exist is not a lighter version of
    # having the corpus — it is a source that fails every sync.
    export CHEMCLAW_DATA_SOURCES="graph"
  fi
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

  # `chem` and `safety` come up inside processes.sh, which resolves the fleet checkout from this
  # same variable but defaults it differently (`$REPO_ROOT/../chemclaw3-mcp`). Exporting the value
  # this lane resolved is what makes one owner work from either lane's default.
  export CHEMCLAW_MCP_REPO="$MCP_REPO"

  local mcp_python; mcp_python="$(mcp_python_bin)"
  local fleet; fleet="$(fleet_connectors_to_start | tr '\n' ' ')"
  log "starting the Chemclaw3-mcp fleet: ${fleet}and calc (chem and safety via processes.sh)"
  local name
  for name in $fleet; do
    start_fleet_connector "$mcp_python" "$name"
  done
  start_calc "$mcp_python"

  if [ "$mock_available" = true ]; then
    log "starting Chemclaw3_mock (ELN mock + mock-vendor MCP tool)"
    local mock_python; mock_python="$(mock_venv_bin)"
    start_mock_eln "$mock_python"
    start_mock_vendor "$mock_python"
  fi

  log "starting this repo's connectors, chem, safety, workers and front door"
  bash "$REPO_ROOT/infra/live/processes.sh" up

  # The two halves of a connector token are still set in two places — the exports above give the
  # *front door* what it sends, processes.sh gives the *server* what it verifies — so the check
  # D-2026-08-17 left behind still has to run. It runs here rather than inside the start, because
  # the start is no longer this lane's and a check is not a start: `/healthz` is unauthenticated,
  # so without it a mismatch shows up only as a degraded turn with nothing naming a credential.
  assert_credential_accepted chem "http://127.0.0.1:8858/mcp" "$CHEMCLAW_CHEM_TOKEN"
  assert_credential_accepted safety "http://127.0.0.1:8859/mcp" "$CHEMCLAW_SAFETY_TOKEN"

  log "starting Chemclaw3_ui (BFF + SPA)"
  start_ui

  # The corpus is Chemclaw3_mock's; with no mock checkout there is nothing to drain, and a
  # backfill against a source that is not attached would report "0 records" as though the drain
  # had run and found nothing.
  if [ "$mock_available" = true ]; then
    backfill_corpus
  else
    log "skipping the corpus backfill — no Chemclaw3_mock checkout, so there is no ELN/ORD corpus"
  fi

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
  if (cd "$REPO_ROOT" && uv run python -m chemclaw.cli.live_data --backfill-only \
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
# covers the processes this script owns (props, rxnpredict, calc, mock-eln, mock-vendor, ui-bff);
# restarting a piece of this repo's own stack is infra/live/processes.sh's `restart` verb — and
# since D-2026-08-27-one-lane-starts-the-fleet that includes chem and safety. They get a named arm
# below rather than falling through to "unknown process", because they *are* known: they are
# simply somebody else's to restart.
restart() {
  local name="$1" pidfile="$RUN_DIR/$1.pid"
  case "$name" in
    chem|safety)
      die "$name is started by infra/live/processes.sh, which this lane calls — restart it there:
  bash infra/live/processes.sh restart $name"
      ;;
  esac
  [ -e "$pidfile" ] || die "no $pidfile — is '$name' up?"
  local pid; pid="$(cat "$pidfile")"
  kill -9 "$pid" 2>/dev/null || true
  for _ in $(seq 1 50); do kill -0 "$pid" 2>/dev/null || break; sleep 0.2; done
  rm -f "$pidfile"
  log "$name killed (pid $pid)"
  case "$name" in
    calc) start_calc "$(mcp_python_bin)" ;;
    mock-eln|mock-vendor)
      [ -d "$MOCK_REPO" ] || die "$name comes from Chemclaw3_mock, which is not at $MOCK_REPO.
This lane was brought up in its three-repo posture, so that process was never started. Set
CHEMCLAW_MOCK_REPO and re-run \`up\` rather than restarting into a stack that does not have it."
      case "$name" in
        mock-eln) start_mock_eln "$(mock_venv_bin)" ;;
        mock-vendor) start_mock_vendor "$(mock_venv_bin)" ;;
      esac
      ;;
    ui-bff) start_ui ;;
    *)
      # Any published fleet manifest this script started, by the same derivation `up` used — so a
      # server added to the other repository is restartable here the day it is startable.
      if fleet_connectors_to_start | grep -qx "$name"; then
        start_fleet_connector "$(mcp_python_bin)" "$name"
      else
        die "restart: unknown process '$name'"
      fi
      ;;
  esac
}

case "${1:-up}" in
  up) up ;;
  down) down ;;
  status) status ;;
  restart) [ $# -ge 2 ] || die "usage: up.sh restart <name>"; restart "$2" ;;
  *) die "usage: up.sh [up|down|status|restart <name>]" ;;
esac
