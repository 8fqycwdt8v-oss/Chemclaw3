"""The connector registry: discover bundles, validate them, and build what the agent advertises.

Bundles are discovered from the filesystem (a connector is a folder, like a skill) and enabled by
config (`connectors_enabled`), so a repo can ship every connector and a deployment run a subset.

A turn binds two products of a manifest: the MCP tools each `endpoint:` advertises over a session
held for the turn (`chemclaw.connectors.identity`), and one generated launcher per `jobs:` entry
(`chemclaw.connectors.jobs`). Nothing here decides whether a call is allowed: the authorization
middlewares and profiles wrap what this assembles, so a connector can add to what is offered and
never to what is permitted.
"""

import asyncio
import importlib
import logging
import os.path
from collections.abc import Iterable
from contextlib import AsyncExitStack
from datetime import timedelta
from functools import cache, partial
from pathlib import Path
from types import ModuleType
from typing import Any, assert_never

import httpx
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.sessions import StdioConnection, StreamableHttpConnection
from pydantic import ValidationError

from chemclaw.connectors.identity import auth_for
from chemclaw.connectors.jobs import build_job_tool, unavailable_reason
from chemclaw.connectors.manifest import (
    ConnectorManifest,
    Endpoint,
    HttpEndpoint,
    JobSpec,
    StdioEndpoint,
)
from chemclaw.connectors.transport import ConnectorSpec, HeldConnectorSession
from chemclaw.core.call_identity import turn_identity_hook
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.http import default_ssl_context
from chemclaw.core.manifest_io import read_manifest, within_root
from chemclaw.core.mcp_session import CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_GRACE_SECONDS
from chemclaw.core.metrics import Metrics
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.core.tool_registry import CapabilityTool, registered_tools

logger = logging.getLogger(__name__)

# The manifest filename inside a bundle. A constant because two modules look for it (here and
# `scripts.validate_connectors`) and a typo in either would report "no connectors found".
MANIFEST_FILENAME = "connector.yaml"

# Transport bounds shared by every outbound MCP client live in `core/mcp_session.py`.
_CONNECT_TIMEOUT_SECONDS = CONNECT_TIMEOUT_SECONDS

# How long a tool call may take when the manifest does not say. Without a value the MCP session
# waits forever; this is what a third-party bundle gets, generous for a slow tool but finite so a
# mute connector cannot hold a turn.
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 60.0

_READ_TIMEOUT_GRACE_SECONDS = READ_TIMEOUT_GRACE_SECONDS

# What one configured connector endpoint becomes, whichever transport it declares. Both open into a
# session advertising the same agent-facing surface, so callers never branch on the transport.


class ConnectorError(ChemclawError):
    """A connector bundle is malformed, or an enabled connector does not exist.

    A `ChemclawError` (so a `ValueError`), like other startup configuration errors. Registered by
    name in `chemclaw.durable.publish._BAD_DATA_TYPES` (Temporal matches by exact name), so a
    template step naming an unknown job fails on its first attempt.
    """


@cache
def _bundle_dirs_by_name(dirs: tuple[str, ...]) -> dict[str, tuple[Path, ...]]:
    """Every connector bundle directory found across `dirs`, by name, in path order.

    Every directory, not only the winner: a name collision decides which manifest describes the
    capability, not which content exists on disk (see `_bundle_content_dirs`). Sorted by name so
    tool order (part of the prompt) is identical on every machine; within a name, path order is the
    precedence. Cached on `dirs`; `forget_discovered` clears it.
    """
    found: dict[str, list[Path]] = {}
    for directory in dirs:
        root = Path(directory)
        if not root.is_dir():
            continue
        for path in sorted(root.iterdir()):
            if (path / MANIFEST_FILENAME).is_file() and within_root(root, path):
                found.setdefault(path.name, []).append(path)
    return {name: tuple(found[name]) for name in sorted(found)}


def _bundle_dirs(dirs: tuple[str, ...]) -> list[Path]:
    """The directory that wins each bundle name, sorted by name.

    First dir wins, like `PATH`, so an operator's private dir can override a shipped bundle. A
    shadowed manifest is never parsed, so an override cannot fail startup over a file the system
    does not use.
    """
    return [paths[0] for paths in _bundle_dirs_by_name(dirs).values()]


def _load_manifest(bundle: Path) -> ConnectorManifest:
    """Parse and validate one bundle's `connector.yaml`, raising `ConnectorError` on any problem.

    The folder name is authoritative: a manifest whose `name` disagrees would be enabled under one
    name and looked up under another.
    """
    path = bundle / MANIFEST_FILENAME
    raw = read_manifest(path, ConnectorError)
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
def _discovered_in(dirs: tuple[str, ...]) -> dict[str, tuple[Path, ConnectorManifest]]:
    """Every bundle found under `dirs`, by name, with its directory: validated, cached on `dirs`.

    Cached because discovery parses every manifest on disk; keyed on the directories because they
    are its only input, so a repointed directory is simply a different entry.
    """
    return {bundle.name: (bundle, _load_manifest(bundle)) for bundle in _bundle_dirs(dirs)}


def discovered() -> dict[str, tuple[Path, ConnectorManifest]]:
    """Every discovered bundle by name, with its directory: validated, regardless of enablement.

    Reads the settings outside the cache, so a changed `connectors_dir` is seen on the next call.
    """
    return _discovered_in(tuple(settings.connectors_dirs))


def forget_discovered() -> None:
    """Drop the cache so the next `discovered()` re-reads bundle manifests from disk.

    Needed only when new manifests are written into an already-discovered directory (same key);
    repointing `connectors_dir` is a different key. A named function, matching the repo's other
    `forget_*` test-isolation resets, so callers type-check.
    """
    _discovered_in.cache_clear()
    # The directory walk is cached on the same key and goes stale the same way.
    _bundle_dirs_by_name.cache_clear()


def bearer_token_env_names() -> tuple[str, ...]:
    """Every environment variable holding an enabled connector's bearer token.

    The one definition used by both scrubs: `core.logging.SecretRedactingFilter` (log lines) and
    `deliver.message.Message.redacted` (webhooks); an opaque token matches no structural pattern, so
    it must be named. Propagates whatever `enabled()` raises; both callers catch and report it.
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

    An empty `connectors_enabled` means every discovered bundle that declares `default_enabled`. An
    explicit list overrides `default_enabled` rather than being filtered by it, which is how an
    opt-in bundle becomes reachable. A listed name no bundle provides is a loud error, not a
    capability that silently stops working.
    """
    found = discovered()
    names = settings.connectors_enabled_list
    if not names:
        return [manifest for _, manifest in found.values() if manifest.default_enabled]
    unknown = sorted(set(names) - found.keys())
    if unknown:
        raise ConnectorError(
            f"connectors_enabled names unknown connector(s) {unknown}; discovered: {sorted(found)}"
        )
    return [found[name][1] for name in names]


def server_tools_module(connector: str) -> ModuleType | None:
    """A bundle's `server.tools` module, or `None` when the bundle ships no MCP server at all.

    The one definition shared by `make connector-validate` and `make template-validate`. Callers
    skip endpoint-less bundles, so `None` means an endpoint-declaring bundle with no module here: a
    `ModuleNotFoundError` naming the bundle package, its `server` package or the module itself (an
    out-of-tree bundle, or one whose server moved to the fleet). Any other import failure
    propagates, so a broken dependency cannot make a validator check less and still pass.
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

    Unioned with core's set by `chemclaw.kg.note.known_note_types`. Enabled bundles only: a note
    whose type came from a disabled bundle should fail validation.
    """
    return frozenset(name for manifest in enabled() for name in manifest.note_types)


def declared_relations() -> frozenset[str]:
    """Every graph relation the enabled bundles declare — the edge-side twin of the above."""
    return frozenset(name for manifest in enabled() for name in manifest.relations)


def skills_dirs() -> list[str]:
    """The `skills/` directory of every enabled connector, wherever on the path it is found.

    A connector's judgment ships with its capability; appended to `settings.skills_dirs`, so bundled
    skills are ordinary skills under every existing gate. Only existing directories are returned
    (`make connector-validate` reports missing ones), and a shadowed bundle's directory is included
    (see `_bundle_content_dirs`).
    """
    return _bundle_content_dirs("skills", enabled())


def _endpoint_url(connector: str, endpoint: HttpEndpoint) -> str:
    """The endpoint URL, after any per-deployment override for this connector.

    A manifest ships a loopback dev default; `connector_urls` lets Helm point at an in-cluster
    Service without patching a bundle.
    """
    return settings.connector_urls.get(connector, endpoint.url)


def request_timeout_seconds(endpoint: Endpoint) -> float:
    """How long one call to this endpoint may take: the single derivation of that number.

    Both the MCP session's `read_timeout_seconds` and the httpx read timeout derive from it and must
    keep a fixed relationship, so neither reads the manifest itself. Public so a test can compare
    the same number. A stdio endpoint gets the same default, since a hung subprocess hangs a turn
    too.
    """
    if isinstance(endpoint, HttpEndpoint) and endpoint.request_timeout is not None:
        return float(endpoint.request_timeout)
    return _DEFAULT_REQUEST_TIMEOUT_SECONDS


def _session_kwargs(endpoint: Endpoint) -> dict[str, Any]:
    """The `ClientSession` arguments that give a tool call a deadline at all.

    This is the bound that fires (`anyio.fail_after(read_timeout_seconds)` raises `McpError`);
    without it the wait is unbounded. `langchain-mcp-adapters` forwards `session_kwargs` on both
    transports.
    """
    return {"read_timeout_seconds": timedelta(seconds=request_timeout_seconds(endpoint))}


def connector_http_client(connector: str, endpoint: HttpEndpoint) -> httpx.AsyncClient:
    """The HTTP client one connector endpoint is reached with: the single definition of it.

    Public so tests exercise this client rather than a lookalike. It carries our credential as
    `auth` (so it is on the MCP handshake too) and the turn's identity as a request hook (a header
    callback would run in the wrong task and never land). The MCP adapter owns and closes it.

    Redirects are not followed, as a security property: httpx carries headers across a redirect
    (stripping only `Authorization`), so a connector answering `302` could harvest the caller's
    identity. MCP streamable-HTTP never needs a redirect; `turn_identity_hook` strips the headers on
    a foreign origin as a second layer.
    """
    return httpx.AsyncClient(
        auth=auth_for(endpoint.auth, connector),
        follow_redirects=False,
        # The process's one trust store, not a fresh CA-bundle parse per client per turn.
        verify=default_ssl_context(),
        # Never inherit an ambient proxy: a connector endpoint is an in-cluster Service, and an
        # HTTPS_PROXY on the pod must not silently reroute a tool call (and its bearer) elsewhere.
        trust_env=False,
        event_hooks={"request": [turn_identity_hook(_endpoint_url(connector, endpoint))]},
        # Without this httpx applies 5 s to every phase and tears down a slow tool call's stream.
        # Connect stays short so a dead host degrades fast. The read bound is looser than the MCP
        # session's (`_session_kwargs`): when the httpx timeout fires first the answer is lost
        # silently, so the session bound, which raises, must win. See `_READ_TIMEOUT_GRACE_SECONDS`.
        timeout=httpx.Timeout(
            request_timeout_seconds(endpoint) + _READ_TIMEOUT_GRACE_SECONDS,
            connect=_CONNECT_TIMEOUT_SECONDS,
        ),
    )


def health_url(manifest: ConnectorManifest) -> str | None:
    """Where to probe this connector, moved to wherever its endpoint actually is.

    The health probe must not read `health_url` off the manifest: `connector_urls` moves the
    endpoint in every cluster, and the declared URL is a loopback dev default. The move re-applies
    the difference between the manifest's health and endpoint URLs at the effective address, because
    deployments differ in path layout (one Service per bundle vs. the dev composite's `/<name>/`
    mounts), not just host.

    Returns None when the bundle declares no health route, which the probe reports as `unprobed`.
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
        # The override does not share the manifest endpoint's suffix, so there is nothing to
        # re-root; probing the declared URL may be wrong but is not silently invented.
        return endpoint.health_url
    return effective.removesuffix(endpoint_tail) + health_tail


def queues_tools(manifest: ConnectorManifest) -> bool:
    """Whether this bundle routes tool calls through its interactive queue, and so needs a worker.

    The one answer for the reachability sweep and the live lane (which starts one
    `interactive_worker` per such bundle), matching the chart's interactive-worker Deployments.
    """
    endpoint = manifest.endpoint
    return isinstance(endpoint, HttpEndpoint) and endpoint.queued is not None


def _mcp_connection(manifest: ConnectorManifest, endpoint: Endpoint) -> ConnectorSpec:
    """Describe one connector endpoint for the LangGraph engine.

    Dispatches on the `Endpoint` union; transports differ only in how the server is reached. The
    HTTP client is ours, passed through `httpx_client_factory`, so the redirect refusal, identity
    hook, credential and split timeouts all survive; the library's `timeout`/`auth`/`headers` are
    not passed. The adapter closes the client it builds. `session_kwargs` is passed on both
    transports, since it is the only per-request deadline.
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
            queued=endpoint.queued,
            request_timeout=request_timeout_seconds(endpoint),
        )
    if isinstance(endpoint, StdioEndpoint):
        # Refused unless the deployment enables it: `command` executes in the chat process, so a
        # manifest written to `connectors_dir` would otherwise be code execution under the identity
        # holding every connector token. Checked here, not at parse time, so `StdioEndpoint` stays
        # constructible.
        if not settings.connector_stdio_enabled:
            raise ConnectorError(
                f"connector {manifest.name!r} declares `transport: stdio`, which launches "
                f"{endpoint.command!r} in this process; it is disabled by default because a "
                "manifest is data. Set CHEMCLAW_CONNECTOR_STDIO_ENABLED=true to allow it."
            )
        # No identity headers: a subprocess runs under our own identity, with no outbound request.
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
    """An `httpx_client_factory` that returns our client, ignoring what the library offers.

    `_mcp_connection` sets no headers, timeout or auth, so nothing is dropped; all of it is decided
    in `connector_http_client`.
    """

    def factory(**_ignored: Any) -> httpx.AsyncClient:
        return connector_http_client(connector, endpoint)

    return factory


def mcp_connections() -> list[ConnectorSpec]:
    """One connection spec per enabled connector that declares an endpoint (unopened).

    The deployment's whole surface; `chemclaw.agent.chemclaw_agent.connector_specs` narrows it per
    turn, so a profile can never widen what the deployment enabled.
    """
    return [
        _mcp_connection(manifest, manifest.endpoint)
        for manifest in enabled()
        if manifest.endpoint is not None
    ]


def connector_spec(name: str) -> ConnectorSpec:
    """How to reach one enabled connector by name, for a process that is not running a turn.

    Used by the interactive worker; built through `_mcp_connection` so its client has the same
    credential, redirect refusal and timeouts as a turn's.

    Raises:
        ConnectorError: `name` is not an enabled connector with an endpoint in this deployment.
    """
    for manifest in enabled():
        if manifest.name == name and manifest.endpoint is not None:
            return _mcp_connection(manifest, manifest.endpoint)
    raise ConnectorError(
        f"connector {name!r} is not enabled in this deployment, or declares no endpoint to call"
    )


def _count_unreachable(connector: str, metrics: Metrics) -> None:
    """Book one connector's absence from one turn, by name.

    A function bound with `partial` rather than a loop lambda, avoiding late binding and staying
    typeable.
    """
    metrics.increment("chemclaw_connectors_unreachable_total", labels={"connector": connector})


async def open_connector_specs(
    stack: AsyncExitStack, specs: Iterable[ConnectorSpec]
) -> tuple[list[BaseTool], list[str]]:
    """Open every connector for this turn; return the tools that came up and the names that did not.

    The single connector lifecycle for every caller that runs a turn; `stack` owns tearing the
    sessions down. Tools come back with the casualties because a connector's tools only exist once
    its session is open. Nothing is caught: a failed connect is non-fatal by construction
    (`chemclaw.connectors.transport`), so an unreachable connector contributes no tools and is
    retried next turn.

    Concurrent, so a dark fleet costs one connect timeout rather than their sum; safe because each
    `HeldConnectorSession` confines its cancel scope to its own task. The degradation is logged and
    counted here; callers that reach a human also surface the list themselves.
    """
    held = [HeldConnectorSession(spec) for spec in specs]
    opened = await asyncio.gather(*(stack.enter_async_context(session) for session in held))
    unreachable = [session.name for session in held if not session.connected]
    if unreachable:
        # WARNING, not ERROR: the turn still runs. A counter rate that stays above zero is a dark
        # connector.
        logger.warning(
            "%d connector(s) did not come up for this scope and contribute no tools: %s",
            len(unreachable),
            ", ".join(unreachable),
        )
        # One increment per connector, labelled by name (a registry bundle, never a caller's string,
        # so a safe label; see `core/metrics._COUNTER_LABELS`).
        for name in unreachable:
            record_metric(partial(_count_unreachable, name))
    return [tool for tools in opened for tool in tools], unreachable


def profiles_dirs() -> list[str]:
    """The `profiles/` directory of every enabled connector, wherever on the path it is found.

    Same rule as `skills_dirs`. A shadowed bundle's profile cannot widen anything: a profile varies
    only instructions and model route, and narrows itself to the tools the winning surface binds.
    """
    return _bundle_content_dirs("profiles", enabled())


def _bundle_content_dirs(kind: str, manifests: Iterable[ConnectorManifest]) -> list[str]:
    """Every named bundle's `<kind>/` directory, across every directory carrying that name.

    The winning manifest decides the tool surface; the directories on disk decide the content. For
    each enabled bundle, every directory carrying its name contributes an existing `<kind>/`, winner
    first, so a same-name replacement manifest that declares no skills cannot silently delete the
    shadowed bundle's judgment.

    A union rather than a startup refusal, so a deliberate replacement can still start; judgment
    about tools the winner does not serve is hidden by `agent.skill_access.ToolScopedSkills`. The
    manifest's declaration is not the gate: `cli/validate_connectors._bundle_content_problems`
    already holds declarations and directories equal for every validated bundle. Non-existent paths
    are never returned.
    """
    dirs: list[str] = []
    by_name = _bundle_dirs_by_name(tuple(settings.connectors_dirs))
    for manifest in manifests:
        for bundle in by_name.get(manifest.name, ()):
            candidate = bundle / kind
            if candidate.is_dir() and str(candidate) not in dirs:
                dirs.append(str(candidate))
    return dirs


def _bound_by_this_process() -> dict[str, str]:
    """Every tool name core binds itself, mapped to the phrase naming what it binds it as.

    Lets `_declared_tool_names` refuse a connector tool or job that collides with an in-process
    tool, which would otherwise shadow it or corrupt the plan gate's view of what is state-changing.

    Imports the agent (function scope, to avoid a cycle) so the tool registry is populated even in a
    validator process. Generated job launchers are excluded, since this registry registers them
    itself on every build. Template launcher names are asked of `chemclaw.templates.registry`
    directly, because they are registered after this check runs.
    """
    from chemclaw.agent import chemclaw_agent

    bound = {
        fn.__name__: "an in-process tool"
        for fn in registered_tools()
        if fn.__module__ != build_job_tool.__module__
    }
    bound.update(dict.fromkeys(chemclaw_agent.skill_tool_names(), "a scratchpad file verb"))
    bound.update(dict.fromkeys(chemclaw_agent.harness_tool_names(), "a plan-harness tool"))
    bound.update(dict.fromkeys(chemclaw_agent.subagent_tool_names(), "the subagent spawner"))
    # Asked for rather than read off `registered_tools()`, because the launchers are registered
    # *after* this check runs — see the paragraph above for the measurement.
    bound.update(dict.fromkeys(chemclaw_agent.template_tool_names(), "a step-template launcher"))
    return bound


def _declared_tool_names() -> dict[str, tuple[str, str]]:
    """Every tool name the enabled bundles advertise, mapped to `(connector, kind)`.

    One name is one capability, whichever half of a bundle declares it, and whether a bundle or core
    holds it (`_bound_by_this_process`): the name is the authorization key and what the model calls,
    and a duplicate would silently drop one tool or apply one gate to another's work.

    Raises:
        ConnectorError: naming both claimants and what each declares the name as.
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

    A name collision is a configuration error, checked by `_declared_tool_names`.
    """
    _declared_tool_names()
    withheld = set(withheld_job_names())
    return [
        build_job_tool(manifest.name, job)
        for manifest in enabled()
        for job in manifest.jobs
        if job.name not in withheld
    ]


def withheld_job_names() -> list[str]:
    """The enabled jobs this deployment declares and cannot run, so binds no launcher for, sorted.

    Decided now by the manifest's `unavailable_reason`. Still declared (validators keep it), but
    subtracted from the bound surface here and in `chemclaw_agent._withheld_tool_names`, since the
    tool registry only grows.
    """
    return sorted(
        job.name
        for manifest in enabled()
        for job in manifest.jobs
        if unavailable_reason(job) is not None
    )


def job_names() -> list[str]:
    """Every declared job name across the enabled connectors, sorted.

    Distinct from `connector_tool_names`: `chemclaw.agent.plan_gate` gates durable launches and must
    not gate a connector's read tools.
    """
    return sorted(job.name for manifest in enabled() for job in manifest.jobs)


def state_changing_tool_names() -> list[str]:
    """Every enabled connector tool that spends real resources or writes data, sorted.

    Each endpoint's declared `state_changing` subset plus every job (durable work by construction).
    Read by `chemclaw.agent.plan_gate`. Declared by the bundle because core cannot tell a
    calculation from a lookup by name.
    """
    names: set[str] = set()
    for manifest in enabled():
        if manifest.endpoint is not None:
            names.update(manifest.endpoint.state_changing)
        names.update(job.name for job in manifest.jobs)
    return sorted(names)


def knowledge_read_tool_names() -> list[str]:
    """Every enabled connector tool that consults the record, sorted.

    Declared by the bundle; read by `chemclaw.agent.authz.knowledge_read_tools` for
    `turn_costs.retrieval_calls`. Jobs are absent: none is a search.
    """
    names: set[str] = set()
    for manifest in enabled():
        if manifest.endpoint is not None:
            names.update(manifest.endpoint.knowledge_read)
    return sorted(names)


def find_job(name: str) -> tuple[str, JobSpec]:
    """Resolve a declared job name to its connector and spec, or raise naming the valid ones.

    For a template's `job` step. Names are unique across enabled connectors
    (`_declared_tool_names`), so one name resolves to exactly one job.
    """
    for manifest in enabled():
        for job in manifest.jobs:
            if job.name == name:
                return manifest.name, job
    valid = sorted(job.name for manifest in enabled() for job in manifest.jobs)
    raise ConnectorError(f"unknown connector job {name!r}; declared jobs: {valid}")


def endpoint_tool_names(servers: Iterable[str] | None = None) -> list[str]:
    """The MCP tools the enabled connectors' endpoints serve, sorted; `servers` selects bundles.

    The half a profile's `mcp_server_names` selects, answered without building connector tools
    (which would open httpx clients nothing closes).

    Args:
        servers: The connector names to include; `None` (the default) means every enabled bundle.
            Unknown names are ignored; `connector_tools` is where a profile naming one fails.
    """
    names: set[str] = set()
    for manifest in enabled():
        if servers is not None and manifest.name not in servers:
            continue
        if manifest.endpoint is not None:
            names.update(manifest.endpoint.tools)
    return sorted(names)


def connector_tool_names() -> list[str]:
    """Every tool name the enabled connectors advertise: endpoint tools and job tools, sorted.

    Checked by `chemclaw.cli.validate_skills` and `validate_prose_contract`, so a skill or prompt
    cannot outlive the tool it teaches.
    """
    return sorted(set(endpoint_tool_names()) | set(job_names()))


def declared_connector_tool_names() -> list[str]:
    """Every tool name any discovered bundle declares, enabled or not, sorted.

    The validator's answer: a skill, prompt or template naming a tool is a claim about the
    repository, so it must not be rejected because an opt-in bundle is off on this machine. Deleted
    tools are still caught.
    """
    found = discovered()
    names: set[str] = set()
    for _, manifest in found.values():
        names.update(job.name for job in manifest.jobs)
        if manifest.endpoint is not None:
            names.update(manifest.endpoint.tools)
    return sorted(names)


def declared_skills_dirs() -> list[str]:
    """The `skills/` directory of every discovered bundle, enabled or not.

    The validator's answer (`skills_dirs` is the runtime one): an opt-in bundle's skills must still
    be validated.
    """
    return _bundle_content_dirs("skills", [m for _, m in discovered().values()])
