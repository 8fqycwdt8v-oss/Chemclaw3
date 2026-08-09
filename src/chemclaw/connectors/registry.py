"""The connector registry: discover bundles, validate them, and build what the agent advertises.

This is the one place that turns folders on disk into agent capability. It deliberately combines
the two discovery idioms the repo already trusts, each where it fits: **filesystem discovery**
for the bundles themselves (a connector is a folder, exactly as a skill is —
`FileSkillsSource`), and a **config enable-token** for which of the discovered bundles a
deployment turns on (exactly as `skills_enabled` and `data_sources` do). Discovery is not
enablement: a repo can ship every connector and a deployment can run the subset it has
validated.

Two products come out of a manifest, and `build_agent` appends both to the in-process tool list:

- **MCP tools** — one MAF MCP tool per `endpoint:`, carrying the turn's identity headers on every
  call and our own credential on the connection (`chemclaw.connectors.identity`).
- **Job tools** — one generated launcher per `jobs:` entry (`chemclaw.connectors.jobs`), registered
into the
  shared tool registry so audit, authorization and profile narrowing address it like any other tool.

Nothing here decides *whether* a call is allowed. The registry only assembles the offered
surface; the audit and authorization middlewares wrap the assembled list in `build_agent`, and a
profile narrows it afterwards — so a connector can add to what is *offered* and never to what is
*permitted*.
"""

import asyncio
import importlib
import logging
import os.path
from collections.abc import Iterable
from contextlib import AsyncExitStack
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any, assert_never

import httpx
import yaml
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
from chemclaw.connectors.transport import DegradingHttpConnector, DegradingStdioConnector
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.core.tool_registry import CapabilityTool

logger = logging.getLogger(__name__)

# The manifest filename inside a bundle. A constant because two modules look for it (here and
# `scripts.validate_connectors`) and a typo in either would report "no connectors found".
MANIFEST_FILENAME = "connector.yaml"

# How long to wait for the TCP/TLS handshake to a connector, as distinct from how long its tools
# may take to answer (the manifest's `request_timeout`, which bounds the read). Deliberately not a
# config field: it is a property of "is this host there at all", the same for every bundle, and a
# deployment that needs a longer one has a network problem a setting would only hide. Short,
# because a dark connector must degrade quickly — the whole point of `DegradingHttpConnector`.
_CONNECT_TIMEOUT_SECONDS = 5.0

# What one configured connector endpoint becomes, whichever transport it declares. Both are MAF
# MCP tools with the same agent-facing surface, so callers never branch on the transport.
ConnectorMcpTool = DegradingStdioConnector | DegradingHttpConnector


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
    pod is a reproducibility problem in a GxP system.
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

    Both callers skip an endpoint-less bundle before asking (that is how `qm`, which is jobs-only,
    never reaches here), so `None` means the narrower thing: a bundle that declares an endpoint and
    has no module behind it.

    A *transitive* import failure propagates. Only `ModuleNotFoundError` naming this exact module
    means "no server module"; a missing or renamed dependency underneath it means the bundle is
    broken, and swallowing that leaves a validator checking less and still reporting success —
    measured, `validate_templates` resolved 46 signatures instead of 50 and printed "template
    validation passed" for a bundle that could not be imported at all.
    """
    target = f"chemclaw.connectors.{connector}.server.tools"
    try:
        return importlib.import_module(target)
    except ModuleNotFoundError as exc:
        if exc.name == target:
            return None
        raise


def skills_dirs() -> list[str]:
    """The `skills/` directory of every enabled connector that declares skills.

    A connector's judgment ships with its capability: the `SKILL.md` explaining *when* to trust
    a similarity hit belongs to the same bundle as the tool that produces one. Appending these
    to `settings.skills_dirs` means `FileSkillsSource` discovers them with no new machinery, and
    the existing enable-list and role gates still narrow them — a bundled skill is an ordinary
    skill in every respect except where it lives.

    Only directories that exist are returned: a manifest may declare skills whose folder a
    deployment has not mounted, and `make connector-validate` is where that mismatch is
    reported, so handing a non-existent path to the skills source here would fail the *agent*
    for a *packaging* problem.
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


def _endpoint_url(connector: str, endpoint: HttpEndpoint) -> str:
    """The endpoint URL, after any per-deployment override for this connector.

    A manifest ships a working dev default (a loopback port), but a cluster's address belongs to
    the deployment, not to a file in the repo. `connector_urls` is that override, so Helm points
    the front door at an in-cluster Service without patching a bundle.
    """
    return settings.connector_urls.get(connector, endpoint.url)


def connector_http_client(connector: str, endpoint: HttpEndpoint) -> httpx.AsyncClient:
    """The HTTP client one connector endpoint is reached with — the single definition of it.

    Public because a test that means to prove something about how a connector is reached has to
    exercise *this* client rather than a hand-rolled lookalike; three transport tests used to build
    their own and were free to drift from what a deployment actually runs.

    One client carries both halves of what travels with a call (`chemclaw.connectors.identity`):
    our own credential as `auth`, so it is present on the MCP handshake too, and the turn's identity
    as a request hook, which is the only place that can see the turn's ambient context — MAF's
    `header_provider` is invoked in the calling task while the request is issued by the MCP
    transport's writer task, so its headers never land.

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
        A client the caller owns; `DegradingHttpConnector.close` is what releases it.
    """
    return httpx.AsyncClient(
        auth=auth_for(endpoint.auth, connector),
        follow_redirects=False,
        event_hooks={"request": [turn_identity_hook(_endpoint_url(connector, endpoint))]},
        # Without this, httpx applies its own 5 s default to *every* phase, and the manifest's
        # `request_timeout` — which this module's docstring credits with "keeping an unreachable
        # host from hanging a turn" — did the opposite. Measured against a real server: an 8 s tool
        # call had its HTTP stream torn down at 5 s, the MCP response then never arrived, and the
        # caller blocked for the full `request_timeout` (60 s for calc, 120 s for bo) before
        # surfacing an opaque failure — holding an admission permit and an agent lease the whole
        # time. A tool slower than 5 s is not exotic here: an uncached `predict_pka` runs xTB
        # inline. `request_timeout` now bounds the read, which is the phase a slow tool occupies;
        # connect stays short so a dead host still degrades fast.
        timeout=httpx.Timeout(endpoint.request_timeout, connect=_CONNECT_TIMEOUT_SECONDS),
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
    — the GxP fail-fast posture — startup would have failed every time. Found by re-running the
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


def _mcp_tool(manifest: ConnectorManifest, endpoint: Endpoint) -> ConnectorMcpTool:
    """Build one MAF MCP tool for a connector endpoint, dispatching on the transport.

    The transports differ only in how the server is *reached* — a locally spawned subprocess vs.
    an already-running endpoint. Everything bounding what the agent may do with it
    (`allowed_tools`, prompts off) is identical on both, so the read/compute-only boundary does
    not depend on transport. The tool is returned **unconnected**: `chemclaw.api.runner.run_turn`
    opens each MCP context for the duration of a turn and tears it down after.
    """
    if isinstance(endpoint, HttpEndpoint):
        return DegradingHttpConnector(
            name=manifest.name,
            url=_endpoint_url(manifest.name, endpoint),
            allowed_tools=endpoint.tools,
            request_timeout=endpoint.request_timeout,
            load_prompts=False,
            http_client=connector_http_client(manifest.name, endpoint),
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


def profiles_dirs() -> list[str]:
    """The `profiles/` directory of every enabled connector that declares profiles.

    The bundle-local half of profile discovery (`chemclaw.agent.profile_discovery`), and the same
    rule
    as `skills_dirs`: only directories that exist are returned, because a manifest may declare
    content a deployment has not mounted and that is `make connector-validate`'s complaint to
    make, not a reason to fail the agent.
    """
    found = discovered()
    dirs = []
    for manifest in enabled():
        if not manifest.profiles:
            continue
        candidate = found[manifest.name][0] / "profiles"
        if candidate.is_dir():
            dirs.append(str(candidate))
    return dirs


async def open_reachable(stack: AsyncExitStack, tools: Iterable[Any]) -> list[str]:
    """Connect every connector for the caller's scope, and report the ones that did not come up.

    The connector lifecycle in one place, used by all three callers that run a turn — the
    front-door runner, the CLI, and the harness tests — so "how a turn reaches its connectors"
    has a single definition rather than three loops that can drift.

    Nothing is caught here: a connector's `connect` is already non-fatal by construction
    (`chemclaw.connectors.transport`), because MAF re-connects an unconnected tool inside
    `Agent.run` and
    would
    raise there even if this function swallowed the failure. So an unreachable connector simply
    comes back not-connected, contributes no tools to the turn, and is retried on the next one.

    **The degradation is announced here, not left to the caller** (REV-6). The return value said
    "for the caller to surface" and all four callers dropped it on the floor, so a turn that lost
    half its capability answered exactly like one that had all of it — the model simply never saw
    the tools and reasoned from what remained. Announcing it in the one place every caller passes
    through means a new caller cannot reintroduce the silence by forgetting to read a return value.
    A caller that can reach a *human* still reads the list and says so on its own surface (the front
    door yields `CapabilityDegradedEvent`, the CLI prints to stderr); what is guaranteed here is the
    operator-visible half.

    Args:
        stack: The caller's exit stack, which owns tearing the connections down.
        tools: This turn's connector tools (`chemclaw.agent.chemclaw_agent.connector_tools`).

    Returns:
        The names of the connectors that are not connected, for the caller to surface.
    """
    # Concurrently, because these are independent hosts and the wait is the *sum* of their
    # latencies otherwise. On the healthy path that is a few hundred milliseconds; the case that
    # matters is the tail, where a dark fleet cost six sequential connect timeouts before the model
    # was called at all. `connectors.health.probe_connectors` already gathers its probes for
    # exactly this reason ("the sum of the timeouts rather than the slowest one") — this is the
    # same argument on the path every turn actually takes.
    #
    # Gathering is safe for the per-turn-instance rule this seam depends on
    # (`agents.chemclaw_agent.connector_tools`): that rule is about object *lifetime*, not connect
    # ordering, and MAF runs each connector's lifecycle on its own task, so no cancel scope is
    # shared between them. Cancellation still propagates — `gather` cancels children with
    # `task.cancel()`, which is precisely what `_is_really_cancelled` reads.
    await asyncio.gather(*(stack.enter_async_context(tool) for tool in tools))
    unreachable = [
        getattr(tool, "name", "?") for tool in tools if not getattr(tool, "is_connected", False)
    ]
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
    return unreachable


def job_tools() -> list[CapabilityTool]:
    """The generated launcher for every job declared by an enabled connector.

    Two connectors declaring the same job name is a configuration error, not a last-one-wins:
    the name is the authorization key, so a collision would silently make one connector's gate
    apply to the other's work.
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
    that into the connector, workflow type and queue `ConnectorJobWorkflow` requires. Job names are
    already unique across enabled connectors (`job_tools` refuses a collision), so one name resolves
    to exactly one job.
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
