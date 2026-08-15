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
   write in the file and are silently nothing at run time (`_write_tool_problems`).

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
`server/tools.py` to read a signature from.

**That third group broke a claim this docstring used to make.** It said the check "covers every
tool the shipped templates call". It no longer does: `hazard-briefing` calls `screen_hazards`,
which is now `Chemclaw3-mcp`'s. Since the skip is silent by design, the loss would have been
invisible — so `unchecked_arguments` reports it by name and `main` prints it on the passing path
too. `job` steps stay left to the launch itself: a connector job's payload is validated against its
declared params model in `prepare_job_launch`.

Read-only; touches nothing.
"""

import importlib
import inspect

from chemclaw.agent.profiles import registered_profile_names
from chemclaw.connectors.registry import discovered as discovered_connectors
from chemclaw.connectors.registry import enabled as enabled_connectors
from chemclaw.connectors.registry import server_tools_module
from chemclaw.core.tool_registry import registered_tools
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


def _resolvable_signatures() -> dict[str, inspect.Signature]:
    """Every tool name whose parameters this tree can answer for, mapped to its signature.

    Two sources, both local: the in-process `@tool` registry, and each discovered bundle's own
    `chemclaw.connectors.<name>.server.tools` module, whose function names *are* the tool names the
    manifest declares. A bundle with no server module (`qm` is jobs-only) and a declared name the
    module does not define are both skipped — whether a bundle serves what it declares is
    `make connector-validate`'s question, and answering it twice, differently, here would be worse
    than not answering it.

    **A bundle that cannot be imported is not "skipped", it is broken.** This used to swallow every
    `ImportError`, transitive ones included, which is the vacuous pass the paragraph below warns
    against, arrived at from the other direction: one injected missing dependency in `chem` took
    the resolved set from 50 signatures to 46 and still printed "template validation passed".
    `server_tools_module` is now the single definition of that import, shared with
    `make connector-validate`, and it raises rather than returning `None` for that case.

    **The agent import is load-bearing, not incidental.** `registered_tools()` is populated as an
    import side effect of `chemclaw.agent.chemclaw_agent`, so without it this returns the connector
    half only: measured, 30 signatures and 31 advertised tools uncovered, against 50 and 11 with it.
    It used to be supplied by `_step_problems` happening to call `_available_tools()` two lines
    earlier — so reordering those lines, or calling this function from anywhere else, would have
    dropped 20 in-process tools from the argument check **with no failure at all**; the validator
    would simply have checked less and still printed "template validation passed".
    """
    importlib.import_module("chemclaw.agent.chemclaw_agent")
    signatures = {fn.__name__: inspect.signature(fn) for fn in registered_tools()}
    for name, (_bundle, manifest) in discovered_connectors().items():
        endpoint = manifest.endpoint
        if endpoint is None:
            continue
        module = server_tools_module(name)
        if module is None:
            continue
        for tool_name in endpoint.tools:
            fn = getattr(module, tool_name, None)
            if callable(fn):
                signatures[tool_name] = inspect.signature(fn)
    return signatures


def _argument_problems(
    template: Template, step: ToolStep, signature: inspect.Signature
) -> list[str]:
    """Check one tool step's argument *keys* against the parameters the tool actually takes.

    Keys only, never values: a template's argument may be a `${...}` reference whose type is known
    only once the run substitutes it, so type-checking here would reject correct templates. A wrong
    key, by contrast, is wrong at every possible substitution.
    """
    named = [
        p
        for p in signature.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    ]
    takes_any_key = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()
    )
    problems: list[str] = []
    given = set(step.arguments)
    accepted = {p.name for p in named}
    unknown = sorted(given - accepted)
    if unknown and not takes_any_key:
        problems.append(
            f"template {template.name!r} step {step.id!r} passes argument(s) {unknown} that "
            f"{step.tool!r} does not take; it accepts: {sorted(accepted)}"
        )
    missing = sorted({p.name for p in named if p.default is inspect.Parameter.empty} - given)
    if missing:
        problems.append(
            f"template {template.name!r} step {step.id!r} omits required argument(s) {missing} "
            f"of {step.tool!r}"
        )
    return problems


def unchecked_arguments() -> dict[str, list[str]]:
    """Tools a *shipped template* names whose arguments this tree cannot check, by template.

    The gap this reports is new and was introduced by the capability migration
    (`D-2026-08-15-capability-moves-judgment-and-declaration-stay`): the argument check resolves a
    signature from a bundle's own `server/tools.py`, and a bundle we declare but do not run has no
    such module here. `screen_hazards` is the first tool a shipped template names that fell into
    it, so the shipped `hazard-briefing` template is name-checked and **not** argument-checked.

    Reported rather than raised, and reported rather than left silent. Not raised, because the
    template is correct — nothing here can prove it, which is a different thing from it being
    wrong, and failing would force deleting a good template to make a validator pass. Not silent,
    because "template validation passed" would otherwise mean less than it did the day before,
    with nothing in the output saying so. This module's own docstring warns against exactly that
    shape ("an unresolvable tool leaves the argument check silent"); the warning was written about
    job launchers, which no template names, and the migration made it true of one that does.
    """
    signatures = _resolvable_signatures()
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


def _step_problems(template: Template) -> list[str]:
    """Check every step's outward references — the tool, job or profile it names, and its args."""
    problems: list[str] = []
    tools = _available_tools()
    jobs = _available_jobs()
    profiles = set(registered_profile_names())
    signatures = _resolvable_signatures()
    for step in template.steps:
        if isinstance(step, ToolStep) and step.tool not in tools:
            problems.append(
                f"template {template.name!r} step {step.id!r} calls unknown tool "
                f"{step.tool!r}; available: {sorted(tools)}"
            )
        elif isinstance(step, ToolStep) and step.tool in signatures:
            problems.extend(_argument_problems(template, step, signatures[step.tool]))
        elif isinstance(step, JobStep) and step.job not in jobs:
            problems.append(
                f"template {template.name!r} step {step.id!r} runs unknown job "
                f"{step.job!r}; declared jobs: {sorted(jobs)}"
            )
        elif isinstance(step, AgentStep):
            known_profile = step.profile is None or step.profile in profiles
            if not known_profile:
                problems.append(
                    f"template {template.name!r} step {step.id!r} names unknown profile "
                    f"{step.profile!r}; known: {sorted(profiles)}"
                )
            problems.extend(_write_tool_problems(template, step, tools, known_profile))
    return problems


def _write_tool_problems(
    template: Template, step: AgentStep, tools: set[str], known_profile: bool
) -> list[str]:
    """Check an agent step's declared writes: each exists, actually writes, and is reachable.

    An `agent` step is read-only unless it declares otherwise (`templates/manifest.AgentStep`), and
    the declaration is applied by subtracting from a set — which is the failure mode this guards.
    A subtraction is silent about names it never had to remove, so every way of writing the
    declaration wrong produces a step that runs and quietly holds a different surface than the file
    appears to grant. Three checks, each closing one of those:

    1. **The name exists.** A typo would otherwise be a write the step believes it declared and does
       not have, discovered when the model reaches for it mid-run — the same "fails at step four
       after spending compute" this validator exists to prevent.
    2. **The name actually writes** (`chemclaw.agent.authz.side_effecting_tools`). A read tool needs
       no declaration to be reachable, so naming one grants nothing — and accepting it would let
       this list drift into a general allow-list wearing a write-list's name, which is how the
       narrowing would eventually be widened by people writing what looks like documentation. The
       same classification the narrowing subtracts, asked here, so the two cannot disagree.
    3. **The step's own profile advertises it.** `step_profile` intersects the declaration with what
       the profile already offered, because a step must not gain capability its profile never had —
       so a name outside that surface is accepted by the file and silently dropped at run time.
       Skipped when the profile itself is unknown: that is already one problem, and asking what an
       unresolvable profile advertises would raise here instead of reporting it.
    """
    if not step.write_tools:
        return []
    from chemclaw.agent.authz import side_effecting_tools
    from chemclaw.agent.chemclaw_agent import advertised_tool_names

    writes = side_effecting_tools()
    advertised = advertised_tool_names(step.profile) if known_profile else frozenset(tools)
    where = f"template {template.name!r} step {step.id!r}"
    problems: list[str] = []
    for name in step.write_tools:
        if name not in tools:
            problems.append(
                f"{where} declares unknown write tool {name!r}; available: {sorted(tools)}"
            )
        elif name not in writes:
            problems.append(
                f"{where} declares {name!r} as a write tool, but it changes nothing — a read tool "
                "needs no declaration, so remove it rather than widening the list"
            )
        elif name not in advertised:
            problems.append(
                f"{where} declares write tool {name!r}, which profile "
                f"{step.profile or 'default'!r} does not advertise; a step cannot gain a tool "
                "its profile never had"
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


def main() -> int:
    """Validate every template; print problems and exit non-zero if any (the CI gate).

    The unchecked-argument note prints on both paths, because it qualifies a pass just as much as
    it qualifies a failure — and a reader who only ever sees the green line is the one it is for.
    """
    problems = validate_templates()
    for name, tools in sorted(unchecked_arguments().items()):
        print(
            f"note: template {name!r} names {tools}, whose bundle is declared but not run here — "
            "name-checked, arguments unchecked"
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
