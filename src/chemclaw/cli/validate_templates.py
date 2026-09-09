"""Validate the step templates: real steps, real tools, real profiles, resolvable references.

`make template-validate`, the CI gate that keeps a template honest — the same job
`make connector-validate` does for bundles. Pydantic already rejects a malformed file at load and
`Template`'s own validators already reject duplicate ids and forward references; this adds the
checks a per-file schema cannot make, because each is about the rest of the system:

1. **A step naming a tool, job or profile that does not exist.** A template is a *pinned* procedure,
   so this is worse than the equivalent typo in a skill: the run gets several steps in, spends real
   compute, and then fails on step four. Catching it in CI is the difference between a broken commit
   and a broken run.
2. **A step passing arguments the tool it names does not take.** This checked only the *name* until
   the 2026-08-08 review, which is half a reference: renaming `smiles` to `smilez` in the shipped
   `hazard-briefing` template and adding `nonexistent_arg: 42` beside it passed validation, and
   would have failed at the first live run of step one, after the launch, inside an activity. The
   name check exists because a template is pinned; the argument check exists for the same reason,
   and the gap between them was the whole distance from "this template is validated" to "this
   template can run".
3. **A template that no deployment can start.** An enabled name with no file behind it advertises
   nothing at run time and looks exactly like a capability that quietly stopped working.
4. **An `agent` step's declared writes.** The step's surface is computed by *subtracting* every
   undeclared side-effecting tool, and a subtraction says nothing about names it never had to
   remove — so a typo, a read tool, or a name outside the step's profile all read as a granted
   write in the file and are silently nothing at run time
   (`agent.template_surface.write_tool_problems`).

**Where the argument check can and cannot reach.** A tool's parameters are knowable here only when
its implementation is a function in this tree: the in-process `@tool` registry, and each connector
bundle's own server tools module (the declared endpoint tool names are that module's function
names — the same convention `cli/connectors_dev.py` and `connectors/server_entry.py` resolve by).

That resolves 43 of the 66 tools a template could name (re-measured; it was 50 of 61 before the
capability migration). The 23 it cannot are skipped rather than guessed at — an unresolvable tool
leaves the argument check silent, which is what keeps it from inventing failures about surfaces
that only exist at run time. They fall in three groups: every job-launcher and template-launcher,
whose `params` model is generated; upstream's filesystem and todo tools; and — this is the new
one — **every tool of a bundle we declare but do not run**, because there is no local
`connectors/<name>/server/tools.py` to read a signature from.

**That third group broke a claim this docstring used to make.** It said the check "covers every
tool the shipped templates call". It no longer does: `hazard-briefing` calls `screen_hazards`,
which is now `Chemclaw3-mcp`'s. Since the skip is silent by design, the loss would have been
invisible — so `unchecked_arguments` reports it by name and `main` prints it on the passing path
too. `job` steps stay left to the launch itself: a connector job's payload is validated against its
declared params model in `prepare_job_launch`.

**The report stays, and the gap is now closed elsewhere.** `make live-template-args`
(`chemclaw.cli.validate_template_args_live`) opens real connector sessions and checks the same
arguments against what each running server advertises, which is the only authority that exists for
a bundle we do not run. It is a *live-lane* target, deliberately not part of `ci`: this module runs
offline and must keep doing so, and a gate that needs a network is a gate that goes red for reasons
that are not about the diff. The two share `ToolArguments` and `argument_problems`, so the rule and
its wording have one definition; only the authority they read differs. See
`docs/decisions/D-2026-08-27-an-argument-check-needs-a-live-session.md`.

Read-only; touches nothing.
"""

import argparse
from collections.abc import Sequence

from chemclaw.agent.template_surface import (
    TemplateSurface,
    ToolArguments,
    argument_problems,
    resolvable_signatures,
    step_problems,
)
from chemclaw.core.config import settings
from chemclaw.templates.manifest import ToolStep
from chemclaw.templates.registry import TemplateError, discovered, enabled

# Re-exported for `chemclaw.cli.validate_template_args_live`, the live half of this gate: it reads
# a running server's `args_schema` where this one reads a local signature, and both hand the result
# to the *same* `argument_problems` so the two lanes cannot disagree about what a template's
# arguments mean. The definitions moved to `agent.template_surface` when the runtime precondition
# needed them (`templates.registry`); the import path the live lane already uses did not have to.
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

    The gap this reports is new and was introduced by the capability migration
    (`D-2026-08-15-capability-moves-judgment-and-declaration-stay`): the argument check resolves a
    signature from a bundle's own `connectors/<name>/server/tools.py`, and a bundle we declare but
    do not run has no such module here. `screen_hazards` is the first tool a shipped template names
    that fell into it, so `hazard-briefing` is name-checked and **not** argument-checked.

    Reported rather than raised, and reported rather than left silent. Not raised, because the
    template is correct — nothing here can prove it, which is a different thing from it being
    wrong, and failing would force deleting a good template to make a validator pass. Not silent,
    because "template validation passed" would otherwise mean less than it did the day before,
    with nothing in the output saying so. This module's own docstring warns against exactly that
    shape ("an unresolvable tool leaves the argument check silent"); the warning was written about
    job launchers, which no template names, and the migration made it true of one that does.

    Takes the signatures `main` already resolved for `validate_templates`, for the reason
    `TemplateSurface` gives: deriving them is the expensive half of this gate and the answer is
    the same for every template and for both callers.

    **This is a note about *this lane*, not a statement that nothing checks these.**
    `make live-template-args` does, against the running servers. Keeping the note is still right:
    the live lane is not run on a diff, so what an offline gate did not check remains something its
    reader has to be told.
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

    Discovery rather than the enabled set, for the reason `validate_connectors` gives: a template
    that is broken while disabled is one nobody can enable, and CI is where that should surface.

    An **empty** discovery is a problem rather than a pass, which it was not: both sibling seams
    already refuse one, and this gate printed its green line over an empty directory and over a
    path that does not exist alike.
    """
    try:
        found = discovered()
        if not found:
            # Zero templates is not a clean sheet, it is a gate with nothing to check — the same
            # refusal `validate_datasources` and `validate_connectors` make, for the same reason.
            # An empty directory and one that does not exist both arrive here identically, and
            # both are what a mis-set `CHEMCLAW_TEMPLATES_DIR` or an image that failed to ship
            # `data/templates/` look like. Every `run_*` launcher the agent advertises is backed
            # by one of these files.
            return [
                f"no templates discovered under {settings.templates_dir!r} — every `run_*` "
                "launcher would be unavailable, and this gate would have checked nothing"
            ]
        # Resolved once for the whole run, not once per template — see `TemplateSurface`, which
        # is also where the file profiles a template may name are registered.
        surface = surface if surface is not None else TemplateSurface.resolve()
    except ValueError as exc:  # ProfileError and TemplateError are both ValueError
        return [str(exc)]
    problems = [
        problem for template in found.values() for problem in step_problems(template, surface)
    ]
    try:
        enabled()  # resolves `templates_enabled` against what exists
    except TemplateError as exc:
        problems.append(str(exc))
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    """Validate every template; print problems and exit non-zero if any (the CI gate).

    The unchecked-argument note prints on both paths, because it qualifies a pass just as much as
    it qualifies a failure — and a reader who only ever sees the green line is the one it is for.

    Parses even though it declares no option, for the reason its siblings do: an argument this
    cannot honour is refused rather than discarded under a green line. `CHEMCLAW_TEMPLATES_DIR`
    is the knob.
    """
    argparse.ArgumentParser(
        prog="python -m chemclaw.cli.validate_templates",
        description="Validate every discovered step template. Set CHEMCLAW_TEMPLATES_DIR to "
        "point this at another tree.",
    ).parse_args(argv)
    # One surface for both halves of the report: `validate_templates` would otherwise resolve it
    # and `unchecked_arguments` resolve the identical thing again, at ~5 s a time.
    #
    # **Guarded, because this is where an invalid *manifest* or *profile* surfaces.** Resolving the
    # surface registers the profile files and reaches `available_tool_names`, which asks the
    # template registry for the `run_*` launchers and so loads every file — before the step checker
    # below has run a single check. A template with an unknown `${inputs.x}` or a forward
    # `${steps.y.result}` therefore raised straight through `main`, and the operator got a pydantic
    # traceback where every sibling validator prints a problem line. The exit code was 1 either
    # way, so CI was never misled: what was wrong is that the gate failed *looking like a crash*,
    # which `validate_kg.main` argues against in as many words. Reported through the same block as
    # every other problem, so there is one report shape.
    try:
        surface = TemplateSurface.resolve()
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
