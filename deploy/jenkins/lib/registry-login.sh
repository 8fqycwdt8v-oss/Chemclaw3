#!/usr/bin/env bash
# Authenticate whichever builder this agent has against the release registry.
#
# Separate from `image.sh` because a credential's lifetime is not a build's: an agent may already
# hold a pull secret, a service-account token, or a `docker login` from an earlier stage. This is
# the one place that knows how each builder is told, and it is a no-op when no password was bound —
# an anonymous push then fails at the push with the registry's own message rather than here with a
# guess about why.
#
# Reads REGISTRY_USER / REGISTRY_PASSWORD, which is what `withCredentials(usernamePassword(...))`
# binds. Neither is ever echoed: both builders below read the password from stdin.
set -euo pipefail

registry_login() {
  local registry="${1:?usage: registry_login <registry-host[/org]>}"
  local host="${registry%%/*}"

  if [ -z "${REGISTRY_PASSWORD:-}" ]; then
    echo "no REGISTRY_PASSWORD bound — relying on the agent's existing credentials for ${host}" >&2
    return 0
  fi

  local builder
  builder="$(command -v buildah || command -v podman || command -v docker || true)"
  case "$(basename "${builder:-none}")" in
    buildah|podman)
      printf '%s' "${REGISTRY_PASSWORD}" | "${builder}" login --username "${REGISTRY_USER}" --password-stdin "${host}" >&2
      ;;
    docker)
      printf '%s' "${REGISTRY_PASSWORD}" | docker login --username "${REGISTRY_USER}" --password-stdin "${host}" >&2
      ;;
    none)
      # kaniko takes no login command: it reads ${DOCKER_CONFIG}/config.json, so write one.
      : "${DOCKER_CONFIG:=${HOME}/.docker}"
      mkdir -p "${DOCKER_CONFIG}"
      python3 - "${host}" "${REGISTRY_USER}" "${DOCKER_CONFIG}/config.json" <<'PY' >&2
import base64, json, os, sys
host, user, path = sys.argv[1], sys.argv[2], sys.argv[3]
auth = base64.b64encode(f"{user}:{os.environ['REGISTRY_PASSWORD']}".encode()).decode()
config = {}
if os.path.exists(path):
    with open(path) as handle:
        config = json.load(handle)
config.setdefault("auths", {})[host] = {"auth": auth}
with open(path, "w") as handle:
    json.dump(config, handle)
os.chmod(path, 0o600)
print(f"wrote a registry credential for {host} to {path}")
PY
      ;;
  esac
}
