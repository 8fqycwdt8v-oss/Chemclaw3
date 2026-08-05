#!/usr/bin/env bash
# Repeat the storm and sample the counters that only time moves — checkpointed, so a reclaimed
# container costs one round rather than the run.
#
# The first attempt at this was a scratch loop and it failed in three separate ways, each of which
# this script is shaped around:
#
#  1. **It died at round 5 of 200.** This container is reclaimed roughly hourly, so a soak that is
#     one process is a soak that produces four minutes of data and a story about hours. Rounds are
#     independent, so the record is append-only and `resume()` reads the next round number back out
#     of it. Re-running this script after a reclaim continues; it does not restart.
#  2. **Its own record was unparseable.** `head -c 200` truncated the mock stats mid-object, so
#     every line was invalid JSON and the one artefact meant to outlive the run could not be read.
#     The line is now built by `json.dumps` from values passed as environment, which cannot produce
#     a malformed line no matter what the samples contain.
#  3. **It sampled the wrong process.** The pool gauges were scraped from :9000 — a *worker's*
#     metrics port — so `chemclaw_pg_pool_*` came back empty for all five rounds while the front
#     door served them on :8000 the whole time. A gauge that is silently absent reads exactly like
#     a gauge that is zero.
#
# The analysis deliberately does not live here: `chemclaw.cli.soak_report` fits each series and
# refuses to name a slope it cannot resolve, which is a rule worth having tests for and shell is
# the wrong place to keep one.
#
# Usage: soak.sh [rounds]     — run (or resume) up to `rounds` rounds, default 200
#        soak.sh report       — fit every series in the record so far
#        soak.sh reset        — start a fresh record

set -uo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly LIVE_DIR="${CHEMCLAW_LIVE_DIR:-$REPO_ROOT/.live}"
readonly RECORD="$LIVE_DIR/soak.jsonl"
readonly API_PORT="${CHEMCLAW_LIVE_API_PORT:-8000}"
readonly MOCK_PORT="${CHEMCLAW_LIVE_MOCK_PORT:-8820}"
# Below this the run stops itself. Writes fail before the disk reads as full in this environment,
# and a soak that fills the disk takes the container down with it.
readonly DISK_FLOOR_GB="${CHEMCLAW_SOAK_DISK_FLOOR_GB:-4}"
# One round is a whole storm at a load small enough that the round is the unit, not the bottleneck.
readonly SWEEP_TURNS="${CHEMCLAW_SOAK_SWEEP_TURNS:-24}"
readonly COLLIDE="${CHEMCLAW_SOAK_COLLIDE:-8}"
# Every family except A and E, and the exclusion is the measurement rather than a speed-up.
# A restarts the front door at each admission cap and E SIGKILLs a worker — so a soak that ran
# them would sample the RSS of a process that had just been replaced, which is not a series at
# all. (E is also where the round's wall clock goes: it waits out
# `xtb_job_heartbeat_timeout_seconds`, ~600 s, by design.) Both belong in `make live-storm`, which
# is the run that asks whether the system survives being disturbed; this run asks what drifts when
# it is not.
readonly FAMILIES="${CHEMCLAW_SOAK_FAMILIES:-BCDFGH}"

log() { printf '\033[36m[soak]\033[0m %s\n' "$*"; }

python_bin() { ( cd "$REPO_ROOT" && uv run python -c 'import sys; print(sys.executable)' ); }

# The next round to run: one past the highest already recorded. Reading the record rather than
# keeping a separate cursor means there is exactly one piece of state, and it is the deliverable.
resume() {
  [ -s "$RECORD" ] || { echo 1; return; }
  "$1" - "$RECORD" <<'PY'
import json, sys
last = 0
for line in open(sys.argv[1], encoding="utf-8"):
    line = line.strip()
    if not line:
        continue
    try:
        row = json.loads(line)
    except ValueError:
        continue
    if isinstance(row.get("round"), int):
        last = max(last, row["round"])
print(last + 1)
PY
}

free_gb() { df -BG --output=avail "$REPO_ROOT" | tail -1 | tr -dc '0-9'; }

# Every sample is optional: a scrape that times out must cost its own field, never the round.
# `|| true` throughout, and the line builder treats an empty value as null.
sample_rows() {
  PGPASSWORD="${CHEMCLAW_PG_PASSWORD:-chemclaw}" psql -h 127.0.0.1 -U chemclaw -d chemclaw -tAc "
    select json_object_agg(t, n) from (
      select 'audit_events' t, count(*) n from audit_events
      union all select 'session_messages', count(*) from session_messages
      union all select 'session_events', count(*) from session_events
      union all select 'session_turns', count(*) from session_turns
      union all select 'job_records', count(*) from job_records
      union all select 'calculation_results', count(*) from calculation_results
      union all select 'turn_costs', count(*) from turn_costs
    ) s" 2>/dev/null || true
}

run() {
  local limit="${1:-200}"
  local python; python="$(python_bin)"
  mkdir -p "$LIVE_DIR"
  local round; round="$(resume "$python")"
  log "resuming at round $round (record: $RECORD)"

  while [ "$round" -le "$limit" ]; do
    local disk; disk="$(free_gb)"
    if [ "${disk:-0}" -lt "$DISK_FLOOR_GB" ]; then
      printf '{"stop":"disk %sGB below floor %sGB","round":null}\n' "$disk" "$DISK_FLOOR_GB" >>"$RECORD"
      log "stopping: ${disk}GB free is below the ${DISK_FLOOR_GB}GB floor"
      return 0
    fi

    local start; start="$(date +%s)"
    local storm_out; storm_out="$LIVE_DIR/soak-round.md"
    CHEMCLAW_LLM_PROVIDER=openai_compatible \
    CHEMCLAW_LLM_BASE_URL="http://127.0.0.1:$MOCK_PORT/v1" \
    CHEMCLAW_LLM_MODEL=mock \
      timeout 1800 "$python" -m chemclaw.cli.live_storm \
        --families "$FAMILIES" \
        --sweep-turns "$SWEEP_TURNS" --collide "$COLLIDE" --report "$storm_out" \
        >"$LIVE_DIR/soak-round.log" 2>&1
    local rc=$?
    local checks; checks="$(grep -oE '[0-9]+/[0-9]+ checks passed' "$storm_out" 2>/dev/null | head -1)"
    # A failing round's report is the only place its *reason* exists, and the next round overwrites
    # it. Rounds 16 and 28 of the first real run each lost a check and neither could be diagnosed
    # afterwards, which is the same defect the record itself had: evidence that does not outlive
    # the thing it describes.
    if [ "$rc" -ne 0 ]; then
      cp "$storm_out" "$LIVE_DIR/soak-round-$round.md" 2>/dev/null || true
      cp "$LIVE_DIR/soak-round.log" "$LIVE_DIR/soak-round-$round.log" 2>/dev/null || true
    fi

    SOAK_ROUND="$round" \
    SOAK_SECS="$(( $(date +%s) - start ))" \
    SOAK_RC="$rc" \
    SOAK_CHECKS="${checks:-}" \
    SOAK_DISK_GB="${disk:-}" \
    SOAK_RSS_KB="$(ps -o rss= -p "$(cat "$LIVE_DIR/run/api.pid" 2>/dev/null)" 2>/dev/null | tr -d ' ')" \
    SOAK_ROWS="$(sample_rows)" \
    SOAK_METRICS="$(curl -s --max-time 5 "http://127.0.0.1:$API_PORT/metrics" || true)" \
    SOAK_MOCK="$(curl -s --max-time 5 "http://127.0.0.1:$MOCK_PORT/__mock/stats" || true)" \
      "$python" - >>"$RECORD" <<'PY'
import json, os


def number(name):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def blob(name):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


# Every unlabelled `chemclaw_*` sample the front door serves, so the soak reads the same numbers an
# operator would rather than a second, private accounting of them. Widened from the pool gauges
# alone after round 30 of the first run: RSS was climbing and the question "is the session LRU still
# filling, or is it full and RSS growing anyway" could not be answered from the record — it needed a
# live scrape, which by definition the record had not kept. The distinction is the whole diagnosis.
gauges = {}
pool = {}
for line in os.environ.get("SOAK_METRICS", "").splitlines():
    if not line.startswith("chemclaw_") or "{" in line:
        continue
    name, _, value = line.partition(" ")
    try:
        gauges[name] = float(value)
    except ValueError:
        continue
    if name.startswith("chemclaw_pg_pool_"):
        pool[name[len("chemclaw_pg_pool_") :]] = gauges[name]

print(
    json.dumps(
        {
            "round": number("SOAK_ROUND"),
            "secs": number("SOAK_SECS"),
            "rc": number("SOAK_RC"),
            "checks": os.environ.get("SOAK_CHECKS") or None,
            "api_rss_kb": number("SOAK_RSS_KB"),
            "pool": pool or None,
            "gauges": gauges or None,
            "rows": blob("SOAK_ROWS"),
            "disk_gb": number("SOAK_DISK_GB"),
            "mock": blob("SOAK_MOCK"),
        },
        separators=(",", ":"),
    )
)
PY
    log "round $round: rc=$rc ${checks:-no checks line} ($(( $(date +%s) - start ))s)"
    round=$(( round + 1 ))
  done
  log "reached round limit $limit"
}

case "${1:-200}" in
  report) exec "$(python_bin)" -m chemclaw.cli.soak_report "$RECORD" ;;
  reset)  rm -f "$RECORD"; log "record cleared" ;;
  *)      run "${1:-200}" ;;
esac
