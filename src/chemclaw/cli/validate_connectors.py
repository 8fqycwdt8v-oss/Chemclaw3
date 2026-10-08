"""Validate the connector bundles: real manifests, real declarations, and a safe tool surface.

`make connector-validate`. Beyond pydantic's per-file schema it refuses:

1. **An enabled connector that does not exist** — it would advertise nothing at run time.
2. **A declaration that does not match the bundle on disk**, both ways: a declared skill or
   profile with no file, and a file nobody declared; and a `skills/` or `profiles/` directory
   beside no connector at all.
3. **A mutating tool on the agent-facing allow-list.** The agent's connector surface is
   read/compute only; mutation goes through a `jobs:` entry (authorized, dry-run-gated,
   attributed) or a core write tool (D-029).
4. **A job that cannot be built** — an unresolvable `params_model` or a duplicate job name.
5. **A disagreement between what a bundle declares and what its server serves**, both ways. A
   connector authenticates nothing (the network policy is the boundary), so an undeclared tool on
   `/mcp` is reachable by anything that can reach the pod; a declared, unserved tool fails at call
   time.
6. **A `connector_urls` key naming no discovered bundle** — `_endpoint_url` would silently fall
   back to the dev-loopback default, which looks like an outage.

Rule 5's served direction needs a server in this tree; bundles served from `Chemclaw3-mcp` (`chem`,
`safety`) are named by `unverified_tool_surfaces` on every run and checked there by
`assert_manifest_matches`.

Read-only; touches nothing.
"""

import argparse
import asyncio
import inspect
from collections.abc import Sequence
from importlib import import_module
from pathlib import Path
from typing import Any

from chemclaw.connectors.jobs import (
    _params_model,
    build_job_tool,
    require_funded_ceiling,
    resolve_precondition,
    unavailable_reason,
)
from chemclaw.connectors.manifest import ConnectorManifest, JobSpec
from chemclaw.connectors.queues import bundle_queue
from chemclaw.connectors.registry import (
    ConnectorError,
    discovered,
    enabled,
    job_tools,
    server_tools_module,
)
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.durable.registry import registered_workflows, temporal_name

# Name prefixes that mark a tool as mutating. A prefix list because the rule is about intent; a
# genuine read tool that trips it is renamed.
_MUTATING_PREFIXES = ("index_", "write_", "delete_", "remove_", "update_", "propose_", "submit_")


def _both_ways(kind: str, declared: list[str], present: set[str], where: Path) -> list[str]:
    """Report a declaration with no file *and* a file with no declaration (rule 2, both directions).

    One helper for skills and profiles so the two cases cannot drift apart.
    """
    missing = [
        f"{where}: declares {kind} {name!r} but no such {kind} exists in the bundle"
        for name in sorted(set(declared) - present)
    ]
    undeclared = [
        f"{where}: contains {kind} {name!r} but connector.yaml does not declare it"
        for name in sorted(present - set(declared))
    ]
    return [*missing, *undeclared]


def _bundle_content_problems(bundle: Path, manifest: ConnectorManifest) -> list[str]:
    """Check that the manifest's declared skills and profiles match the files in the bundle."""
    skills_dir = bundle / "skills"
    profiles_dir = bundle / "profiles"
    skills_present = (
        {path.name for path in skills_dir.iterdir() if (path / "SKILL.md").is_file()}
        if skills_dir.is_dir()
        else set()
    )
    profiles_present = (
        {path.stem for path in profiles_dir.glob("*.yaml")} if profiles_dir.is_dir() else set()
    )
    return [
        *_both_ways("skill", manifest.skills, skills_present, bundle),
        *_both_ways("profile", manifest.profiles, profiles_present, bundle),
    ]


def _orphan_content_problems(discovered_names: set[str]) -> list[str]:
    """Refuse a `skills/` or `profiles/` directory that belongs to no discovered connector.

    A connector's judgment may sit beside a manifest the fleet owns (a directory with no
    `connector.yaml`), found by name. If the manifest is renamed or withdrawn there, that directory
    stops loading with no error; this is the half that says so.
    """
    problems: list[str] = []
    for directory in settings.connectors_dirs:
        root = Path(directory)
        if not root.is_dir():
            continue
        for path in sorted(root.iterdir()):
            if path.name in discovered_names or not path.is_dir():
                continue
            held = [kind for kind in ("skills", "profiles") if (path / kind).is_dir()]
            if held:
                problems.append(
                    f"{path}: holds {held} but no connector named {path.name!r} is discovered, so "
                    "nothing would load them. Restore the manifest (it comes from the installed "
                    "`chemclaw-contracts` package) or move the directory"
                )
    return problems


def _tool_surface_problems(manifest: ConnectorManifest) -> list[str]:
    """Refuse a mutating tool name on the agent-facing allow-list (rule 3 above)."""
    return [
        f"connector {manifest.name!r}: tool {tool!r} looks mutating "
        f"(prefix in {list(_MUTATING_PREFIXES)}); the agent-facing surface is read/compute only — "
        "expose it as a job, or keep it off `tools` for the ingestion path to use"
        for tool in sorted(manifest.endpoint.tools if manifest.endpoint else [])
        if tool.startswith(_MUTATING_PREFIXES)
    ]


def _served_tool_problems(manifest: ConnectorManifest) -> list[str]:
    """Refuse any disagreement between what a bundle declares and what it serves (rule 5).

    The only check that reads the running server rather than the YAML. `state_changing` and
    `read_only` must be subsets of `tools`, so the manifest cannot express "served but not
    agent-facing": `tools` is the served set and the two must agree exactly. A capability that must
    stay off the agent's surface is a `jobs:` entry or a core write tool.

    A bundle with no server module (`results`, job-only) is not a violation; one whose server module
    has no `server` object is reported, since a rename is what this rule must survive.

    Imports every bundle's server package (slow: rdkit and the ML stack); acceptable in a separate,
    short-lived CI process.
    """
    if manifest.endpoint is None:
        return []
    try:
        # `server_tools_module` returns None only when the bundle has no server module. A transitive
        # `ModuleNotFoundError` means the bundle is broken and propagates, so the rule cannot pass
        # vacuously.
        module = server_tools_module(manifest.name)
    except ModuleNotFoundError as exc:
        return [f"connector {manifest.name!r}: its server module could not be imported ({exc})"]
    except Exception as exc:
        # Any other import failure is reported as "connector X: ..." rather than a bare traceback.
        return [f"connector {manifest.name!r}: its server module raised on import ({exc!r})"]
    if module is None:
        return []
    server = getattr(module, "server", None)
    if server is None:
        return [
            f"connector {manifest.name!r}: its server module defines no `server`, so this rule "
            "cannot ask what the bundle actually serves and would pass without checking anything. "
            "The declared tools stay unverified against the running surface until it is restored"
        ]
    served = {tool.name for tool in asyncio.run(server.list_tools())}
    declared = set(manifest.endpoint.tools)
    undeclared = [
        f"connector {manifest.name!r}: tool {tool!r} is served on /mcp but the manifest does not "
        "declare it — connectors authenticate nothing by design, so an undeclared tool is callable "
        "by anything that can reach the pod, around every gate core applies. Declare it in `tools` "
        "(and classify it), or make it a `jobs:` entry, or stop serving it"
        for tool in sorted(served - declared)
    ]
    unserved = [
        f"connector {manifest.name!r}: tool {tool!r} is declared in `tools` but the bundle's "
        "server does not serve it — core advertises it to the model, every other validator "
        "resolves names through it, and the call fails at the MCP server. Serve it, or take it "
        "off `tools` (and out of `read_only`/`state_changing`)"
        for tool in sorted(declared - served)
    ]
    return [*undeclared, *unserved]


def unverified_tool_surfaces() -> dict[str, list[str]]:
    """Endpoint-bearing bundles whose declared tools nothing here can check, by connector.

    `chem` and `safety` are served from `Chemclaw3-mcp`, so their `tools:` lists cannot be verified
    offline. Reported rather than raised (failing would force deleting a correct manifest) and
    rather than silenced (so a pass says what it did not check). `Chemclaw3-mcp`'s
    `assert_manifest_matches` checks them against the running server.
    """
    try:
        found = discovered()
    except ConnectorError:
        return {}  # already reported as a problem by `validate_connectors`
    unverified: dict[str, list[str]] = {}
    for _bundle, manifest in found.values():
        if manifest.endpoint is None:
            continue
        try:
            if server_tools_module(manifest.name) is not None:
                continue
        except Exception:
            continue  # a broken server module is a problem, not an unverified surface
        if manifest.endpoint.tools:
            unverified[manifest.name] = sorted(manifest.endpoint.tools)
    return unverified


def _precondition_problems(connector: str, job: JobSpec) -> list[str]:
    """Check that a declared `precondition` can accept the params model it will be handed.

    `resolve_precondition` only proves the reference imports and is callable; `connectors/jobs.py`
    calls `precondition(spec)` with the job's params model, and the type is erased to
    `Callable[[Any], None]`. Binding the signature catches arity; comparing the annotation catches
    shape. An unannotated or `Any` parameter is accepted.
    """
    if job.precondition is None:
        return []
    try:
        check = resolve_precondition(job.precondition)
        model = _params_model(connector, job)
    except ValueError:
        return []  # already reported by the build above; do not say it twice
    try:
        signature = inspect.signature(check)
        signature.bind(model.model_construct())
    except TypeError as exc:
        return [
            f"connector {connector!r}: job {job.name!r} precondition {job.precondition!r} "
            f"cannot be called with the job's params object: {exc}"
        ]
    (parameter,) = signature.parameters.values()
    annotation = parameter.annotation
    if annotation in (inspect.Parameter.empty, Any) or annotation is model:
        return []
    return [
        f"connector {connector!r}: job {job.name!r} precondition {job.precondition!r} takes "
        f"{getattr(annotation, '__name__', annotation)!r}, but the launcher passes it the "
        f"validated {model.__name__!r} params object"
    ]


def _unavailable_reason_problems(connector: str, job: JobSpec) -> list[str]:
    """Check that a declared `unavailable_reason` resolves, takes nothing, and answers str | None.

    Called, not only resolved: the launcher filter calls it on every agent build, and a raise there
    would take down every bundle's job launchers.
    """
    if job.unavailable_reason is None:
        return []
    try:
        reason = unavailable_reason(job)
    except (ValueError, TypeError) as exc:
        return [
            f"connector {connector!r}: job {job.name!r} unavailable_reason "
            f"{job.unavailable_reason!r} cannot be called with no arguments: {exc}"
        ]
    if reason is not None and not isinstance(reason, str):
        return [
            f"connector {connector!r}: job {job.name!r} unavailable_reason "
            f"{job.unavailable_reason!r} returned {type(reason).__name__}, not a sentence or None"
        ]
    return []


def _registered_workflow_names(connector: str) -> set[str] | None:
    """The Temporal type names this bundle's own modules register, or `None` if it has no worker.

    Importing `connectors.<name>.workflows` registers them, as it does for the worker. A missing
    module is not reported here (it fails loudly at worker start); the silent case caught is a
    module that does not register the name the manifest promises.
    """
    try:
        import_module(f"chemclaw.connectors.{connector}.workflows")
    except ImportError:
        return None
    return {temporal_name(cls) for cls in registered_workflows(bundle_queue(connector))}


def _job_problems(manifest: ConnectorManifest) -> list[str]:
    """Build each declared job's tool, so an unresolvable `params_model` fails here (rule 4).

    Also refuses an `inline_wait_seconds` at or beyond `service_turn_timeout_seconds`: the wait is
    spent inside a turn, so the turn would be killed first and every call would read as a timeout.
    """
    problems: list[str] = []
    served = _registered_workflow_names(manifest.name)
    for job in manifest.jobs:
        try:
            build_job_tool(manifest.name, job)
        except ValueError as exc:
            problems.append(f"connector {manifest.name!r}: job {job.name!r} cannot be built: {exc}")
        # `require_funded_ceiling` refuses only at launch, so building a tool no longer raises;
        # asking it here keeps an unfunded declaration from reaching production green.
        try:
            require_funded_ceiling(manifest.name, job)
        except ValueError as exc:
            problems.append(f"connector {manifest.name!r}: {exc}")
        problems.extend(_precondition_problems(manifest.name, job))
        problems.extend(_unavailable_reason_problems(manifest.name, job))
        # `workflow` is a Temporal type name resolved at dispatch, invisible to mypy. A typo would
        # start the child on a queue whose worker serves no such type, and the parent would wait the
        # whole `connector_job_timeout_seconds`.
        if served is not None and job.workflow not in served:
            queue = bundle_queue(manifest.name)
            problems.append(
                f"connector {manifest.name!r}: job {job.name!r} names workflow {job.workflow!r}, "
                f"which the bundle's own modules do not register on {queue!r} "
                f"(registered: {sorted(served) or 'none'}) — the job would start and then wait "
                "for a worker that serves no such type"
            )
        budget = job.inline_wait_seconds
        if budget is not None and budget >= settings.service_turn_timeout_seconds:
            problems.append(
                f"connector {manifest.name!r}: job {job.name!r} waits {budget}s inline, which is "
                f"not below the {settings.service_turn_timeout_seconds}s turn timeout — the turn "
                "would be killed before the wait could ever return a result"
            )
    return problems


def _queued_problems(manifest: ConnectorManifest) -> list[str]:
    """Check a queued endpoint's inline wait against the deployment's turn timeout.

    Same arithmetic as `_job_problems`: a wait at or beyond the turn timeout can never return an
    answer.
    """
    endpoint = manifest.endpoint
    queued = getattr(endpoint, "queued", None)
    if queued is None or queued.inline_wait_seconds < settings.service_turn_timeout_seconds:
        return []
    return [
        f"connector {manifest.name!r}: `queued.inline_wait_seconds` is "
        f"{queued.inline_wait_seconds}s, which is not below the "
        f"{settings.service_turn_timeout_seconds}s turn timeout — a queued call could never "
        "answer inside the turn"
    ]


def _connector_urls_problems(discovered_names: set[str]) -> list[str]:
    """Check that every key in `connector_urls` names a discovered bundle (rule 6).

    A typo'd key is silently ignored by `_endpoint_url`, which falls back to the unreachable
    dev-loopback default, so a configuration bug looks like a transient outage.
    """
    return [
        f"settings.connector_urls names unknown connector {key!r}; "
        f"discovered connectors: {sorted(discovered_names)}"
        for key in sorted(settings.connector_urls)
        if key not in discovered_names
    ]


def validate_connectors() -> list[str]:
    """Return one problem string per violation across every discovered bundle (empty = all good).

    Discovered, not only enabled: a bundle broken while disabled is one nobody can enable.
    """
    try:
        found = discovered()
    except ConnectorError as exc:
        return [str(exc)]
    problems: list[str] = []
    discovered_names = {manifest.name for bundle, manifest in found.values()}
    for bundle, manifest in found.values():
        problems.extend(_bundle_content_problems(bundle, manifest))
        problems.extend(_tool_surface_problems(manifest))
        problems.extend(_served_tool_problems(manifest))
        problems.extend(_job_problems(manifest))
        problems.extend(_queued_problems(manifest))
    # Check that connector_urls configuration is valid (rule 6).
    problems.extend(_connector_urls_problems(discovered_names))
    problems.extend(_orphan_content_problems(discovered_names))
    try:
        # Two properties of the enabled set: `connectors_enabled` names bundles that exist (rule 1),
        # and no two enabled connectors claim one tool name — job or endpoint tool (rule 4).
        names = [manifest.name for manifest in enabled()]
        job_tools()
    except ChemclawError as exc:
        # `ChemclawError`, not `ConnectorError`: `job_tools()` re-raises `ConnectorJobError`, a
        # sibling under `ChemclawError`. Every such error means "misconfigured", which this entry
        # point prints rather than raises. Appended only if `_job_problems` has not already reported
        # it more specifically.
        message = str(exc)
        if not any(message in problem for problem in problems):
            problems.append(message)
    else:
        if not names:
            problems.append("no connectors enabled — the agent would have no out-of-process tools")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    """Validate every connector bundle; print problems and exit non-zero if any (the CI gate).

    The unverified-surface note prints on both paths, since it qualifies a pass as much as a
    failure. Parses arguments though it declares none, so a stray directory argument is refused
    rather than ignored; `CHEMCLAW_CONNECTORS_DIR` is the knob.
    """
    argparse.ArgumentParser(
        prog="python -m chemclaw.cli.validate_connectors",
        description="Validate every discovered connector bundle. Set CHEMCLAW_CONNECTORS_DIR "
        "(a PATH-style list) to point this at another tree.",
    ).parse_args(argv)
    problems = validate_connectors()
    for name, tools in sorted(unverified_tool_surfaces().items()):
        print(
            f"note: connector {name!r} declares {tools} and is served by a server this tree does "
            "not hold — declared tools name-checked here, agreement with the running surface "
            "checked in Chemclaw3-mcp"
        )
    if problems:
        print("connector validation failed:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("connector validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
