#!/usr/bin/env bash
# git's `GIT_ASKPASS` program for the knowledge repo: answers git's two prompts from the environment.
#
# **One helper, shipped in the image, for every process that talks to the notes remote.** The note
# writer (`kg/git_writer.py`) fetches and pushes from the front door and the background worker, and
# `deploy/knowledge-sync.sh` clones and refreshes from the init containers and the sidecar. Only the
# second ever had a credential path: it wrote this same helper into its own container's `/tmp`,
# which no other container can see — so the writer's checkout (cloned *with* the token by
# `note-repo-init`, its remote URL deliberately carrying none) pushed with no way to authenticate,
# and every note write against a private HTTPS remote failed `could not read Username`. Measured
# against a Basic-auth smart-HTTP remote: that push exits 128 with the token in the environment and
# no `GIT_ASKPASS`, and succeeds with this script as `GIT_ASKPASS`.
#
# The token is read from the environment at prompt time and written only to git's stdin pipe. It is
# never in argv (git passes the *prompt* as `$1`, never the answer), never in a file, never in
# `.git/config` or the remote URL, and never in a log line: `core/logging.py` redacts the value of
# `CHEMCLAW_KNOWLEDGE_REPO_TOKEN` from every record, and `kg/git_writer.py`'s child environment keeps
# this one variable for exactly this reader.
#
# Armed by `deploy/entrypoint.sh` for the application components and by `knowledge-sync.sh` for the
# sync containers, each only when the token is set — so a remote that needs no credential, or one
# carrying its own in the URL, behaves exactly as before.
case "${1:-}" in
  Username*) printf '%s\n' "${CHEMCLAW_KNOWLEDGE_REPO_USER:-x-access-token}" ;;
  Password*) printf '%s\n' "${CHEMCLAW_KNOWLEDGE_REPO_TOKEN:-}" ;;
esac
