#!/usr/bin/env bash
# Dispatch the container to the right Chemclaw component (plan F6-T1).
#
# One image runs many roles; CHEMCLAW_COMPONENT selects which. Kept in a script (not per-image CMDs)
# so the Helm chart sets one env var per Deployment and the image stays single-target. `exec` so the
# component is PID 1 and receives SIGTERM directly for graceful shutdown on pod termination.
set -euo pipefail

component="${CHEMCLAW_COMPONENT:-service}"

case "${component}" in
  service)
    # One asyncio event loop saturates one CPU, so a multi-CPU pod served by a single process
    # leaves the rest idle — a load test measured front-door throughput flat from 10 to 50
    # concurrent users. CHEMCLAW_SERVICE_UVICORN_WORKERS is the knob for that; it defaults to 1
    # because `active_turns` (the 409 that keeps two turns from interleaving on one session) and
    # the admission semaphore are per-process, so N workers each see 1/N of the traffic. Passed
    # only when raised, so the default keeps today's single-process signal handling and PID 1.
    args=(--host "${CHEMCLAW_SERVICE_HOST:-0.0.0.0}" --port "${CHEMCLAW_SERVICE_PORT:-8080}")
    if [[ "${CHEMCLAW_SERVICE_UVICORN_WORKERS:-1}" -gt 1 ]]; then
      args+=(--workers "${CHEMCLAW_SERVICE_UVICORN_WORKERS}")
    fi
    exec uvicorn service.app:create_app --factory "${args[@]}"
    ;;
  background-worker)
    exec python -m workers.background_worker
    ;;
  connector-worker-*)
    # A connector bundle's own Temporal worker, for a bundle that owns durable work
    # (`connectors/<name>/worker.py`). Matched before `connector-*` so the more specific prefix wins.
    name="${component#connector-worker-}"
    exec python -m "connectors.${name}.worker"
    ;;
  connector-*)
    # A connector bundle's own FastAPI app (`connectors/<name>/server/app.py`). One case for every
    # connector rather than one per name: the component name carries the bundle, so adding a
    # connector needs no change to this image — which is the whole point of the connector seam.
    name="${component#connector-}"
    exec uvicorn "connectors.${name}.server.app:app" \
      --host "${CHEMCLAW_SERVICE_HOST:-0.0.0.0}" --port "${CHEMCLAW_SERVICE_PORT:-8080}"
    ;;
  *)
    echo "unknown CHEMCLAW_COMPONENT=${component}" >&2
    exit 64
    ;;
esac
