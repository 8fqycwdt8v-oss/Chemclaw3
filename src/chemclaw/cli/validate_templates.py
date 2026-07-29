"""Validate the step templates: real steps, real tools, real profiles, resolvable references.

`make template-validate`, the CI gate that keeps a template honest — the same job
`make connector-validate` does for bundles. Pydantic already rejects a malformed file at load and
`Template`'s own validators already reject duplicate ids and forward references; this adds the two
checks a per-file schema cannot make, because both are about the rest of the system:

1. **A step naming a tool, job or profile that does not exist.** A template is a *pinned* procedure,
   so this is worse than the equivalent typo in a skill: the run gets several steps in, spends real
   compute, and then fails on step four. Catching it in CI is the difference between a broken commit
   and a broken run.
2. **A template that no deployment can start.** An enabled name with no file behind it advertises
   nothing at run time and looks exactly like a capability that quietly stopped working.

Read-only; touches nothing.
"""

import sys

from chemclaw.agent.profiles import registered_profile_names
from chemclaw.connectors.registry import enabled as enabled_connectors
from chemclaw.templates.manifest import AgentStep, JobStep, Template, ToolStep
from chemclaw.templates.registry import TemplateError, discovered, enabled


def _available_tools() -> set[str]:
    """Every tool a template step could legitimately call: in-process plus every connector's.

    Importing the agent package is what populates the in-process registry, exactly as
    `chemclaw.cli.validate_skills` does it — the check has to see the real set, not a hardcoded
    list.
    """
    from chemclaw.agent.chemclaw_agent import available_tool_names

    return available_tool_names()


def _available_jobs() -> set[str]:
    """Every durable job an enabled connector declares (what a `job` step may name)."""
    return {job.name for manifest in enabled_connectors() for job in manifest.jobs}


def _step_problems(template: Template) -> list[str]:
    """Check every step's outward references — the tool, job or profile it names."""
    problems: list[str] = []
    tools = _available_tools()
    jobs = _available_jobs()
    profiles = set(registered_profile_names())
    for step in template.steps:
        if isinstance(step, ToolStep) and step.tool not in tools:
            problems.append(
                f"template {template.name!r} step {step.id!r} calls unknown tool "
                f"{step.tool!r}; available: {sorted(tools)}"
            )
        elif isinstance(step, JobStep) and step.job not in jobs:
            problems.append(
                f"template {template.name!r} step {step.id!r} runs unknown job "
                f"{step.job!r}; declared jobs: {sorted(jobs)}"
            )
        elif isinstance(step, AgentStep) and step.profile is not None:
            if step.profile not in profiles:
                problems.append(
                    f"template {template.name!r} step {step.id!r} names unknown profile "
                    f"{step.profile!r}; known: {sorted(profiles)}"
                )
    return problems


def validate_templates() -> list[str]:
    """Return one problem string per violation across every discovered template (empty = good).

    Discovery rather than the enabled set, for the reason `validate_connectors` gives: a template
    that is broken while disabled is one nobody can enable, and CI is where that should surface.
    """
    # Profiles are files too, and a template may name one — so they have to be registered before the
    # check can tell "unknown profile" from "not loaded yet".
    from chemclaw.agent.profile_discovery import load_profiles

    try:
        load_profiles()
        found = discovered()
    except ValueError as exc:  # ProfileError and TemplateError are both ValueError
        return [str(exc)]
    problems = [problem for template in found.values() for problem in _step_problems(template)]
    try:
        enabled()  # resolves `templates_enabled` against what exists
    except TemplateError as exc:
        problems.append(str(exc))
    return problems


def main() -> None:
    """Validate every template; print problems and exit non-zero if any (the CI gate)."""
    problems = validate_templates()
    if problems:
        print("template validation failed:")
        for problem in problems:
            print(f"  - {problem}")
        sys.exit(1)
    print("template validation passed.")


if __name__ == "__main__":
    main()
