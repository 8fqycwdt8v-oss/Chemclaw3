#!/usr/bin/env bash
# Stand up the two pieces of infrastructure the live lane needs — Postgres/pgvector and a real
# Temporal frontend — with or without a Docker daemon.
#
# `make up` is the right way to do this and stays the first branch: the compose file is one
# declaration and its images are the ones production resembles. This script exists because the
# environments the live lane most needs to run in are the ones that have no daemon — CI runners
# with no privileged socket, and the agent containers where every prior live pass was performed
# by hand and recorded "Temporal absent" (`docs/archive/live-grounded-2026-08-03.md`). A lane you
# can only run where Docker runs is a lane that keeps not being run.
#
# The native path is therefore not a second-class copy of the compose file; it is what makes the
# lane reachable at all. It binds the same ports the compose file does (5432, 7233, 8081), so
# everything downstream — `settings.postgres_dsn`, `settings.temporal_address`, the runbook —
# is identical either way and no configuration knows which path produced the stack.
#
# Two acquisition details are deliberate, both measured rather than assumed:
#
#   * The Temporal CLI is **built from source** instead of downloaded. `temporal.download` is a
#     policy denial (403 to CONNECT) behind a filtering egress proxy, and `go install` refuses the
#     module because its `go.mod` carries replace directives — so a git clone plus `go build` is
#     the only route that works, and it is the one that keeps working when the binary host is
#     blocked.
#   * pgvector is likewise built from a git clone rather than a release tarball, because
#     `codeload.github.com` archives are denied by the same policy while git-over-HTTPS is not.
#
# Usage: bootstrap.sh [up|down|status]

set -euo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly LIVE_DIR="${CHEMCLAW_LIVE_DIR:-$REPO_ROOT/.live}"
readonly PGDATA="${CHEMCLAW_LIVE_PGDATA:-/var/lib/postgresql/chemclaw-live}"
readonly PGPORT="${CHEMCLAW_LIVE_PGPORT:-5432}"
readonly TEMPORAL_PORT="${CHEMCLAW_LIVE_TEMPORAL_PORT:-7233}"
readonly TEMPORAL_UI_PORT="${CHEMCLAW_LIVE_TEMPORAL_UI_PORT:-8081}"
# Pinned, not `@latest`: the version the stack was verified against is part of the record, and a
# lane whose broker version drifts under it reports a different system than the one reviewed.
readonly TEMPORAL_CLI_VERSION="${CHEMCLAW_LIVE_TEMPORAL_CLI_VERSION:-v1.8.2}"
readonly PGVECTOR_VERSION="${CHEMCLAW_LIVE_PGVECTOR_VERSION:-v0.8.6}"
# Debian/Ubuntu put the *server* binaries (`initdb`, `pg_ctl`) under /usr/lib/postgresql/<ver>/bin
# and leave them off PATH — only the client package is linked into /usr/bin. Asking `pg_config`
# rather than hard-coding the path also means a machine with two clusters uses the one whose
# `pg_config` is first, which is the same one `pg_isready` and `psql` will talk to.
readonly PGBIN="$(pg_config --bindir)"
# Where the PR-gate writes. Never the working checkout — see `ensure_note_repo`.
readonly NOTE_REPO_DIR="${CHEMCLAW_NOTE_REPO_DIR:-$LIVE_DIR/knowledge-repo}"
# The role/password/database the default DSN in `core/config/store.py` already names, written
# once. Every admin command below connects over TCP with this password rather than through the
# trusted local socket, so bootstrap fails here if the credential the application will use is
# wrong — that check is worth more than the convenience of skipping it.
readonly PGUSER_NAME="chemclaw"
readonly PGPASSWORD_VALUE="chemclaw"
readonly PGDB_NAME="chemclaw"

log() { printf '\033[36m[live]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[live] %s\033[0m\n' "$*" >&2; exit 1; }

# Postgres refuses to run as root, so every server-side command is routed through the `postgres`
# account when this script *is* root. Off root (a developer laptop) the current user owns the
# cluster and no switch is needed — one wrapper covers both rather than two code paths.
as_postgres() {
  if [ "$(id -u)" -eq 0 ]; then
    su postgres -c "$1"
  else
    bash -c "$1"
  fi
}

docker_available() { docker info >/dev/null 2>&1; }

# ---------------------------------------------------------------------------- prerequisites

ensure_pgvector() {
  local sharedir
  sharedir="$(pg_config --sharedir)"
  if [ -f "$sharedir/extension/vector.control" ]; then
    log "pgvector already installed ($sharedir/extension)"
    return
  fi
  [ -f "$(pg_config --includedir-server)/postgres.h" ] || die \
    "PostgreSQL server headers missing. Install them first, e.g.
       apt-get update && apt-get install -y postgresql-server-dev-$(pg_config --version | awk '{print $2}' | cut -d. -f1)"

  log "building pgvector $PGVECTOR_VERSION from source"
  mkdir -p "$LIVE_DIR"
  [ -d "$LIVE_DIR/pgvector" ] || git clone --quiet --depth 1 -b "$PGVECTOR_VERSION" \
    https://github.com/pgvector/pgvector.git "$LIVE_DIR/pgvector"
  make -C "$LIVE_DIR/pgvector" -j"$(nproc)" >/dev/null
  make -C "$LIVE_DIR/pgvector" install >/dev/null
  log "pgvector installed"
}

ensure_temporal_cli() {
  if command -v temporal >/dev/null 2>&1; then
    log "temporal CLI already installed ($(temporal --version 2>/dev/null | head -1))"
    return
  fi
  command -v go >/dev/null 2>&1 || die "no 'temporal' binary and no Go toolchain to build one"

  log "building the Temporal CLI $TEMPORAL_CLI_VERSION from source (temporal.download is blocked)"
  mkdir -p "$LIVE_DIR"
  [ -d "$LIVE_DIR/temporal-cli" ] || git clone --quiet --depth 1 -b "$TEMPORAL_CLI_VERSION" \
    https://github.com/temporalio/cli.git "$LIVE_DIR/temporal-cli"
  # `-X ...Version` because the module defaults to "0.0.0-DEV" when built outside its own
  # release pipeline, and a live run whose record cannot name its broker version is a live run
  # nobody can reproduce.
  ( cd "$LIVE_DIR/temporal-cli" && go build \
      -ldflags "-X github.com/temporalio/cli/internal/temporalcli.Version=${TEMPORAL_CLI_VERSION#v}" \
      -o /usr/local/bin/temporal ./cmd/temporal )
  log "temporal CLI installed ($(temporal --version | head -1))"
}

# ---------------------------------------------------------------------------- knowledge repo

ensure_note_repo() {
  if [ -d "$NOTE_REPO_DIR/.git" ]; then
    log "note repo clone already present ($NOTE_REPO_DIR)"
    return
  fi
  # A *dedicated* clone, because the PR-gate refuses anything else and is right to:
  # `GitNoteSubmitter` creates `note/<id>` here and force-pushes it to this clone's origin, so
  # pointing it at the working checkout would publish agent-authored notes into the source
  # repository. `note_repo_dir` defaults to "." — the checkout — so a lane that does not set this
  # starts workers whose every note submission is refused before a git command runs (G4).
  #
  # That is not a small subset of the system: the PR-gate is the one path job results, reports and
  # distilled playbooks all take (D-005), so without this the entire knowledge-contribution half
  # of a live run is unreachable. Found by running the ELN sync against a lane that lacked it.
  log "cloning a dedicated knowledge repo for the PR-gate ($NOTE_REPO_DIR)"
  mkdir -p "$(dirname "$NOTE_REPO_DIR")"
  git clone --quiet "$REPO_ROOT" "$NOTE_REPO_DIR"
}

# ---------------------------------------------------------------------------- postgres

ensure_cluster() {
  if [ -f "$PGDATA/PG_VERSION" ]; then
    log "postgres cluster already initialised ($PGDATA)"
    return
  fi
  log "initialising a postgres cluster at $PGDATA"
  # The superuser is `chemclaw` so the default DSN in `core/config/store.py`
  # (postgresql://chemclaw:chemclaw@localhost:5432/chemclaw) connects with no override at all.
  # scram-sha-256 rather than trust: the password in that DSN should be a real one, so a
  # misconfigured password is a live-lane failure here instead of a production-only surprise.
  local pwfile
  pwfile="$(mktemp)"
  printf '%s' "$PGPASSWORD_VALUE" >"$pwfile"
  chmod 0644 "$pwfile"
  install -d -o "$(stat -c %U "$(dirname "$PGDATA")")" -g "$(stat -c %G "$(dirname "$PGDATA")")" \
    "$PGDATA"
  as_postgres "$PGBIN/initdb -D '$PGDATA' -U '$PGUSER_NAME' --auth-local=trust \
      --auth-host=scram-sha-256 --pwfile='$pwfile' --encoding=UTF8 >/dev/null"
  rm -f "$pwfile"
}

# Every administrative query goes through one shape, and that shape is the application's own:
# TCP, the application's role, the application's password. Using the trusted local socket instead
# would be shorter and would stop testing the credential the app actually presents.
run_sql() {
  local database="$1" statement="$2"
  as_postgres "PGPASSWORD='$PGPASSWORD_VALUE' $PGBIN/psql -h 127.0.0.1 -p $PGPORT \
      -U '$PGUSER_NAME' -d '$database' -tAqc \"$statement\""
}

start_postgres() {
  if pg_isready -h 127.0.0.1 -p "$PGPORT" >/dev/null 2>&1; then
    log "postgres already accepting connections on $PGPORT"
  else
    mkdir -p "$LIVE_DIR"
    : >"$LIVE_DIR/postgres.log"
    chmod 0666 "$LIVE_DIR/postgres.log"
    as_postgres "$PGBIN/pg_ctl -D '$PGDATA' -l '$LIVE_DIR/postgres.log' \
        -o '-p $PGPORT -c listen_addresses=127.0.0.1 -c unix_socket_directories=/var/run/postgresql' \
        -w start >/dev/null"
  fi
  if [ "$(run_sql postgres "select count(*) from pg_database where datname='$PGDB_NAME'")" = "0" ]; then
    as_postgres "PGPASSWORD='$PGPASSWORD_VALUE' $PGBIN/createdb -h 127.0.0.1 -p $PGPORT \
        -U '$PGUSER_NAME' '$PGDB_NAME'"
  fi
  # Created here rather than left to migration 002, so a missing pgvector fails while the message
  # can still say what to install — inside `make db-migrate` it would surface as a DDL error.
  run_sql "$PGDB_NAME" "create extension if not exists vector" >/dev/null
  log "postgres up on $PGPORT (pgvector $(run_sql "$PGDB_NAME" \
    "select extversion from pg_extension where extname='vector'"))"
}

stop_postgres() {
  [ -f "$PGDATA/postmaster.pid" ] || { log "postgres not running"; return; }
  as_postgres "$PGBIN/pg_ctl -D '$PGDATA' -m fast -w stop >/dev/null"
  log "postgres stopped"
}

# ---------------------------------------------------------------------------- temporal

start_temporal() {
  if temporal operator cluster health --address "127.0.0.1:$TEMPORAL_PORT" >/dev/null 2>&1; then
    log "temporal already serving on $TEMPORAL_PORT"
    return
  fi
  mkdir -p "$LIVE_DIR"
  # A file-backed dev server, not the in-memory one: workflow history has to survive a worker
  # restart for the durability claim to mean anything, and the whole point of this lane is to
  # exercise that claim rather than assert it.
  nohup temporal server start-dev \
    --ip 127.0.0.1 --port "$TEMPORAL_PORT" --ui-port "$TEMPORAL_UI_PORT" \
    --db-filename "$LIVE_DIR/temporal.db" --log-level warn \
    >"$LIVE_DIR/temporal.log" 2>&1 &
  echo $! >"$LIVE_DIR/temporal.pid"

  for _ in $(seq 1 60); do
    if temporal operator cluster health --address "127.0.0.1:$TEMPORAL_PORT" >/dev/null 2>&1; then
      log "temporal up on $TEMPORAL_PORT (UI on $TEMPORAL_UI_PORT)"
      return
    fi
    sleep 1
  done
  die "temporal did not become healthy within 60s — see $LIVE_DIR/temporal.log"
}

stop_temporal() {
  [ -f "$LIVE_DIR/temporal.pid" ] || { log "temporal not running"; return; }
  kill "$(cat "$LIVE_DIR/temporal.pid")" 2>/dev/null || true
  rm -f "$LIVE_DIR/temporal.pid"
  log "temporal stopped"
}

# ---------------------------------------------------------------------------- entrypoint

case "${1:-up}" in
  up)
    if docker_available; then
      log "docker daemon reachable — using infra/docker-compose.yml"
      exec docker compose -f "$REPO_ROOT/infra/docker-compose.yml" up -d
    fi
    log "no docker daemon — bringing the stack up natively"
    ensure_pgvector
    ensure_temporal_cli
    ensure_note_repo
    ensure_cluster
    start_postgres
    start_temporal
    log "stack ready. Next: make db-migrate && make live-up"
    ;;
  down)
    if docker_available; then
      exec docker compose -f "$REPO_ROOT/infra/docker-compose.yml" down
    fi
    stop_temporal
    stop_postgres
    ;;
  status)
    pg_isready -h 127.0.0.1 -p "$PGPORT" || true
    temporal operator cluster health --address "127.0.0.1:$TEMPORAL_PORT" || true
    ;;
  # The four verbs the chaos family needs. They are subcommands rather than inlined `pg_ctl` and
  # `kill` calls inside the harness for the reason `connector_urls` reads the dev runner instead of
  # rebuilding its port: one place knows how this stack is started, so a chaos test cannot restart
  # it differently from how it was brought up and then measure the difference.
  restart-postgres)
    stop_postgres
    start_postgres
    ;;
  stop-temporal) stop_temporal ;;
  start-temporal) start_temporal ;;
  *)
    die "usage: bootstrap.sh [up|down|status|restart-postgres|stop-temporal|start-temporal]"
    ;;
esac
