"""Validate the connector bundles: real manifests, real declarations, and a safe tool surface.

`make connector-validate`, the gate that keeps a connector's *declaration* honest — the same job
`make skill-validate` does for `SKILL.md` and `make kg-validate` does for notes. Pydantic already
rejects a malformed manifest at load; this gate catches the four things a per-file schema cannot
see:

1. **An enabled connector that does not exist.** `connectors_enabled` naming a missing bundle would
   otherwise advertise nothing at run time and look like a capability that quietly stopped working.
2. **A declaration that does not match the bundle on disk.** A manifest listing a skill or profile
   whose file is absent ships a promise nothing fulfils; a bundle containing a skill the manifest
   does not declare ships a skill nobody reviewed as part of that capability. Both directions
   matter.
3. **A mutating tool on the agent-facing allow-list.** The agent's connector surface is
   read/compute only: mutation goes through a `jobs:` entry (which core authorizes, dry-run-gates
   and
   attributes) or through a core PR-gate tool. This is the `allowed_tools` boundary (D-029) promoted
   from a convention to something CI enforces, because a connector quietly adding an `index_*` tool
   to its allow-list would hand the model a write path around the PR-gate.
4. **A job that cannot be built.** A `params_model` reference that does not resolve, or two
   connectors claiming the same job name, fails here rather than when a chemist first calls it.
5. **A tool the server serves and the manifest never mentions.** Everything above reads the
   manifest, so none of it could see the gap between what a bundle *declares* and what its MCP
   server actually *answers*. `molfp` and `rxnfp` each served an `index_*` write tool that no
   manifest named: not on `tools` (correct, D-029), but not in `state_changing`/`read_only` either,
   so `_check_classification` never saw it and nothing else looked. A connector authenticates
   nothing by design — the network policy is the boundary — so an undeclared tool on `/mcp` is
   reachable by anything that can open a socket to that pod. Proved by completing an anonymous MCP
   handshake against the real app and writing a row into `molecule_fingerprints`, the table the
   report path cites as lab precedent.

Read-only; touches nothing.
"""

import asyncio
import inspect
from importlib import import_module
from pathlib import Path
from typing import Any

from chemclaw.connectors.jobs import _params_model, build_job_tool, resolve_precondition
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

# Name prefixes that mark a tool as mutating. A prefix list rather than an exact allowlist because
# the rule is about *intent*: a connector author naming a tool `index_*`, `write_*`, `delete_*` or
# `propose_*` is writing something, and the agent-facing surface is not where that belongs. A
# genuine read tool that trips this is renamed — which is cheaper than a write path nobody noticed.
_MUTATING_PREFIXES = ("index_", "write_", "delete_", "remove_", "update_", "propose_", "submit_")


def _both_ways(kind: str, declared: list[str], present: set[str], where: Path) -> list[str]:
    """Report a declaration with no file *and* a file with no declaration (rule 2, both directions).

    One helper for skills and profiles because the asymmetry is the same in both cases and stating
    it twice would let the two copies drift: a declaration without a file is a promise nothing
    fulfils, and a file without a declaration is content shipped inside a capability nobody reviewed
    as part of it.
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
    """Refuse a tool the bundle's MCP server serves that its manifest never declares (rule 5).

    The only check here that reads the *running server* rather than the YAML. Everything the other
    rules know comes from the manifest, which is precisely why an undeclared tool was invisible to
    all of them: `_check_classification` validates the `tools` allow-list against
    `state_changing`/`read_only`, and a tool on none of the three lists is not a violation of
    anything it can see.

    The comparison is against `tools`, and that is forced rather than chosen.
    `_check_classification` already refuses a manifest that classifies a tool it does not serve,
    so `state_changing` and `read_only` are constrained to be subsets of `tools` — **the manifest
    has no vocabulary for "served but not agent-facing" at all.** The comment that justified the
    gap ("the server still
    exposes it, for the ingestion path") described a state the schema cannot express, which is why
    the only place it was ever written down was a comment.

    So `tools` is the served set, and the two must agree exactly. A capability that genuinely must
    not be on the agent's surface is a `jobs:` entry — which core authorizes, dry-run-gates and
    attributes — or a core PR-gate tool. That is D-029's actual shape; an undeclared MCP tool was
    never the third option it looked like.

    A bundle with no server module is not a violation: `qm` is job-only, and its capability is a
    Temporal workflow behind `jobs:` rather than an MCP surface. A bundle that *has* one and no
    `server` object in it is a different thing entirely, and used to return the same empty list:
    the rule then passed without ever asking what is served. All six bundles with an endpoint
    define `server = FastMCP(...)`, so the only way in is a rename — the change this rule most
    needs to survive — and it is reported.

    Costs one import of every bundle's server package (measured 20.8s for the whole gate, mostly
    rdkit and the ML stack). That is a real cost for a CI gate and it is the price of asking the
    server instead of the file — the isolation this would otherwise violate is a *runtime* property
    of the chat pod
    (`tests/test_connector_isolation.py`), and this is a separate short-lived process.
    """
    if manifest.endpoint is None:
        return []
    try:
        # `server_tools_module` returns None only for "this bundle has no server module" — `qm` is
        # job-only, its capability a Temporal workflow behind `jobs:`. A *transitive*
        # ModuleNotFoundError (a missing rdkit, a renamed dependency) means the bundle is broken and
        # comes back out of it, because swallowing that made the rule pass vacuously for exactly the
        # bundle most likely to be misbuilt.
        module = server_tools_module(manifest.name)
    except ModuleNotFoundError as exc:
        return [f"connector {manifest.name!r}: its server module could not be imported ({exc})"]
    except Exception as exc:
        # Anything else — an ImportError from a submodule, a failure at import time — is reported
        # rather than propagated, so CI prints "connector X: ..." instead of a bare traceback from
        # a validator that never reached its own `main()`.
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
    return [
        f"connector {manifest.name!r}: tool {tool!r} is served on /mcp but the manifest does not "
        "declare it — connectors authenticate nothing by design, so an undeclared tool is callable "
        "by anything that can reach the pod, around every gate core applies. Declare it in `tools` "
        "(and classify it), or make it a `jobs:` entry, or stop serving it"
        for tool in sorted(served - set(manifest.endpoint.tools))
    ]


def _precondition_problems(connector: str, job: JobSpec) -> list[str]:
    """Check that a declared `precondition` can accept the params model it will be handed.

    `resolve_precondition` proves the reference imports and is callable, and stops there — so
    `connectors/bo/connector.yaml` could name `require_rounds_within_ceiling(n_rounds: int)` while
    `connectors/jobs.py` calls `precondition(spec)` with a `CampaignSpec`. Every
    `start_optimization_campaign` raised `TypeError` before any durable work, and nothing caught it:
    the type is erased to `Callable[[Any], None]` so mypy cannot see it, the validator built the
    tool without invoking it, and the only tests called the rule directly with a bare `int`.

    Binding the signature catches an arity mismatch; comparing the annotation catches the shape.
    An unannotated or `Any` parameter is accepted — the contract is stated in prose for those, and
    a validator that demanded annotations would be inventing a rule the manifest does not make.
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


def _registered_workflow_names(connector: str) -> set[str] | None:
    """The Temporal type names this bundle's own modules register, or `None` if it has no worker.

    Importing `connectors.<name>.workflows` is what registers them — the same side-effect-import
    contract the workers themselves rely on — so this validator has to do the import the worker
    would do. `None` (no such module) is not a problem to report here: a bundle may legitimately
    declare jobs whose workflow lives elsewhere in a future arrangement, and a missing module is
    already an unmistakable failure at worker start. What this function exists to catch is the
    silent case: a module that *is* there and does not register the name the manifest promises.
    """
    try:
        import_module(f"chemclaw.connectors.{connector}.workflows")
    except ImportError:
        return None
    return {temporal_name(cls) for cls in registered_workflows(bundle_queue(connector))}


def _job_problems(manifest: ConnectorManifest) -> list[str]:
    """Build each declared job's tool, so an unresolvable `params_model` fails here (rule 4).

    Also checks the one cross-cutting number a manifest cannot validate on its own:
    `inline_wait_seconds` is spent *inside* a turn, so a bundle declaring a wait at or beyond
    `service_turn_timeout_seconds` has written a job whose fast path can never win — the turn is
    killed first, and every call looks like a timeout rather than like the deferral it should have
    been. The manifest cannot see the deployment's timeout; this check can.
    """
    problems: list[str] = []
    served = _registered_workflow_names(manifest.name)
    for job in manifest.jobs:
        try:
            build_job_tool(manifest.name, job)
        except ValueError as exc:
            problems.append(f"connector {manifest.name!r}: job {job.name!r} cannot be built: {exc}")
        problems.extend(_precondition_problems(manifest.name, job))
        # **The last unchecked string in a seam whose design is two plain strings.** `workflow` is a
        # Temporal type name, resolved at dispatch against whatever the bundle's worker registered —
        # so `mypy` cannot see it, no test covered it, and a typo passed lint, type, pytest and
        # every other rule in this file. What it costs at runtime: the child starts on a queue whose
        # worker serves no such type, the parent waits `connector_job_timeout_seconds` (25 h at the
        # shipped default), and the chemist is told "running" for a day. That is the failure
        # `durable/registry.py` exists to prevent, one level above where it can see.
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


def _connector_urls_problems(discovered_names: set[str]) -> list[str]:
    """Check that every key in `connector_urls` names a discovered bundle (rule 5).

    A typo'd key is silently ignored by `_endpoint_url`, falling back to the manifest's
    dev-loopback default, which is unreachable in a cluster. The symptom is a WARNING plus a
    degraded `/readyz`, identical to a transient outage — so a configuration bug presents as an
    infrastructure problem. This check forces any configured URL to name a real bundle.
    """
    return [
        f"settings.connector_urls names unknown connector {key!r}; "
        f"discovered connectors: {sorted(discovered_names)}"
        for key in sorted(settings.connector_urls)
        if key not in discovered_names
    ]


def validate_connectors() -> list[str]:
    """Return one problem string per violation across every discovered bundle (empty = all good).

    Discovery (not just the enabled set) is validated, because a bundle that is broken while
    disabled is a bundle nobody can enable — and CI is where that should surface, not the day an
    operator turns it on.
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
    # Check that connector_urls configuration is valid (rule 5).
    problems.extend(_connector_urls_problems(discovered_names))
    try:
        # Two properties of the enabled *set*, not of any one manifest: `connectors_enabled` naming
        # a bundle that exists (rule 1), and no two enabled connectors claiming one job name (rule
        # 4).
        names = [manifest.name for manifest in enabled()]
        job_tools()
    except ChemclawError as exc:
        # `ChemclawError`, not `ConnectorError`. `job_tools()` rebuilds every enabled bundle's job
        # tools, so it re-raises whatever `build_job_tool` raises — and that is `ConnectorJobError`,
        # which is a *sibling* of `ConnectorError` under `ChemclawError`, not a subclass. A bundle
        # with an unresolvable `params_model` therefore escaped this arm and killed the CLI with a
        # traceback, after `_job_problems` above had already worked out the clean sentence and put
        # it in `problems` — the report was computed and then thrown away on the way out.
        #
        # The common base is the right width here for the reason both classes' docstrings give:
        # every one of them means "this deployment is misconfigured", which is exactly what this
        # entry point exists to print rather than raise.
        #
        # Appended only if nothing above already said it. `_job_problems` builds the same tools per
        # manifest and reports the same fault with the connector and job named, so re-appending the
        # bare message would describe one fault twice and lose the more specific line in the noise.
        message = str(exc)
        if not any(message in problem for problem in problems):
            problems.append(message)
    else:
        if not names:
            problems.append("no connectors enabled — the agent would have no out-of-process tools")
    return problems


def main() -> int:
    """Validate every connector bundle; print problems and exit non-zero if any (the CI gate)."""
    problems = validate_connectors()
    if problems:
        print("connector validation failed:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("connector validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
