"""The connector-side runtime: wrap a FastMCP capability as the FastAPI app a connector serves.

Every connector we own is the same shape — a FastAPI application exposing `/healthz` for the startup
probe, `/metrics` for the scrape, and `/mcp` for the MCP streamable-HTTP transport, over a `FastMCP`
instance holding the capability's tools. That shape is written once here so a new connector's
`connectors/<name>/server/app.py` is three lines, and so the two cross-cutting behaviors it needs
cannot be forgotten per connector:

- **Running the MCP session manager.** `FastMCP.streamable_http_app()` returns a Starlette app whose
  *own* lifespan starts the session manager; mounting that app inside FastAPI does not run a
  sub-app's lifespan, so the parent must drive it explicitly. Getting this wrong produces a server
  that accepts connections and then hangs on the first request — the failure this helper exists to
  make impossible.
- **Logging the calling identity.** The `X-Chemclaw-*` headers arrive on every request
  (`chemclaw.connectors.identity`); logging the actor and session here is what lets a connector's
  own records
  be reconciled with the core audit trail, which is the whole point of sending them. It is logged,
  never trusted: authorization happened in core before the call was made, and a header on a request
  is not evidence of anything a connector should act on.
"""

import asyncio
import functools
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from hmac import compare_digest
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.lowlevel.server import request_ctx
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from chemclaw.connectors.caller import bind_caller, reset_caller
from chemclaw.connectors.identity import (
    HEADER_ACTOR,
    HEADER_CORRELATION,
    HEADER_DRY_RUN,
    HEADER_SESSION,
)
from chemclaw.core import db
from chemclaw.core.asgi import BodySizeLimit
from chemclaw.core.config import settings
from chemclaw.core.metrics import CONTENT_TYPE, METRICS
from chemclaw.core.tracing import continue_trace

logger = logging.getLogger(__name__)


# The sentinel `_declared_bearer_env` returns when it cannot find out what this bundle requires.
# No environment variable has this name, so `os.environ.get` yields `""` and the middleware refuses
# every request — a connector that cannot read its own manifest serves nothing rather than
# everything.
_UNRESOLVED_AUTH = "CHEMCLAW_CONNECTOR_AUTH_UNRESOLVED"


def _declared_bearer_env(name: str) -> str | None:
    """The env var holding this bundle's bearer token, `None` for `mode: none`, or fail closed.

    Imported lazily because `connector_app` is called at import time by seven bundle modules.

    **A read failure returns the sentinel, not `None`.** The first version returned `None` — no
    middleware, whole `/mcp` surface anonymous — and justified it with "`connector-validate` checks
    the declaration separately". That justification was false: the validator has no auth check of
    any kind, and it validates the *repository's* manifest directory, not the one mounted in the
    pod. `discovered()` parses every bundle in `connectors_dirs` and raises `ConnectorError` on one
    bad YAML, so a single typo in an operator's prepended directory — the documented PATH-like
    override — would have taken every bearer-mode connector in the process unauthenticated, logging
    only that it "could not read manifests to resolve its auth mode".

    A control whose absence is decided by a file being unreadable is not a control. Failing closed
    makes the same event loud: the connector answers 401 until an operator fixes the manifest.

    **A manifest that ships and is not discovered fails closed too, and that half was missing.**
    Only `discovered()` *raising* failed closed. `discovered()` succeeding without this bundle in
    its result fell through to `return None` — "no credential required", whole surface anonymous —
    which is the identical outcome the paragraph above refuses, reached by the likelier route: a
    `connectors_dir` pointing somewhere else, or an operator's prepended override directory
    shadowing the tree. Neither raises, because a directory with no bundles parses perfectly well,
    and the deployment goes on recording the pod as credential-gated.

    **Undiscovered is not the same as undeclared, and the packaged tree is what separates them.**
    `connector_app` also serves apps no bundle backs at all — every transport and identity test
    builds one, and that is a supported construction, not a misconfiguration: nothing was declared,
    so there is no promise to betray and no token anyone could present. What distinguishes the two
    is whether a `connector.yaml` for this name ships *beside this module* — `_ships_a_manifest`.
    If one does and discovery did not find it, this process is looking at the wrong tree and must
    refuse; if none does, the app is synthetic and stays open. That check resolves against
    `__file__` rather than through the registry or `settings`, because the configured roots are
    exactly what the first case has wrong.

    **What that does not cover, stated rather than implied:** a bundle an operator ships *outside*
    this package — the documented PATH-like override — has no manifest beside this module, so a
    misconfigured registry makes it indistinguishable from a synthetic app and it stays open. There
    is no second source of truth to consult for one: its manifest lives in the same configured roots
    that are under suspicion. The shipped bundles are the ones this can speak for, and it speaks for
    them; a private bundle that wants the same guarantee has to assert its own credential.
    """
    from chemclaw.connectors.manifest import BearerAuth, HttpEndpoint
    from chemclaw.connectors.registry import discovered

    try:
        found = discovered()
    except Exception:
        logger.exception(
            "connector_auth_unresolved: connector %s could not read its manifests, so it cannot "
            "tell whether it requires a bearer token; refusing every MCP request until it can",
            name,
        )
        return _UNRESOLVED_AUTH
    for _bundle, manifest in found.values():
        if manifest.name == name and isinstance(manifest.endpoint, HttpEndpoint):
            auth = manifest.endpoint.auth
            return auth.token_env if isinstance(auth, BearerAuth) else None
    if _ships_a_manifest(name):
        logger.error(
            "connector_auth_unresolved: connector %s ships a manifest that this process did not "
            "discover, so it cannot tell whether it requires a bearer token; check connectors_dir "
            "(currently %s). Refusing every MCP request until it resolves",
            name,
            settings.connectors_dir,
        )
        return _UNRESOLVED_AUTH
    return None


def _ships_a_manifest(name: str) -> bool:
    """Whether a `connector.yaml` for `name` ships inside this package.

    The one question that separates "this deployment is pointed at the wrong tree" from "this app
    was built without a bundle behind it" — see `_declared_bearer_env`. Resolved against `__file__`
    (this module lives in the bundle root, one level above every bundle) rather than through
    `settings.connectors_dirs`, because those roots are exactly what the first case has wrong.
    """
    from chemclaw.connectors.registry import MANIFEST_FILENAME

    return (Path(__file__).parent / name / MANIFEST_FILENAME).is_file()


def _app_relative_path(request: Request) -> str:
    """This request's path *within this app*, with any mount prefix removed.

    **Not `request.url.path`, and the difference is a security boundary rather than a nicety.**
    Starlette leaves `scope["path"]` whole when it dispatches into a mounted sub-app and records
    the prefix in `root_path` — measured: a `GET /molfp/healthz` reaches a middleware inside the
    mounted app as `url.path == scope["path"] == "/molfp/healthz"`, `root_path == "/molfp"`. So a
    probe allowlist written against `/healthz` matches at the root and silently *stops* matching
    the moment the same app is mounted under a name.

    In the cluster each connector is its own Deployment serving at the root, so the allowlist held
    there. `chemclaw.cli.connectors_dev` — `make connectors`, the live lane, and the transport
    tests — mounts every bundle under `/<name>`, and there it did not: with a credential declared,
    the readiness probe `connectors.health` makes against `health_url` would have come back 401 and
    reported the whole fleet unreachable. That was invisible while every bundle we host declared
    `auth: mode: none`, because nothing was ever refused.
    """
    root = request.scope.get("root_path", "")
    path = request.url.path
    return path[len(root) :] if root and path.startswith(root) else path


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Verify the bearer token a `mode: bearer` manifest says this connector requires.

    `BearerAuth` existed only on the *sending* side: `connectors/identity.py` set an
    `Authorization` header and no connector ever read one, while `connector-validate` raised no
    objection. A deployment that followed the manifest's own advice ("bearer for everything
    in-cluster") therefore mounted a secret, believed the pod was credential-gated, and served
    every tool to anything that could reach it — a control the deployment records as enabled and
    that does not exist. Proved by completing an unauthenticated MCP handshake against the real app.

    **Middleware, not a route dependency, and that is the whole reason this was missable**: `/mcp`
    is `app.mount`ed, and a mount bypasses the enclosing app's dependencies entirely. Anything
    written as `Depends(...)` would have guarded the two routes that need it least and none of the
    surface that matters.

    `/healthz` and `/metrics` stay open, matching the front door's probe allowlist: a kubelet probe
    and a Prometheus scrape happen independently of any identity, and the exposition carries counts
    only. The MCP surface is what the credential is for.

    Comparison is `compare_digest`, and a missing/short token is refused rather than compared, so a
    misconfigured deployment fails closed instead of accepting the empty string.
    """

    def __init__(self, app: Any, *, connector: str) -> None:
        """Bind the connector name; the declared auth mode is resolved on first request."""
        super().__init__(app)
        self._connector = connector
        self._token_env: str | None = None
        self._resolved = False

    def _declared(self) -> str | None:
        """The env var this bundle's manifest names, resolved once, on first use.

        **Lazily, and that is not an optimisation.** Resolving it in `connector_app` called the
        `lru_cache`d `discovered()` at app-build time, which warmed that cache against whatever
        `connectors_dir` happened to be set to *then* — so a caller that builds an app and only
        afterwards points the registry at its own bundle (which is exactly what
        `tests/test_connector_safety_rubric.py`'s fixture does, and what any late configuration
        would do) found the registry serving stale contents. Building an app is not a moment that
        should have side effects on shared state; the first request is.

        **The fail-closed answer is not cached, and that is what makes the other promise true.**
        `_declared_bearer_env`'s docstring says the connector "answers 401 until an operator fixes
        the manifest"; latching `_resolved` on the sentinel made that "until an operator fixes the
        manifest *and* restarts the pod", because nothing would ever ask again. `discovered()` is
        cached but does not cache exceptions, so re-asking after a fix is cheap and can succeed.
        Only a *resolved* answer is worth keeping — a real env var name, or `None` for `mode: none`.
        """
        if not self._resolved:
            self._token_env = _declared_bearer_env(self._connector)
            self._resolved = self._token_env != _UNRESOLVED_AUTH
        return self._token_env

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Refuse anything but `/healthz` and `/metrics` without the configured bearer token."""
        if _app_relative_path(request) in ("/healthz", "/metrics"):
            return await call_next(request)
        token_env = self._declared()
        if token_env is None:
            return await call_next(request)
        expected = os.environ.get(token_env, "")
        presented = request.headers.get("authorization", "")
        scheme, _, offered = presented.partition(" ")
        # Compared as *bytes*. `compare_digest` on `str` requires both operands to be ASCII-only and
        # raises `TypeError` otherwise, and Starlette decodes headers as latin-1 — so a single
        # non-ASCII byte in the header turned this security boundary into a 500 with a traceback,
        # which any remote party could produce at will. The refusal must come from the branch
        # written for it, not from an exception handler upstream.
        if (
            not expected
            or scheme.lower() != "bearer"
            or not compare_digest(
                offered.strip().encode("utf-8", "surrogateescape"),
                expected.encode("utf-8", "surrogateescape"),
            )
        ):
            logger.warning(
                "connector %s refused an unauthenticated MCP request to %s",
                self._connector,
                request.url.path,
            )
            return Response(status_code=401, content="unauthorized")
        return await call_next(request)


class CallerLogMiddleware(BaseHTTPMiddleware):
    """Log the `X-Chemclaw-*` caller identity of every request, and bind it for the tools.

    Advisory only, in both roles. The headers say who core says is asking; they are recorded so a
    connector's own records and logs can be joined to the core audit trail by actor and session,
    and they are never an input to an access decision — a connector that gated on one would be
    trusting an unauthenticated string.

    Binding is what makes the second half of that sentence reachable. Logging alone let a connector
    correlate its *log lines*; a connector that writes a durable row — a persisted BO suggestion —
    had no way to stamp it with the conversation that asked for it, so the row could not be traced
    back to a chemist or a turn. `chemclaw.connectors.caller` holds the contextvars and the trust
    rule; this is where they are set and, importantly, reset.
    """

    def __init__(self, app: ASGIApp, connector: str) -> None:
        """Bind the connector's name so one log line identifies which capability was called."""
        super().__init__(app)
        self._connector = connector

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Log the caller, bind it for the duration of the request, then serve it unchanged."""
        actor = request.headers.get(HEADER_ACTOR, "")
        session = request.headers.get(HEADER_SESSION, "")
        logger.info(
            "connector %s request: path=%s actor=%s session=%s dry_run=%s",
            self._connector,
            request.url.path,
            actor or "-",
            session or "-",
            request.headers.get(HEADER_DRY_RUN, "-"),
        )
        tokens = bind_caller(actor, session, request.headers.get(HEADER_CORRELATION, ""))
        try:
            # Adopt the caller's trace, so this connector's spans are children of the turn that
            # called it rather than the root of an unrelated one. Note the asymmetry with the two
            # lines above: the `X-Chemclaw-*` identity headers are advisory and must never reach an
            # access decision, while trace context is safe to take from outside precisely because
            # it grants nothing — the worst a forged `traceparent` achieves is attaching spans to
            # someone else's trace.
            with continue_trace(request.headers):
                return await call_next(request)
        finally:
            # This used to say "each request runs in its own task context, so a `ContextVar` set
            # here is already invisible to the next one", and measurement disproved it in the
            # direction that mattered: a *tool body* does not run in this task at all, so what it
            # read was the handshake's identity rather than the call's, for the whole life of the
            # MCP session. `_bind_caller_per_tool_call` is what makes a tool see its own caller.
            # This binding stays for everything else on the request path, and the reset with it.
            reset_caller(tokens)


def _bind_caller_per_tool_call(server: FastMCP) -> None:
    """Re-bind the caller from the request the tool call is *serving*, not the one that connected.

    `CallerLogMiddleware` binds the contextvars in `dispatch`, which is an ASGI task. An MCP tool
    body does not run there: it runs in the session-manager task created by `initialize`, so the
    contextvar it reads is whatever the *handshake* set. Measured over the real streamable-HTTP
    transport — handshake carrying alice's headers, then `tools/call` carrying bob's on the same
    `mcp-session-id` — the tool body read `('alice-oid', 'sess-alice', '')`. The middleware log
    line for that call says bob, because it reads the headers directly; a durable row stamped by
    the same call said alice. The two artifacts this feature exists to reconcile disagreed.

    Not a cross-user *leak*: a second, independent MCP session showed no bleed, so the scope is
    "frozen at the handshake within one session". It is a mis-attribution, and it becomes a live
    one the moment a connection is pooled or reused across turns.

    The serving request is reachable — `request_ctx` is set per JSON-RPC message and carries the
    ASGI request — so the fix is to read it here rather than to weaken the docstrings. When there
    is no request context (a stdio transport, a tool called directly in a test) this falls through
    to whatever the middleware bound, which is today's behaviour and the right one.

    Wrapped around `_sanitize_tool_errors`'s interception of the same method rather than merged
    into it: two concerns, two functions, one patch point each.
    """
    manager = server._tool_manager
    wrapped_call_tool = manager.call_tool

    async def _call_tool(
        tool_name: str,
        arguments: dict[str, Any],
        context: Any = None,
        convert_result: bool = False,
    ) -> Any:
        request = getattr(request_ctx.get(None), "request", None)
        headers = getattr(request, "headers", None)
        if headers is None:
            return await wrapped_call_tool(
                tool_name, arguments, context=context, convert_result=convert_result
            )
        tokens = bind_caller(
            headers.get(HEADER_ACTOR, ""),
            headers.get(HEADER_SESSION, ""),
            headers.get(HEADER_CORRELATION, ""),
        )
        try:
            # **The caller's trace, adopted for the same reason the caller's identity is.**
            # `CallerLogMiddleware.dispatch` also attaches `continue_trace`, and that attachment is
            # in the ASGI task — the same task a tool body does *not* run in. So a span opened
            # inside a tool would have been rooted at nothing rather than parented to the turn that
            # asked for it, which is the exact defect measured one function up for the contextvars.
            # Latent today (no tool body in this repository opens a span) and pre-emptied here,
            # because the first one to do so would produce an orphan and nothing would say why.
            with continue_trace(headers):
                return await wrapped_call_tool(
                    tool_name, arguments, context=context, convert_result=convert_result
                )
        finally:
            reset_caller(tokens)

    manager.call_tool = _call_tool  # type: ignore[method-assign,assignment]


def _sanitize_tool_errors(server: FastMCP, *, name: str) -> None:
    """Replace an unexpected tool exception's text with a generic notice before it reaches a caller.

    Measured, not assumed (a probe against this exact `mcp` version, over the real streamable-HTTP
    transport): `Tool.run` already turns any exception a tool raises into a JSON-RPC tool-error
    result rather than an HTTP fault, but it folds the exception's `str()` in verbatim —
    `f"Error executing tool {name}: {e}"` — so an unhandled `psycopg.OperationalError` or a stray
    path reaches the model with a DSN or an internal identifier attached. This is not about
    *whether* a caller sees an error, only about *what it is allowed to say*.

    `ValueError` is the one exception family this codebase already treats as "a deliberately-worded,
    caller-safe message" (`chemclaw.core.errors.ChemclawError` and `ConnectorError` both derive from
    it, and so does pydantic's own `ValidationError`) — every connector tool that raises to explain
    a bad SMILES or a bad argument already raises one of these, so only this family is let through
    unchanged. Anything else is a bug or an infrastructure fault, not a message written for the
    model to read, and is replaced here — with the real exception logged so an operator can still
    find it.

    There is no supported hook for this in `FastMCP` (no tool-call middleware in this version), so
    the interception point is the tool manager's own `call_tool` — the one place every tool call
    passes through before `Tool.run` composes the leaking message. Patched once here, the one
    shared choke point every connector's app is built through, rather than once per bundle.
    """
    manager = server._tool_manager
    original_call_tool = manager.call_tool

    async def _call_tool(
        tool_name: str,
        arguments: dict[str, Any],
        context: Any = None,
        convert_result: bool = False,
    ) -> Any:
        try:
            return await original_call_tool(
                tool_name, arguments, context=context, convert_result=convert_result
            )
        except ToolError as exc:
            if isinstance(exc.__cause__, ValueError):
                raise  # a deliberately-worded domain message (or a validation error) — safe as-is
            logger.exception(
                "connector %s: tool %r raised an unexpected exception", name, tool_name
            )
            raise ToolError(
                f"Error executing tool {tool_name}: an internal error occurred"
            ) from exc.__cause__

    manager.call_tool = _call_tool  # type: ignore[method-assign,assignment]


def _publish_tool_results(server: FastMCP, *, name: str) -> None:
    """Offer every tool's own result to the external results store, for every bundle at once.

    **The third publish hook** (`D-2026-08-27-a-composite-needs-a-hook-not-a-projector`). Two
    already exist and neither can see a *tool* composite: the cache hook fires on a cache miss and
    a composite has no cache row by design (its key would name its own output), and the job hook
    fires off a Temporal envelope and a tool is not a job. `compute_thermochemistry` and
    `predict_logd` both had a projector and no caller for as long as the seam has shipped, which is
    the `audit_events.agent` shape — a record that reads as kept and is not.

    Installed here rather than called by each tool, and that is the whole design: a tool author has
    nothing to remember, because a tool is registered with `@server.tool()` and is therefore already
    inside `_tool_manager`. `chemclaw.publish.hooks` decides what is actually published, and the
    suite derives that set rather than trusting it.

    **Wrapped on the tool's function, not on `call_tool`.** The two patches above intercept
    `ToolManager.call_tool`, which is right for an error and for an identity because neither needs
    the result. This one does: by the time a call returns from the manager, `convert_result` has
    turned the model into content blocks and a structured dict — a tool result is not a model on
    the wire (`D-2026-08-26-a-tool-result-is-not-a-model-on-the-wire`) — and the hook routes on the
    model's own name. `Tool.fn` is the last point at which it still is one.

    The wrapper returns the tool's result unchanged and swallows nothing the tool raises: a failed
    tool publishes nothing, and a failed publish is invisible to the caller. Idempotent, so a
    process that builds two apps over one server does not wrap twice.
    """
    for tool in server._tool_manager.list_tools():
        if not tool.is_async or getattr(tool.fn, "_chemclaw_publishes", False):
            # A synchronous tool is left alone rather than wrapped in a coroutine: `Tool.is_async`
            # was decided at registration and `call_fn_with_arg_validation` dispatches on it, so an
            # async wrapper over a sync function would be awaited by nobody. No tool in this tree
            # is synchronous; a future one that is would need `is_async` moved with it.
            continue
        tool.fn = _publishing(tool.fn, connector=name, tool_name=tool.name)


def _publishing(
    fn: Callable[..., Awaitable[Any]], *, connector: str, tool_name: str
) -> Callable[..., Awaitable[Any]]:
    """One tool's body, with its result offered to the results store after it returns."""

    @functools.wraps(fn)
    async def _run(**kwargs: Any) -> Any:
        result = await fn(**kwargs)
        # Imported here rather than at module scope for the reason `publish_stored_result` gives
        # for the same import: `connector_app` runs at import time in seven bundle modules, and a
        # deployment with no sink configured should never load the projection machinery — or
        # RDKit's canonicalization behind it — at all.
        from chemclaw.publish.hooks import publish_tool_result

        await publish_tool_result(
            connector=connector, tool=tool_name, arguments=kwargs, result=result
        )
        return result

    _run._chemclaw_publishes = True  # type: ignore[attr-defined]
    return _run


def connector_app(
    server: FastMCP, *, name: str, on_start: Callable[[], Coroutine[Any, Any, None]] | None = None
) -> FastAPI:
    """Build the FastAPI app that serves one connector's MCP capability.

    Args:
        server: The `FastMCP` instance holding the capability's tools. Which of them the *agent*
            may call is decided by the manifest's `tools` allow-list in core, not here — but every
            tool served is reachable by anything that can open a socket to this pod, so the served
            set is not a free surface. This docstring used to say the server "can also expose
            index/write tools for the ingestion path"; it did, nothing in the tree called them, and
            an anonymous MCP handshake wrote a row into the fingerprint corpus. `connector-validate`
            now refuses a served tool the manifest does not declare.
        name: The connector's name (must match its bundle folder and manifest `name`), used in the
            health payload and the request log.
        on_start: Optional coroutine started once at startup — the hook a bundle uses to report
            the state of what it serves (`molfp`/`rxnfp` log how many fingerprints their index
            actually holds, so an operator learns of an unbuilt index before a chemist does).
            Diagnostics only, and treated as such: it is *started*, not awaited (see the lifespan),
            and a bundle's hook owns swallowing its own failures — a connector that refuses to
            start because it could not describe itself is strictly worse than one that starts.

    Returns:
        A FastAPI app exposing `GET /healthz`, `GET /metrics`, and the MCP endpoint at `/mcp`.
    """
    _sanitize_tool_errors(server, name=name)
    # Outermost, so the identity a tool stamps on a durable row is bound before anything else runs.
    _bind_caller_per_tool_call(server)
    # Innermost of the three: it runs inside the tool body's own frame, where the result is still
    # the model it was declared as. See `_publish_tool_results`.
    _publish_tool_results(server, name=name)
    mcp_app = server.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Run the MCP session manager, and pool Postgres, for the app's lifetime.

        The session manager is the sub-app's own lifespan, which mounting does not run.
        `db.pooling` is here for the same reason it is in the front door's lifespan: a
        connector that touches a store (`calc` reads and writes the calculation cache) otherwise
        opens a connection per tool call, and the handshake lands on the same event loop that
        serves every other request on this process.

        `on_start` is launched inside the pool (so a bundle's report borrows a pooled connection
        rather than paying its own handshake) but deliberately **not awaited**: it touches the
        database, and an unreachable one would hold readiness for the whole pool timeout — a
        diagnostic that can delay a connector becoming ready is worse than the blindness it cures.
        Measured, not assumed: awaiting it kept the connector composite from starting inside the
        transport test's window with no Postgres running. The task is kept referenced so it is not
        garbage-collected mid-flight, and cancelled if shutdown beats it.
        """
        async with db.pooling(), server.session_manager.run():
            report: asyncio.Task[None] | None = (
                asyncio.create_task(on_start()) if on_start is not None else None
            )
            try:
                yield
            finally:
                if report is not None:
                    report.cancel()

    app = FastAPI(title=f"chemclaw-connector-{name}", lifespan=lifespan)
    app.add_middleware(CallerLogMiddleware, connector=name)
    # Always installed; it resolves what this bundle's own manifest requires on the first request
    # and passes straight through for `mode: none`. Read from the registry rather than taken as an
    # argument, so the seven `app.py` modules stay one line each and no bundle can forget to wire
    # it — the declaration is in the manifest and the enforcement follows it.
    app.add_middleware(BearerAuthMiddleware, connector=name)
    # Added *after* `CallerLogMiddleware`: Starlette wraps in add-order with the most recently
    # added outermost, so this one now sits outside it and refuses an oversized body before any
    # handler — including the logging middleware's own `dispatch` — ever reads it (Sec-5: `/mcp`
    # had no cap at all, unlike the front door's `_add_body_size_limit`).
    if settings.connector_max_request_bytes:
        app.add_middleware(BodySizeLimit, max_bytes=settings.connector_max_request_bytes)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness/readiness for the core startup probe (`chemclaw.connectors.health`).

        One route for both, and honestly so rather than by omission: uvicorn accepts connections
        only after the lifespan above has completed, so this route answering *is* the evidence
        that the MCP session manager is running and the Postgres pool is open. A separate
        `/readyz` here could only assert the same fact a second time.
        """
        return {"status": "ok", "connector": name}

    @app.get("/metrics")
    async def metrics() -> Response:
        """Prometheus exposition for this connector process.

        A connector records through `chemclaw.core.metrics_bridge` like everything else, and the
        registry that bridge finds is per-process — so these counters are this pod's, and until
        this route existed nothing could read them. Unauthenticated for the same reason the front
        door's copy is: a scrape happens independently of user identity, the NetworkPolicy keeps
        the port inside the cluster, and the exposition carries counts only.
        """
        return Response(content=METRICS.render(), media_type=CONTENT_TYPE)

    # Mounted last: Starlette matches routes in definition order, so the routes above win and
    # everything else — notably `/mcp` — falls through to the MCP transport.
    app.mount("/", mcp_app)
    return app
