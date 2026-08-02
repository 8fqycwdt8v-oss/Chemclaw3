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
    # concurrent users. `CHEMCLAW_SERVICE_UVICORN_WORKERS` was the knob for that, and it is now
    # refused above 1 by `Settings` validation: five per-process guarantees (the rate limiter, the
    # budget tracker, the attachment store, the live-session LRU and the metrics registry) each
    # break silently across processes, and the turn guard being durable (D-121) fixed only the
    # sixth. Replicas plus Route affinity remain the supported way to use more CPU. No `--workers`
    # flag is passed here: the setting is refused where it is read, so passing it would only turn
    # one clear startup error into N of them.
    args=(--host "${CHEMCLAW_SERVICE_HOST:-0.0.0.0}" --port "${CHEMCLAW_SERVICE_PORT:-8080}")
    # Transport-level bounds, none of which the application can impose on itself: by the time a
    # request reaches an ASGI app, uvicorn has already accepted the connection and parsed the
    # headers. Every one is a way to exhaust the process without ever sending a valid request.
    #
    #   --limit-concurrency        Connections, not turns. `service_max_concurrent_turns` bounds
    #                              what may hit the LLM; nothing bounded how many sockets could be
    #                              open waiting for a permit or holding an SSE stream. Well above
    #                              the turn cap on purpose — this is the backstop, not the policy.
    #   --timeout-keep-alive       An idle keep-alive connection held a slot indefinitely.
    #   --h11-max-incomplete-event-size  The header/request-line ceiling. Without it a client can
    #                              dribble an unbounded header block and grow the parse buffer for
    #                              as long as it likes (the classic slowloris shape).
    #
    # `_BodySizeLimit` covers the request *body*; these cover everything before it.
    args+=(--limit-concurrency "${CHEMCLAW_SERVICE_MAX_CONNECTIONS:-256}")
    args+=(--timeout-keep-alive "${CHEMCLAW_SERVICE_KEEPALIVE_SECONDS:-15}")
    args+=(--h11-max-incomplete-event-size "${CHEMCLAW_SERVICE_MAX_HEADER_BYTES:-32768}")
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
