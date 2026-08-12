#!/usr/bin/env bash
# Start (and stop) the five processes a live ChemClaw3 is made of, ready-checked rather than slept.
#
# README lines 44-51 have always documented these by hand, and every recorded live pass started
# them by hand — which is precisely why every one of them ran with some subset missing. The last
# one (`docs/archive/live-grounded-2026-08-03.md`) reached 36 probes with **no Temporal worker at
# all**, so the entire durable half of the system was untested while the run read as a live run.
# A script that starts the whole set, or fails saying which one did not come up, is the difference.
#
# Readiness is asked, never assumed. `/readyz` is polled until the front door reports its
# connectors, and each Temporal worker is confirmed by polling its own health endpoint — the
# workers serve one (`durable/serve.py::serve_worker` mounts `worker_http(...)` with
# `ready=lambda: worker.is_running`), which is a far better signal than "the process has not exited
# yet". The compose Temporal has no healthcheck either, which is why `make up` returning has never
# meant 7233 accepts connections.
#
# Each worker gets its own `CHEMCLAW_WORKER_METRICS_PORT`. `worker_http` documents 0 (disable the
# surface) as the way to run more than one worker on one machine, since they would otherwise
# contend for 9000 — but disabling it trades the readiness signal away, and this lane needs it
# more than a laptop does. Distinct ports keep every worker probeable, which is also how
# `make live-jobs` can tell a stopped worker from a slow one.
#
# Usage: processes.sh [up|down|status]

set -euo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly LIVE_DIR="${CHEMCLAW_LIVE_DIR:-$REPO_ROOT/.live}"
readonly RUN_DIR="$LIVE_DIR/run"
readonly API_PORT="${CHEMCLAW_LIVE_API_PORT:-8000}"

log() { printf '\033[36m[live]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[live] %s\033[0m\n' "$*" >&2; exit 1; }

# The lane's environment, in one place. Every key already exists; nothing here is new config.
#
# `service_host` is not cosmetic: `api/middleware.py::_refuse_unauthenticated_exposure` (SEC-2)
# refuses to boot on a non-loopback bind while `entra_required=false`, and the default is 0.0.0.0 —
# so a live lane that did not pin loopback would simply fail to start, correctly.
#
# `session_store=postgres` and `connectors_required=true` are pinned because they are what the Helm
# chart ships, and LIVE-8's lesson is exactly that: a configuration only production sets is a
# configuration nothing tests.
export CHEMCLAW_SERVICE_HOST="${CHEMCLAW_SERVICE_HOST:-127.0.0.1}"
export CHEMCLAW_ENTRA_REQUIRED="${CHEMCLAW_ENTRA_REQUIRED:-false}"
export CHEMCLAW_SESSION_STORE="${CHEMCLAW_SESSION_STORE:-postgres}"
export CHEMCLAW_CONNECTORS_REQUIRED="${CHEMCLAW_CONNECTORS_REQUIRED:-true}"
# Traces, when something is listening for them. `make phoenix-up` puts an OTLP receiver on 4317;
# with nothing there the exporter retries in the background and the run is unaffected, which is why
# this is a probe rather than a flag somebody has to remember. Content stays suppressed:
# `CHEMCLAW_OTEL_INCLUDE_SENSITIVE_DATA` is left alone, so spans carry token counts, model names and
# durations and not a word the chemist typed. A lane that wants the prompts sets it deliberately.
if (exec 3<>/dev/tcp/127.0.0.1/4317) 2>/dev/null; then
  exec 3>&- 3<&-
  export CHEMCLAW_OTEL_ENABLED="${CHEMCLAW_OTEL_ENABLED:-true}"
  export CHEMCLAW_OTEL_LLM_SPANS="${CHEMCLAW_OTEL_LLM_SPANS:-true}"
  export CHEMCLAW_OTEL_ENDPOINT="${CHEMCLAW_OTEL_ENDPOINT:-http://127.0.0.1:4317}"
fi
# The PR-gate's dedicated clone, created by bootstrap.sh. Without it `note_repo_dir`
# defaults to "." — this checkout — and every note submission is refused before a git
# command runs, which silently removes the whole knowledge-contribution half of a run.
export CHEMCLAW_NOTE_REPO_DIR="${CHEMCLAW_NOTE_REPO_DIR:-$LIVE_DIR/knowledge-repo}"

# The connector URLs come from `connectors_dev.build_composite()` itself rather than being
# rebuilt here from the same string pattern. One reader for one shape: if the dev runner ever
# changes its port or its mount path, this follows automatically instead of drifting.
connector_urls() {
  "$1" -c '
import json
from chemclaw.cli.connectors_dev import build_composite
print(json.dumps(build_composite()[1], separators=(",", ":")))'
}

# The pid file must hold the pid of the *worker*, not of a wrapper around it. `uv run python -m …`
# would record `uv`, whose child is the real process — so `kill` would reach the launcher and a
# signal aimed at the worker (the wedged-worker check in `make live-jobs` sends SIGSTOP) would
# land on something that is not polling anything. Resolving the interpreter once and starting it
# directly removes the layer instead of working around it.
python_bin() { ( cd "$REPO_ROOT" && uv run python -c 'import sys; print(sys.executable)' ); }

start() {
  local name="$1"; shift
  local pidfile="$RUN_DIR/$name.pid"
  if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    log "$name already running (pid $(cat "$pidfile"))"
    return
  fi
  # No subshell around the launch: with `( … & echo $! )` the recorded pid is the forked subshell,
  # which is exactly the off-by-one that made the signal above miss. `up()` has already cd'd.
  nohup "$@" >"$LIVE_DIR/$name.log" 2>&1 &
  echo $! >"$pidfile"
  log "$name started (pid $(cat "$pidfile"))"
}

# A worker plus the probe port it answers on, recorded so `status` and `make live-jobs` can find it.
start_worker() {
  local name="$1" port="$2"; shift 2
  echo "$port" >"$RUN_DIR/$name.port"
  CHEMCLAW_WORKER_METRICS_PORT="$port" start "$name" "$@"
}

# Whether a model can be reached at all. Asked once, in the same terms `agent/llm_provider.py`
# branches on, so the two cannot disagree about what "configured" means.
llm_configured() {
  [ -n "${ANTHROPIC_API_KEY:-}" ] || [ "${CHEMCLAW_LLM_PROVIDER:-anthropic}" = "openai_compatible" ]
}

# Poll a URL until it answers 200, or fail naming the log to read. Never a bare sleep: a fixed
# wait is either too short (a flaky lane) or too long (a slow one), and it reports nothing.
#
# The budget is 300s, not 90. Measured: on a *cold* page cache — a fresh container, or the first
# start after one is reclaimed — importing this dependency set (torch, rdkit, bofire) pages in
# ~1 GB and the process sits in uninterruptible disk sleep for minutes. At 90s the lane declared
# a healthy process dead and killed the run; the second start, with the cache warm, took ten
# seconds. A readiness budget has to cover the slowest legitimate start, not the typical one.
#
# A process that has genuinely died is not made slower to detect by this: the liveness check below
# fails within a second of the pid going away, so only real waiting waits.
wait_for() {
  local name="$1" url="$2" attempts="${3:-300}"
  local pidfile="$RUN_DIR/$name.pid"
  for _ in $(seq 1 "$attempts"); do
    # `-s` without `-S`: a poll that has not succeeded yet is not an error to report,
    # and printing one per second buries the line that says which process never came up.
    if curl -fs -o /dev/null --max-time 2 "$url"; then
      log "$name ready"
      return
    fi
    # Exited processes are reported as exited rather than waited out. Without this the lane spends
    # the whole budget on a process that crashed on its first line, and then blames the timeout.
    if [ -f "$pidfile" ] && ! kill -0 "$(cat "$pidfile")" 2>/dev/null; then
      die "$name exited before becoming ready — see $LIVE_DIR/$name.log"
    fi
    sleep 1
  done
  die "$name did not become ready at $url — see $LIVE_DIR/$name.log"
}

up() {
  mkdir -p "$RUN_DIR"
  command -v uv >/dev/null 2>&1 || die "uv not found"
  pg_isready -h 127.0.0.1 -p "${CHEMCLAW_LIVE_PGPORT:-5432}" >/dev/null 2>&1 \
    || die "postgres is not up — run infra/live/bootstrap.sh first"

  local python
  python="$(python_bin)"
  cd "$REPO_ROOT"

  # The connectors first: the front door refuses to report ready without them under
  # `connectors_required=true`, and the workers call them through the same URLs.
  start connectors "$python" -m chemclaw.cli.connectors_dev
  wait_for connectors "http://127.0.0.1:8810/openapi.json"

  local urls
  urls="$(connector_urls "$python")"
  export CHEMCLAW_CONNECTOR_URLS="$urls"
  log "connector urls: $urls"

  # Workers next, so a job launched by the first turn has somewhere to run.
  start_worker worker-background 9000 "$python" -m chemclaw.durable.background_worker
  start_worker worker-calc 9001 "$python" -m chemclaw.connectors.calc.worker
  start_worker worker-bo 9002 "$python" -m chemclaw.connectors.bo.worker
  start_worker worker-qm 9003 "$python" -m chemclaw.connectors.qm.worker

  # The mock model, when the lane is pointed at it. Started before the front door because the front
  # door builds a chat client at startup and would come up pointed at nothing.
  #
  # It is an *HTTP* mock rather than an injected `BaseChatClient` deliberately: the streaming
  # assembler, the middleware stack, budget admission, the audit sink and the session store all sit
  # between the socket and the agent, and the in-process scripted client in `tests/` bypasses every
  # one of them — its own docstring records passing green while production failed 100% of the time.
  if [ "${CHEMCLAW_LLM_BASE_URL:-}" = "http://127.0.0.1:8820/v1" ]; then
    start mock-llm "$python" -m chemclaw.cli.mock_llm
    wait_for mock-llm "http://127.0.0.1:8820/__mock/stats"
  fi

  # The front door builds the agent — and therefore a chat client — during startup, so with no
  # model credential it does not fail at the first turn, it fails to boot at all
  # (`agent/llm_provider.py::_anthropic_client` raises on a missing key). That is correct
  # behaviour, and it is also why the durable half of the lane is deliberately independent of it:
  # `make live-jobs` drives Temporal and Postgres with no model in the loop, so it runs here.
  if llm_configured; then
    start api "$python" -m uvicorn chemclaw.api.app:create_app --factory \
      --host 127.0.0.1 --port "$API_PORT"
  else
    # Clear any pid file from an earlier run that *did* start it, so `status` reports the
    # front door as absent rather than as a process that died.
    rm -f "$RUN_DIR/api.pid"
    log "no ANTHROPIC_API_KEY and no openai_compatible endpoint — skipping the front door."
    log "  'make live-jobs' (Temporal + Postgres) runs without it; 'make live-probes' needs it."
  fi

  for worker in worker-background worker-calc worker-bo worker-qm; do
    wait_for "$worker" "http://127.0.0.1:$(cat "$RUN_DIR/$worker.port")/readyz"
  done
  if llm_configured; then
    wait_for api "http://127.0.0.1:$API_PORT/readyz"
    log "live stack up. front door: http://127.0.0.1:$API_PORT · logs: $LIVE_DIR"
  else
    log "live stack up (durable half only) · logs: $LIVE_DIR"
  fi
}

down() {
  [ -d "$RUN_DIR" ] || { log "nothing running"; return; }
  for pidfile in "$RUN_DIR"/*.pid; do
    [ -e "$pidfile" ] || continue
    local name pid
    name="$(basename "$pidfile" .pid)"
    pid="$(cat "$pidfile")"
    if kill -0 "$pid" 2>/dev/null; then
      # SIGTERM, not SIGKILL: `serve_worker` installs a handler that drains in-flight activities,
      # and killing a worker outright is the one thing this lane exists to *test*, not to do by
      # default (`make live-jobs` does it deliberately, in one case, and restarts it).
      kill "$pid" 2>/dev/null || true
      log "$name stopped (pid $pid)"
    fi
    rm -f "$pidfile" "$RUN_DIR/$name.port"
  done
}

status() {
  [ -d "$RUN_DIR" ] || { log "nothing running"; return; }
  # A process that was deliberately skipped leaves no pid file, so it would simply be absent from
  # the listing below — and "absent" reads the same as "never existed". The front door is the one
  # that gets skipped on purpose, so it says so rather than going quiet.
  if [ ! -e "$RUN_DIR/api.pid" ] && ! llm_configured; then
    printf '  %-20s not started (no model credential — see `make live-up` output)\n' "api"
  fi
  for pidfile in "$RUN_DIR"/*.pid; do
    [ -e "$pidfile" ] || continue
    local name pid
    name="$(basename "$pidfile" .pid)"
    pid="$(cat "$pidfile")"
    if kill -0 "$pid" 2>/dev/null; then printf '  %-20s up   (pid %s)\n' "$name" "$pid"
    else printf '  %-20s DOWN\n' "$name"; fi
  done
}

# Stop one named process and bring the stack back to full — the shape every chaos check needs.
#
# `up` is already idempotent (it skips what is running and ready-checks what it starts), so
# "restart X" is "stop X, then up" and nothing else. Written as a verb rather than left to the
# caller because the caller is a test: a harness that stopped a process and then started a
# *replacement* by hand would be measuring recovery of something the lane never runs.
restart() {
  local name="$1" pidfile
  pidfile="$RUN_DIR/$name.pid"
  [ -e "$pidfile" ] || die "no $pidfile — is the lane up?"
  local pid
  pid="$(cat "$pidfile")"
  # SIGKILL, not SIGTERM: a restart check that let the process drain first would be testing a
  # graceful shutdown, and the failure worth knowing about is the ungraceful one.
  kill -9 "$pid" 2>/dev/null || true
  # Wait for the pid to actually go, so `up` does not see a still-live process and skip the start.
  for _ in $(seq 1 50); do kill -0 "$pid" 2>/dev/null || break; sleep 0.2; done
  rm -f "$pidfile"
  log "$name killed (pid $pid)"
  up
}

case "${1:-up}" in
  up) up ;;
  down) down ;;
  status) status ;;
  restart) [ $# -ge 2 ] || die "usage: processes.sh restart <name>"; restart "$2" ;;
  *) die "usage: processes.sh [up|down|status|restart <name>]" ;;
esac
