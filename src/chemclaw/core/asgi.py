"""Shared pure-ASGI pieces with no framework or layer affinity.

Here in the kernel because both the front door and a connector's `/mcp` need them, and `api` and
`connectors` may not import each other (`tests/test_layering.py`).
"""

import json
import logging
from typing import Any

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from chemclaw.core.config import settings
from chemclaw.core.metrics_bridge import record_metric

logger = logging.getLogger(__name__)


class BodySizeLimit:
    """Refuse an oversized request body before anything reads it (413).

    Starlette's multipart parser spools the whole body to memory or disk before a route runs, so a
    route-level size check refuses only after ingesting. This refuses a declared `Content-Length`
    over
    the cap without reading a byte, and counts a chunked body as it arrives.

    Pure ASGI, wrapping only `receive`, because `BaseHTTPMiddleware` turns cancelled SSE streams
    into
    spurious 500s. `parse_attachment`'s own check stays: it bounds what an attachment may be (422)
    and
    has a caller that never passes through here.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        """Wrap `app`, refusing bodies over `max_bytes`."""
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Bound this request's body, or pass a non-HTTP scope straight through."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self._max_bytes:
            await self._refuse(send)
            return

        received = 0
        too_large = False
        answered = False

        async def _receive() -> Message:
            """Pass the body through, truncating the stream the moment it crosses the ceiling."""
            nonlocal received, too_large
            message = await receive()
            if message["type"] == "http.request" and not too_large:
                received += len(message.get("body", b""))
                if received > self._max_bytes:
                    too_large = True
                    # Truncate rather than raise: FastAPI would report an exception here as a
                    # malformed body (400).
                    # Ending the stream lets `_send` below give the truthful answer.
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        async def _send(message: Message) -> None:
            """Replace whatever the app decided to say with the 413 that is actually true."""
            nonlocal answered
            if not too_large:
                await send(message)
                return
            if message["type"] == "http.response.start" and not answered:
                answered = True
                await self._refuse(send)
            # Everything after the substituted response is dropped: the app is answering a request
            # it only saw part of, and two responses on one connection is a protocol error.

        await self._app(scope, _receive, _send)

    async def _refuse(self, send: Send) -> None:
        """Answer 413, either before the app runs or in place of what it produced.

        Counted and logged at WARNING, since this answers above the access log. No path or header is
        logged: they are attacker-controlled and would cost the redaction filter.
        """
        record_metric(lambda m: m.increment("chemclaw_requests_too_large_total"))
        logger.warning("refused a request body over the %d byte limit with 413", self._max_bytes)
        body = json.dumps(
            {"detail": f"request body exceeds the {self._max_bytes} byte limit"}
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def transport_bounds(*, concurrency: bool = True) -> dict[str, Any]:
    """The uvicorn keyword arguments that bound a connection before a route can refuse it.

    Concurrency, keep-alive and header-size limits, shared by every process that launches uvicorn
    itself (`api/mcp_face.py`, `connectors/server_entry.py`, `core/worker_http.py`); the front door
    gets the same bounds from `deploy/entrypoint.sh`. In `core` because `api` and `connectors` may
    not
    import each other.

    Args:
        concurrency: Whether to bound simultaneous connections. False for `core/worker_http.py`,
        which
            answers the kubelet probes: a liveness probe refused because the limit is full would
            restart a pod that is merely busy.
    """
    bounds: dict[str, Any] = {
        "timeout_keep_alive": settings.service_keepalive_seconds,
        "h11_max_incomplete_event_size": settings.service_max_header_bytes,
    }
    if concurrency:
        bounds["limit_concurrency"] = settings.service_max_connections
    return bounds
