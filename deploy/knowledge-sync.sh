#!/usr/bin/env bash
# Keep a pod-local checkout of the knowledge repo in step with its base branch (gap DEP-1).
#
# Why this exists: every reader resolves the knowledge graph as a plain local directory
# (`kg/graph.py`, `report/retrievers.py`, `agents/verifier.py`), so a note merged through the
# PR-gate only becomes visible to a running pod once something writes it to that pod's filesystem.
# Nothing did. This script is that something.
#
# Three modes, sharing one clone-or-refresh core so the init container and the refresh sidecar
# cannot drift:
#   checkout  — provision the full writable clone the PR-gate submitter branches from, on every pod
#               that can propose a note. Runs *first*, because the publish target is inside it.
#   once      — refresh the read replica and publish it, then exit. Used as an init container so a
#               pod never serves traffic against an empty graph.
#   loop      — `once`, then refresh every CHEMCLAW_KNOWLEDGE_SYNC_INTERVAL_SECONDS. Used as a
#               sidecar so merges reach live pods without a redeploy.
#
# The refresh is `fetch` + `reset --hard`, never `pull`: the checkout is a read-only *replica* of the
# base branch, so a fast-forward failure must not be able to leave it on a merge conflict.
#
# **Where the publish lands, and why it is inside the submitter's clone.** Every reader resolves
# `settings.knowledge_path`, which is `note_repo_dir / knowledge_dir` and nothing else — one
# property, deliberately, so "where notes are written" and "where notes are read" cannot be two
# answers (`core/config.py`, and `kg/git_submitter.py::_return_to_base`, which returns the checkout
# to the base branch precisely *because* readers share it). Publishing anywhere else does not fail;
# it silently answers with no evidence, because a missing note is not an error. So the publish
# target is `${CHEMCLAW_NOTE_REPO_DIR}/${CHEMCLAW_KNOWLEDGE_DIR}` — the directory the application
# reads — and the chart derives it from those same two settings rather than naming a second path.
#
# That makes this script the *second* writer of that tree, the submitter being the first. It
# therefore takes the submitter's own cross-process lock (`.git/chemclaw-submit.lock`, an advisory
# `flock` — see `kg/git_submitter.py`) for the duration of the publish, so an `rsync --delete` can
# never land between the submitter's `write_text` and its `git add`, and can never dirty the tree
# under a `git checkout -B`. A held lock means a submission is in flight: skip this tick and publish
# on the next one.
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
# The submitter's advisory lock file, relative to its checkout. Must match
# `kg/git_submitter.py::_LOCK_FILE_NAME` — the two are the same lock or they are no lock at all.
submit_lock=".git/chemclaw-submit.lock"

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

if [[ -z "${repo_url}" ]]; then
  # Not configured: publish the corpus the image shipped and exit success. A deployment that
  # deliberately runs without a knowledge remote (dev, or a seeded read-only corpus) must not
  # crash-loop its pods over an unset optional value.
  if [[ "${mode}" == "checkout" ]]; then
    log "CHEMCLAW_KNOWLEDGE_REPO_URL unset — no submitter clone, so no note can be proposed"
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
  publish_under_submit_lock
}

# Run `publish` holding the PR-gate submitter's checkout lock, when there is a checkout to lock.
#
# The publish target lives inside `CHEMCLAW_NOTE_REPO_DIR` (see the header), so this script and
# `kg/git_submitter.py` write one tree from two processes. The submitter already enforces
# cross-process ownership with an advisory `flock` under `.git/`; taking the same lock is what makes
# this script a well-behaved second holder rather than a race. Non-blocking on purpose: a held lock
# means a submission is running, and waiting behind a `git push` inside a 300 s tick buys nothing
# that the next tick does not.
#
# With no checkout (no remote, or a pod that submits nothing) there is no lock file and no second
# writer, so the publish runs unguarded — the same reasoning `git_submitter` uses for a dev tree.
publish_under_submit_lock() {
  local lock="${note_repo}/${submit_lock}"
  if [[ -z "${note_repo}" ]] || [[ ! -d "${note_repo}/.git" ]]; then
    publish
    return
  fi
  if ! command -v flock >/dev/null; then
    log "ERROR flock is not installed — refusing to publish into a live checkout (see deploy/Containerfile)"
    return 1
  fi
  (
    if ! flock -n 9; then
      log "WARNING a note submission holds ${lock} — publishing on the next tick"
      exit 0
    fi
    publish
  ) 9>>"${lock}"
}

publish() {
  # Publish into the directory the app reads. A plain copy (not a symlink) keeps the app's
  # stat-fingerprint cache (`kg/graph.py`) honest and keeps the read path a real directory.
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

# A full, writable clone for the PR-gate submitter (gap DEP-2) — a *different* directory from the
# shallow read replica above, because `git checkout -B note/<id>` switches the whole working tree
# and the submitter force-pushes, neither of which a hard-reset replica survives. Not shallow:
# `--force-with-lease` needs real history to reason about.
# Idempotent, so a restarted pod reuses the existing clone instead of re-cloning.
#
# This runs as the *first* init container, before the publish: `git clone` refuses a non-empty
# destination, and the publish directory lives inside this one.
provision_note_repo() {
  if [[ -z "${note_repo}" ]]; then
    log "CHEMCLAW_NOTE_REPO_DIR unset — no submitter clone provisioned"
    return 0
  fi
  if [[ -d "${note_repo}/.git" ]]; then
    log "submitter clone already present at ${note_repo}"
    git -C "${note_repo}" fetch origin "${branch}"
    return 0
  fi
  log "cloning ${branch} into submitter checkout ${note_repo}"
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
    echo "usage: chemclaw-knowledge-sync [once|loop|checkout]" >&2
    exit 64
    ;;
esac
