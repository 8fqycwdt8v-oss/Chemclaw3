"""Shared pure-ASGI middleware with no framework or layer affinity (Sec-5).

`BodySizeLimit` started as the front door's own `api.app._BodySizeLimit`, closing the spooled-upload
problem there (see its docstring for the full story: Starlette's multipart parser writes an entire
body to disk before a route can refuse it). It belongs in the kernel, not in `api/`, because a
connector's `/mcp` has exactly the same defect and neither `chemclaw.api` nor `chemclaw.connectors`
may import the other's internals — `core` is the one package both layers already depend on
(`tests/test_layering.py`), and the class touches nothing framework-specific: it is `Scope`/
`Receive`/`Send` and a byte count, so it belongs wherever both callers can reach it without a new
edge in the dependency graph.
"""

import json
import logging

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from chemclaw.core.metrics_bridge import record_metric

logger = logging.getLogger(__name__)


class BodySizeLimit:
    """Refuse an oversized request body before anything reads it (413).

    The attachment route validated its size in the wrong place, and the mistake is easy to make
    because the check *was* there: `parse_attachment` refuses anything over
    `attachment_max_bytes`. But by the time a route handler runs, Starlette's multipart parser has
    already consumed the whole request body into a `SpooledTemporaryFile` — memory up to 1 MB, then
    the pod's ephemeral disk. So a 5 GB upload was written out in full and *then* refused, and the
    route's own `await file.read()` would have pulled whatever survived into RAM. The cap was a
    statement about what the parser would accept, never about what the process would ingest.

    This is the layer that can actually refuse it, because it runs before the body is touched:

    - A declared `Content-Length` over the cap is refused without reading a byte.
    - A chunked body (no `Content-Length`) is counted as it arrives and refused the moment it
      crosses, so the ceiling holds for a client that simply declines to declare a size.

    Pure ASGI and wrapping only `receive`, for the reason the front door's `_SecurityHeaders` is
    pure ASGI: a `BaseHTTPMiddleware` runs the app as a second task through a memory stream and
    turns every cancelled SSE stream into a spurious 500.

    `parse_attachment`'s own check stays. It is not redundant — it is a *different* check: this
    one bounds what the process will ingest and is transport-shaped (413), that one bounds what an
    attachment may be and is data-shaped (422), and it has a second caller (the backfill CLI)
    that never passes through here at all.
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
                    # Truncate rather than raise. An exception here surfaces *inside* whatever is
                    # reading the body, and FastAPI wraps any failure during body parsing in
                    # `HTTPException(400, "There was an error parsing the body")` — so the caller
                    # would be told their JSON was malformed when what happened is that it was too
                    # big. Ending the stream lets the app react however it likes; `_send` below is
                    # what makes the answer the truthful one.
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

        **Counted *and* said, and it used to be only counted.** A refusal here happens above the
        front door's access log — this middleware answers without ever calling down, deliberately,
        which is the whole point of refusing before the body is read — so a 413 appeared in no log
        line anywhere in this process. An operator watching `chemclaw_requests_too_large_total`
        rise had a rate and nothing to look at: not the method, not the path, not which of the two
        arms fired. `warning`, because the caller is being refused and somebody has to decide
        whether it is an attack or an attachment cap set too low; no path and no header is
        included, for the reason `_route_template` gives in `api/middleware.py` — a request line is
        attacker-controlled and the redaction filter is where that becomes a pod stall.
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
