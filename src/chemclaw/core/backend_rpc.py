"""The backend RPC client: typed HTTP/JSON to a `mount: backend` server over pooled connections.

The fleet's `calc` and `rxnlabel` serve every tool at `POST /rpc/<tool>` beside MCP
(`chemclaw_contracts.rpc`). A backend caller has no turn to hold an MCP session across, so this
keeps one `httpx.AsyncClient` per event loop and base URL and reuses its connections.

Invariants:
- Failures are raised as `core.mcp_session`'s exceptions, mapped from the contract's `RpcError.kind`
  to the class `invoke` raises for the same condition, with the same wording, so a caller's
  `except` chain cannot tell the transports apart.
- The pool never falls back to MCP: a server whose `/healthz` reports a `contract_version` below
  `RPC_MIN_CONTRACT_VERSION` is `RpcUnsupported`, a clear error.
- Identity headers come from `turn_identity_hook` (the MCP path's hook) and the bearer is read from
  the environment per request, so neither is fixed when the pool opens.
"""

import asyncio
import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from chemclaw_contracts.rpc import RpcError, rpc_path
from mcp import ClientSession
from pydantic import ValidationError

from chemclaw.core.call_identity import turn_identity_hook
from chemclaw.core.config import settings
from chemclaw.core.http import default_ssl_context
from chemclaw.core.mcp_session import (
    CONNECT_TIMEOUT_SECONDS,
    McpAtCapacity,
    McpConnectFailed,
    McpCredentialRefused,
    McpRequestRefused,
    McpServerFault,
    McpTimeBudget,
    bearer_from_env,
)

logger = logging.getLogger(__name__)

#: The first `contract_version` of a backend that serves `/rpc` (the fleet's contract 1.1.0).
RPC_MIN_CONTRACT_VERSION = (1, 1, 0)

# The path suffix of the MCP endpoint every backend URL setting ends in.
_MCP_SUFFIX = "/mcp"


class RpcUnsupported(Exception):
    """The server does not serve `/rpc`, so nothing ran and nothing was sent over MCP instead.

    `served` is the `contract_version` it reported, `None` when it reported none or could not be
    asked. Raised rather than falling back, so a deployment on the wrong transport is visible.
    """

    def __init__(self, base_url: str, served: str | None) -> None:
        """Record the server and the version it reported, for the caller's message."""
        super().__init__(f"{base_url} serves contract_version {served or 'none'}")
        self.base_url = base_url
        self.served = served


@dataclass
class _Pool:
    """One client for a (loop, base URL), and whether its server has passed the version check."""

    client: httpx.AsyncClient
    verified: bool = False


_POOLS: dict[tuple[asyncio.AbstractEventLoop, str], _Pool] = {}
_POOLS_LOCK = threading.Lock()


def rpc_base_url(mcp_url: str) -> str:
    """The origin-and-prefix a backend's `/rpc` and `/healthz` live under, from its MCP URL.

    The URL settings name the MCP endpoint (`http://host:8860/mcp`); the routes sit beside it, so
    the trailing `/mcp` is dropped.
    """
    parts = urlsplit(mcp_url)
    path = parts.path.rstrip("/").removesuffix(_MCP_SUFFIX)
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _build_client(base_url: str) -> httpx.AsyncClient:
    """The pooled client: no redirects, no ambient proxy, the turn's identity on every request.

    `keepalive_expiry` is below the server's keep-alive timeout so a connection is retired here
    before the server closes it, which would surface as a failed request on a reused connection.
    """
    return httpx.AsyncClient(
        base_url=base_url,
        limits=httpx.Limits(
            max_connections=None,
            max_keepalive_connections=None,
            keepalive_expiry=settings.backend_rpc_keepalive_seconds,
        ),
        verify=default_ssl_context(),
        trust_env=False,
        follow_redirects=False,
        event_hooks={"request": [turn_identity_hook(base_url)]},
    )


def _pool_for(base_url: str) -> _Pool:
    """This loop's pool for `base_url`, opened on first use.

    Pools of loops that have ended are dropped on the way: their sockets belong to a loop that can
    no longer close them.
    """
    key = (asyncio.get_running_loop(), base_url)
    with _POOLS_LOCK:
        for stale in [k for k in _POOLS if k[0].is_closed()]:
            del _POOLS[stale]
        pool = _POOLS.get(key)
        if pool is None:
            pool = _POOLS[key] = _Pool(_build_client(base_url))
        return pool


def open_pool_count() -> int:
    """How many pools this process holds across loops, for tests and diagnostics."""
    with _POOLS_LOCK:
        return len(_POOLS)


async def close_pools_of_this_loop() -> None:
    """Close and forget the pools the running loop opened, before the loop ends."""
    here = asyncio.get_running_loop()
    with _POOLS_LOCK:
        mine = [key for key in _POOLS if key[0] is here]
        pools = [_POOLS.pop(key) for key in mine]
    for pool in pools:
        await pool.client.aclose()


@asynccontextmanager
async def pooling() -> AsyncIterator[None]:
    """Close this loop's backend pools when the block ends.

    Entered once per process beside `db.pooling()` (front door lifespan, connector servers, worker
    entrypoints). Pools open lazily on first use, so entering it costs nothing.
    """
    try:
        yield
    finally:
        await close_pools_of_this_loop()


def _version_tuple(text: object) -> tuple[int, ...] | None:
    """`MAJOR.MINOR.PATCH` as integers, or `None` for anything else."""
    if not isinstance(text, str):
        return None
    try:
        return tuple(int(part) for part in text.split("."))
    except ValueError:
        return None


def _tool_text(tool: str, message: str) -> str:
    """`message` with the lead the MCP transport puts on a tool's own failure."""
    return f"Error executing tool {tool}: {message}"


def _rpc_error(response: httpx.Response) -> RpcError | None:
    """The `RpcError` the route answered with, or `None` for a body that is not one."""
    try:
        return RpcError.model_validate_json(response.content)
    except (ValidationError, ValueError):
        return None


@dataclass(frozen=True)
class RpcSession:
    """One caller's handle on a backend: where it is, the bearer's variable, the read bound.

    Holds no connection. The pooled client behind `base_url` serves every handle, so a handle is
    cheap to make per call and the identity headers, read per request, are the calling turn's.
    """

    base_url: str
    token_env: str
    timeout_seconds: float

    def _timeout(self) -> httpx.Timeout:
        """The connect bound is short however long the read bound is, as on the MCP path."""
        return httpx.Timeout(self.timeout_seconds, connect=CONNECT_TIMEOUT_SECONDS)

    async def _verified(self, pool: _Pool) -> None:
        """Ask `/healthz` once per pool whether the server serves `/rpc`, or raise why not.

        A transport failure is `McpConnectFailed` (nothing ran). A failed check is not remembered,
        so a rollout that finishes needs no restart here.
        """
        if pool.verified:
            return
        try:
            response = await pool.client.get("/healthz", timeout=self._timeout())
        except httpx.TransportError as exc:
            raise McpConnectFailed(self.base_url) from exc
        served: object = None
        try:
            body = response.json()
            served = body.get("contract_version") if isinstance(body, dict) else None
        except ValueError:
            pass
        parsed = _version_tuple(served)
        if parsed is None or parsed < RPC_MIN_CONTRACT_VERSION:
            raise RpcUnsupported(self.base_url, served if isinstance(served, str) else None)
        pool.verified = True

    async def invoke(self, tool: str, arguments: dict[str, Any]) -> Any:
        """Call one tool and return its decoded JSON, or raise as `core.mcp_session.invoke` would.

        `None` means the tool answered with an empty body.
        """
        pool = _pool_for(self.base_url)
        await self._verified(pool)
        token = bearer_from_env(self.token_env)
        try:
            response = await pool.client.post(
                rpc_path(tool),
                json=arguments,
                headers={"Authorization": f"Bearer {token}"} if token else None,
                timeout=self._timeout(),
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise McpConnectFailed(self.base_url) from exc
        except httpx.TransportError as exc:
            # A read bound that expired, a reset connection, a half-sent answer: the call may have
            # run, so it is the server stopping, as the MCP path's timeout and transport errors are.
            raise McpServerFault(tool) from exc
        return self._decoded(tool, response, pool)

    def _decoded(self, tool: str, response: httpx.Response, pool: _Pool) -> Any:
        """The 200 body as JSON, or the exception its `RpcError` (or its absence) stands for."""
        if response.status_code == httpx.codes.OK:
            text = response.text
            if not text.strip():
                return None
            try:
                return response.json()
            except ValueError as exc:
                raise McpRequestRefused(f"{tool} returned no JSON: {text[:200]}") from exc
        error = _rpc_error(response)
        if error is None:
            return self._unstructured(tool, response, pool)
        match error.kind:
            case "at_capacity":
                raise McpAtCapacity(f"{tool} was refused: {_tool_text(tool, error.message)}")
            case "time_budget":
                raise McpTimeBudget(f"{tool} was stopped: {_tool_text(tool, error.message)}")
            case "failed":
                raise McpServerFault(tool, internal=True)
            case "unknown_op":
                raise McpRequestRefused(f"{tool} failed: Unknown tool: {tool}")
            case "rejected":
                # Host, Origin or Content-Type refused by the server's transport guard: a
                # misconfiguration an operator fixes (`MCP_ALLOWED_HOSTS`), not an input fault. The
                # MCP path reads the same refusal as an unreachable server, or a refused credential
                # for a 403.
                logger.warning(
                    "%s rejected %s before it ran (HTTP %d): %s",
                    self.base_url,
                    tool,
                    response.status_code,
                    error.message,
                )
                if response.status_code == httpx.codes.FORBIDDEN:
                    raise McpCredentialRefused(response.status_code)
                raise McpConnectFailed(self.base_url)
            case _:
                raise McpRequestRefused(f"{tool} failed: {_tool_text(tool, error.message)}")

    def _unstructured(self, tool: str, response: httpx.Response, pool: _Pool) -> Any:
        """The exception for an answer that is not an `RpcError`: the layers below the route.

        401 and 413 are plain text from the bearer and body-cap middleware; a 404 or 405 is a pod
        whose image predates `/rpc` (it answers the version check, so the pool is re-asked).
        """
        status = response.status_code
        if status in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
            raise McpCredentialRefused(status)
        if status in (httpx.codes.NOT_FOUND, httpx.codes.METHOD_NOT_ALLOWED):
            pool.verified = False
            raise RpcUnsupported(self.base_url, None)
        if status == httpx.codes.REQUEST_ENTITY_TOO_LARGE:
            raise McpRequestRefused(f"{tool} failed: the request was larger than the server takes")
        if status >= httpx.codes.INTERNAL_SERVER_ERROR:
            raise McpServerFault(tool)
        raise McpRequestRefused(f"{tool} failed: HTTP {status} {response.text[:200]}")


#: What a call runs on: an MCP session (opened for it) or a handle on the pooled RPC client.
BackendSession = ClientSession | RpcSession


@asynccontextmanager
async def rpc_session(
    url: str, *, token_env: str, timeout_seconds: float
) -> AsyncIterator[RpcSession]:
    """A handle on the pooled client for the backend whose MCP URL is `url`.

    The counterpart of `core.mcp_session.open_session` for `calc_transport="rpc"`. Nothing is opened
    here, so a connection failure is raised by the first call, from inside the caller's block, as
    the same exceptions `open_session` raises on entry.
    """
    yield RpcSession(rpc_base_url(url), token_env, timeout_seconds)
