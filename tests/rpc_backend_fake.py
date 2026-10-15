"""A stand-in for a fleet backend's typed HTTP surface: `/healthz` and `POST /rpc/<tool>`.

Built from `chemclaw_contracts`: the operations are a request-model mapping (`CALC_REQUESTS`,
`RXNLABEL_REQUESTS`), the body is validated by the tool's model as the real route does, and every
route-level failure is a contract `RpcError`. It answers over a real socket (`tests.served`), so a
test sees what the wire carries: the headers each request arrived with, the TCP connection it came
over and how often `/healthz` was asked.

What it is not: the fleet's app. The kind-to-status table and the plain-text 401/413 are
transcribed from `chemclaw_contracts.rpc`'s docstring; `tests/test_backend_rpc_fleet.py` runs the
same client against the real `calc` app wherever a Chemclaw3-mcp checkout is built.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from chemclaw_contracts.rpc import RpcError, RpcErrorKind, WireRequest
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

#: The statuses `RpcError.kind` travels on (`chemclaw_contracts.rpc.RpcErrorKind`).
STATUS_OF: dict[RpcErrorKind, int] = {
    "refused": 400,
    "unknown_op": 404,
    "rejected": 421,
    "at_capacity": 503,
    "time_budget": 422,
    "failed": 500,
}


#: The statuses each kind may travel on; `rejected` is the transport guard's Content-Type (400),
#: Origin (403) and Host (421) refusals.
ALLOWED_STATUSES: dict[RpcErrorKind, frozenset[int]] = {
    "refused": frozenset({400}),
    "unknown_op": frozenset({404}),
    "rejected": frozenset({400, 403, 421}),
    "at_capacity": frozenset({503}),
    "time_budget": frozenset({422}),
    "failed": frozenset({500}),
}


def rpc_error(kind: RpcErrorKind, message: str, status: int | None = None) -> Response:
    """The route's error answer for `kind`, on its usual status unless `status` says otherwise.

    A status the real route never uses for `kind` is a mistake in the test, so it raises.
    """
    chosen = status or STATUS_OF[kind]
    if chosen not in ALLOWED_STATUSES[kind]:
        raise ValueError(f"the route never answers {kind!r} with HTTP {chosen}")
    return JSONResponse(RpcError(kind=kind, message=message).model_dump(), status_code=chosen)


@dataclass(frozen=True)
class Seen:
    """One request as the server received it."""

    tool: str
    headers: Mapping[str, str]
    body: Any
    peer_port: int


Answer = Callable[[Any], Response | dict[str, Any] | Awaitable[Response | dict[str, Any]]]


@dataclass
class RpcBackendFake:
    """The `/rpc` and `/healthz` of one backend, with the knobs a failure test turns.

    `answers` maps a tool to what it returns for a validated body (a dict is a 200). `reported`
    is the `contract_version` `/healthz` carries (`None` leaves it out).
    """

    requests: Mapping[str, type[WireRequest]]
    token: str = "rpc-fake-token"
    reported: str | None = "1.1.0"
    answers: dict[str, Answer] = field(default_factory=dict)
    seen: list[Seen] = field(default_factory=list)
    health_hits: int = 0
    # Answers every `/rpc` request with a bare 404, as a pod whose image predates the route does.
    route_missing: bool = False
    # Replaces the whole `/healthz` answer (a 5xx, a page that is not JSON) when set.
    health_override: Response | None = None

    async def _healthz(self, request: Request) -> Response:
        """Open, like the fleet's: the version a caller checks before it uses `/rpc`."""
        del request
        self.health_hits += 1
        if self.health_override is not None:
            return self.health_override
        payload: dict[str, Any] = {"status": "ok", "server": "fake"}
        if self.reported is not None:
            payload["contract_version"] = self.reported
        return JSONResponse(payload)

    async def _rpc(self, request: Request) -> Response:
        """Bearer, Content-Type, operation, model, then the answer, in the real route's order."""
        operation = request.path_params["operation"]
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return PlainTextResponse("unauthorized", status_code=401)
        if self.route_missing:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        model = self.requests.get(operation)
        if model is None:
            return rpc_error("unknown_op", f"this server serves no operation {operation!r}")
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            return rpc_error("rejected", "Invalid Content-Type header", 400)
        body = await request.json()
        client = request.scope["client"]
        self.seen.append(Seen(operation, dict(request.headers), body, client[1]))
        try:
            arguments = model.model_validate(body).wire()
        except ValidationError as exc:
            return rpc_error("refused", str(exc.error_count()) + " validation error(s)")
        answer = self.answers[operation](arguments)
        if asyncio.iscoroutine(answer):
            answer = await answer
        if isinstance(answer, Response):
            return answer
        return JSONResponse(answer)

    def app(self) -> Starlette:
        """The ASGI app to hand to `tests.served.ServerThread`."""
        return Starlette(
            routes=[
                Route("/healthz", self._healthz, methods=["GET"]),
                Route("/rpc/{operation}", self._rpc, methods=["POST"]),
            ]
        )

    def peers(self) -> set[int]:
        """The client ports requests arrived from: one per TCP connection the client opened."""
        return {seen.peer_port for seen in self.seen}
