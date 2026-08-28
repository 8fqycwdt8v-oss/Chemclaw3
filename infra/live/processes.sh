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

# ---------------------------------------------------------------------------- enforced identity
#
# The lane can run the posture the chart ships — every request carrying a validated Entra token —
# by pointing it at an issuer. That used to be impossible offline and was recorded as gated on "a
# real Entra tenant", which was never what it needed: a tenant, to a resource server, is a JWKS
# document and an issuer string, and `Chemclaw3_mock`'s `app/entra/` is both (MOCK_ENTRA_ENABLED).
#
# Opt in with CHEMCLAW_ENTRA_REQUIRED=true and a token endpoint to mint from:
#
#   MOCK_ENTRA_ENABLED=true uvicorn app.main:app --port 8090        # in the mock checkout
#   CHEMCLAW_ENTRA_REQUIRED=true #   CHEMCLAW_LIVE_ENTRA_TOKEN_URL=http://127.0.0.1:8090/entra/mock-tenant/oauth2/v2.0/token #     make live-up
#
# The issuer and the JWKS URL are both derived below rather than one from the other, because that
# is how the front door reads them — `entra_jwks_endpoint` and `entra_issuer_url` resolve
# independently, so an issuer alone cannot find the keys.
readonly ENTRA_TOKEN_URL="${CHEMCLAW_LIVE_ENTRA_TOKEN_URL:-}"
readonly ENTRA_BASE="${ENTRA_TOKEN_URL%/oauth2/v2.0/token}"
if [ "$CHEMCLAW_ENTRA_REQUIRED" = "true" ]; then
  [ -n "$ENTRA_TOKEN_URL" ] || die "CHEMCLAW_ENTRA_REQUIRED=true needs CHEMCLAW_LIVE_ENTRA_TOKEN_URL"
  export CHEMCLAW_ENTRA_AUDIENCE="${CHEMCLAW_ENTRA_AUDIENCE:-api://chemclaw}"
  export CHEMCLAW_ENTRA_ISSUER="${CHEMCLAW_ENTRA_ISSUER:-$ENTRA_BASE/v2.0}"
  export CHEMCLAW_ENTRA_JWKS_URL="${CHEMCLAW_ENTRA_JWKS_URL:-$ENTRA_BASE/discovery/v2.0/keys}"
  # The roles the probe identity holds. Named rather than left empty because both authorization
  # gates fail *closed* on an empty privileged set — so an unset role here does not mean "no RBAC
  # in this lane", it means every expensive job and every write tool is refused and the probe run
  # measures a permissions error instead of the system.
  export CHEMCLAW_ENTRA_PRIVILEGED_ROLES="${CHEMCLAW_ENTRA_PRIVILEGED_ROLES:-process-chemist}"
  # `Settings` refuses `entra_required=true` while `harness_autonomy` still says `plan_only` and
  # `harness_enabled` is off: the approval-first posture would be named in one setting and attached
  # by neither. This lane is deliberately unsupervised — a probe run has no human to approve a plan
  # — so it states that, which is what the setting is for. With the harness off the value changes
  # no behaviour; it is the statement the refusal asks for, and it stays overridable so the lane can
  # also run the chart's own posture (`CHEMCLAW_HARNESS_ENABLED=true`).
  export CHEMCLAW_HARNESS_AUTONOMY="${CHEMCLAW_HARNESS_AUTONOMY:-execute}"
fi

# Mint the identity the probe runner presents, from the issuer the front door is validating
# against. Called after the mock is known to be up (a token is minted, not fetched at startup), and
# only in the enforced posture — an empty `live_probe_token` is how the dev posture is spelled.
mint_probe_token() {
  local oid="${CHEMCLAW_LIVE_PROBE_OID:-live-probe-runner}"
  local roles="${CHEMCLAW_ENTRA_PRIVILEGED_ROLES:-process-chemist}"
  curl -sf "$ENTRA_TOKEN_URL" -H 'content-type: application/json' \
    -d "{\"oid\":\"$oid\",\"upn\":\"$oid@live.test\",\"roles\":[\"$roles\"]}" \
    | "$1" -c 'import json,sys; print(json.load(sys.stdin)["access_token"])'
}
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

# The connector URLs *and* the per-connector `/mcp` credentials come from the dev runner itself
# rather than being rebuilt here from the same string patterns. One reader for one shape: if the
# runner changes its port, its mount path or which bundles carry a credential, this follows
# automatically instead of drifting.
#
# It must run **before** the connectors process starts, not after: `bo`, `calc`, `molfp` and `rxnfp`
# now declare `auth: mode: bearer`, so the serving process and core have to inherit the *same*
# minted tokens. Exporting them afterwards would leave the servers holding one secret and core
# presenting another, which surfaces as every tool call 401ing — a failure that reads as a broken
# connector rather than as a missing variable.
connector_env() {
  "$1" -m chemclaw.cli.connectors_dev --export-env
}

# The pid file must hold the pid of the *worker*, not of a wrapper around it. `uv run python -m …`
# would record `uv`, whose child is the real process — so `kill` would reach the launcher and a
# signal aimed at the worker (the wedged-worker check in `make live-jobs` sends SIGSTOP) would
# land on something that is not polling anything. Resolving the interpreter once and starting it
# directly removes the layer instead of working around it.
python_bin() { ( cd "$REPO_ROOT" && uv run python -c 'import sys; print(sys.executable)' ); }

# Whether *this lane* has a live process recorded under `name`.
#
# It answers a question about this lane's own bookkeeping and nothing else, which is exactly its
# limit: a pidfile is a per-lane record of a machine-wide resource. `start_fleet_bundles` says what
# that costs and asks the address itself instead.
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
  for _ in $(seq 1 "$attempts"); do
    # **Liveness first, and the order is the point.** A URL answering is evidence that *something*
    # serves that address — never that this process does. When a start loses a race for a bound
    # port the incumbent answers the poll, so with the checks the other way round the lane logs
    # "$name ready" over a process that died on its first line and records a pid nothing can
    # signal. Measured: two lanes both starting `chem` left `.live/run/chem.pid` pointing at a dead
    # pid while `wait_for` reported ready off the other lane's server, and `status` then said DOWN
    # about a capability that was serving fine
    # (D-2026-08-27-one-lane-starts-the-fleet).
    #
    # This also keeps the original reason the check exists: a process that crashed is reported as
    # crashed rather than waited out for the whole budget and then blamed on the timeout.
    if [ -e "$RUN_DIR/$name.pid" ] && ! running "$name"; then
      die "$name exited before becoming ready — see $LIVE_DIR/$name.log"
    fi
    # `-s` without `-S`: a poll that has not succeeded yet is not an error to report,
    # and printing one per second buries the line that says which process never came up.
    if curl -fs -o /dev/null --max-time 2 "$url"; then
      log "$name ready"
      return
    fi
    sleep 1
  done
  die "$name did not become ready at $url — see $LIVE_DIR/$name.log"
}

# ------------------------------------------------------------------ the fleet's two bundles
#
# **This script is the only thing that starts them.** The four-repo lane
# (`infra/live/e2e-full-stack/up.sh`) reaches them by calling this script, which it already does for
# the connectors, the workers and the front door; it deliberately no longer starts its own
# (D-2026-08-27-one-lane-starts-the-fleet).
#
# `chem` and `safety` are enabled bundles whose capability is `Chemclaw3-mcp`'s: they carry a
# manifest here and no `server/`, which is `D-2026-08-09-a-connector-we-do-not-run` working as
# designed. `cli/connectors_dev.py` therefore emits no URL for either — deliberately, since minting
# a token for a server we do not run would replace a clear `MissingConnectorCredential` with a 401
# from a server that never heard of it.
#
# The consequence was that this lane could not start at all. `CHEMCLAW_CONNECTORS_REQUIRED=true` is
# pinned below, both bundles keep their loopback defaults, and `check_connectors_at_startup` raises
# before the front door binds. **The pin is not the bug and must not be relaxed to fix this** —
# LIVE-8's lesson is that a configuration only production sets is a configuration nothing tests, and
# turning it off here would delete the test rather than pass it. So the lane starts the two servers
# it needs from the fleet checkout, and that is also why the ownership falls here rather than there:
# measured, with the two unreachable the front door does not degrade, it exits 3 with
# `ConnectorsUnavailable: chem (unreachable), safety (unreachable)` — so a `make live-up` that did
# not start them could not run a single one of the 259 probes in `data/evals/probes/`.
readonly MCP_REPO="${CHEMCLAW_MCP_REPO:-$REPO_ROOT/../chemclaw3-mcp}"

# Ports and package names come from the fleet's own manifests, which is the same "one reader for one
# shape" rule `connector_env` follows: a server that moves port there moves here without an edit.
fleet_python_bin() { ( cd "$MCP_REPO" && uv sync --quiet && uv run python -c 'import sys; print(sys.executable)' ); }

fleet_port() {
  "$1" - "$MCP_REPO/manifests/$2/connector.yaml" <<'PY'
import re, sys
url = re.search(r"url:\s*(\S+)", open(sys.argv[1]).read()).group(1)
print(re.search(r":(\d+)/mcp", url).group(1))
PY
}

start_fleet_bundles() {
  local python="$1"
  [ -d "$MCP_REPO" ] || die "chem and safety are served by Chemclaw3-mcp, which is not at $MCP_REPO.
Clone it beside this checkout, or set CHEMCLAW_MCP_REPO. Relaxing CHEMCLAW_CONNECTORS_REQUIRED is
not the fix: it is the posture the chart ships and the one this lane exists to exercise."

  local fleet_python
  fleet_python="$(fleet_python_bin)" || die "could not resolve an interpreter in $MCP_REPO"

  local name port
  for name in chem safety; do
    port="$(fleet_port "$python" "$name")" || die "no port in $MCP_REPO/manifests/$name/connector.yaml"
    # The same variable name on both sides, which is the manifest's `token_env` and the whole
    # reason a dev token works here: core reads it to send, the server reads it to verify.
    local var="CHEMCLAW_$(printf '%s' "$name" | tr '[:lower:]' '[:upper:]')_TOKEN"
    export "$var=${!var:-dev-token}"
    # A port is machine-wide; the guard in `start` is not. It reads this lane's pidfile, so a
    # server this lane did not launch — the four-repo lane started by hand, `make run-chem` in the
    # fleet checkout — is invisible to it, and the uvicorn launched here dies on the bound address.
    # `wait_for` no longer passes that off, so the failure would be loud either way; asking the
    # address itself is what lets it name the cause instead of pointing at a log.
    if ! running "$name" && curl -fs -o /dev/null --max-time 2 "http://127.0.0.1:$port/healthz"; then
      die "$name: 127.0.0.1:$port is already served, and not by a process this lane started.
This lane owns chem and safety; the four-repo lane reaches them by calling this script, so nothing
should be starting them twice. Stop the other server, or run \`make live-e2e-full-stack\`."
    fi
    ( cd "$MCP_REPO" && start "$name" "$fleet_python" -m "uvicorn" "chemclaw_mcp_$name.app:app" \
        --host 127.0.0.1 --port "$port" )
    wait_for "$name" "http://127.0.0.1:$port/healthz"
    CHEMCLAW_CONNECTOR_URLS="$("$python" - "$CHEMCLAW_CONNECTOR_URLS" "$name" "$port" <<'PY'
import json, sys
urls = json.loads(sys.argv[1] or "{}")
urls[sys.argv[2]] = f"http://127.0.0.1:{sys.argv[3]}/mcp"
print(json.dumps(urls))
PY
)"
  done
  export CHEMCLAW_CONNECTOR_URLS
}


up() {
  mkdir -p "$RUN_DIR"
  command -v uv >/dev/null 2>&1 || die "uv not found"
  pg_isready -h 127.0.0.1 -p "${CHEMCLAW_LIVE_PGPORT:-5432}" >/dev/null 2>&1 \
    || die "postgres is not up — run infra/live/bootstrap.sh first"

  local python
  python="$(python_bin)"
  cd "$REPO_ROOT"

  # Addresses and credentials first, so every process below inherits both (see `connector_env`).
  #
  # Captured before `eval`, not inside it: command substitution inside `eval` discards the exit
  # status, so a runner that died — a bad setting, an unreadable manifest — evaluated to nothing
  # and the lane continued to fail twenty lines later on an unbound variable, naming the variable
  # instead of the error. Measured while adding this: a `llm_model` validation error surfaced as
  # `CHEMCLAW_CONNECTOR_URLS: unbound variable`.
  local connector_exports
  connector_exports="$(connector_env "$python")" \
    || die "could not resolve the connector addresses and credentials — see the error above"
  eval "$connector_exports"
  # **Persisted, because the credentials are minted rather than derived.** The URL map is a pure
  # function of the manifests, so a later shell could always recompute it; a token is not, and a
  # second `--export-env` in another terminal mints *different* ones. That leaves the running
  # servers holding one secret and a later `make live-jobs` presenting another, which surfaces as
  # 401s from a connector that is plainly up — a genuinely confusing failure, and one this file
  # introduced the moment those bundles started requiring a credential.
  #
  # 0600 and under the run dir, which `.gitignore` already covers. `processes.sh env` prints it.
  #
  # **Written after `start_fleet_bundles`, not here.** The paragraph above is the reason: a token is
  # minted, not derived. `start_fleet_bundles` mints `CHEMCLAW_CHEM_TOKEN`/`CHEMCLAW_SAFETY_TOKEN`
  # and rewrites `CHEMCLAW_CONNECTOR_URLS` with the fleet's two addresses, so persisting at this
  # point captures the map *before* those exist and hands a second shell exactly the failure this
  # comment warns about — 401s from a server that is plainly up. The file is written once, below,
  # when every address and every credential is known.
  log "connector urls: $CHEMCLAW_CONNECTOR_URLS"

  # The probe identity, in the enforced posture only. Minted before the front door starts so the
  # export is inherited, and named in the log by *identity* rather than by value — a token in a
  # lane's stdout is a token in whatever collects that lane's stdout.
  if [ "$CHEMCLAW_ENTRA_REQUIRED" = "true" ]; then
    CHEMCLAW_LIVE_PROBE_TOKEN="$(mint_probe_token "$python")" \
      || die "could not mint a probe token from $ENTRA_TOKEN_URL — is the mock tenant running with MOCK_ENTRA_ENABLED=true?"
    export CHEMCLAW_LIVE_PROBE_TOKEN
    # Persisted with the connector credentials below, for the same reason: `make live-probes` run
    # from another terminal would otherwise present nothing and 401 before a probe starts.
    log "identity enforced: issuer $CHEMCLAW_ENTRA_ISSUER, probe identity ${CHEMCLAW_LIVE_PROBE_OID:-live-probe-runner}"
  fi

  # `chem` and `safety` come from the fleet checkout, and they come up *before* the front door for
  # the reason `connectors_required=true` exists: an unreachable enabled bundle is a boot failure,
  # not a degraded turn.
  start_fleet_bundles "$python"
  log "connector urls (with the fleet): $CHEMCLAW_CONNECTOR_URLS"

  # Now every address and credential is known, so the file a second shell reads can be complete.
  # `connector_env`'s own exports, then the fleet's two tokens and the URL map it rewrote.
  ( umask 077
    printf '%s\n' "$connector_exports"
    printf 'export CHEMCLAW_CONNECTOR_URLS=%q\n' "$CHEMCLAW_CONNECTOR_URLS"
    printf 'export CHEMCLAW_CHEM_TOKEN=%q\n' "$CHEMCLAW_CHEM_TOKEN"
    printf 'export CHEMCLAW_SAFETY_TOKEN=%q\n' "$CHEMCLAW_SAFETY_TOKEN"
    # The fleet checkout this invocation *resolved*, not the one a second shell would default to.
    #
    # `env` is documented above as the contract a second shell reads, and it carried only
    # credentials — but `restart` re-runs `start_fleet_bundles`, which needs `MCP_REPO`, and a
    # second shell that never set `CHEMCLAW_MCP_REPO` falls back to `$REPO_ROOT/../chemclaw3-mcp`.
    # Measured: the storm's family A restarts the front door at every admission cap through this
    # very verb, and the whole run died at the first one with "chem and safety are served by
    # Chemclaw3-mcp, which is not at /home/user/Chemclaw3/../chemclaw3-mcp" — from a shell that had
    # sourced `env` exactly as the runbook says to. A checkout path is the same kind of thing as a
    # minted token: something this invocation settled that nobody downstream can re-derive.
    printf 'export CHEMCLAW_MCP_REPO=%q\n' "$MCP_REPO"
    # The model posture this lane came up under, for the same reason and with a sharper cost.
    #
    # `llm_configured` decides whether `up` starts the front door at all, and `restart` *is* `up`.
    # So a second shell that sourced this file and ran `processes.sh restart api` — which the
    # storm's family A does at every admission cap — killed the front door, found neither
    # `ANTHROPIC_API_KEY` nor `openai_compatible`, skipped starting it, printed "live stack up",
    # and **exited 0**. Measured exactly that way: `api killed (pid 12668)`, then
    # `skipping the front door`, then `live stack up`, then `/readyz` refused the connection.
    # Every turn measured after that point would have been measured against nothing.
    #
    # Only what is set is written, and only when it is set: an empty `CHEMCLAW_LLM_MODEL` exported
    # into a second shell would fail `_llm_provider_config`'s validator rather than fall back.
    local key
    for key in CHEMCLAW_LLM_PROVIDER CHEMCLAW_LLM_BASE_URL CHEMCLAW_LLM_MODEL; do
      [ -n "${!key:-}" ] && printf 'export %s=%q\n' "$key" "${!key}"
    done
  ) > "$RUN_DIR/connector-env.sh"
  if [ "${CHEMCLAW_LIVE_PROBE_TOKEN:-}" != "" ]; then
    ( umask 077; printf 'export CHEMCLAW_LIVE_PROBE_TOKEN=%q\n' "$CHEMCLAW_LIVE_PROBE_TOKEN" \
      >> "$RUN_DIR/connector-env.sh" )
  fi

  # The connectors themselves: the front door refuses to report ready without them under
  # `connectors_required=true`, and the workers call them through the same URLs.
  start connectors "$python" -m chemclaw.cli.connectors_dev
  wait_for connectors "http://127.0.0.1:8810/openapi.json"

  # Workers next, so a job launched by the first turn has somewhere to run.
  start_worker worker-background 9000 "$python" -m chemclaw.durable.background_worker
  start_worker worker-calc 9001 "$python" -m chemclaw.connectors.calc.worker
  start_worker worker-bo 9002 "$python" -m chemclaw.connectors.bo.worker
  # The `results` bundle owns a job (`republish_calculations`) and therefore a queue, and the
  # chart renders it a worker Deployment like the other three. It was missing here, so the one
  # thing this lane exists for — running the deployed shape — did not include it and a job
  # launched against that queue would have sat unpolled. Inert in practice until
  # `CHEMCLAW_RESULT_SINKS` names a sink, which is a reason it went unnoticed rather than a
  # reason to leave it out.
  start_worker worker-results 9004 "$python" -m chemclaw.connectors.results.worker

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

  for worker in worker-background worker-calc worker-bo worker-results; do
    wait_for "$worker" "http://127.0.0.1:$(cat "$RUN_DIR/$worker.port")/readyz"
  done
  if llm_configured; then
    wait_for api "http://127.0.0.1:$API_PORT/readyz"
    log "live stack up. front door: http://127.0.0.1:$API_PORT · logs: $LIVE_DIR"
    log "  from another terminal, first: eval \"\$(bash infra/live/processes.sh env)\""
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
  # The credentials belong to the processes that just stopped. Left behind, `processes.sh env`
  # would hand a later shell tokens for servers that are gone — a stale secret is a slower version
  # of the mismatch this file exists to prevent, not a milder one.
  rm -f "$RUN_DIR/connector-env.sh"
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
  # `up` is conditional in places — it skips the front door with no model configured, and skips the
  # mock with no base URL pointing at it — and it reports those skips and exits 0. That is right
  # for `up`, which is being asked to bring up whatever this configuration describes. It is wrong
  # for `restart`, which was asked about one named process: a restart that ends with that process
  # not running is a failure however reasonable the reason, and saying so is the difference between
  # a chaos check that disturbed something and one that quietly removed it.
  #
  # This is the same rule `_chaos_postgres_bounce` now applies from the other side: the actor
  # verifies the act, and the observer verifies it independently.
  [ -e "$pidfile" ] || die "restart $name: the lane came back up without it.
Read the lines above for the reason it was skipped — with no model configured, \`up\` deliberately
does not start the front door. Whatever it says, this invocation asked for $name specifically and
$name is not running, so this is a failure rather than a stack that is up in some other shape."
  log "$name restarted (pid $(cat "$pidfile"))"
}

# Print the exports a *later* shell needs to talk to the running lane, so a tool started by hand
# presents the credentials the running connectors actually hold:
#
#   eval "$(bash infra/live/processes.sh env)" && make live-jobs
#
# `up` writes the file; this only reads it back. Without it every command run outside the shell
# that started the lane mints its own tokens and gets 401s from healthy servers.
print_env() {
  local file="$RUN_DIR/connector-env.sh"
  [ -r "$file" ] || die "no $file — is the lane up? (run: make live-up)"
  cat "$file"
}

case "${1:-up}" in
  up) up ;;
  down) down ;;
  status) status ;;
  env) print_env ;;
  restart) [ $# -ge 2 ] || die "usage: processes.sh restart <name>"; restart "$2" ;;
  *) die "usage: processes.sh [up|down|status|env|restart <name>]" ;;
esac
