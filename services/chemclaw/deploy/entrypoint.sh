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
    exec uvicorn service.app:create_app --factory \
      --host "${CHEMCLAW_SERVICE_HOST:-0.0.0.0}" --port "${CHEMCLAW_SERVICE_PORT:-8080}"
    ;;
  hpc-worker)
    exec python -m workers.hpc_worker
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
  mcp-calc)
    exec python -m mcp_servers.calc.server
    ;;
  *)
    echo "unknown CHEMCLAW_COMPONENT=${component}" >&2
    exit 64
    ;;
esac
