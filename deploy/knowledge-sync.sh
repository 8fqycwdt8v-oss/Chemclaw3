#!/usr/bin/env bash
# Keep a pod-local checkout of the knowledge repo in step with its base branch (gap DEP-1).
#
# Why this exists: every reader resolves the knowledge graph as a plain local directory
# (`kg/graph.py`, `report/retrievers.py`, `agents/verifier.py`), so a note another pod or a person
# pushed only becomes visible to a running pod once something writes it to that pod's filesystem.
# Nothing did. This script is that something.
#
# Three modes, sharing one clone-or-refresh core so the init container and the refresh sidecar
# cannot drift:
#   checkout  — provision the full writable clone `kg/git_writer.py` commits into, on every pod
#               that can record a note. Runs *first*, because the publish target is inside it.
#   once      — refresh the read replica and publish it, then exit. Used as an init container so a
#               pod never serves traffic against an empty graph.
#   loop      — `once`, then refresh every CHEMCLAW_KNOWLEDGE_SYNC_INTERVAL_SECONDS. Used as a
#               sidecar so merges reach live pods without a redeploy.
#   staleness — exit non-zero when the last *successful* refresh is older than the given number of
#               seconds. The sidecar's liveness probe, and the only thing that makes a wedged loop
#               visible from outside the pod; see `heartbeat` below.
#
# The refresh is `fetch` + `reset --hard`, never `pull`: the checkout is a read-only *replica* of the
# base branch, so a fast-forward failure must not be able to leave it on a merge conflict.
#
# **Where the refresh lands, and why it is inside the writer's clone.** Every reader resolves
# `settings.knowledge_path`, which is `note_repo_dir / knowledge_dir` and nothing else — one
# property, deliberately, so "where notes are written" and "where notes are read" cannot be two
# answers (`core/config/`). Refreshing anywhere else does not fail; it silently answers with no
# evidence, because a missing note is not an error. So the target is
# `${CHEMCLAW_NOTE_REPO_DIR}/${CHEMCLAW_KNOWLEDGE_DIR}` — the directory the application reads — and
# the chart derives it from those same two settings rather than naming a second path.
#
# **That tree has two writers now, and `rsync --delete` was the wrong instrument for it.** Until
# `D-2026-09-05-the-gate-follows-behaviour-not-knowledge` the note writer committed inside a private
# worktree and never touched this directory, so publishing a read replica over it with `--delete`
# could only remove notes that had genuinely left the base branch. The writer commits *here* now.
# A note whose push failed is committed locally and is not on the remote — the intended behaviour,
# asserted by `tests/test_knowledge.py` — so the next tick's `--delete` deleted it from the tree
# every reader scans, permanently and silently: it stays in the local `HEAD`, so no later
# path-limited `git add` ever restores it.
#
# So where there is a writer's clone, the refresh **is** that clone's own fast-forward
# (`refresh_note_repo`): `git fetch` + `merge --ff-only`, exactly what `kg/git_writer.py` does
# before each write. Remote notes arrive, local commits survive, and nothing deletes a file. The
# shallow replica and its `rsync` remain for the case they were always right for — a pod that
# records nothing and therefore has no clone to fast-forward.
#
# Both forms take the writer's cross-process lock (`.git/chemclaw-submit.lock`, an advisory
# `flock` — see `kg/git_writer.py`), because both run git in a checkout the writer also runs git
# in. A held lock means a write is in flight: skip this tick and refresh on the next one.
#
# The token is delivered through a credential helper rather than baked into the remote URL, so it
# never lands in `.git/config`, in `git remote -v`, or in any log line this script emits.
set -euo pipefail

mode="${1:-once}"
repo_url="${CHEMCLAW_KNOWLEDGE_REPO_URL:-}"
target="${CHEMCLAW_KNOWLEDGE_SYNC_DIR:-/app/knowledge-repo}"
branch="${CHEMCLAW_NOTE_BASE_BRANCH:-main}"
# Where the notes actually live inside the repo; must match CHEMCLAW_KNOWLEDGE_DIR, because that is
# the path the application reads.
notes_subdir="${CHEMCLAW_KNOWLEDGE_DIR:-knowledge}"
note_repo="${CHEMCLAW_NOTE_REPO_DIR:-}"
publish_dir="${CHEMCLAW_KNOWLEDGE_PUBLISH_DIR:-/var/lib/chemclaw/note-repo/${notes_subdir}}"
interval="${CHEMCLAW_KNOWLEDGE_SYNC_INTERVAL_SECONDS:-300}"
# The corpus baked into the image (`Containerfile`: WORKDIR /app, `COPY knowledge ./knowledge`).
# Only ever *copied from*, never written to.
seed_dir="/app/${notes_subdir}"
# The note writer's advisory lock file, relative to its checkout. Must match
# `kg/git_writer.py::_LOCK_FILE_NAME` — the two are the same lock or they are no lock at all.
submit_lock=".git/chemclaw-submit.lock"
# The last time a refresh actually completed, as a file whose mtime is the answer.
#
# **This exists because a wedged sidecar was invisible.** `loop` catches a failing refresh so a dead
# remote cannot kill the pod — correct, and the pod then serves the previous snapshot indefinitely
# while logging one WARNING per interval into a stream nobody tails. On an expired push credential
# (the exact cause `templates/prometheusrule.yaml` names for `ChemclawKnowledgeNotesLost`) the graph
# silently stops moving: `ChemclawKnowledgeNotesLost` covers notes going *out* and nothing covered
# the graph coming *in*. There is no counter for it either, because this is a shell script in a
# sidecar with no registry to increment.
#
# A file's mtime is what a container *can* publish with no listener and no library, and the
# `staleness` mode below is what turns it into a probe. It is the degraded half of the real fix,
# which is a `chemclaw_knowledge_sync_age_seconds` gauge the reading process exposes — see
# `docs/planning/BACKLOG.md`. Written on success only: a refresh that failed must not look recent.
#
# In `/tmp` and deliberately not inside `${target}`: the refresh runs `git clean -fd`, which deletes
# untracked files, so a heartbeat living in the checkout would be removed at the *start* of every
# tick and a refresh that then failed would read as "never succeeded" rather than as "last succeeded
# an interval ago". Per-container state is also the right lifetime — the probe runs in this
# container, and a restarted one has genuinely not refreshed yet.
heartbeat="${CHEMCLAW_KNOWLEDGE_SYNC_HEARTBEAT:-/tmp/chemclaw-knowledge-sync.heartbeat}"

log() { printf '%s knowledge-sync: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# Fill an empty publish directory from the corpus the image shipped.
#
# Why this exists: with no remote configured the publish directory is an empty volume, and every
# reader then resolves a path that does not exist. `rglob` over a missing directory yields nothing
# and raises nothing, so the agent answers with zero knowledge-graph evidence and says so nowhere —
# the same silent-empty-graph failure the rsync fallback used to cause, arrived at from the other
# direction. `values.yaml` has always claimed an empty `repoUrl` "runs against whatever corpus the
# image shipped"; this is what makes that true.
#
# Only when the directory is empty: a configured deployment's real corpus must never be overwritten
# by the seed, and a restart must not resurrect notes a merge deleted.
seed_from_image() {
  if [[ -d "${publish_dir}" ]] && [[ -n "$(ls -A "${publish_dir}" 2>/dev/null)" ]]; then
    log "${publish_dir} is already populated — not seeding"
    return 0
  fi
  if [[ ! -d "${seed_dir}" ]]; then
    log "WARNING no corpus at ${seed_dir} and no remote configured — ${publish_dir} stays empty"
    return 0
  fi
  mkdir -p "${publish_dir}"
  cp -a "${seed_dir}/." "${publish_dir}/"
  log "seeded ${publish_dir} from the image corpus ($(find "${publish_dir}" -name '*.md' | wc -l) notes)"
}

# Exit 0 when the last successful refresh is younger than `$1` seconds, non-zero otherwise.
#
# This is the whole probe: the kubelet's `exec` liveness on the sidecar calls it, so a sidecar that
# has stopped refreshing becomes a *restarting container* — a restart count and a `Warning` event —
# instead of a quiet WARNING loop nobody tails. A restart does not repair an expired credential and
# is not meant to; what it buys is that the failure stops being indistinguishable from health.
#
# Deliberately *not* wired to readiness. A sidecar's readiness is the pod's readiness, so a stale
# graph would take the front door out of its Service — and serving a chemist an answer from a
# three-hour-old corpus is better than serving them a connection error.
#
# Dispatched here, above the no-remote branch below, for two reasons: it reads one file's mtime and
# needs none of the git setup, and that branch re-seeds the image corpus on every call — which on a
# probe schedule would rewrite a shared volume every interval forever.
if [[ "${mode}" == "staleness" ]]; then
  max="${2:-0}"
  if [[ -z "${repo_url}" ]]; then
    # Nothing refreshes, so nothing can be stale. Reported rather than silently passed, because a
    # probe that always succeeds should say which of the two reasons it succeeded for.
    log "CHEMCLAW_KNOWLEDGE_REPO_URL unset — no refresh loop to be stale"
    exit 0
  fi
  if [[ ! -f "${heartbeat}" ]]; then
    log "ERROR no successful refresh has been recorded at ${heartbeat}"
    exit 1
  fi
  age=$(( $(date -u +%s) - $(date -u -r "${heartbeat}" +%s) ))
  if (( age > max )); then
    log "ERROR last successful refresh was ${age}s ago, over the ${max}s budget"
    exit 1
  fi
  log "last successful refresh was ${age}s ago"
  exit 0
fi

if [[ -z "${repo_url}" ]]; then
  # Not configured: publish the corpus the image shipped and exit success. A deployment that
  # deliberately runs without a knowledge remote (dev, or a seeded read-only corpus) must not
  # crash-loop its pods over an unset optional value.
  if [[ "${mode}" == "checkout" ]]; then
    log "CHEMCLAW_KNOWLEDGE_REPO_URL unset — no writer clone, so no note can be recorded"
  else
    log "CHEMCLAW_KNOWLEDGE_REPO_URL unset — publishing the image corpus into ${publish_dir}"
    seed_from_image
  fi
  # A sidecar must stay alive (a completed container would restart forever); every other mode is
  # a one-shot. Written as an `if`, not `[[ ]] && …`, because under `set -e` a false test would
  # itself be a failing top-level command and exit non-zero — crash-looping the init container.
  if [[ "${mode}" == "loop" ]]; then
    exec sleep infinity
  fi
  exit 0
fi

# Credential helper: prints the token on demand, so it is never persisted or echoed.
if [[ -n "${CHEMCLAW_KNOWLEDGE_REPO_TOKEN:-}" ]]; then
  export GIT_ASKPASS=/tmp/chemclaw-askpass
  cat >"${GIT_ASKPASS}" <<'ASKPASS'
#!/usr/bin/env bash
case "$1" in
  Username*) echo "${CHEMCLAW_KNOWLEDGE_REPO_USER:-x-access-token}" ;;
  Password*) echo "${CHEMCLAW_KNOWLEDGE_REPO_TOKEN}" ;;
esac
ASKPASS
  chmod 700 "${GIT_ASKPASS}"
fi
# Never block on an interactive prompt: a bad credential must fail fast and loudly, not hang the
# init container until the pod's startup probe gives up.
export GIT_TERMINAL_PROMPT=0

refresh() {
  if [[ ! -d "${target}/.git" ]]; then
    log "cloning ${branch} into ${target}"
    rm -rf "${target}"
    git clone --depth 1 --branch "${branch}" "${repo_url}" "${target}"
  else
    git -C "${target}" fetch --depth 1 origin "${branch}"
    git -C "${target}" reset --hard "origin/${branch}"
    git -C "${target}" clean -fd
  fi
  refresh_under_write_lock
  # After the refresh, not before it: what this timestamp claims is that the tree readers resolve
  # was brought up to date, and a fetch whose refresh then failed brought nothing up to date.
  : > "${heartbeat}"
}

# Bring the tree readers scan up to date, holding the note writer's checkout lock.
#
# The target lives inside `CHEMCLAW_NOTE_REPO_DIR` (see the header), so this script and
# `kg/git_writer.py` operate on one tree from two processes. The writer already enforces
# cross-process ownership with an advisory `flock` under `.git/`; taking the same lock is what makes
# this script a well-behaved second holder rather than a race. Non-blocking on purpose: a held lock
# means a write is running, and waiting behind a `git push` inside a 300 s tick buys nothing that
# the next tick does not.
#
# With no checkout (no remote, or a pod that records nothing) there is no lock file and no second
# writer, so the replica publish runs unguarded — the same reasoning `git_writer` uses for a dev
# tree.
refresh_under_write_lock() {
  local lock="${note_repo}/${submit_lock}"
  if [[ -z "${note_repo}" ]] || [[ ! -d "${note_repo}/.git" ]]; then
    publish
    return
  fi
  if ! command -v flock >/dev/null; then
    log "ERROR flock is not installed — refusing to touch a live checkout (see deploy/Containerfile)"
    return 1
  fi
  (
    if ! flock -n 9; then
      log "WARNING a note write holds ${lock} — refreshing on the next tick"
      exit 0
    fi
    refresh_note_repo
  ) 9>>"${lock}"
}


# Fast-forward the writer's own checkout onto the base branch — the refresh where one exists.
#
# `--ff-only`, never a merge, a rebase or a `reset --hard`. A divergence here means this pod holds
# a commit the remote does not, which is a note whose push failed: a merge would invent a commit
# nobody wrote, a rebase would move a commit this script did not author, and a hard reset would
# delete the note outright — the very failure this function replaced.
#
# **A divergence is a warning, not an error, and that distinction is load-bearing.** Returning
# non-zero here fails `once`, which is an init container: the pod would crash-loop on a stranded
# note rather than serve it. Resolving the divergence belongs to `kg/git_writer.py`, which replays
# its own unpushed commits on the next write; this script keeps serving what the pod holds until
# then.
refresh_note_repo() {
  if ! git -C "${note_repo}" fetch origin "${branch}"; then
    log "WARNING could not fetch ${branch} into ${note_repo} — serving the previous snapshot"
    return 1
  fi
  if ! git -C "${note_repo}" merge --ff-only "origin/${branch}"; then
    log "WARNING ${note_repo} holds a commit origin/${branch} does not — a note whose push failed. Serving what it holds; the next successful note write replays it and remote notes resume."
    return 0
  fi
  log "refreshed ${note_repo} to $(git -C "${note_repo}" rev-parse --short HEAD) ($(find "${note_repo}/${notes_subdir}" -name '*.md' 2>/dev/null | wc -l) notes)"
}

# Publish the shallow read replica into the directory the app reads.
#
# **Only where there is no writer's clone.** With one, `refresh_note_repo` above does the job
# without deleting anything; `--delete` here would remove a locally-committed note that has not
# reached the remote. Reached only through `refresh_under_write_lock`, which makes that choice.
publish() {
  # A plain copy (not a symlink) keeps the app's stat-fingerprint cache (`kg/graph.py`) honest and
  # keeps the read path a real directory.
  #
  # `rsync -a --delete` is the only acceptable mechanism here, and the reason is the failure this
  # replaced. The previous form swallowed rsync's stderr and fell back to
  # `rm -rf "${publish_dir}"/* && cp -a`. The image never installed rsync, so `command not found`
  # took the fallback — meaning every sync interval emptied and refilled the directory the serving
  # container was reading, and a retrieval landing in that window returned a partial or empty graph
  # with no error anywhere (a missing note is not a failure, it is just less evidence).
  #
  # So: rsync is required, checked for by name, and its absence is a loud failure rather than a
  # quiet deletion. rsync is also what makes the window small in the good case — it writes only the
  # delta, where a wholesale copy rewrites the entire corpus on every tick. stderr is no longer
  # discarded, because "the transfer failed" and "the tool is missing" must not look alike again.
  #
  # Failing (rather than falling back) is safe in both callers by design: `once` fails the init
  # container, so a pod never serves against a half-published tree, and `loop` logs a warning and
  # keeps serving the previous good snapshot. Neither path can destroy what is already published.
  mkdir -p "${publish_dir}"
  if [[ -d "${target}/${notes_subdir}" ]]; then
    if ! command -v rsync >/dev/null 2>&1; then
      log "ERROR rsync is not installed — refusing to publish (see deploy/Containerfile)"
      return 1
    fi
    rsync -a --delete "${target}/${notes_subdir}/" "${publish_dir}/"
    log "published $(find "${publish_dir}" -name '*.md' | wc -l) notes at $(git -C "${target}" rev-parse --short HEAD)"
  else
    log "WARNING ${notes_subdir}/ absent in ${repo_url}@${branch} — nothing published"
  fi
}

# A full, writable clone for the note writer (gap DEP-2) — a *different* directory from the shallow
# read replica above, because the writer commits and pushes, which a hard-reset replica does not
# survive. Not shallow: a fast-forward needs real history to reason about.
# Idempotent, so a restarted pod reuses the existing clone instead of re-cloning.
#
# This runs as the *first* init container, before the refresh: `git clone` refuses a non-empty
# destination, and the directory readers scan lives inside this one.
provision_note_repo() {
  if [[ -z "${note_repo}" ]]; then
    log "CHEMCLAW_NOTE_REPO_DIR unset — no writer clone provisioned"
    return 0
  fi
  if [[ -d "${note_repo}/.git" ]]; then
    log "writer clone already present at ${note_repo}"
    git -C "${note_repo}" fetch origin "${branch}"
    return 0
  fi
  log "cloning ${branch} into writer checkout ${note_repo}"
  mkdir -p "$(dirname "${note_repo}")"
  git clone --branch "${branch}" "${repo_url}" "${note_repo}"
}

case "${mode}" in
  once) refresh ;;
  checkout) provision_note_repo ;;
  loop)
    refresh
    while true; do
      sleep "${interval}"
      # A transient remote failure must not kill the sidecar and take the pod with it; the pod keeps
      # serving the last good corpus and the next tick retries.
      refresh || log "WARNING refresh failed; serving the previous snapshot"
    done
    ;;
  *)
    echo "usage: chemclaw-knowledge-sync [once|loop|checkout|staleness <seconds>]" >&2
    exit 64
    ;;
esac
