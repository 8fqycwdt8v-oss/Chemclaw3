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
#   once      — clone (or hard-refresh) the read replica and exit. Used as an init container so a
#               pod never serves traffic against an empty graph.
#   loop      — `once`, then refresh every CHEMCLAW_KNOWLEDGE_SYNC_INTERVAL_SECONDS. Used as a
#               sidecar so merges reach live pods without a redeploy.
#   checkout  — provision the background worker's full writable clone for the PR-gate submitter.
#
# The refresh is `fetch` + `reset --hard`, never `pull`: the checkout is a read-only *replica* of the
# base branch, so a fast-forward failure must not be able to leave it on a merge conflict. The
# submitter's own clone (CHEMCLAW_NOTE_REPO_DIR on the background worker) is a different directory
# and is never touched here — it branches and force-pushes, which is incompatible with a replica.
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
publish_dir="${CHEMCLAW_KNOWLEDGE_PUBLISH_DIR:-/app/knowledge}"
interval="${CHEMCLAW_KNOWLEDGE_SYNC_INTERVAL_SECONDS:-300}"

log() { printf '%s knowledge-sync: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

if [[ -z "${repo_url}" ]]; then
  # Not configured: leave whatever the image shipped in place and exit success. A deployment that
  # deliberately runs without a knowledge remote (dev, or a seeded read-only corpus) must not
  # crash-loop its pods over an unset optional value.
  log "CHEMCLAW_KNOWLEDGE_REPO_URL unset — leaving ${publish_dir} as shipped"
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
# read replica above, because `git checkout -B note/<id>` switches the whole working tree and the
# submitter force-pushes. Not shallow: `--force-with-lease` needs real history to reason about.
# Idempotent, so a restarted pod reuses the existing clone instead of re-cloning.
provision_note_repo() {
  local dir="${CHEMCLAW_NOTE_REPO_DIR:-}"
  if [[ -z "${dir}" ]]; then
    log "CHEMCLAW_NOTE_REPO_DIR unset — no submitter clone provisioned"
    return 0
  fi
  if [[ -d "${dir}/.git" ]]; then
    log "submitter clone already present at ${dir}"
    git -C "${dir}" fetch origin "${branch}"
    return 0
  fi
  log "cloning ${branch} into submitter checkout ${dir}"
  mkdir -p "$(dirname "${dir}")"
  git clone --branch "${branch}" "${repo_url}" "${dir}"
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
