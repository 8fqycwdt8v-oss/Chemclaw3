#!/usr/bin/env bash
# The multi-replica lane: three front doors, two background workers and two calc workers on one
# database, asserting that limits hold globally, a killed pod's turn resumes, and one calculation
# miss computes once. `make live-replicas`; the assertions are `tests/live_replicas/`.
#
# This script owns only what the processes stand on. Postgres is the server `make up` starts (or one
# already answering at CHEMCLAW_POSTGRES_DSN); the lane runs in a database of its own, created here
# and dropped on exit, and every other DSN setting is blanked, so nothing it does can land outside
# that database. Temporal is a private dev server the tests start, and the model is a function of
# the thread, so there is nothing else to bring up and no credential to hold.
#
# **Asked for, the lane fails when it cannot run; under `make ci` it skips, named and counted.**
# The Makefile sets LIVE_REPLICAS_OPTIONAL only when `ci` is among the goals. Then a missing
# psql, a Postgres that does not answer or refuses CREATE DATABASE become a pytest skip carrying the
# reason (the epilogue of tests/conftest.py counts it), because an air-gapped runner that only ran
# `make db-migrate` must not be red for a prerequisite the gate never promised, nor silently green.
# GitHub's `replicas` job runs `make live-replicas`, so there it is a failure.
#
# **Postgres is started only when the lane is asked for directly.** A gate must not start containers
# as a side effect of verifying something else, and `make ci` already assumes a reachable database
# (it follows `make db-migrate`); `make live-replicas` has nothing else to lean on, so it starts the
# compose one when none answers.
#
# Usage: replicas.sh [pytest arguments]   e.g. replicas.sh -k resumes

set -euo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly ADMIN_DSN="${CHEMCLAW_POSTGRES_DSN:-postgresql://chemclaw:chemclaw@localhost:5432/chemclaw}"
readonly SCRATCH="chemclaw_replicas_$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')"
readonly OPTIONAL="${LIVE_REPLICAS_OPTIONAL:-}"

log() { printf '\033[36m[live]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[live] %s\033[0m\n' "$*" >&2; exit 1; }

# `host:port/database` of a DSN: the credential never reaches a log line.
describe_dsn() { printf '%s' "$1" | sed -E 's#^[a-z+]+://([^@/]*@)?##; s#\?.*$##'; }
readonly ADMIN_WHERE="$(describe_dsn "$ADMIN_DSN")"

command -v uv >/dev/null 2>&1 || die "uv not found"
command -v python3 >/dev/null 2>&1 || die "python3 not found"

# One reason, two outcomes: skip under `make ci`, fail when asked for.
reason=""
unavailable() {
  if [ -n "$OPTIONAL" ]; then reason="$1"; else die "$1"; fi
}

scratch_dsn=""
if ! command -v psql >/dev/null 2>&1 || ! command -v pg_isready >/dev/null 2>&1; then
  unavailable "psql and pg_isready are needed to create and drop the lane's database"
else
  if ! pg_isready -d "$ADMIN_DSN" >/dev/null 2>&1; then
    if [ -n "$OPTIONAL" ]; then
      unavailable "no Postgres answers at $ADMIN_WHERE, and \`make ci\` does not start one"
    else
      log "no Postgres at $ADMIN_WHERE; starting the compose one"
      docker compose -f "$REPO_ROOT/infra/docker-compose.yml" up -d postgres \
        || die "could not start Postgres: is the Docker daemon running? (sudo -n dockerd &)"
      for _ in $(seq 1 60); do
        pg_isready -d "$ADMIN_DSN" >/dev/null 2>&1 && break
        sleep 1
      done
      pg_isready -d "$ADMIN_DSN" >/dev/null 2>&1 || die "Postgres never answered at $ADMIN_WHERE"
    fi
  fi
  if [ -z "$reason" ]; then
    # The same server and credentials, another database: `.../db?opts` -> `.../$SCRATCH?opts`.
    dsn_without_query="${ADMIN_DSN%%\?*}"
    dsn_query="${ADMIN_DSN#"$dsn_without_query"}"
    scratch_dsn="${dsn_without_query%/*}/$SCRATCH$dsn_query"
    if created="$(psql "$ADMIN_DSN" -v ON_ERROR_STOP=1 -qc "CREATE DATABASE $SCRATCH" 2>&1)"; then
      drop_scratch() {
        psql "$ADMIN_DSN" -qc "DROP DATABASE IF EXISTS $SCRATCH WITH (FORCE)" >/dev/null 2>&1 || true
      }
      trap drop_scratch EXIT
    else
      scratch_dsn=""
      unavailable "could not create database $SCRATCH on $ADMIN_WHERE: $created"
    fi
  fi
fi

cd "$REPO_ROOT"
if [ -n "$reason" ]; then
  log "skipping the lane: $reason"
  export CHEMCLAW_LIVE_REPLICAS_UNAVAILABLE="$reason"
else
  log "database $SCRATCH on $ADMIN_WHERE; running tests/live_replicas"
  export CHEMCLAW_POSTGRES_DSN="$scratch_dsn"
fi
# Every other DSN setting is blanked (blank falls back to `postgres_dsn`).
export CHEMCLAW_SESSION_STORE_DSN="" CHEMCLAW_POSTGRES_MIGRATION_DSN=""
export CHEMCLAW_LIVE_REPLICAS=1

# SIGKILLed when this script dies, so a killed `make` leaves no pytest (and, through the same hook in
# tests/replicas.py, no replica or worker) behind: the parent-death signal survives exec. The
# interpreter is started directly, because `uv run` would be the parent and pytest its child.
# In the background and waited for, so a TERM or INT aimed at this script reaches pytest (which then
# stops its replicas) before the database is dropped.
python="$(uv run python -c 'import sys; print(sys.executable)' 2>/dev/null)"
python3 -c 'import ctypes, os, sys; ctypes.CDLL(None).prctl(1, 9); os.execvp(sys.argv[1], sys.argv[1:])' \
  "$python" -m pytest -p no:cacheprovider -n 0 -ra tests/live_replicas "$@" &
pytest_pid=$!
trap 'kill -TERM "$pytest_pid" 2>/dev/null || true' INT TERM HUP
status=0
wait "$pytest_pid" || status=$?
while kill -0 "$pytest_pid" 2>/dev/null; do
  wait "$pytest_pid" || status=$?
done
exit "$status"
