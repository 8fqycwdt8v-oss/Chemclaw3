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
    # concurrent users. CHEMCLAW_SERVICE_UVICORN_WORKERS is the knob for that. It still defaults
    # to 1, but no longer because of the turn guard: that is a leased row in `session_turns` now
    # and every process shares it (D-121). What stays per-process is capability — attachments,
    # harness todos and the admission cap — and no ingress can pin a request below the pod, so
    # replicas plus Route affinity remain the supported way to use more CPU. Passed only when
    # raised, so the default keeps today's single-process signal handling and PID 1.
    args=(--host "${CHEMCLAW_SERVICE_HOST:-0.0.0.0}" --port "${CHEMCLAW_SERVICE_PORT:-8080}")
    if [[ "${CHEMCLAW_SERVICE_UVICORN_WORKERS:-1}" -gt 1 ]]; then
      args+=(--workers "${CHEMCLAW_SERVICE_UVICORN_WORKERS}")
    fi
    exec uvicorn chemclaw.api.app:create_app --factory "${args[@]}"
    ;;
  background-worker)
    exec python -m chemclaw.durable.background_worker
    ;;
  connector-worker-*)
    # A connector bundle's own Temporal worker, for a bundle that owns durable work
    # (`src/chemclaw/connectors/<name>/worker.py`). Matched before `connector-*` so the more specific prefix wins.
    name="${component#connector-worker-}"
    exec python -m "chemclaw.connectors.${name}.worker"
    ;;
  connector-*)
    # A connector bundle's own FastAPI app (`src/chemclaw/connectors/<name>/server/app.py`). One case for every
    # connector rather than one per name: the component name carries the bundle, so adding a
    # connector needs no change to this image — which is the whole point of the connector seam.
    name="${component#connector-}"
    exec uvicorn "chemclaw.connectors.${name}.server.app:app" \
      --host "${CHEMCLAW_SERVICE_HOST:-0.0.0.0}" --port "${CHEMCLAW_SERVICE_PORT:-8080}"
    ;;
  *)
    echo "unknown CHEMCLAW_COMPONENT=${component}" >&2
    exit 64
    ;;
esac
