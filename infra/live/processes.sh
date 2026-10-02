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

# One resolution of the sibling checkouts, shared with `e2e-full-stack/up.sh`. See its header.
# shellcheck source=infra/live/siblings.sh
. "$(dirname "${BASH_SOURCE[0]}")/siblings.sh"

log() { printf '\033[36m[live]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[live] %s\033[0m\n' "$*" >&2; exit 1; }

# Source a file of `export NAME=value` lines, keeping every NAME the caller already set.
#
# The run dir holds two of these, and both exist because a *later* invocation of this script must
# come up in the environment the running lane was started in rather than in whatever shell ran it:
#
#   * `lane-env.sh` — what a wrapping lane composed before calling `up` (the four-repo lane's
#     connector directories, data sources, sibling tokens and model gateway; `e2e-full-stack/up.sh`
#     writes it). Without it `restart api` from the storm, or from the line `up.sh` prints, brought
#     the front door back with none of that: no mock-vendor, no pyexec, no ELN/ORD sources.
#   * `connector-env.sh` — the credentials `connectors_dev --export-env` *mints* (see `up`). Not
#     reloaded, a second `up` minted new ones while the connectors process kept the old, and every
#     tool call from the restarted front door 401'd against a server that was plainly up.
#
# Caller wins, so an operator can still override any one of them for one invocation, and a line
# that is not an export is ignored rather than executed.
#
# **The model gateway is never read back**, whatever the file holds. Which gateway a process dials
# — and the credential that pays for it — is the invoking shell's decision, every time: a lane
# persisted `CHEMCLAW_LLM_BASE_URL`, `_MODEL` and `_API_KEY` here, so a restart meant to bring the
# front door back on the mock came back on the paid gateway (billing as it did), and the key sat in
# a file on disk. `e2e-full-stack/up.sh` no longer writes them; this skip is what keeps a file an
# older one wrote from doing the same. Name the gateway in the restarting shell to keep it.
source_unset_only() {
  local file="$1" line name
  [ -r "$file" ] || return 0
  while IFS= read -r line; do
    case "$line" in export\ [A-Za-z_]*=*) ;; *) continue ;; esac
    name="${line#export }"
    name="${name%%=*}"
    case "$name" in CHEMCLAW_LLM_*) continue ;; esac
    [ -n "${!name+x}" ] || eval "$line"
  done <"$file"
}
source_unset_only "$RUN_DIR/lane-env.sh"

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
# This lane's model gateway is `chemclaw.cli.mock_llm` on loopback, started below, and every
# process that makes model calls now refuses to boot on a loopback `llm_base_url` unless the
# posture is *stated* (`core/llm_gateway.refuse_unconfigured_llm_gateway`). Stated here, once, for
# the whole lane: the front door, the background worker and the mcp face all read it (`make chat`
# is its own lane and exports its own), and a lane
# pointed at a real gateway (`CHEMCLAW_LLM_BASE_URL` set by `e2e-full-stack/up.sh`) is unaffected
# either way because the guard only looks at loopback addresses.
export CHEMCLAW_LLM_ALLOW_LOOPBACK_GATEWAY="${CHEMCLAW_LLM_ALLOW_LOOPBACK_GATEWAY:-true}"
# The eval profile directory beside the shipped one, because every control arm this repository
# measures against is a profile — `no-tools.yaml`, which swaps the system prompt as well as the
# tools, for the merged tool-utility A/B, and `tools-removed.yaml` for the arm that varies only the
# tools — and a profile has to be registered by the process
# that builds the agent. It is not in `data/profiles/` on purpose — a toolless agent is a
# measurement instrument, and every deployment that starts the front door advertises what is in
# there. This lane is where measurements run, so this is where the two directories meet.
export CHEMCLAW_PROFILES_DIR="${CHEMCLAW_PROFILES_DIR:-data/profiles:data/evals/profiles}"
export CHEMCLAW_ENTRA_REQUIRED="${CHEMCLAW_ENTRA_REQUIRED:-false}"
# The lane's Temporal workers bind no request surface, so the loopback bind above is not
# their exemption: with sign-in off a worker refuses to boot unless the posture is *stated*
# (`durable/serve.refuse_unauthenticated_worker`). Stated here, once, for every worker this lane
# starts — and inert when the lane runs `CHEMCLAW_ENTRA_REQUIRED=true`, because the guard only
# looks at an unauthenticated worker.
export CHEMCLAW_WORKER_ALLOW_UNAUTHENTICATED="${CHEMCLAW_WORKER_ALLOW_UNAUTHENTICATED:-true}"

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
fi
# **The lane is unsupervised in every posture, so it says so in every posture.** This export used
# to sit inside the enforced-identity branch above, where it was written to satisfy `Settings`'
# refusal of `entra_required=true` beside an unattached `plan_only`. Since D-2026-09-13 the code
# default is the harness *on* and `plan_only`, so the dev posture — the one `make live-up` and the
# four-repo lane run — inherited the approval-first gate with no human to approve anything: every
# durable job a turn launched was refused with `PlanNotApprovedError`, and the storm's family D
# recorded 0 `job_records` rows across 12 turns. A probe or storm run has nobody to approve a plan,
# so the lane states `execute` here, for both postures.
#
# Overridable like every default in this block. `make live-plan-gate` is the one suite that needs
# the gate: start the lane with `CHEMCLAW_HARNESS_AUTONOMY=plan_only` for it.
export CHEMCLAW_HARNESS_AUTONOMY="${CHEMCLAW_HARNESS_AUTONOMY:-execute}"

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
# **This lane's bundles are derived, never listed.** `start_fleet_bundles` iterates
# `fleet_bundle_names`, the intersection of core's endpoint-declaring bundles and the manifests the
# fleet actually ships, so a bundle added to either side is picked up without editing this file.
# That matters because `CHEMCLAW_CONNECTORS_REQUIRED` is true (set above): with `connectors_enabled`
# unset, discovery is enablement, so a bundle core enables and nothing starts is not a warning —
# it is a front door that refuses to boot. `rxnpredict` did exactly that when it was wired in
# against a hardcoded pair, and the fix is to start what is enabled rather than to narrow what is
# enabled: the core-served bundles (`bo`, `calc`, `molfp`, `rxnfp`, `results`) come from the dev
# connector process, so constraining `CHEMCLAW_CONNECTORS_ENABLED` here would take those off the
# lane too.
#
# **What the loop cannot derive is a server's own configuration, and `rxnpredict` needs some.** A
# fleet checkout carries none of its ML extras, so with the server's `*` default every predictor
# reports `not_installed`, `/healthz` answers 200 and the tool surface is empty: `wait_for` passes
# and every forward or conditions call fails. The `fake_a`/`fake_c` deterministic doubles are the
# fleet's answer to exactly that (`engine/base_doubles.py::register_requested` — a working surface,
# no weights, no checkpoint download). `e2e-full-stack/up.sh` used to set them in its own
# `start_rxnpredict`; when this loop took `rxnpredict` over, the defaults were deleted with the
# call and the lane went on starting an empty server. They are set here, once, so every lane that
# reaches the loop gets them; a lane pointed at real weights overrides both.
export CHEMCLAW_RXNPREDICT_ENABLED_FORWARD_MODELS="${CHEMCLAW_RXNPREDICT_ENABLED_FORWARD_MODELS:-fake_a}"
export CHEMCLAW_RXNPREDICT_ENABLED_CONDITIONS_MODELS="${CHEMCLAW_RXNPREDICT_ENABLED_CONDITIONS_MODELS:-fake_c}"
# **A double has to say it is one, in the result the model reads.** The doubles return the same
# fixed products for every input, and on 2026-10-02 a real model on this lane told the chemist "the
# forward reaction prediction confirms" a product `fake_a` returns for anything. Naming the connector
# in `CHEMCLAW_CONNECTOR_STAND_INS` makes core put its own stand-in notice on every result it
# returns (`agent/tool_framing.py::stand_in_notice`). Keyed on the doubles actually being selected,
# so a lane pointed at real weights reads its predictions as predictions.
case "$CHEMCLAW_RXNPREDICT_ENABLED_FORWARD_MODELS,$CHEMCLAW_RXNPREDICT_ENABLED_CONDITIONS_MODELS" in
  *fake_*) export CHEMCLAW_CONNECTOR_STAND_INS="${CHEMCLAW_CONNECTOR_STAND_INS:-rxnpredict}" ;;
esac

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
# The note writer's dedicated clone, created by bootstrap.sh. Without it `note_repo_dir`
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

# Every variable an enabled connector's manifest names as its bearer token — asked of the registry
# that already answers this question for the log redactor and the webhook redactor, rather than
# listed a third time.
#
# The list *was* written out (`CHEMCLAW_CHEM_TOKEN`, `..._SAFETY_...`, `..._CALC_...`), directly
# below the paragraph explaining that this file exists so a second shell does not get "401s from a
# connector that is plainly up" — and it dropped `rxnpredict`, the same bundle whose absence from a
# hardcoded list already broke this lane once (see `fleet_bundle_names`). The env file therefore
# handed a second shell three credentials out of four, and every `make live-jobs`/`live-probes`
# call that reached rxnpredict raised `MissingConnectorCredential` against a server this lane had
# started and was serving. Derived, a bundle added next year is persisted the day it is enabled.
bearer_token_vars() {
  "$1" -c 'from chemclaw.connectors.registry import bearer_token_env_names
print("\n".join(bearer_token_env_names()))'
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

# Poll a URL until it answers 200, or fail naming the log to read. Never a bare sleep: a fixed
# wait is either too short (a flaky lane) or too long (a slow one), and it reports nothing.
#
# The budget is 300 *attempts*, not 90 — and an attempt is a `curl --max-time 2` plus a one-second
# sleep, so a lane whose polls all time out waits up to ~15 minutes. This paragraph said "300s" and
# the number is load-bearing prose, so an operator was told five minutes for a hang that can run
# three times that. Measured: on a *cold* page cache — a fresh container, or the first
# start after one is reclaimed — importing this dependency set (torch, rdkit, bofire) pages in
# ~1 GB and the process sits in uninterruptible disk sleep for minutes. At 90 the lane declared
# a healthy process dead and killed the run; the second start, with the cache warm, took ten
# seconds. A readiness budget has to cover the slowest legitimate start, not the typical one.
#
# A process that has genuinely died is not made slower to detect by this: the liveness check below
# fails within a second of the pid going away, so only real waiting waits.
#
# **And it is a setting, `CHEMCLAW_LIVE_READY_ATTEMPTS`, because 300 was measured too short too.**
# On a loaded host `worker-bo` — torch and bofire on the import path, started beside three other
# workers doing the same — exceeded it and the lane killed a process that was still importing. The
# slowest legitimate start is a property of the machine, not of this script, so the machine's
# operator is the one who can move it.
readonly READY_ATTEMPTS="${CHEMCLAW_LIVE_READY_ATTEMPTS:-300}"
[[ "$READY_ATTEMPTS" =~ ^[1-9][0-9]*$ ]] \
  || die "CHEMCLAW_LIVE_READY_ATTEMPTS must be a positive integer, got '$READY_ATTEMPTS'"
wait_for() {
  local name="$1" url="$2" attempts="${3:-$READY_ATTEMPTS}"
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
readonly MCP_REPO="$(sibling_repo CHEMCLAW_MCP_REPO Chemclaw3-mcp)"

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

# Resolve the fleet checkout and its interpreter once, for every server this lane takes from it.
# One call, because `fleet_python_bin` runs `uv sync` and because two resolutions are two chances
# to disagree about which interpreter the fleet's servers run on.
fleet_checkout_python() {
  [ -d "$MCP_REPO" ] || die "chem, safety and the calc and rxnlabel backends are served by Chemclaw3-mcp, which is not at $MCP_REPO.
Clone it beside this checkout, or set CHEMCLAW_MCP_REPO. Relaxing CHEMCLAW_CONNECTORS_REQUIRED is
not the fix: it is the posture the chart ships and the one this lane exists to exercise."
  fleet_python_bin || die "could not resolve an interpreter in $MCP_REPO"
}

# Which bundles this lane must take from the fleet, derived rather than listed.
#
# It *was* listed — `for name in chem safety` — and the list went stale the day a third endpoint
# bundle was added here: `rxnpredict` was discovered, enabled and therefore required, no process
# served it, and `make live-up` brought up eleven processes and then failed the front door on a
# connector nobody had noticed was missing. A hardcoded list is a second declaration of a set that
# already has two authorities, which is the failure this repository names in `manifests/`.
#
# So the set is the intersection of the two: a bundle whose manifest *here* declares an
# `endpoint:` (so the front door will dial it) and which the fleet publishes a manifest for (so
# this lane knows its port and its module). `bo`, `calc`, `molfp` and `rxnfp` declare an endpoint
# too and are absent from the fleet's `manifests/`, which is exactly right — they are served by
# this repository's own `connectors_dev` process, and the loop below rewrites their URLs.
#
# **And the front door has to bind it**, asked of `registry.enabled()` — the function the front
# door itself asks. Without this third term the lane started `props`, `kinetics`, `suitability`,
# `thermalsafety` and `unitops`, all `default_enabled: false`, under an empty enable-list that
# binds none of them: five servers up, healthy and never called, reading as tested. A lane that
# wants them names them in `CHEMCLAW_CONNECTORS_ENABLED` (the four-repo lane does, for all of
# them), and then they are started *and* bound.
fleet_bundle_names() {
  "$1" - "$REPO_ROOT" "$MCP_REPO" <<'PY'
import pathlib, sys, yaml

from chemclaw.connectors.registry import enabled

repo, fleet = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
bound = {manifest.name for manifest in enabled()}
for manifest in sorted((repo / "src/chemclaw/connectors").glob("*/connector.yaml")):
    name = manifest.parent.name
    if not (yaml.safe_load(manifest.read_text()) or {}).get("endpoint"):
        continue
    if name in bound and (fleet / "manifests" / name / "connector.yaml").exists():
        print(name)
PY
}

start_fleet_bundles() {
  local python="$1" fleet_python="$2"

  # Derived from the fleet, never a list here — see the note beside CONNECTORS_REQUIRED.
  #
  # Assigned first, then iterated. `for name in $(cmd)` does not propagate a non-zero exit under
  # `set -e` — a traceback inside the derivation would print to stderr and the loop would then run
  # over whatever partial output preceded it, starting some bundles and silently skipping others.
  # The failure would surface minutes later as the front door's `ConnectorsUnavailable`, naming a
  # connector nobody had noticed was missing, which is exactly the defect this derivation replaced.
  # An assignment *does* propagate, so `|| die` here is what makes the guard real - the same shape
  # `fleet_port` below already uses.
  local names name port
  names="$(fleet_bundle_names "$python")" || die "could not derive the fleet bundle list from \
$REPO_ROOT/src/chemclaw/connectors and $MCP_REPO/manifests"
  # An empty derivation is a wrong checkout, not a lane with nothing to start. `fleet_checkout_python`
  # already refuses a missing `$MCP_REPO`; this catches one that exists and publishes no manifest
  # this repository declares an endpoint for - which would otherwise start no fleet server at all and
  # fail the front door minutes later on connectors it never mentions.
  [ -n "$names" ] || die "no fleet bundle is served by both sides: this checkout declares endpoint \
bundles that $MCP_REPO/manifests does not publish. Is CHEMCLAW_MCP_REPO the right checkout?"
  for name in $names; do
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
This lane owns every fleet bundle this repository declares an endpoint for; the four-repo lane
reaches them by calling this script, so nothing should be starting them twice. Stop the other server, or run \`make live-e2e-full-stack\`."
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

# The fleet servers core reaches by **configuration**, not by discovery: `calc` and `rxnlabel`.
#
# Neither is a connector and neither may enter `CHEMCLAW_CONNECTOR_URLS` — the fleet keeps both
# manifests in `manifests-internal/`, which no published export line reaches, so their tools never
# land in a prompt. Each is addressed by one setting pair instead, `<name>_server_url` and
# `<name>_server_token_env`, read by one client module. So both are invisible to
# `check_connectors_at_startup`, `/readyz` is green with either down, and the front door boots
# happily — while the durable half that dials them fails at run time.
#
# * `calc` is the *physics* behind core's calculator tools
#   (D-2026-08-16-the-physics-leaves-the-cache-stays), dialled by
#   `connectors/calc/remote.py::calc_session` on a cache miss. Down, every durable calculation job
#   fails with `CalcServerError: the calculation service is not answering` — what `make live-jobs`
#   did on this lane for as long as it existed: 0 of 5 checks.
# * `rxnlabel` is the reaction labeller (D-2026-08-25-the-labeller-leaves-the-index-stays), dialled
#   by `ReactionLabelWorkflow` on the background worker. Down — and this lane never started it — no
#   reaction is ever labelled past its record phase, so `substrate_precedent`,
#   `conditions_for_similar_reaction` and `reactions_making_substructure` answered "NOT ANSWERABLE
#   YET: … NONE of them have been labelled" over the whole seeded corpus on every run (#520). A
#   fleet checkout carries no `models` extra, so it labels on the RDKit path and its
#   `labeller_version` says so (`mapper@absent`): coarser, stamped honestly, and re-labelled the day
#   real weights are installed.
#
# Both are started here for D-2026-08-27's reason: the lane that cannot do its work without a
# server is the lane that owns it (D-2026-08-28-the-durable-half-has-a-backend-too). And both before
# the workers, because the token export below is what the background worker inherits to send.
readonly CONFIGURED_BACKENDS=(calc rxnlabel)

# `<port> <token variable>` for one configured backend, read from the settings the *client* reads.
#
# The difference is load-bearing: this is the address the client dials, so reading it from anywhere
# else (the fleet's manifest, a literal here) would let the two drift and turn a misconfiguration
# into a connection refused. The token variable is the setting's own, so a deployment that renames
# it is followed rather than contradicted.
backend_address() {
  "$1" - "$2" <<'PYEOF'
import sys
from urllib.parse import urlsplit

from chemclaw.core.config import settings

name = sys.argv[1]
url = getattr(settings, f"{name}_server_url")
port = urlsplit(url).port
if port is None:
    sys.exit(f"{name}_server_url names no port: {url}")
print(port, getattr(settings, f"{name}_server_token_env"))
PYEOF
}

start_backend() {
  local name="$1" python="$2" fleet_python="$3"
  local address port token_var
  address="$(backend_address "$python" "$name")" \
    || die "could not read a port from ${name}_server_url"
  port="${address% *}"
  token_var="${address#* }"

  # Both halves of the credential, as for `chem` and `safety`: core reads this to send, the server
  # reads the same variable name to verify. Without it the server answers `/healthz` and refuses
  # every `/mcp` call, which reaches the reader as a 401 from a server that looks healthy.
  export "$token_var=${!token_var:-dev-token}"
  BACKEND_TOKEN_VARS+=("$token_var")

  # The same address guard `start_fleet_bundles` uses, for the same reason: a pidfile is a per-lane
  # record of a machine-wide port, so ask the address itself before launching onto it.
  if ! running "$name" && curl -fs -o /dev/null --max-time 2 "http://127.0.0.1:$port/healthz"; then
    die "$name: 127.0.0.1:$port is already served, and not by a process this lane started.
This lane owns the $name backend; the four-repo lane reaches it by calling this script, so nothing
should be starting it twice. Stop the other server, or run \`make live-e2e-full-stack\`."
  fi

  ( cd "$MCP_REPO" && start "$name" "$fleet_python" -m "uvicorn" "chemclaw_mcp_$name.app:app" \
      --host 127.0.0.1 --port "$port" )
  wait_for "$name" "http://127.0.0.1:$port/healthz"
}

# ------------------------------------------------------------------ the interactive workers
#
# One per enabled bundle whose manifest lists `queued:` tools, which is where every call to such a
# tool waits for a slot (`D-2026-09-30-a-heavy-tool-call-waits-in-a-queue-rather-than-being-refused`).
# The chart renders one Deployment for each (`templates/deployment-interactive-workers.yaml`) and
# runs `python -m chemclaw.connectors.interactive_worker <name>` in it (`deploy/entrypoint.sh`,
# `interactive-worker-*`); this lane starts the same module under the same name.
#
# **This lane used to start none of them**, and nothing said so: every `predict_pka`,
# `compute_xtb_energy`, `run_python` and prediction call waited out its inline wait on a queue with
# no poller, became a durable job nothing would ever run, and read `running` until it timed out —
# while the front door's sweep, which asked only about the jobs queues, called each bundle healthy.
#
# **Derived, never listed** — the set is `registry.queues_tools` over `registry.enabled()`, the
# question the front door's own sweep asks, so a bundle that starts queueing a tool (or a fleet
# manifest the four-repo lane binds, such as `pyexec`) gets its worker the day it does.
interactive_worker_names() {
  "$1" -c 'from chemclaw.connectors.registry import enabled, queues_tools
print("\n".join(m.name for m in enabled() if queues_tools(m)))'
}

# The first probe port the interactive workers take, one each upwards. Clear of the bundle workers'
# 9000-9004; `.live/run/<name>.port` is the authority for which one a worker holds.
readonly INTERACTIVE_PORT_BASE=9010

# Wait until Temporal reports a poller on each named worker's queue, or die naming the ones that
# never polled.
#
# **A worker's `/readyz` is not this.** It says the process built its `Worker` and is running it;
# the broker only lists a poller once the first long-poll arrives, and the front door's startup
# sweep (`connectors/health.py::check_connectors_at_startup`) asks the broker. On a cold start
# `worker-bo` (torch on the import path) lost that race and the front door died with
# `ConnectorsUnavailable: ... bo (unpolled)` — the first `up` failing and the second succeeding,
# every time the page cache was cold. So the front door starts only after this passes.
#
# The queue each worker serves follows from its name, the one convention this script already
# names them by: `worker-background` polls `background_task_queue`, `worker-<bundle>` polls
# `bundle_queue(<bundle>)`, `interactive-worker-<bundle>` polls `interactive_queue(<bundle>)`. A
# poller of either task type counts, because a worker registering only activities is polling too.
# A worker whose pid is gone is reported at once rather than waited out, as `wait_for` does.
wait_for_pollers() {
  local python="$1"; shift
  "$python" - "$RUN_DIR" "$READY_ATTEMPTS" "$@" <<'PY' || die "a worker never polled its queue — see the line above and $LIVE_DIR/<worker>.log"
import asyncio
import os
import sys
from datetime import timedelta
from pathlib import Path

from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest

from chemclaw.connectors.queues import bundle_queue, interactive_queue
from chemclaw.core.config import settings
from chemclaw.core.temporal_client import connect

run_dir, attempts, workers = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3:]


def queue_of(worker: str) -> str:
    if worker == "worker-background":
        return settings.background_task_queue
    if worker.startswith("interactive-worker-"):
        return interactive_queue(worker.removeprefix("interactive-worker-"))
    return bundle_queue(worker.removeprefix("worker-"))


def alive(worker: str) -> bool:
    try:
        os.kill(int((run_dir / f"{worker}.pid").read_text()), 0)
    except (OSError, ValueError):
        return False
    return True


async def polled(client, queue: str) -> bool:
    for kind in (TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW, TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY):
        answer = await client.workflow_service.describe_task_queue(
            DescribeTaskQueueRequest(
                namespace=client.namespace, task_queue=TaskQueue(name=queue), task_queue_type=kind
            ),
            timeout=timedelta(seconds=5),
        )
        if answer.pollers:
            return True
    return False


async def main() -> int:
    client = await connect()
    waiting = {worker: queue_of(worker) for worker in workers}
    for _ in range(attempts):
        for worker, queue in list(waiting.items()):
            if not alive(worker):
                print(f"[live] {worker} exited before polling {queue}", file=sys.stderr)
                return 1
            if await polled(client, queue):
                print(f"\033[36m[live]\033[0m {worker} polling {queue}")
                del waiting[worker]
        if not waiting:
            return 0
        await asyncio.sleep(1)
    unpolled = ", ".join(f"{w} ({q})" for w, q in waiting.items())
    print(f"[live] no poller registered for: {unpolled}", file=sys.stderr)
    return 1


sys.exit(asyncio.run(main()))
PY
}

up() {
  mkdir -p "$RUN_DIR"
  command -v uv >/dev/null 2>&1 || die "uv not found"
  pg_isready -h 127.0.0.1 -p "${CHEMCLAW_LIVE_PGPORT:-5432}" >/dev/null 2>&1 \
    || die "postgres is not up — run infra/live/bootstrap.sh first"

  local python
  python="$(python_bin)"
  cd "$REPO_ROOT"

  # The credentials a previous `up` minted, when that lane is still the one running (`down` deletes
  # the file). `connectors_dev --export-env` keeps a token already in the environment, so this is
  # what makes a second `up` — `restart <name>` is one — re-use the secrets the running servers
  # hold instead of minting new ones beside them. See `source_unset_only`.
  source_unset_only "$RUN_DIR/connector-env.sh"

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
  local fleet_python
  fleet_python="$(fleet_checkout_python)"
  start_fleet_bundles "$python" "$fleet_python"
  log "connector urls (with the fleet): $CHEMCLAW_CONNECTOR_URLS"

  # The configured backends are not connectors and so are not in that map — and are needed all the
  # same, by the durable half rather than by the front door. See `CONFIGURED_BACKENDS`.
  local backend
  BACKEND_TOKEN_VARS=()
  for backend in "${CONFIGURED_BACKENDS[@]}"; do
    start_backend "$backend" "$python" "$fleet_python"
  done

  # Now every address and credential is known, so the file a second shell reads can be complete.
  # `connector_env`'s own exports, then the fleet's two tokens and the URL map it rewrote.
  # Which variables carry a credential is derived (`bearer_token_vars`), plus each configured
  # backend's — collected by `start_backend` from its own setting, because those servers are
  # deliberately *not* connectors and so are in no manifest the registry reads.
  #
  # An unset variable is skipped rather than written empty: an `export X=` in this file would
  # overwrite a credential the reading shell already held, which is the mismatch this file exists
  # to prevent, running backwards. What was skipped is logged, because a silently short list is
  # exactly what went wrong here.
  local token_var
  local -a held=() unheld=()
  for token_var in $(bearer_token_vars "$python") "${BACKEND_TOKEN_VARS[@]}"; do
    # `connector_env` already emits its own bundles' minted tokens; writing them a second time
    # would say the same thing twice and invite the two copies to disagree.
    case "$connector_exports" in *"export $token_var="*) continue ;; esac
    if [ -n "${!token_var:-}" ]; then held+=("$token_var"); else unheld+=("$token_var"); fi
  done
  # `umask` inside the subshell and the redirection inside it too: `( umask 077; … ) > file` opens
  # the file before the body runs, so it was created 0644 — every minted credential world-readable.
  ( umask 077
    { printf '%s\n' "$connector_exports"
      printf 'export CHEMCLAW_CONNECTOR_URLS=%q\n' "$CHEMCLAW_CONNECTOR_URLS"
      for token_var in "${held[@]}"; do printf 'export %s=%q\n' "$token_var" "${!token_var}"; done
    } > "$RUN_DIR/connector-env.sh"
  )
  [ ${#unheld[@]} -eq 0 ] \
    || log "no credential held for ${unheld[*]} — a second shell will 401 on those connectors"
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
  # The interactive workers — see `interactive_worker_names`. Assigned before iterating, for the
  # reason `start_fleet_bundles` gives: a failed derivation must stop the lane, not start a subset.
  local interactive name port=$INTERACTIVE_PORT_BASE
  local -a workers=(worker-background worker-calc worker-bo worker-results)
  interactive="$(interactive_worker_names "$python")" \
    || die "could not derive which bundles queue tool calls — see the error above"
  for name in $interactive; do
    start_worker "interactive-worker-$name" "$port" \
      "$python" -m chemclaw.connectors.interactive_worker "$name"
    workers+=("interactive-worker-$name")
    port=$((port + 1))
  done

  # The mock model, when the lane is pointed at it. Started before the front door because the front
  # door builds a chat client at startup and would come up pointed at nothing.
  #
  # It is an *HTTP* mock rather than an injected `BaseChatClient` deliberately: the streaming
  # assembler, the middleware stack, budget admission, the audit sink and the session store all sit
  # between the socket and the agent, and the in-process scripted client in `tests/` bypasses every
  # one of them — its own docstring records passing green while production failed 100% of the time.
  #
  # **Which gateway this lane runs against is asked of the code that decides it.** The test is
  # `Settings`' own resolved `llm_base_url` — the value the front door and every worker below will
  # actually dial, from this same environment — against the address `cli/mock_llm` serves. Neither
  # string is written here.
  #
  # It used to compare `$CHEMCLAW_LLM_BASE_URL` against the mock's address transcribed into this
  # file, and that broke the moment the address became a `Settings` *default*: no shell in this
  # lane sets that variable (`Makefile`'s `live-up` is a bare `bash infra/live/processes.sh up`),
  # so the condition was false, the mock never started, and the front door came up pointed at a
  # closed port while the line below named the gateway as though it were serving. The defect is
  # the transcription, not the particular string — `tests/test_config.py` now fails if either
  # address is written into this script again.
  local llm_base_url mock_base_url
  llm_base_url="$("$python" -c \
    'from chemclaw.core.config import settings; print(settings.llm_base_url)')" \
    || die "could not resolve the model gateway address — see the error above"
  mock_base_url="$("$python" -c 'from chemclaw.cli.mock_llm import MOCK_BASE_URL; print(MOCK_BASE_URL)')"
  if [ "$llm_base_url" = "$mock_base_url" ]; then
    start mock-llm "$python" -m chemclaw.cli.mock_llm
    wait_for mock-llm "${mock_base_url%/v1}/__mock/stats"
  fi

  # Every worker up *and polling* before the front door, because its startup sweep asks the broker
  # about pollers and refuses to boot on a queue with none (`wait_for_pollers`). The front door
  # used to start first, and the readiness loop below it ran afterwards.
  for worker in "${workers[@]}"; do
    wait_for "$worker" "http://127.0.0.1:$(cat "$RUN_DIR/$worker.port")/readyz"
  done
  wait_for_pollers "$python" "${workers[@]}"

  # **The front door always starts now, and the `llm_configured` gate that used to guard it is
  # gone.** It asked whether `ANTHROPIC_API_KEY` was set, because building the agent built a chat
  # client and that client's constructor raised on a missing key — so with no credential the front
  # door failed to *boot* rather than at the first turn. There is one client now
  # (`D-2026-09-04-a-gateway-is-the-only-provider`), it takes a placeholder bearer for the many
  # internal gateways that ignore one, and `CHEMCLAW_LLM_BASE_URL` always names a destination:
  # the mock above by default. So there is nothing left to be un-configured, and a gate that always
  # passes is worse than none.
  #
  # A gateway that *does* want a credential and was not given one is a 401 on the first turn, which
  # `make live-probes` reports and `make live-jobs` — Temporal and Postgres, no model in the loop —
  # does not care about.
  start api "$python" -m uvicorn chemclaw.api.app:create_app --factory \
    --host 127.0.0.1 --port "$API_PORT"
  wait_for api "http://127.0.0.1:$API_PORT/readyz"
  log "live stack up. front door: http://127.0.0.1:$API_PORT · logs: $LIVE_DIR"
  log "  model gateway: $llm_base_url"
  log "  from another terminal, first: eval \"\$(bash infra/live/processes.sh env)\""
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
  # of the mismatch this file exists to prevent, not a milder one. The wrapping lane's environment
  # goes with them for the same reason: the next `up` is a new lane, not a restart of this one.
  rm -f "$RUN_DIR/connector-env.sh" "$RUN_DIR/lane-env.sh"
}

status() {
  [ -d "$RUN_DIR" ] || { log "nothing running"; return; }
  # This block used to name the front door as "deliberately skipped" when no model credential was
  # set, because "absent" reads the same as "never existed". Nothing is skipped any more — the
  # front door needs no credential to boot — so a missing `api.pid` now means it died, which the
  # loop below reports as DOWN.
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
  # Every precondition `up` would `die` on, checked *before* anything is killed. `restart` is
  # "kill, then up", and `up` dies on a missing Chemclaw3-mcp checkout — so a restart run without
  # `CHEMCLAW_MCP_REPO` used to kill the process, fail to bring the lane back, and leave the lane
  # in a worse state than it found it. That is the wrong failure mode anywhere and a disqualifying
  # one here: this verb is the primitive the storm's chaos family uses, so its own environment
  # became a silent, delayed cause of unrelated red checks two families later. Measured, one such
  # call killed `mock-llm` and left the whole run driving a lane with no model.
  [ -d "$MCP_REPO" ] || die "refusing to restart $name: chem and safety are served by Chemclaw3-mcp,
which is not at $MCP_REPO, so \`up\` could not bring the lane back after the kill. Clone it beside
this checkout, or set CHEMCLAW_MCP_REPO. Nothing has been killed."
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
