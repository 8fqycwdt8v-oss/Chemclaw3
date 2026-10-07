"""Validate the step templates: real steps, real tools, real profiles, resolvable references.

`make template-validate`. Beyond what pydantic and `Template`'s own validators reject, a template
is a pinned procedure, so a broken reference fails a run several steps in. This refuses:

1. **A step naming a tool, job or profile that does not exist.**
2. **A step passing arguments the tool it names does not take.**
3. **An enabled template with no file behind it.**
4. **An `agent` step's declared writes** that the surface subtraction would silently ignore — a
   typo, a read tool, or a name outside the step's profile
   (`agent.template_surface.write_tool_problems`).

Arguments are checkable only when the tool is a function in this tree: the in-process `@tool`
registry and each bundle's own server tools module. Generated job/template launchers, upstream's
filesystem and todo tools, and every tool of a bundle served elsewhere are skipped, not guessed;
`unchecked_arguments` names the shipped-template steps in that last group, and
`make live-template-args` checks them against running servers. `job` payloads are validated at
launch by `prepare_job_launch`.

Read-only; touches nothing.
"""

import argparse
from collections.abc import Sequence

from chemclaw.agent.template_surface import (
    TemplateSurface,
    ToolArguments,
    argument_problems,
    resolvable_signatures,
    run_ceiling_problems,
    step_problems,
)
from chemclaw.core.config import settings
from chemclaw.templates.manifest import ToolStep
from chemclaw.templates.registry import TemplateError, discovered, enabled

# Re-exported for `validate_template_args_live`, so both lanes share one `argument_problems`. The
# definitions live in `agent.template_surface`.
__all__ = [
    "TemplateSurface",
    "ToolArguments",
    "argument_problems",
    "main",
    "resolvable_signatures",
    "step_problems",
    "unchecked_arguments",
    "validate_templates",
]


def unchecked_arguments(surface: TemplateSurface | None = None) -> dict[str, list[str]]:
    """Tools a *shipped template* names whose arguments this tree cannot check, by template.

    A bundle served from `Chemclaw3-mcp` has no local `server/tools.py`, so e.g. `hazard-briefing`'s
    `screen_hazards` step is name-checked only. Reported rather than raised (the template is not
    known
    to be wrong) and rather than silenced (so a pass says what it skipped); `make
    live-template-args`
    checks these against running servers. Takes the surface `main` already resolved.
    """
    signatures = surface.signatures if surface is not None else resolvable_signatures()
    unchecked: dict[str, list[str]] = {}
    for template in discovered().values():
        names = sorted(
            {
                step.tool
                for step in template.steps
                if isinstance(step, ToolStep) and step.tool not in signatures
            }
        )
        if names:
            unchecked[template.name] = names
    return unchecked


def validate_templates(surface: TemplateSurface | None = None) -> list[str]:
    """Return one problem string per violation across every discovered template (empty = good).

    Discovered, not only enabled: a template broken while disabled is one nobody can enable. An
    empty
    discovery is a problem, not a pass.
    """
    try:
        found = discovered()
        if not found:
            # Zero templates means nothing was checked: a mis-set `CHEMCLAW_TEMPLATES_DIR` or an
            # image missing
            # `data/templates/` looks exactly like this.
            return [
                f"no templates discovered under {settings.templates_dir!r} — every `run_*` "
                "launcher would be unavailable, and this gate would have checked nothing"
            ]
        # Resolved once for the whole run, not once per template — see `TemplateSurface`, which
        # is also where the file profiles a template may name are registered.
        surface = surface if surface is not None else TemplateSurface.resolve(declared=True)
    except ValueError as exc:  # ProfileError and TemplateError are both ValueError
        return [str(exc)]
    problems = [
        problem
        for template in found.values()
        # Both questions: do the steps resolve, and may the run finish them within its ceiling.
        for problem in [*step_problems(template, surface), *run_ceiling_problems(template)]
    ]
    try:
        enabled()  # resolves `templates_enabled` against what exists
    except TemplateError as exc:
        problems.append(str(exc))
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    """Validate every template; print problems and exit non-zero if any (the CI gate).

    The unchecked-argument note prints on both paths, since it qualifies a pass as much as a
    failure.
    Parses arguments though it declares none, so an unsupported argument is refused rather than
    ignored; `CHEMCLAW_TEMPLATES_DIR` is the knob.
    """
    argparse.ArgumentParser(
        prog="python -m chemclaw.cli.validate_templates",
        description="Validate every discovered step template. Set CHEMCLAW_TEMPLATES_DIR to "
        "point this at another tree.",
    ).parse_args(argv)
    # One surface for both halves of the report (resolving it is the slow part). Guarded because
    # resolving it loads every template and profile, so an invalid file surfaces here; report it as
    # a
    # problem line rather than a traceback.
    try:
        # Declared rather than bound, so a template naming an opt-in bundle's tool validates on a
        # checkout
        # that has not enabled it. `registry.unrunnable_reason` uses the bound set.
        surface = TemplateSurface.resolve(declared=True)
    except ValueError as exc:  # ProfileError and TemplateError are both ValueError
        problems = [str(exc)]
    else:
        problems = validate_templates(surface)
        for name, tools in sorted(unchecked_arguments(surface).items()):
            print(
                f"note: template {name!r} names {tools}, whose bundle is declared but not run "
                "here — name-checked, arguments unchecked"
            )
    if problems:
        print("template validation failed:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("template validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
