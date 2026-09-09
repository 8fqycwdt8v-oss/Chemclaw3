"""What a step template's references resolve against — the one definition, for both readers.

`make template-validate` has always been able to say that a template names a tool, a job or a
profile that does not exist: at a deployment with no `calc` bundle it reports *"template
'bond-strength-survey' step 'survey' runs unknown job 'survey_bond_strengths'; declared jobs: []"*
and exits 1. **Nothing at run time consulted that knowledge**, so `templates.registry.enabled()`
bound all nine `run_*` launchers on `templates_enabled` alone, a chemist could start a
bond-dissociation survey against a fleet that does not exist, and the system prompt then told the
model to report the id as work in progress and poll it.

This module is that check, promoted out of the CLI so the gate and the launcher share **one**
definition of what "resolves" means. Two copies of this rule would be the defect class this
repository keeps finding — a gate and a runtime that agree only by coincidence, until one of them
is edited.

**It lives in `agent/` because that is where the answer is.** Resolving a step means asking for the
whole tool surface (`chemclaw_agent.available_tool_names`), the durable jobs the enabled bundles
declare, and the registered profiles — and `chemclaw.templates` may not import `chemclaw.connectors`
(`tests/test_layering.py`), so a resolver inside the template package could not answer the job half
at all. `templates -> agent` and `agent -> connectors` are both edges this architecture has.

**The signatures are optional and that is a cost decision, not a taste one.** Resolving them
imports every discovered bundle's `server.tools` module and introspects every function in it,
which `TemplateSurface` measured at 14.45 s of the gate's 20.80 s. A CI gate pays that once; a tool
launch must not, so `TemplateSurface.resolve(with_signatures=False)` answers the name half and the
argument check simply does not fire — the same silence an unresolvable tool already produces.

Read-only; touches nothing.
"""

import importlib
import inspect
from typing import NamedTuple

from chemclaw.agent.profiles import registered_profile_names
from chemclaw.connectors.registry import discovered as discovered_connectors
from chemclaw.connectors.registry import enabled as enabled_connectors
from chemclaw.connectors.registry import server_tools_module
from chemclaw.core.tool_registry import registered_tools
from chemclaw.templates.manifest import AgentStep, JobStep, Template, ToolStep


def available_tools() -> set[str]:
    """Every tool a template step could legitimately call: in-process plus every connector's.

    Importing the agent package is what populates the in-process registry, exactly as
    `chemclaw.cli.validate_skills` does it — the check has to see the real set, not a hardcoded
    list.
    """
    from chemclaw.agent.chemclaw_agent import available_tool_names

    return available_tool_names()


def available_jobs() -> set[str]:
    """Every durable job an enabled connector declares (what a `job` step may name)."""
    return {job.name for manifest in enabled_connectors() for job in manifest.jobs}


def resolvable_signatures() -> dict[str, inspect.Signature]:
    """Every tool name whose parameters this tree can answer for, mapped to its signature.

    Two sources, both local: the in-process `@tool` registry, and each discovered bundle's own
    `chemclaw.connectors.<name>.server.tools` module, whose function names *are* the tool names the
    manifest declares. A bundle with no server module (`results` is jobs-only) and a declared
    name the
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
    It used to be supplied by `step_problems` happening to call `available_tools()` two lines
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


class ToolArguments(NamedTuple):
    """What a tool accepts, in the only three terms an argument check needs.

    Extracted because there are now two authorities for the same question and they must give the
    same answer in the same words. This gate reads a local `inspect.Signature`; the live gate
    (`chemclaw.cli.validate_template_args_live`) reads a running server's `args_schema`, which is
    the only authority that exists for a bundle we declare and do not run. Both build one of these
    and hand it to `argument_problems`, so "wrong key" and "missing required argument" have one
    definition rather than one per lane — two lanes disagreeing about what a template's arguments
    mean would be worse than the gap the second one closes.
    """

    accepted: frozenset[str]
    required: frozenset[str]
    takes_any_key: bool
    """True when the tool absorbs any keyword (`**kwargs`, or an open JSON schema): the unknown-key
    check is then vacuous and is skipped, while the missing-required check still applies."""

    @classmethod
    def of_signature(cls, signature: inspect.Signature) -> "ToolArguments":
        """Read a local implementation's parameters — this gate's authority."""
        named = [
            p
            for p in signature.parameters.values()
            if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        ]
        return cls(
            accepted=frozenset(p.name for p in named),
            required=frozenset(p.name for p in named if p.default is inspect.Parameter.empty),
            takes_any_key=any(
                p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()
            ),
        )


def argument_problems(template: Template, step: ToolStep, accepts: ToolArguments) -> list[str]:
    """Check one tool step's argument *keys* against the arguments the tool actually takes.

    Keys only, never values: a template's argument may be a `${...}` reference whose type is known
    only once the run substitutes it, so type-checking here would reject correct templates. A wrong
    key, by contrast, is wrong at every possible substitution.
    """
    problems: list[str] = []
    given = set(step.arguments)
    unknown = sorted(given - accepts.accepted)
    if unknown and not accepts.takes_any_key:
        problems.append(
            f"template {template.name!r} step {step.id!r} passes argument(s) {unknown} that "
            f"{step.tool!r} does not take; it accepts: {sorted(accepts.accepted)}"
        )
    missing = sorted(accepts.required - given)
    if missing:
        problems.append(
            f"template {template.name!r} step {step.id!r} omits required argument(s) {missing} "
            f"of {step.tool!r}"
        )
    return problems


class TemplateSurface(NamedTuple):
    """What every template is checked against: the tools, jobs, profiles and signatures that exist.

    Invariant across templates, and it used to be rebuilt for each one — `step_problems` called
    all four helpers on entry, so the whole surface was re-derived per template. Measured on the
    nine shipped templates, `resolvable_signatures` alone ran ten times for **14.45 s of the
    gate's 20.80 s**, because it imports the agent package and every discovered bundle's
    `server.tools` module and introspects every function in them. The cost grew linearly with each
    template added, for an answer that cannot change between two of them.

    Computed once and passed down rather than memoised with `functools.cache`, deliberately. Two
    tests in `tests/test_templates.py` pin behaviour a process-wide cache would erase: one asserts
    `resolvable_signatures` *raises* when a bundle cannot be imported, which a cached earlier
    success would swallow, and one asserts the resolved set is independent of call order, which a
    cache would satisfy trivially while the ordering hazard it guards stayed open.
    """

    tools: set[str]
    jobs: set[str]
    profiles: set[str]
    signatures: dict[str, inspect.Signature]

    @classmethod
    def resolve(cls, *, with_signatures: bool = True) -> "TemplateSurface":
        """Derive the whole surface once. The call order matters — see `resolvable_signatures`.

        Registering the file profiles is part of resolving, not something each caller does first.
        `registered_profile_names()` holds only the built-in `default` until `load_profiles()` has
        run, and `main` resolved the surface before anything had — so from the CI gate every
        shipped profile read as unknown, a template naming one was rejected, and rule 3 of
        `write_tool_problems` could never fire, because an unknown profile falls back to the whole
        tool surface. The load is idempotent, so resolving twice registers once.

        Args:
            with_signatures: Whether to resolve each tool's parameters as well as its name. The
                runtime precondition (`templates.registry`) passes False: the signatures are the
                14.45 s half of this derivation, and an empty mapping is not a *weaker* answer to
                the name question — it is the same silence an unresolvable tool already produces,
                so the argument check simply does not fire. A launch that must not pay 14 s to
                start is a different caller from a gate that runs once per commit.

        Returns:
            The surface every template is checked against.

        Raises:
            ProfileError: When a profile file is malformed, or two claim one name. Both callers
                report it rather than raising, the way every other problem here is reported.
        """
        from chemclaw.agent.profile_discovery import load_profiles

        load_profiles()
        return cls(
            tools=available_tools(),
            jobs=available_jobs(),
            profiles=set(registered_profile_names()),
            signatures=resolvable_signatures() if with_signatures else {},
        )


def step_problems(template: Template, surface: TemplateSurface | None = None) -> list[str]:
    """Check every step's outward references — the tool, job or profile it names, and its args.

    `surface` is passed by `validate_templates`, which resolves it once for the whole run. The
    default resolves it here, so a caller checking a single template — the tests do — needs no
    ceremony to do the obvious thing.
    """
    problems: list[str] = []
    surface = surface if surface is not None else TemplateSurface.resolve()
    tools = surface.tools
    jobs = surface.jobs
    profiles = surface.profiles
    signatures = surface.signatures
    for step in template.steps:
        if isinstance(step, ToolStep) and step.tool not in tools:
            problems.append(
                f"template {template.name!r} step {step.id!r} calls unknown tool "
                f"{step.tool!r}; available: {sorted(tools)}"
            )
        elif isinstance(step, ToolStep) and step.tool in signatures:
            problems.extend(
                argument_problems(template, step, ToolArguments.of_signature(signatures[step.tool]))
            )
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
            problems.extend(write_tool_problems(template, step, tools, known_profile))
    return problems


def write_tool_problems(
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
