#!/usr/bin/env bash
# Build one OCI image and publish it, returning the **digest the registry assigned**.
#
# Why a library rather than four `docker build` lines in four Jenkinsfiles: the builder is the one
# thing about a Jenkins estate that is not knowable from a repository. OpenShift will not hand an
# agent a Docker socket, a VM agent usually has one, and a pod agent normally gets buildah or
# kaniko. All four produce the same bytes from the same Containerfile, and none of them agree on
# how you ask. So the *pipeline* names what it wants built and this decides how.
#
# The digest is the reason this returns anything at all. `deploy/helm/chemclaw/values.yaml` treats
# `image.digest` as the release knob and ignores `image.tag` when it is set, because a tag is a
# pointer and `helm rollback` to a re-pushed tag fetches bytes nobody reviewed
# (D-2026-08-01-a-tag-is-a-pointer-not-a-build, runbook §(xiv)). A pipeline that pushed a tag and
# deployed that tag would reintroduce exactly the hole the chart was built to close.
#
# Usage:  build_and_push <containerfile> <context> <image-ref> [extra build args...]
# Env:    IMAGE_BUILDER = buildah | podman | kaniko | docker   (default: autodetect)
#         REGISTRY_AUTH_FILE, DOCKER_CONFIG — whatever the agent's credential binding set.
set -euo pipefail

detect_builder() {
  if [ -n "${IMAGE_BUILDER:-}" ]; then printf '%s' "${IMAGE_BUILDER}"; return; fi
  for candidate in buildah podman docker; do
    command -v "${candidate}" >/dev/null 2>&1 && { printf '%s' "${candidate}"; return; }
  done
  [ -x /kaniko/executor ] && { printf 'kaniko'; return; }
  echo "no image builder found — set IMAGE_BUILDER or install buildah/podman/docker" >&2
  return 1
}

# Print the digest of an image reference that has already been pushed. Kept separate because the
# three builders disagree about *where* the digest appears, and reading it back from the registry
# is the only answer that cannot be a local artefact of the build.
image_digest() {
  local ref="$1" builder="$2" digestfile="$3"
  if [ -s "${digestfile}" ]; then
    tr -d '[:space:]' < "${digestfile}"
    return
  fi
  case "${builder}" in
    docker)
      # `RepoDigests` is populated by the push, not the build, so this is only valid after one.
      docker image inspect "${ref}" --format '{{ index .RepoDigests 0 }}' | sed 's/.*@//'
      ;;
    *)
      skopeo inspect --format '{{.Digest}}' "docker://${ref}" 2>/dev/null \
        || { echo "cannot read back the digest for ${ref}" >&2; return 1; }
      ;;
  esac
}

build_and_push() {
  local containerfile="$1" context="$2" ref="$3"; shift 3
  local builder digestfile
  builder="$(detect_builder)"
  digestfile="$(mktemp)"

  # Progress goes to stderr: stdout carries the digest and nothing else, because the
  # caller reads it with a command substitution.
  echo "==> building ${ref} with ${builder} (from ${containerfile})" >&2
  case "${builder}" in
    buildah|podman)
      "${builder}" build --format docker --layers -f "${containerfile}" -t "${ref}" "$@" "${context}"
      "${builder}" push --digestfile "${digestfile}" "${ref}" "docker://${ref}"
      ;;
    docker)
      docker build -f "${containerfile}" -t "${ref}" "$@" "${context}"
      docker push "${ref}"
      ;;
    kaniko)
      # kaniko builds and pushes in one pass; it has no local image afterwards, which is why the
      # verification stages in the Jenkinsfiles run against a *pulled* image rather than a built one.
      /kaniko/executor --context "${context}" --dockerfile "${containerfile}" \
        --destination "${ref}" --digest-file "${digestfile}" "$@"
      ;;
    *) echo "unknown IMAGE_BUILDER '${builder}'" >&2; return 1 ;;
  esac

  local digest
  digest="$(image_digest "${ref}" "${builder}" "${digestfile}")"
  rm -f "${digestfile}"
  case "${digest}" in
    sha256:*) : ;;
    *) echo "refusing to report '${digest}' as a digest for ${ref}" >&2; return 1 ;;
  esac
  echo "==> ${ref} @ ${digest}" >&2
  printf '%s' "${digest}"
}
