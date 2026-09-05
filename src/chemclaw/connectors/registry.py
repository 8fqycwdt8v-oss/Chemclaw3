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
from chemclaw.core.tool_registry import CapabilityTool, registered_tools

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


#: Why each bundle on disk could not be loaded, by directory name, for the discovery generation
#: `discovered()` last computed. A module-level dict rather than a second cached function because
#: `discovered.cache_clear()` is the seam every test uses and there must be exactly one cache to
#: clear: this is refilled inside `discovered()`'s uncached body, so it always describes the same
#: generation as the mapping that function returns. Read it through `discovery_problems()`.
_DISCOVERY_PROBLEMS: dict[str, str] = {}


@cache
def discovered() -> dict[str, tuple[Path, ConnectorManifest]]:
    """Every bundle that *loaded*, by name, with its directory — regardless of enablement.

    Cached because discovery reads and parses every manifest on disk, while the result is fixed
    for the process's lifetime (config is read once at import, and bundles do not appear at run
    time). `discovered.cache_clear()` is the seam a test uses after pointing `connectors_dir`
    elsewhere.

    **A bundle that does not load costs itself and nothing else.** This used to build the mapping
    in one comprehension, so a single unparseable `connector.yaml` anywhere on `connectors_dirs`
    raised out of *every* caller of `enabled()` — the agent build, `bearer_token_env_names()`
    (which is the log and webhook redaction set, so the failure silently widened what could be
    logged), `kg.note.known_note_types`, the health sweep — in a deployment that had never enabled
    that bundle. That directly contradicts this module's own first paragraph: discovery is not
    enablement. The failure is not swallowed, it is *deferred to the party that can act on it*: it
    is logged here at WARNING once per discovery, `enabled()` raises when the deployment actually
    turns that bundle on, and `discovery_problems()` is how a validator reports every one of them.
    """
    loaded: dict[str, tuple[Path, ConnectorManifest]] = {}
    problems: dict[str, str] = {}
    for bundle in _bundle_dirs():
        try:
            loaded[bundle.name] = (bundle, _load_manifest(bundle))
        except ConnectorError as exc:
            problems[bundle.name] = str(exc)
    _DISCOVERY_PROBLEMS.clear()
    _DISCOVERY_PROBLEMS.update(problems)
    if problems:
        # WARNING rather than ERROR: nothing is broken yet for a deployment that does not enable
        # these, and `enabled()` is where it becomes an error for one that does. Loud enough that
        # "the connector I dropped in is not there" is answerable from the log.
        logger.warning(
            "%d connector bundle(s) on disk could not be loaded and are not available to be "
            "enabled: %s",
            len(problems),
            "; ".join(f"{name}: {problem}" for name, problem in sorted(problems.items())),
        )
    return loaded


def discovery_problems() -> dict[str, str]:
    """Why each bundle on disk failed to load, by directory name — empty when every one did.

    The half `discovered()` cannot return, exposed because a bundle that is broken while disabled
    is a bundle nobody can ever enable, and CI is where that should surface rather than the day an
    operator turns it on. `chemclaw.cli.validate_connectors` is the caller that reports every entry.

    Calls `discovered()` first so the answer describes the current cache generation rather than
    whatever the last uncached run left behind — the two are one fact and must not be readable
    apart.
    """
    discovered()
    return dict(_DISCOVERY_PROBLEMS)


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
        The variable names.

    Raises:
        Whatever `enabled()` raises on a malformed manifest — this function has no handler and does
        not swallow. Both callers catch, and each decides how loudly to say so: `core.logging` at
        ERROR with a counter, `deliver.message` on the path that leaves the cluster. An earlier
        version of this docstring promised `()` on failure, which no code here delivers.
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

    **A bundle that failed to load is loud exactly where it is enabled, and nowhere else**
    (`discovered`). Two cases, and the asymmetry is the whole point of the seam:

    * The enable-list *names* it. That is the "a name in the list that no bundle provides" rule
      with more force, not less — the bundle is present and invalid, so the deployment asked for a
      capability that cannot be built. Refusing names the file to fix.
    * The enable-list is empty, so discovery *is* enablement. Every bundle on disk is enabled by
      that default, which makes a broken one an enabled broken one; refusing is the same rule.

    What is left is the case this function used to fail on and no longer does: a bundle nobody
    enabled. It costs its own capability, is logged once at discovery, and is reported in full by
    `make connector-validate` through `discovery_problems()`.
    """
    found = discovered()
    problems = discovery_problems()
    names = settings.connectors_enabled_list
    if not names:
        if problems:
            raise ConnectorError(
                "every discovered connector is enabled (connectors_enabled is empty) and "
                f"{len(problems)} bundle(s) could not be loaded: "
                + "; ".join(f"{name}: {problem}" for name, problem in sorted(problems.items()))
            )
        return [manifest for _, manifest in found.values()]
    broken = sorted(set(names) & problems.keys())
    if broken:
        raise ConnectorError(
            f"connectors_enabled names connector(s) {broken} whose manifest could not be loaded: "
            + "; ".join(f"{name}: {problems[name]}" for name in broken)
        )
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

    **The bundle package is in the set for the same reason, one level higher again.** `chem` is a
    bundle whose *directory* still ships here; a bundle discovered on an operator's own
    `connectors_dir` — the `PATH`-style override `_bundle_dirs` implements and `ARCHITECTURE.md`
    advertises — has no `chemclaw.connectors.<name>` package at all, so the missing name is the
    bundle rather than its `server` child. Without it, `make connector-validate` failed by
    construction for every out-of-tree bundle, with a message describing a *broken* server module,
    and the only way to get CI green was to stop validating that directory. Treated as `chem` is
    instead: name-checked, and reported by `unverified_tool_surfaces()` as a surface this tree
    cannot verify.
    """
    bundle = f"chemclaw.connectors.{connector}"
    package = f"{bundle}.server"
    target = f"{package}.tools"
    try:
        return importlib.import_module(target)
    except ModuleNotFoundError as exc:
        if exc.name in {target, package, bundle}:
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


def max_request_timeout_seconds() -> float:
    """The largest per-call bound that can still fire *inside* a turn, derived not chosen.

    A manifest's `request_timeout` is the bound whose expiry the model can recover from: the MCP
    session raises, `agent.tool_authz` hands back a `transport_error_result`, and the chemist gets
    an answer that says a tool was unavailable. The front door's `service_turn_timeout_seconds`
    bound is the one nobody recovers from — it takes the whole turn. So the first must be strictly
    smaller than the second, and the shipped tree did not have that property: `calc` declares
    `request_timeout: 600` against a 600 s turn deadline, which makes *which one fires* a race.

    The margin is `READ_TIMEOUT_GRACE_SECONDS` rather than a number invented here, because that is
    already the interval this module keeps between its two nested bounds and for the identical
    reason (`connector_http_client`): the visible bound must trip before the invisible one. Laid
    end to end the chain is now `request_timeout` < httpx's read bound (`+ grace`) <= the turn
    deadline, so the bound that *raises* is always the one that fires first.

    Returns:
        The ceiling in seconds. Deployments whose turn deadline is not even one grace interval long
        get `0.0`, which `request_timeout_seconds` reads as "there is no room to clamp into" and
        leaves declarations alone rather than manufacturing a zero-second timeout.
    """
    return max(settings.service_turn_timeout_seconds - _READ_TIMEOUT_GRACE_SECONDS, 0.0)


#: Connectors already warned about a clamped `request_timeout`, so the line is written once per
#: process rather than twice per turn per connector. Keyed by the declared number as well as the
#: name so a deployment that fixes a manifest and reloads is told again if it is still too large.
_CLAMPED_TIMEOUTS: set[tuple[str, int]] = set()


def request_timeout_seconds(endpoint: Endpoint, connector: str = "") -> float:
    """How long one call to this endpoint may take — the single derivation of that number.

    Two independent bounds are built from it (the MCP session's `read_timeout_seconds` and the
    httpx read timeout), and they must stay in a fixed relationship to each other, so neither may
    read the manifest on its own. Public because a test proving that relationship has to compare
    the same number a deployment uses.

    `StdioEndpoint` declares no timeout at all — a subprocess of our own process is not a network
    dependency — but an unresponsive subprocess hangs a turn exactly as a mute HTTP host does, so
    it gets the same default rather than an exemption.

    **A declaration above the deployment's ceiling is lowered, not obeyed and not refused**
    (`max_request_timeout_seconds`). `HttpEndpoint.request_timeout` is `gt=0` and nothing else, so a
    third-party bundle could declare `100000` and get it: the turn deadline would fire first, the
    model would never receive the recoverable transport error, and the chemist would lose the whole
    turn instead of one tool call. Clamping is the same shape `JobSpec.timeout_seconds` already
    has — `min(what the bundle asks, what the deployment funds)`, a lowering of the deployment's
    ceiling and never a raise — and it is preferred to refusing the manifest for the reason
    `mcp_connections` gives about raising on the turn path at all. The clamp is announced once per
    process with both numbers and the setting that produced the ceiling.

    Args:
        endpoint: The endpoint whose declaration to read.
        connector: The bundle's name, for the WARNING when its declaration is clamped. Defaulted
            because the two bound-builders below have the endpoint and not the name, and a missing
            name costs the log line a word rather than costing the clamp.
    """
    if isinstance(endpoint, HttpEndpoint) and endpoint.request_timeout is not None:
        declared = float(endpoint.request_timeout)
        ceiling = max_request_timeout_seconds()
        if 0.0 < ceiling < declared:
            key = (connector, endpoint.request_timeout)
            if key not in _CLAMPED_TIMEOUTS:
                _CLAMPED_TIMEOUTS.add(key)
                logger.warning(
                    "connector %s declares request_timeout: %s, which is not below the %ss turn "
                    "deadline (service_turn_timeout_seconds); a call that ran that long would be "
                    "killed with the turn instead of returning a recoverable error. Using %ss.",
                    connector or "(unnamed)",
                    endpoint.request_timeout,
                    settings.service_turn_timeout_seconds,
                    ceiling,
                )
            return ceiling
        return declared
    return _DEFAULT_REQUEST_TIMEOUT_SECONDS


def _session_kwargs(endpoint: Endpoint, connector: str) -> dict[str, Any]:
    """The `ClientSession` arguments that give a tool call a deadline at all.

    This is the bound that actually fires: `mcp.shared.session.send_request` waits inside
    `anyio.fail_after(read_timeout_seconds)` and raises `McpError` when it expires. Without it the
    argument is `None` and the wait is unbounded — see `_DEFAULT_REQUEST_TIMEOUT_SECONDS`.
    `langchain-mcp-adapters` forwards `session_kwargs` verbatim into `ClientSession`, and both
    transports' connection mappings accept it, so one function serves both branches.
    """
    return {"read_timeout_seconds": timedelta(seconds=request_timeout_seconds(endpoint, connector))}


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
    its Service port) could answer the MCP POST with a `302` toward an origin it controls and
    collect the caller's Entra object id, session and correlation id once per turn. (This sentence
    used to add "all shipped manifests declare `auth: mode: none`" as the reason anything on the
    port qualifies. Every endpoint-bearing manifest in this tree declares
    `mode: bearer`
    (D-2026-08-20), and has since before this paragraph was last edited — the guard is right and
    the stated stakes were a snapshot of a fleet that no longer exists. It never carried the
    caller's *role set*: `X-Chemclaw-Roles` was deleted by
    `D-2026-08-26-an-entitlement-set-is-not-provenance`.) MCP streamable-HTTP needs no redirect
    for any real flow:
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
            request_timeout_seconds(endpoint, connector) + _READ_TIMEOUT_GRACE_SECONDS,
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


def unusable_reason(connector: str, endpoint: Endpoint | None) -> str | None:
    """Why *this deployment* cannot open this bundle's endpoint at all, or `None` when it can.

    A **configuration** verdict, not a network one, and the distinction is what makes it worth
    having in one place: every reason here is decidable offline, is the same on every turn, and is
    an operator's to fix. A dark host is `connectors.health`'s question and is asked over a socket;
    this one is asked of a manifest and an environment.

    Two callers, which is why it exists rather than living inside the one function that used to
    raise it. `mcp_connections` skips such a bundle so it costs its own tools instead of the turn,
    and `connectors.health.probe_connectors` reports it `unusable` so `/readyz`, the unhealthy
    gauge and `connectors_required` can all see the thing that `mcp_connections` degraded past —
    the half without which "degraded" quietly becomes "silent".

    One reason today, measured: **`transport: stdio` while `connector_stdio_enabled` is false.**
    Refusing is right — `command` is executed in the chat process — and *raising* was not:
    `mcp_connections` built every spec in one comprehension, `connector_specs()` is called inline
    by `api/runner.py` with no handler anywhere in `api/` or `agent/`, so one such manifest landing
    on `connectors_dir` (the documented PATH-style operator override) killed **every turn** before
    any tool bound, while the startup sweep reported that bundle `unprobed` and
    `connectors_required` never saw it. That inverts `connectors.transport`'s own trade, one module
    away: losing a capability is a much smaller failure than losing the turn.

    **A declared bearer whose variable is unset is deliberately *not* one of these**, and the
    reason is a measurement rather than a principle. It is the same class of fault — decidable
    offline, permanent, an operator's to fix — and it is the one this sweep is worst at seeing,
    because the probe hits the unauthenticated `/healthz` and reports the bundle healthy while
    every tool call is refused. Adding it here was tried and reverted: no token is mounted in an
    ordinary test, CLI or worker process, so every shipped bearer bundle became unusable at once
    and `connector_specs()` returned `[]` — five test files outside this seam changed meaning,
    which is a change to what a tokenless process *is*, not a bug fix. It needs its own decision.
    What is fixed instead is the diagnosis: `transport.describe_failure` unwraps the task group so
    the `MissingConnectorCredential` and the variable's name reach the log, and
    `core.mcp_session.bearer_from_env` refuses the other hop rather than calling it anonymously.

    Takes the endpoint rather than the manifest, because that is what it reads and because both
    callers already hold the two separately — `_mcp_connection` is handed them apart, and a
    jobs-only bundle has `None` here and nothing to judge.

    Args:
        connector: The bundle's name, for the sentence an operator reads.
        endpoint: Its declared endpoint, or `None` for a bundle whose capability is durable work.

    Returns:
        A sentence naming the bundle, the fault and the knob that fixes it, or `None`.
    """
    if isinstance(endpoint, StdioEndpoint) and not settings.connector_stdio_enabled:
        return (
            f"connector {connector!r} declares `transport: stdio`, which launches "
            f"{endpoint.command!r} in this process; it is disabled by default because a manifest "
            "is data. Set CHEMCLAW_CONNECTOR_STDIO_ENABLED=true to allow it."
        )
    return None


def _mcp_connection(manifest: ConnectorManifest, endpoint: Endpoint) -> ConnectorSpec:
    """Describe one connector endpoint for the LangGraph engine (M7).

    Dispatches on the `Endpoint` union for the reason `request_timeout_seconds` and
    `_session_kwargs` do: the transports differ only in how the server is *reached*, and everything
    bounding what the agent may do with it is identical on both. (This docstring called itself "the
    twin of `_mcp_tool`" from the commit that wrote it — a function that has never been defined in
    this repository, which made a reader look for a second dispatcher over the union and find one
    function plus its client factory.)

    **The HTTP client is still ours, and that is what keeps four security properties alive.**
    `httpx_client_factory` is the seam `langchain-mcp-adapters` exposes, so `connector_http_client`
    crosses unchanged and with it the refusal to follow redirects (a connector answering `302`
    would otherwise harvest the caller's Entra oid, session and correlation id — not its role
    set, which `D-2026-08-26-an-entitlement-set-is-not-provenance` stopped sending),
    `turn_identity_hook`, `auth_for`,
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
                session_kwargs=_session_kwargs(endpoint, manifest.name),
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
        #
        # `mcp_connections` filters on the same predicate before it ever gets here, so on the turn
        # path this is unreachable and a `transport: stdio` bundle costs its own tools rather than
        # the turn. It stays because the refusal is a *security* control and this is the function
        # that would otherwise launch the subprocess: a second caller reaching for a spec must not
        # be able to get one by not knowing to ask `unusable_reason` first.
        refusal = unusable_reason(manifest.name, endpoint)
        if refusal is not None:
            raise ConnectorError(refusal)
        # No identity headers, for the same reason the HTTP branch above attaches them: a subprocess
        # of our own process runs under our own identity, with no outbound request to attach them to
        # (`connectors.identity`).
        return ConnectorSpec(
            name=manifest.name,
            connection=StdioConnection(
                transport="stdio",
                command=endpoint.command,
                args=list(endpoint.args),
                session_kwargs=_session_kwargs(endpoint, manifest.name),
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
    """One connection spec per enabled connector this deployment can actually open (unopened).

    The deployment's whole surface; `chemclaw.agent.chemclaw_agent.connector_specs` is the
    profile-narrowed half. Split that way because enablement is a deployment decision and narrowing
    is a per-turn one, and a profile must never be able to widen what the deployment enabled.

    **A bundle this deployment cannot open costs its own tools, never the turn**
    (`unusable_reason`).
    This is on the pre-first-token path of every turn through `api/runner.py`, which has no handler
    for a `ConnectorError` — nor does anything else in `api/` or `agent/` — so raising from here
    ended the conversation over one manifest. It is announced instead, in the vocabulary the
    package already uses for a connector that contributes nothing: a WARNING naming each one, and
    `chemclaw_connectors_unreachable_total`. `connectors.health` is where the same verdict reaches
    `/readyz` and `connectors_required`, so degrading here does not make it silent.
    """
    specs: list[ConnectorSpec] = []
    unusable: list[str] = []
    for manifest in enabled():
        if manifest.endpoint is None:
            continue
        refusal = unusable_reason(manifest.name, manifest.endpoint)
        if refusal is not None:
            unusable.append(refusal)
            continue
        specs.append(_mcp_connection(manifest, manifest.endpoint))
    if unusable:
        # The same WARNING-and-count vocabulary `open_connector_specs` uses for a connector that
        # did not come up, and deliberately the *same counter*: from the turn's point of view the
        # outcome is identical — those tools are absent — and a second series would split one
        # "capability is missing" alert in two. It differs from an outage in that it will not clear
        # on its own, which is what makes the health sweep's `unusable` verdict the other half.
        logger.warning(
            "%d enabled connector(s) cannot be opened by this deployment and contribute no tools: "
            "%s",
            len(unusable),
            "; ".join(unusable),
        )
        record_metric(lambda m: m.increment("chemclaw_connectors_unreachable_total", len(unusable)))
    return specs


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


def _bound_by_this_process() -> dict[str, str]:
    """Every tool name core binds itself, mapped to the phrase naming what it binds it as.

    The half `_declared_tool_names` could not see, and the reason it could not is that a manifest
    walk only ever meets other manifests. Measured: a bundle declaring an endpoint tool
    `find_notes` bound **two** tools of that name — `ToolNode` keys `tools_by_name` by name and
    `build_langgraph_agent` appends the connector tools *after* the in-process ones, so the
    connector won and the knowledge-graph read was never invoked. A *job* of that name failed the
    other way and worse: the launcher was dropped in silence, while `state_changing_tool_names()`
    kept reporting the name, so a pure graph read landed in `side_effecting_tools()` and
    `expensive_actions()` — refused by the plan gate under an unapproved plan and subtracted from
    every helper's tool set. `make connector-validate` passed for both.

    **Importing the agent here is what makes the check exist in the validator too.** The registry
    is populated by import side effect (`chemclaw.agent.tool_modules`), so a process that has not
    imported it sees an empty registry and would get a check that silently tests nothing —
    measured, `make connector-validate` holds **0** registered tools at the moment it calls
    `job_tools()`. `chemclaw.cli.validate_templates` does the same import for the same reason and
    says so; that this is the declared `connectors -> agent` edge rather than a new one is
    `tests/test_layering.py`'s record. It is a function-scope import because
    `chemclaw.agent.chemclaw_agent` imports this module back at module scope, and it costs nothing
    in the chat pod, which has imported it before the first turn.

    **A generated launcher is excluded, and that exclusion is what keeps a second build working.**
    `build_langgraph_agent` runs once per profile and registers this registry's own job launchers
    into the very registry read here — so reading them back would make every deployment with jobs
    fail on its second build, on a name it declared itself. The launchers are recognised by the
    module that generated them rather than by a marker, so there is nothing to remember to set.

    **Template launchers are asked for by name rather than read out of the registry, and that is
    the fix for a guard that depended on import order.** They are registered by the same function
    that registers the job launchers — `_register_generated_tools` evaluates
    `[*job_tools(), *template_tools()]`, and `job_tools()` is what runs this check — so on the
    *first* build of a process no `run_<name>` was in the registry yet. Measured with a bundle
    declaring an endpoint tool `run_tautomer_resolution`: build #1 passed and the connector tool
    *shadowed* the template launcher (`ToolNode` keys by name and connector tools are appended
    last), while build #2 onward raised `ConnectorError` and killed the turn — the same
    configuration reading as two different faults depending on how many agents a process had built.
    `make connector-validate` exited 0, because a validator registers no template launchers at all.
    Asking `chemclaw.templates.registry` what it will generate makes the answer the same on every
    build and in every process, which is what a collision guard has to be.
    """
    from chemclaw.agent import chemclaw_agent
    from chemclaw.templates.registry import template_tool_names

    bound = {
        fn.__name__: "an in-process tool"
        for fn in registered_tools()
        if fn.__module__ != build_job_tool.__module__
    }
    bound.update(dict.fromkeys(template_tool_names(), "a template launcher"))
    bound.update(dict.fromkeys(chemclaw_agent.skill_tool_names(), "a scratchpad file verb"))
    bound.update(dict.fromkeys(chemclaw_agent.harness_tool_names(), "a plan-harness tool"))
    bound.update(dict.fromkeys(chemclaw_agent.subagent_tool_names(), "the subagent spawner"))
    return bound


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

    **A first-party name is claimed too, and by whoever holds it** (`_bound_by_this_process`).
    The rule was always stated in general terms — one name is one capability — and checked in one
    direction only, which is how a bundle came to be able to take over `propose_knowledge_note`
    with every gate still firing on the name and the note body going to the connector's server.

    Raises:
        ConnectorError: naming both claimants and what each declares the name as. Loud at build
            time is the whole point — the alternative is a capability that is simply absent from
            the agent's surface, which reads as a broken tool rather than a misconfiguration.
    """
    owner: dict[str, tuple[str, str]] = {}
    bound = _bound_by_this_process()
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
            held = bound.get(name)
            if held is not None:
                raise ConnectorError(
                    f"connector {manifest.name!r} declares {kind} {name!r}, which this deployment "
                    f"already binds as {held}; a connector cannot take a first-party capability's "
                    "name, because the name is the authorization key and the model has only one "
                    "of them to call"
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
