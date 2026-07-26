"""The connector registry: discover bundles, validate them, and build what the agent advertises.

This is the one place that turns folders on disk into agent capability. It deliberately combines the
two discovery idioms the repo already trusts, each where it fits: **filesystem discovery** for the
bundles themselves (a connector is a folder, exactly as a skill is — `FileSkillsSource`), and a
**config enable-token** for which of the discovered bundles a deployment turns on (exactly as
`skills_enabled` and `data_sources` do). Discovery is not enablement: a repo can ship every
connector and a deployment can run the subset it has validated.

Two products come out of a manifest, and `build_agent` appends both to the in-process tool list:

- **MCP tools** — one MAF MCP tool per `endpoint:`, carrying the turn's identity headers on every
  call and our own credential on the connection (`connectors.identity`).
- **Job tools** — one generated launcher per `jobs:` entry (`connectors.jobs`), registered into the
  shared tool registry so audit, authorization and profile narrowing address it like any other tool.

Nothing here decides *whether* a call is allowed. The registry only assembles the offered surface;
the audit and authorization middlewares wrap the assembled list in `build_agent`, and a profile
narrows it afterwards — so a connector can add to what is *offered* and never to what is
*permitted*.
"""

import logging
from collections.abc import Iterable
from contextlib import AsyncExitStack
from functools import cache
from pathlib import Path
from typing import Any, assert_never

import httpx
import yaml
from pydantic import ValidationError

from agents.tool_registry import CapabilityTool
from chemclaw.config import settings
from connectors.identity import auth_for, stamp_turn_identity
from connectors.jobs import build_job_tool
from connectors.manifest import ConnectorManifest, Endpoint, HttpEndpoint, StdioEndpoint
from connectors.transport import DegradingHttpConnector, DegradingStdioConnector

logger = logging.getLogger(__name__)

# The manifest filename inside a bundle. A constant because two modules look for it (here and
# `scripts.validate_connectors`) and a typo in either would report "no connectors found".
MANIFEST_FILENAME = "connector.yaml"

# What one configured connector endpoint becomes, whichever transport it declares. Both are MAF MCP
# tools with the same agent-facing surface, so callers never branch on the transport.
ConnectorMcpTool = DegradingStdioConnector | DegradingHttpConnector


class ConnectorError(ValueError):
    """A connector bundle is malformed, or an enabled connector does not exist.

    A `ValueError` subclass because this is a configuration error surfaced at startup — the same
    class the config validators and `sources.registry` raise, so one `except ValueError` at an entry
    point catches every "this deployment is misconfigured" failure.
    """


def _bundle_dirs() -> list[Path]:
    """Every connector bundle directory found across the configured connector dirs, sorted by name.

    Sorted rather than filesystem order so the advertised tool order is identical on every machine —
    tool order is part of the prompt the model sees, and a surface that reshuffles per pod is a
    reproducibility problem in a GxP system.
    """
    found: dict[str, Path] = {}
    for directory in settings.connectors_dirs:
        root = Path(directory)
        if not root.is_dir():
            continue
        for path in sorted(root.iterdir()):
            if (path / MANIFEST_FILENAME).is_file():
                # First dir wins, so an operator's private connectors dir listed ahead of the repo's
                # can override a shipped bundle — the same precedence a `PATH` entry has.
                found.setdefault(path.name, path)
    return [found[name] for name in sorted(found)]


def load_manifest(bundle: Path) -> ConnectorManifest:
    """Parse and validate one bundle's `connector.yaml`, raising `ConnectorError` on any problem.

    The folder name is authoritative: a manifest whose `name` disagrees with its directory would be
    enabled under one name and looked up under the other, so the mismatch is rejected here rather
    than surfacing as a connector that silently never loads (the rule `validate_skills` applies to
    `SKILL.md`).
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

    Cached because discovery reads and parses every manifest on disk, while the result is fixed for
    the process's lifetime (config is read once at import, and bundles do not appear at run time).
    `discovered.cache_clear()` is the seam a test uses after pointing `connectors_dir` elsewhere.
    """
    return {bundle.name: (bundle, load_manifest(bundle)) for bundle in _bundle_dirs()}


def enabled() -> list[ConnectorManifest]:
    """The manifests this deployment turns on, in the order the enable-list (or discovery) gives.

    An empty `connectors_enabled` means every discovered connector — the same "discovery is
    enablement until you say otherwise" default `skills_enabled` uses, so a fresh checkout runs the
    full shipped surface. A name in the list that no bundle provides is a loud error: it would
    otherwise advertise nothing and look like a capability that simply stopped working.
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


def enabled_bundle_dirs() -> list[Path]:
    """The directories of the enabled bundles — how their skills and profiles are found (§6)."""
    found = discovered()
    return [found[manifest.name][0] for manifest in enabled()]


def skills_dirs() -> list[str]:
    """The `skills/` directory of every enabled connector that declares skills.

    A connector's judgment ships with its capability: the `SKILL.md` explaining *when* to trust a
    similarity hit belongs to the same bundle as the tool that produces one. Appending these to
    `settings.skills_dirs` means `FileSkillsSource` discovers them with no new machinery, and the
    existing enable-list and role gates still narrow them — a bundled skill is an ordinary skill in
    every respect except where it lives.

    Only directories that exist are returned: a manifest may declare skills whose folder a
    deployment has not mounted, and `make connector-validate` is where that mismatch is reported, so
    handing a non-existent path to the skills source here would fail the *agent* for a *packaging*
    problem.
    """
    found = discovered()
    dirs = []
    for manifest in enabled():
        if not manifest.skills:
            continue
        candidate = found[manifest.name][0] / "skills"
        if candidate.is_dir():
            dirs.append(str(candidate))
    return dirs


def _endpoint_url(manifest: ConnectorManifest, endpoint: HttpEndpoint) -> str:
    """The endpoint URL, after any per-deployment override for this connector.

    A manifest ships a working dev default (a loopback port), but a cluster's address belongs to the
    deployment, not to a file in the repo. `connector_urls` is that override, so Helm points the
    front door at an in-cluster Service without patching a bundle.
    """
    return settings.connector_urls.get(manifest.name, endpoint.url)


def _mcp_tool(manifest: ConnectorManifest, endpoint: Endpoint) -> ConnectorMcpTool:
    """Build one MAF MCP tool for a connector endpoint, dispatching on the transport.

    The transports differ only in how the server is *reached* — a locally spawned subprocess vs. an
    already-running endpoint. Everything bounding what the agent may do with it (`allowed_tools`,
    prompts off) is identical on both, so the read/compute-only boundary does not depend on
    transport. The tool is returned **unconnected**: `service.runner.run_turn` opens each MCP
    context for the duration of a turn and tears it down after.
    """
    if isinstance(endpoint, HttpEndpoint):
        # One client carries both halves of what travels with a call (`connectors.identity`): our
        # own credential as `auth`, so it is present on the MCP handshake too, and the turn's
        # identity as a request hook, which is the only place that can see the turn's ambient
        # context — MAF's `header_provider` is invoked in the calling task while the request is
        # issued by the MCP transport's writer task, so its headers never land.
        return DegradingHttpConnector(
            name=manifest.name,
            url=_endpoint_url(manifest, endpoint),
            allowed_tools=endpoint.tools,
            request_timeout=endpoint.request_timeout,
            load_prompts=False,
            http_client=httpx.AsyncClient(
                auth=auth_for(endpoint.auth, manifest.name),
                follow_redirects=True,
                event_hooks={"request": [stamp_turn_identity]},
            ),
        )
    if isinstance(endpoint, StdioEndpoint):
        # No identity headers: a subprocess of our own process, under our own identity, with no
        # request to attach them to (`connectors.identity`).
        return DegradingStdioConnector(
            name=manifest.name,
            command=endpoint.command,
            args=endpoint.args,
            allowed_tools=endpoint.tools,
            load_prompts=False,
        )
    assert_never(endpoint)  # exhaustive over the union — a new transport without a branch is a bug


def mcp_tools() -> list[Any]:
    """One MCP tool per enabled connector that declares an endpoint (unconnected)."""
    return [
        _mcp_tool(manifest, manifest.endpoint)
        for manifest in enabled()
        if manifest.endpoint is not None
    ]


async def open_reachable(stack: AsyncExitStack, tools: Iterable[Any]) -> list[str]:
    """Connect every connector for the caller's scope, and report the ones that did not come up.

    The connector lifecycle in one place, used by all three callers that run a turn — the front-door
    runner, the CLI, and the harness tests — so "how a turn reaches its connectors" has a single
    definition rather than three loops that can drift.

    Nothing is caught here: a connector's `connect` is already non-fatal by construction
    (`connectors.transport`), because MAF re-connects an unconnected tool inside `Agent.run` and
    would
    raise there even if this function swallowed the failure. So an unreachable connector simply
    comes back not-connected, contributes no tools to the turn, and is retried on the next one.

    Args:
        stack: The caller's exit stack, which owns tearing the connections down.
        tools: The agent's MCP tools (`agent.mcp_tools`).

    Returns:
        The names of the connectors that are not connected, for the caller to surface.
    """
    for tool in tools:
        await stack.enter_async_context(tool)
    return [
        getattr(tool, "name", "?") for tool in tools if not getattr(tool, "is_connected", False)
    ]


def job_tools() -> list[CapabilityTool]:
    """The generated launcher for every job declared by an enabled connector.

    Two connectors declaring the same job name is a configuration error, not a last-one-wins: the
    name is the authorization key, so a collision would silently make one connector's gate apply to
    the other's work.
    """
    tools: dict[str, CapabilityTool] = {}
    for manifest in enabled():
        for job in manifest.jobs:
            if job.name in tools:
                raise ConnectorError(
                    f"connector {manifest.name!r} declares job {job.name!r}, "
                    "which another enabled connector already provides"
                )
            tools[job.name] = build_job_tool(manifest.name, job)
    return list(tools.values())


def connector_tool_names() -> list[str]:
    """Every tool name the enabled connectors advertise — endpoint tools and job tools, sorted.

    The set `scripts.validate_skills` and `scripts.validate_prose_contract` check declared names
    against, so a skill or a prompt that teaches a connector tool cannot outlive it.
    """
    names: set[str] = set()
    for manifest in enabled():
        if manifest.endpoint is not None:
            names.update(manifest.endpoint.tools)
        names.update(job.name for job in manifest.jobs)
    return sorted(names)
