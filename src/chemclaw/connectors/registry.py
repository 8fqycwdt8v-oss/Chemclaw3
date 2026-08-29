"""The connector registry: discover bundles, validate them, and build what the agent advertises.

This is the one place that turns folders on disk into agent capability. It deliberately combines
the two discovery idioms the repo already trusts, each where it fits: **filesystem discovery**
for the bundles themselves (a connector is a folder, exactly as a skill is), and a **config
enable-token** for which of the discovered bundles a
deployment turns on (exactly as `skills_enabled` and `data_sources` do). Discovery is not
enablement: a repo can ship every connector and a deployment can run the subset it has
validated.

Two products come out of a manifest, and a turn's graph binds both:

- **MCP tools** — whatever each `endpoint:` advertises over a session held for the turn, carrying
  the turn's identity headers on every call and our own credential on the connection
  (`chemclaw.connectors.identity`).
- **Job tools** — one generated launcher per `jobs:` entry (`chemclaw.connectors.jobs`), registered
  into the shared tool registry so audit, authorization and profile narrowing address it like any
  other tool.

Nothing here decides *whether* a call is allowed. The registry only assembles the offered
surface; the audit and authorization middlewares wrap the assembled list in `build_langgraph_agent`,
and a profile narrows it afterwards — so a connector can add to what is *offered* and never to what
is *permitted*.
"""

import asyncio
import importlib
import logging
import os.path
from collections.abc import Callable, Iterable
from contextlib import AsyncExitStack
from datetime import timedelta
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any, assert_never

import httpx
import yaml
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.sessions import StdioConnection, StreamableHttpConnection
from pydantic import ValidationError

from chemclaw.connectors.identity import auth_for, turn_identity_hook
from chemclaw.connectors.jobs import build_job_tool
from chemclaw.connectors.manifest import (
    ConnectorManifest,
    Endpoint,
    HttpEndpoint,
    JobSpec,
    StdioEndpoint,
)
from chemclaw.connectors.transport import ConnectorSpec, HeldConnectorSession
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.mcp_session import CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_GRACE_SECONDS
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.core.tool_registry import CapabilityTool

logger = logging.getLogger(__name__)

# The manifest filename inside a bundle. A constant because two modules look for it (here and
# `scripts.validate_connectors`) and a typo in either would report "no connectors found".
MANIFEST_FILENAME = "connector.yaml"

# The two transport bounds every outbound MCP client shares now live with the client itself
# (`core/mcp_session.py`), because the reaction labeller became a second caller that needs both.
# Re-exported under their old private names so this module's own call sites read unchanged.
_CONNECT_TIMEOUT_SECONDS = CONNECT_TIMEOUT_SECONDS

# How long a tool call may take when the manifest does not say. `HttpEndpoint.request_timeout`
# defaults to `None`, and `None` used to mean *unbounded*: nothing set `session_kwargs`, so the MCP
# client session's `read_timeout_seconds` stayed `None` and `mcp.shared.session` reached
# `anyio.fail_after(None)` — it waits forever. Measured against a real server: a 4 s tool behind an
# endpoint declaring `request_timeout: 2` was still blocked at 25 s, and its answer was discarded
# when it finally arrived. Only the front door's 600 s turn deadline bounded it. Every shipped
# bundle declares a timeout, so this is the number a *third-party* bundle gets — generous enough
# that a legitimately slow tool is not cut off, finite so a mute connector cannot hold a turn.
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 60.0

_READ_TIMEOUT_GRACE_SECONDS = READ_TIMEOUT_GRACE_SECONDS

# What one configured connector endpoint becomes, whichever transport it declares. Both open into a
# session advertising the same agent-facing surface, so callers never branch on the transport.


class ConnectorError(ChemclawError):
    """A connector bundle is malformed, or an enabled connector does not exist.

    A `ChemclawError` (so a `ValueError`) because this is a configuration error surfaced at
    startup — the same class the config validators and `chemclaw.ingest.sources.registry` raise,
    so one `except ValueError` at an entry point catches every "this deployment is misconfigured"
    failure. It is also registered in `chemclaw.durable.publish._BAD_DATA_TYPES` by its own class
    name, because Temporal matches non-retryable error types by exact name, not isinstance — a
    template step that names an unknown job (`chemclaw.durable.template_activities`) must fail on
    its first attempt, not burn the transient-retry budget on a job that will never exist.
    """


def _bundle_dirs() -> list[Path]:
    """Every connector bundle directory found across the configured connector dirs, sorted by name.

    Sorted rather than filesystem order so the advertised tool order is identical on every
    machine — tool order is part of the prompt the model sees, and a surface that reshuffles per
    pod is a reproducibility problem.
    """
    found: dict[str, Path] = {}
    for directory in settings.connectors_dirs:
        root = Path(directory)
        if not root.is_dir():
            continue
        for path in sorted(root.iterdir()):
            if (path / MANIFEST_FILENAME).is_file():
                # First dir wins, so an operator's private connectors dir listed ahead of the
                # repo's can override a shipped bundle — the same precedence a `PATH` entry has.
                found.setdefault(path.name, path)
    return [found[name] for name in sorted(found)]


def _load_manifest(bundle: Path) -> ConnectorManifest:
    """Parse and validate one bundle's `connector.yaml`, raising `ConnectorError` on any problem.

    The folder name is authoritative: a manifest whose `name` disagrees with its directory would
    be enabled under one name and looked up under the other, so the mismatch is rejected here
    rather than surfacing as a connector that silently never loads (the rule `validate_skills`
    applies to `SKILL.md`).
    """
    path = bundle / MANIFEST_FILENAME
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConnectorError(f"{path}: unreadable or malformed YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConnectorError(f"{path}: must contain a YAML mapping, got {type(raw).__name__}")
    try:
        manifest = ConnectorManifest.model_validate(raw)
    except ValidationError as exc:
        raise ConnectorError(f"{path}: invalid manifest: {exc}") from exc
    if manifest.name != bundle.name:
        raise ConnectorError(
            f"{path}: declares name {manifest.name!r} but lives in directory {bundle.name!r}"
        )
    return manifest


@cache
def discovered() -> dict[str, tuple[Path, ConnectorManifest]]:
    """Every discovered bundle by name, with its directory — validated, regardless of enablement.

    Cached because discovery reads and parses every manifest on disk, while the result is fixed
    for the process's lifetime (config is read once at import, and bundles do not appear at run
    time). `discovered.cache_clear()` is the seam a test uses after pointing `connectors_dir`
    elsewhere.
    """
    return {bundle.name: (bundle, _load_manifest(bundle)) for bundle in _bundle_dirs()}


def bearer_token_env_names() -> tuple[str, ...]:
    """Every environment variable holding an enabled connector's bearer token.

    **One definition, because two things scrub with it and a scrub that covers one is worse than
    none.** `core.logging.SecretRedactingFilter` resolves these so a token cannot reach a log line,
    and `deliver.message.Message.redacted` needs the same set so a token cannot reach a *webhook* —
    which is the more consequential of the two, since a log line stays inside the cluster and a
    delivery does not. `message.py` claimed the same filter ran on it and it did not: the default
    `redact_secrets` path covers `_SECRET_SETTINGS` and the structural patterns, and an opaque
    site-issued connector token matches none of them.

    Returns:
        The variable names, or `()` when discovery fails — the caller decides how loudly to say so.
    """
    from chemclaw.connectors.manifest import BearerAuth, HttpEndpoint

    return tuple(
        manifest.endpoint.auth.token_env
        for manifest in enabled()
        if isinstance(manifest.endpoint, HttpEndpoint)
        and isinstance(manifest.endpoint.auth, BearerAuth)
    )


def enabled() -> list[ConnectorManifest]:
    """The manifests this deployment turns on, in the order the enable-list (or discovery) gives.

    An empty `connectors_enabled` means every discovered connector — the same "discovery is
    enablement until you say otherwise" default `skills_enabled` uses, so a fresh checkout runs
    the full shipped surface. A name in the list that no bundle provides is a loud error: it
    would otherwise advertise nothing and look like a capability that simply stopped working.
    """
    found = discovered()
    names = settings.connectors_enabled_list
    if not names:
        return [manifest for _, manifest in found.values()]
    unknown = sorted(set(names) - found.keys())
    if unknown:
        raise ConnectorError(
            f"connectors_enabled names unknown connector(s) {unknown}; discovered: {sorted(found)}"
        )
    return [found[name][1] for name in names]


def server_tools_module(connector: str) -> ModuleType | None:
    """A bundle's `server.tools` module, or `None` when the bundle ships no MCP server at all.

    The one definition of "import a bundle's tool functions", because the two validators that do
    it — `make connector-validate` (does the served surface match the manifest) and
    `make template-validate` (does a template step pass arguments the tool takes) — had opposite
    answers to the same question, and only one of them was right.

    Both callers skip an endpoint-less bundle before asking (that is how `results`, which is
    jobs-only,
    never reaches here), so `None` means the narrower thing: a bundle that declares an endpoint and
    has no module behind it.

    A *transitive* import failure propagates. Only a `ModuleNotFoundError` naming this bundle's own
    server package or the module inside it means "no server module"; a missing or renamed dependency
    underneath it means the bundle is broken, and swallowing that leaves a validator checking less
    and still reporting success — measured, `validate_templates` resolved 46 signatures instead of
    50 and printed "template validation passed" for a bundle that could not be imported at all.

    **The package name is in that set because a bundle can now be declared and not run.** This used
    to check the module alone, which was complete while every endpoint-bearing bundle shipped a
    `server/` directory. `chem`'s capability moved out and its directory went with it, so the
    *parent* is what is missing and `exc.name` is the package — and this function raised where its
    own docstring says it should return `None`.

    That it survived a local run is worth recording: a deleted `server/` leaves its `__pycache__`
    behind, so the directory persists as a PEP 420 namespace package, the import gets one level
    further, and the error names the module after all. Locally it returned `None` and CI raised, off
    the same commit.
    """
    package = f"chemclaw.connectors.{connector}.server"
    target = f"{package}.tools"
    try:
        return importlib.import_module(target)
    except ModuleNotFoundError as exc:
        if exc.name in {target, package}:
            return None
        raise


def declared_note_types() -> frozenset[str]:
    """Every knowledge-graph note type the enabled bundles declare.

    Read by `chemclaw.kg.note.known_note_types`, which unions it with core's own set to get the
    vocabulary `make kg-validate` accepts in this deployment. Scoped to *enabled* bundles rather
    than discovered ones for the same reason every other registry answer is: a bundle a deployment
    does not run contributes nothing, including vocabulary — and a note whose type came from a
    since-disabled bundle should fail validation, because nothing in the running system can produce
    or interpret one.
    """
    return frozenset(name for manifest in enabled() for name in manifest.note_types)


def declared_relations() -> frozenset[str]:
    """Every graph relation the enabled bundles declare — the edge-side twin of the above."""
    return frozenset(name for manifest in enabled() for name in manifest.relations)


def skills_dirs() -> list[str]:
    """The `skills/` directory of every enabled connector that declares skills.

    A connector's judgment ships with its capability: the `SKILL.md` explaining *when* to trust
    a similarity hit belongs to the same bundle as the tool that produces one. Appending these
    to `settings.skills_dirs` means the skills backend discovers them with no new machinery, and
    the existing enable-list and role gates still narrow them — a bundled skill is an ordinary
    skill in every respect except where it lives.

    Only directories that exist are returned: a manifest may declare skills whose folder a
    deployment has not mounted, and `make connector-validate` is where that mismatch is
    reported, so handing a non-existent path to the skills source here would fail the *agent*
    for a *packaging* problem.
    """
    return _bundle_content_dirs("skills", lambda manifest: bool(manifest.skills))


def _endpoint_url(connector: str, endpoint: HttpEndpoint) -> str:
    """The endpoint URL, after any per-deployment override for this connector.

    A manifest ships a working dev default (a loopback port), but a cluster's address belongs to
    the deployment, not to a file in the repo. `connector_urls` is that override, so Helm points
    the front door at an in-cluster Service without patching a bundle.
    """
    return settings.connector_urls.get(connector, endpoint.url)


def request_timeout_seconds(endpoint: Endpoint) -> float:
    """How long one call to this endpoint may take — the single derivation of that number.

    Two independent bounds are built from it (the MCP session's `read_timeout_seconds` and the
    httpx read timeout), and they must stay in a fixed relationship to each other, so neither may
    read the manifest on its own. Public because a test proving that relationship has to compare
    the same number a deployment uses.

    `StdioEndpoint` declares no timeout at all — a subprocess of our own process is not a network
    dependency — but an unresponsive subprocess hangs a turn exactly as a mute HTTP host does, so
    it gets the same default rather than an exemption.
    """
    if isinstance(endpoint, HttpEndpoint) and endpoint.request_timeout is not None:
        return float(endpoint.request_timeout)
    return _DEFAULT_REQUEST_TIMEOUT_SECONDS


def _session_kwargs(endpoint: Endpoint) -> dict[str, Any]:
    """The `ClientSession` arguments that give a tool call a deadline at all.

    This is the bound that actually fires: `mcp.shared.session.send_request` waits inside
    `anyio.fail_after(read_timeout_seconds)` and raises `McpError` when it expires. Without it the
    argument is `None` and the wait is unbounded — see `_DEFAULT_REQUEST_TIMEOUT_SECONDS`.
    `langchain-mcp-adapters` forwards `session_kwargs` verbatim into `ClientSession`, and both
    transports' connection mappings accept it, so one function serves both branches.
    """
    return {"read_timeout_seconds": timedelta(seconds=request_timeout_seconds(endpoint))}


def connector_http_client(connector: str, endpoint: HttpEndpoint) -> httpx.AsyncClient:
    """The HTTP client one connector endpoint is reached with — the single definition of it.

    Public because a test that means to prove something about how a connector is reached has to
    exercise *this* client rather than a hand-rolled lookalike; three transport tests used to build
    their own and were free to drift from what a deployment actually runs.

    One client carries both halves of what travels with a call (`chemclaw.connectors.identity`):
    our own credential as `auth`, so it is present on the MCP handshake too, and the turn's identity
    as a request hook, which is the only place that can see the turn's ambient context — a
    header-provider callback is invoked in the calling task while the request is issued by the MCP
    transport's writer task, so its headers would never land.

    **Redirects are not followed, and that is a security property rather than a tuning choice.**
    An httpx request hook runs on every hop and httpx carries the previous request's headers into
    the redirected one, stripping `Authorization` alone — so a connector (or anything that can bind
    its Service port; all shipped manifests declare `auth: mode: none`) could answer the MCP POST
    with a `302` toward an origin it controls and collect the caller's Entra object id and full role
    set once per turn. MCP streamable-HTTP needs no redirect for any real flow:
    `FastMCP.streamable_http_app` serves the endpoint as an exact Starlette `Route`, so neither the
    per-bundle Service address nor the dev composite's `/<name>/mcp` mount ever answers 3xx — proven
    by the transport tests, which complete the handshake and a tool call over this client.
    `turn_identity_hook` strips the headers on a foreign origin as the second layer.

    Args:
        connector: The bundle's name, for the deployment URL override and the credential error.
        endpoint: The manifest's HTTP endpoint declaration.

    Returns:
        A client the MCP adapter owns and closes with the session it opened.
    """
    return httpx.AsyncClient(
        auth=auth_for(endpoint.auth, connector),
        follow_redirects=False,
        # Never inherit an ambient proxy: a connector endpoint is an in-cluster Service, and an
        # HTTPS_PROXY on the pod must not silently reroute a tool call (and its bearer) elsewhere.
        trust_env=False,
        event_hooks={"request": [turn_identity_hook(_endpoint_url(connector, endpoint))]},
        # Without this, httpx applies its own 5 s default to *every* phase, and the manifest's
        # `request_timeout` — which this module's docstring credits with "keeping an unreachable
        # host from hanging a turn" — did the opposite. Measured against a real server: an 8 s tool
        # call had its HTTP stream torn down at 5 s, the MCP response then never arrived, and the
        # caller blocked for the full `request_timeout` (60 s for calc, 120 s for bo) before
        # surfacing an opaque failure — holding an admission permit and an agent lease the whole
        # time. A tool slower than 5 s is not exotic here: an uncached `predict_pka` runs xTB
        # inline. Connect stays short so a dead host still degrades fast.
        #
        # The read bound is deliberately *looser* than the MCP session's (`_session_kwargs`) rather
        # than equal to it. The measurement above is what that ordering encodes: this timeout's
        # firing is invisible — `mcp.client.streamable_http` catches it at debug level and does not
        # reconnect — so whenever it trips first it converts a merely slow answer into a lost one
        # with no error to show for it. The session bound is the one that must win, because it is
        # the one that raises. See `_READ_TIMEOUT_GRACE_SECONDS`.
        timeout=httpx.Timeout(
            request_timeout_seconds(endpoint) + _READ_TIMEOUT_GRACE_SECONDS,
            connect=_CONNECT_TIMEOUT_SECONDS,
        ),
    )


def health_url(manifest: ConnectorManifest) -> str | None:
    """Where to probe this connector, moved to wherever its endpoint actually is (D-131).

    Public because the startup probe is a second caller and it must not read `health_url` off the
    manifest directly — **which is exactly the bug this exists to fix.** `connector_urls` moved the
    *tool* endpoint to the deployment's real address and left the probe pointed at the manifest's
    loopback dev default. The shipped chart always sets that override (it computes one Service URL
    per enabled bundle), so in a cluster the front door probed `127.0.0.1:881x` — its own pod, where
    nothing listens. Every connector therefore reported `unreachable` on `/readyz` and in
    `chemclaw_connectors_unhealthy` however healthy it was, and under `connectors_required: true`
    — the fail-fast posture — startup would have failed every time. Found by re-running the
    Stage 5e connector-kill scenario, which could not tell "killed" from "never probed correctly".

    The move is a suffix replacement rather than an origin swap, because the two deployments that
    exist put the connector in different *places*, not merely on different hosts: Helm gives each
    bundle its own Service (`…:8814/mcp` + `…:8814/healthz`) while `chemclaw.cli.connectors_dev`
    mounts
    them all under one port by name (`…:8810/chem/mcp` + `…:8810/chem/healthz`). Taking the health
    path verbatim would be right for the first and wrong for the second. So the manifest's own two
    URLs define the relationship — whatever distinguishes its health URL from its endpoint URL —
    and that difference is re-applied at the effective address.

    Returns None when the bundle declares no health route (a third-party MCP server may expose
    none), which the probe reports as `unprobed` rather than guessing a path.
    """
    endpoint = manifest.endpoint
    if not isinstance(endpoint, HttpEndpoint) or endpoint.health_url is None:
        return None
    effective = _endpoint_url(manifest.name, endpoint)
    if effective == endpoint.url:
        return endpoint.health_url
    shared = len(os.path.commonprefix([endpoint.url, endpoint.health_url]))
    endpoint_tail, health_tail = endpoint.url[shared:], endpoint.health_url[shared:]
    if not effective.endswith(endpoint_tail):
        # The override does not end the way the manifest's own endpoint does, so there is nothing
        # to re-root against. Probing the declared URL is the honest fallback: it may be wrong, but
        # inventing a path from an address we do not understand would be wrong *and* silent.
        return endpoint.health_url
    return effective.removesuffix(endpoint_tail) + health_tail


def _mcp_connection(manifest: ConnectorManifest, endpoint: Endpoint) -> ConnectorSpec:
    """Describe one connector endpoint for the LangGraph engine (M7).

    The twin of `_mcp_tool`, dispatching on the same union for the same reason: the transports
    differ only in how the server is *reached*, and everything bounding what the agent may do with
    it is identical on both.

    **The HTTP client is still ours, and that is what keeps four security properties alive.**
    `httpx_client_factory` is the seam `langchain-mcp-adapters` exposes, so `connector_http_client`
    crosses unchanged and with it the refusal to follow redirects (a connector answering `302`
    would otherwise harvest the caller's Entra oid and role set), `turn_identity_hook`, `auth_for`,
    and the split connect/read timeout. The library's own `timeout`/`auth`/`headers` arguments are
    deliberately *not* passed on the connection: the factory ignores what it is handed, and the
    honest way to ignore an argument is to never let a caller supply one. The adapter closes the
    client it builds through the factory — `_create_streamable_http_session` enters it with `async
    with client` — so the D-119-class connection leak cannot arise here.

    `session_kwargs` is the one library argument that *is* passed, on both transports, because it
    is the only place a tool call can be given a deadline at all (`_session_kwargs`) — the httpx
    client bounds the bytes, not the JSON-RPC request waiting on them.
    """
    if isinstance(endpoint, HttpEndpoint):
        return ConnectorSpec(
            name=manifest.name,
            connection=StreamableHttpConnection(
                transport="streamable_http",
                url=_endpoint_url(manifest.name, endpoint),
                httpx_client_factory=_connector_client_factory(manifest.name, endpoint),
                session_kwargs=_session_kwargs(endpoint),
            ),
            allowed_tools=tuple(endpoint.tools),
        )
    if isinstance(endpoint, StdioEndpoint):
        # **Refused unless the deployment asked for it**, because this is the one endpoint field
        # that executes: `command` is run in the chat process, before the handshake, so a manifest
        # dropped on `connectors_dir` by anything that can write there (a CI job syncing a sibling
        # repo, a ConfigMap edit) used to be arbitrary code execution under the identity holding
        # every connector token. Refusing here rather than at parse time keeps `StdioEndpoint`
        # constructible — the transport's own tests build one directly — while making a *file* an
        # inert declaration until an operator turns the transport on.
        if not settings.connector_stdio_enabled:
            raise ConnectorError(
                f"connector {manifest.name!r} declares `transport: stdio`, which launches "
                f"{endpoint.command!r} in this process; it is disabled by default because a "
                "manifest is data. Set CHEMCLAW_CONNECTOR_STDIO_ENABLED=true to allow it."
            )
        # No identity headers, for the same reason as `_mcp_tool`: a subprocess of our own process,
        # under our own identity, with no request to attach them to (`connectors.identity`).
        return ConnectorSpec(
            name=manifest.name,
            connection=StdioConnection(
                transport="stdio",
                command=endpoint.command,
                args=list(endpoint.args),
                session_kwargs=_session_kwargs(endpoint),
            ),
            allowed_tools=tuple(endpoint.tools),
        )
    assert_never(endpoint)  # exhaustive over the union — a new transport without a branch is a bug


def _connector_client_factory(connector: str, endpoint: HttpEndpoint) -> Any:
    """An `httpx_client_factory` that returns *our* client, ignoring what the library offers.

    The library calls this with `headers`, `timeout` and `auth` drawn from the connection mapping.
    `_mcp_connection` sets none of them, so all three arrive empty and there is nothing to drop —
    the signature exists to satisfy the caller, not to carry configuration. Every one of those
    concerns is already decided inside `connector_http_client`, which is the one place they may be
    decided.
    """

    def factory(**_ignored: Any) -> httpx.AsyncClient:
        return connector_http_client(connector, endpoint)

    return factory


def mcp_connections() -> list[ConnectorSpec]:
    """One connection spec per enabled connector that declares an endpoint (unopened).

    The deployment's whole surface; `chemclaw.agent.chemclaw_agent.connector_specs` is the
    profile-narrowed half. Split that way because enablement is a deployment decision and narrowing
    is a per-turn one, and a profile must never be able to widen what the deployment enabled.
    """
    return [
        _mcp_connection(manifest, manifest.endpoint)
        for manifest in enabled()
        if manifest.endpoint is not None
    ]


async def open_connector_specs(
    stack: AsyncExitStack, specs: Iterable[ConnectorSpec]
) -> tuple[list[BaseTool], list[str]]:
    """Open every connector for this turn; return the tools that came up and the names that did not.

    The connector lifecycle in one place, used by every caller that runs a turn — the front-door
    runner, the CLI, the template activities — so "how a turn reaches its connectors" has a single
    definition rather than several loops that can drift.

    **The tools come back with the casualties** because a connector's tools do not exist until its
    session is open: `load_mcp_tools` needs a live session. That is why this returns a pair rather
    than a list of names, and it is the one structural difference from the process-lived connector
    objects this replaced.

    Nothing is caught here: a session's `connect` is already non-fatal by construction
    (`chemclaw.connectors.transport`), so an unreachable connector simply comes back not-connected,
    contributes no tools to the turn, and is retried on the next one.

    Concurrent, because these are independent hosts and the wait is the *sum* of their latencies
    otherwise. On the healthy path that is a few hundred milliseconds; the case that matters is the
    tail, where a dark fleet cost six sequential connect timeouts before the model was called at
    all. Safe to be concurrent because each `HeldConnectorSession` confines its `anyio` cancel scope
    to a task of its own, so entering them together does not exit them from the wrong task (see
    `chemclaw.connectors.transport.HeldConnectorSession`).

    **The degradation is announced here, not left to the caller** (REV-6). The return value once
    said "for the caller to surface" and all four callers dropped it on the floor, so a turn that
    lost half its capability answered exactly like one that had all of it — the model simply never
    saw the tools and reasoned from what remained. Announcing it in the one place every caller
    passes through means a new caller cannot reintroduce the silence by forgetting to read a return
    value. A caller that can reach a *human* still reads the list and says so on its own surface
    (the front door yields `CapabilityDegradedEvent`, the CLI prints to stderr); what is guaranteed
    here is the operator-visible half.

    Args:
        stack: The caller's exit stack, which owns tearing the sessions down.
        specs: This turn's connector specs
            (`chemclaw.agent.chemclaw_agent.connector_specs`).

    Returns:
        The tools every reachable connector advertises, and the names of those that did not come up.
    """
    held = [HeldConnectorSession(spec) for spec in specs]
    opened = await asyncio.gather(*(stack.enter_async_context(session) for session in held))
    unreachable = [session.name for session in held if not session.connected]
    if unreachable:
        # WARNING rather than ERROR: the turn still runs, and a connector that is down for a
        # deployment is a normal transient. The counter is what makes it alertable — a rate that
        # stays above zero across turns is a dark connector, not a restart.
        logger.warning(
            "%d connector(s) did not come up for this scope and contribute no tools: %s",
            len(unreachable),
            ", ".join(unreachable),
        )
        record_metric(
            lambda m: m.increment("chemclaw_connectors_unreachable_total", len(unreachable))
        )
    return [tool for tools in opened for tool in tools], unreachable


def profiles_dirs() -> list[str]:
    """The `profiles/` directory of every enabled connector that declares profiles.

    The bundle-local half of profile discovery (`chemclaw.agent.profile_discovery`), and the same
    rule
    as `skills_dirs`: only directories that exist are returned, because a manifest may declare
    content a deployment has not mounted and that is `make connector-validate`'s complaint to
    make, not a reason to fail the agent.
    """
    return _bundle_content_dirs("profiles", lambda manifest: bool(manifest.profiles))


def _bundle_content_dirs(kind: str, declares: Callable[[ConnectorManifest], bool]) -> list[str]:
    """Every enabled bundle's `<kind>/` directory, for the bundles that declare that content.

    `skills_dirs` and `profiles_dirs` were this function twice, three hundred lines apart, differing
    in two tokens and each carrying its own copy of the "only directories that exist" paragraph —
    which is the sentence most likely to be fixed in one copy and not the other. Two callers is the
    second one, so this is the extraction rather than a speculative one; the third bundle-local
    content type costs a line instead of another twelve.

    `declares` is passed rather than derived from `kind` by `getattr`: the manifest fields are a
    typed surface, and reaching into them by string would make a renamed field a silently empty list
    instead of a type error.
    """
    found = discovered()
    dirs = []
    for manifest in enabled():
        if not declares(manifest):
            continue
        candidate = found[manifest.name][0] / kind
        if candidate.is_dir():
            dirs.append(str(candidate))
    return dirs


def _declared_tool_names() -> dict[str, tuple[str, str]]:
    """Every tool name the enabled bundles advertise, mapped to `(connector, kind)`.

    **One name is one capability, whichever half of a bundle's surface declares it.** The name is
    the authorization key — `state_changing_tool_names` and the plan gate both look a capability up
    by it — so two capabilities sharing one name means one bundle's gate silently applies to the
    other's work. It is also what the model calls: it has one name and gets whichever tool survived
    the merge.

    This used to check jobs against *jobs* only, and the gap was not hypothetical. `props` served an
    MCP tool `compare_solvents` (tabulated physical properties, microseconds) while `calc` declared
    a durable job `compare_solvents` (one reaction computed per solvent, minutes), and the wiring
    that brings them together — this fleet's `manifests/` on `connectors_dir` — is the documented
    one. Measured with both enabled: 21 endpoint tools plus 9 jobs is 30 declared names and 29
    distinct. Nothing raised, because `connector_tool_names()` is a set union and
    `agent.chemclaw_agent._narrow` keys its lookup by name, so the loser vanished with no error.

    Raises:
        ConnectorError: naming both claimants and what each declares the name as. Loud at build
            time is the whole point — the alternative is a capability that is simply absent from
            the agent's surface, which reads as a broken tool rather than a misconfiguration.
    """
    owner: dict[str, tuple[str, str]] = {}
    for manifest in enabled():
        served = () if manifest.endpoint is None else manifest.endpoint.tools
        declared = [(name, "tool") for name in served]
        declared += [(job.name, "job") for job in manifest.jobs]
        for name, kind in declared:
            claimed = owner.get(name)
            if claimed is not None:
                connector, as_kind = claimed
                raise ConnectorError(
                    f"connector {manifest.name!r} declares {kind} {name!r}, which connector "
                    f"{connector!r} already provides as a {as_kind}"
                )
            owner[name] = (manifest.name, kind)
    return owner


def job_tools() -> list[CapabilityTool]:
    """The generated launcher for every job declared by an enabled connector.

    Two enabled connectors claiming one name is a configuration error rather than a last-one-wins,
    and the check is `_declared_tool_names`'s because the collision is not specific to jobs — see
    there for the rule and for the collision that was live when it was written.
    """
    _declared_tool_names()
    return [build_job_tool(manifest.name, job) for manifest in enabled() for job in manifest.jobs]


def job_names() -> list[str]:
    """Every declared job name across the enabled connectors, sorted.

    Distinct from `connector_tool_names`, which unions these with each endpoint's *MCP* tools. The
    caller that wants this one wants the jobs specifically — `chemclaw.agent.plan_gate` gates
    durable launches and must not gate a connector's read tools — and deriving it from the same
    `enabled()` walk is what keeps the two answers consistent as bundles come and go.
    """
    return sorted(job.name for manifest in enabled() for job in manifest.jobs)


def state_changing_tool_names() -> list[str]:
    """Every enabled connector tool that spends real resources or writes data, sorted.

    Both halves of a bundle's surface: each endpoint's declared `state_changing` subset, plus every
    declared job — a job is durable work by construction, so it needs no declaration to be one.

    Read by `chemclaw.agent.plan_gate` to decide what an unapproved harness plan may not call.
    Assembled here rather than listed in core because whether a connector tool calculates or merely
    looks up is the *bundle's* fact: `compute_xtb_energy` runs a semiempirical calculation and
    caches it, `resolve_compound` is a lookup, and core cannot tell them apart from the name. A
    copy of that knowledge in core would be a second source of truth that goes stale the first time
    a bundle changes what a tool does.
    """
    names: set[str] = set()
    for manifest in enabled():
        if manifest.endpoint is not None:
            names.update(manifest.endpoint.state_changing)
        names.update(job.name for job in manifest.jobs)
    return sorted(names)


def find_job(name: str) -> tuple[str, JobSpec]:
    """Resolve a declared job name to its connector and spec, or raise naming the valid ones.

    The lookup a template's `job` step needs: it names a job the way the model does, and has to turn
    that into the connector, workflow type and queue `ConnectorJobWorkflow` requires. Every declared
    name is unique across the enabled connectors — `_declared_tool_names` refuses a collision with
    an endpoint tool as well as with another job — so one name resolves to exactly one job.
    """
    for manifest in enabled():
        for job in manifest.jobs:
            if job.name == name:
                return manifest.name, job
    valid = sorted(job.name for manifest in enabled() for job in manifest.jobs)
    raise ConnectorError(f"unknown connector job {name!r}; declared jobs: {valid}")


def endpoint_tool_names(servers: Iterable[str] | None = None) -> list[str]:
    """The MCP tools the enabled connectors' *endpoints* serve, sorted; `servers` selects bundles.

    Distinct from `job_names` (the generated launchers, which are in-process registry tools) and
    from `connector_tool_names` (their union). The caller that wants this one wants the half that
    travels over MCP specifically, because that is the half a profile's `mcp_server_names` selects:
    `chemclaw.agent.chemclaw_agent.advertised_tool_names` has to answer "what will this profile's
    agent actually be able to call" without *building* the connector tools, since constructing one
    opens an httpx client that nothing would then close.

    Args:
        servers: The connector names to include; `None` (the default) means every enabled bundle.
            A name here that no enabled bundle provides is silently ignored — this function reports
            a surface, and `connector_tools` is where a profile naming an unknown connector fails.
    """
    names: set[str] = set()
    for manifest in enabled():
        if servers is not None and manifest.name not in servers:
            continue
        if manifest.endpoint is not None:
            names.update(manifest.endpoint.tools)
    return sorted(names)


def connector_tool_names() -> list[str]:
    """Every tool name the enabled connectors advertise — endpoint tools and job tools, sorted.

    The set `chemclaw.cli.validate_skills` and `chemclaw.cli.validate_prose_contract` check
    declared names
    against, so a skill or a prompt that teaches a connector tool cannot outlive it.
    """
    return sorted(set(endpoint_tool_names()) | set(job_names()))
