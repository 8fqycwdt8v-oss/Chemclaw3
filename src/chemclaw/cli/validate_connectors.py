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

Read-only; touches nothing.
"""

import sys
from pathlib import Path

from chemclaw.connectors.jobs import build_job_tool
from chemclaw.connectors.manifest import ConnectorManifest
from chemclaw.connectors.registry import ConnectorError, discovered, enabled, job_tools
from chemclaw.core.config import settings

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


def _job_problems(manifest: ConnectorManifest) -> list[str]:
    """Build each declared job's tool, so an unresolvable `params_model` fails here (rule 4).

    Also checks the one cross-cutting number a manifest cannot validate on its own:
    `inline_wait_seconds` is spent *inside* a turn, so a bundle declaring a wait at or beyond
    `service_turn_timeout_seconds` has written a job whose fast path can never win — the turn is
    killed first, and every call looks like a timeout rather than like the deferral it should have
    been. The manifest cannot see the deployment's timeout; this check can.
    """
    problems: list[str] = []
    for job in manifest.jobs:
        try:
            build_job_tool(manifest.name, job)
        except ValueError as exc:
            problems.append(f"connector {manifest.name!r}: job {job.name!r} cannot be built: {exc}")
        budget = job.inline_wait_seconds
        if budget is not None and budget >= settings.service_turn_timeout_seconds:
            problems.append(
                f"connector {manifest.name!r}: job {job.name!r} waits {budget}s inline, which is "
                f"not below the {settings.service_turn_timeout_seconds}s turn timeout — the turn "
                "would be killed before the wait could ever return a result"
            )
    return problems


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
    for bundle, manifest in found.values():
        problems.extend(_bundle_content_problems(bundle, manifest))
        problems.extend(_tool_surface_problems(manifest))
        problems.extend(_job_problems(manifest))
    try:
        # Two properties of the enabled *set*, not of any one manifest: `connectors_enabled` naming
        # a bundle that exists (rule 1), and no two enabled connectors claiming one job name (rule
        # 4).
        names = [manifest.name for manifest in enabled()]
        job_tools()
    except ConnectorError as exc:
        problems.append(str(exc))
    else:
        if not names:
            problems.append("no connectors enabled — the agent would have no out-of-process tools")
    return problems


def main() -> None:
    """Validate every connector bundle; print problems and exit non-zero if any (the CI gate)."""
    problems = validate_connectors()
    if problems:
        print("connector validation failed:")
        for problem in problems:
            print(f"  - {problem}")
        sys.exit(1)
    print("connector validation passed.")


if __name__ == "__main__":
    main()
