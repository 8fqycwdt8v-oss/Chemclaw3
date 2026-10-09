#!/usr/bin/env bash
# The multi-replica lane: three front doors, two background workers and two calc workers on one
# database, asserting that limits hold globally, a killed pod's turn resumes, and one calculation
# miss computes once. `make live-replicas`; the assertions are `tests/live_replicas/`.
#
# This script owns only what the processes stand on. Postgres is the server `make up` starts (or one
# already answering at CHEMCLAW_POSTGRES_DSN); the lane runs in a database of its own, created here
# and dropped on exit, so a migration or a schema another branch left in the shared database cannot
# colour it. Temporal is a private dev server the tests start, and the model is a function of the
# thread, so there is nothing else to bring up and no credential to hold.
#
# Usage: replicas.sh [pytest arguments]   e.g. replicas.sh -k resumes

set -euo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly ADMIN_DSN="${CHEMCLAW_POSTGRES_DSN:-postgresql://chemclaw:chemclaw@localhost:5432/chemclaw}"
readonly SCRATCH="chemclaw_replicas_$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')"

log() { printf '\033[36m[live]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[live] %s\033[0m\n' "$*" >&2; exit 1; }

command -v uv >/dev/null 2>&1 || die "uv not found"
command -v psql >/dev/null 2>&1 || die "psql not found: it creates and drops the lane's database"

if ! pg_isready -d "$ADMIN_DSN" >/dev/null 2>&1; then
  log "no Postgres at $ADMIN_DSN; starting the compose one"
  docker compose -f "$REPO_ROOT/infra/docker-compose.yml" up -d postgres \
    || die "could not start Postgres: is the Docker daemon running? (sudo -n dockerd &)"
  for _ in $(seq 1 60); do
    pg_isready -d "$ADMIN_DSN" >/dev/null 2>&1 && break
    sleep 1
  done
  pg_isready -d "$ADMIN_DSN" >/dev/null 2>&1 || die "Postgres never answered at $ADMIN_DSN"
fi

# The same server and credentials, another database: `.../db?opts` -> `.../$SCRATCH?opts`.
dsn_without_query="${ADMIN_DSN%%\?*}"
dsn_query="${ADMIN_DSN#"$dsn_without_query"}"
scratch_dsn="${dsn_without_query%/*}/$SCRATCH$dsn_query"

drop_scratch() { psql "$ADMIN_DSN" -qc "DROP DATABASE IF EXISTS $SCRATCH WITH (FORCE)" >/dev/null || true; }
trap drop_scratch EXIT
psql "$ADMIN_DSN" -v ON_ERROR_STOP=1 -qc "CREATE DATABASE $SCRATCH" >/dev/null \
  || die "could not create database $SCRATCH on $ADMIN_DSN"
log "database $SCRATCH; running tests/live_replicas"

cd "$REPO_ROOT"
CHEMCLAW_LIVE_REPLICAS=1 CHEMCLAW_POSTGRES_DSN="$scratch_dsn" \
  uv run pytest -p no:cacheprovider -n 0 -ra tests/live_replicas "$@"
