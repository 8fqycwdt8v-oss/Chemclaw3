"""What a step template's references resolve against — the one definition, for both readers.

`make template-validate` and the runtime launch gate (`templates.registry`) both ask whether a
template's tools, jobs and profiles exist here, so they share this one definition rather than two
that could drift. It lives in `agent/` because answering needs the whole tool surface, the enabled
bundles' jobs and the registered profiles, and `chemclaw.templates` may not import
`chemclaw.connectors` (`tests/test_layering.py`).

Resolving signatures is optional: it imports and introspects every bundle's `server.tools` module,
which dominates the gate's runtime. A CI gate pays that once; a launch passes
`with_signatures=False`, and the argument check then simply does not fire.

Read-only; touches nothing.
"""

import importlib
import inspect
from collections.abc import Mapping
from typing import Any, NamedTuple

from pydantic import BaseModel

from chemclaw.agent.profiles import registered_profile_names
from chemclaw.connectors.registry import discovered as discovered_connectors
from chemclaw.connectors.registry import enabled as enabled_connectors
from chemclaw.connectors.registry import server_tools_module
from chemclaw.core.config import settings
from chemclaw.core.tool_registry import registered_tools
from chemclaw.templates.manifest import AgentStep, JobStep, Template, ToolStep
from chemclaw.templates.schedule import batches, schedule


def available_tools(*, declared: bool = False) -> set[str]:
    """Every tool a template step could legitimately call: in-process plus every connector's.

    Importing the agent package populates the in-process registry, so the check sees the real set.
    `declared` chooses the question: `make template-validate` asks what this tree declares (a
    template using an off-by-default bundle must still resolve), while `registry.unrunnable_reason`
    asks what this deployment binds (and must refuse at launch). The default is the bound set.
    """
    from chemclaw.agent.chemclaw_agent import available_tool_names, declared_tool_names

    return declared_tool_names() if declared else available_tool_names()


def available_jobs() -> set[str]:
    """Every durable job an enabled connector declares (what a `job` step may name)."""
    return {job.name for manifest in enabled_connectors() for job in manifest.jobs}


def unbound_opt_in_references(template: Template) -> list[str]:
    """The tools and jobs `template` names that a bundle declares and this deployment leaves off.

    A name some bundle declares but no enabled one binds is an opt-in capability this deployment has
    not turned on. A name no bundle declares is a typo or deletion, left to `make template-validate`
    and not counted here, so a broken template keeps its launcher and is refused at launch with the
    problem named. Asked against the connector registry alone, because `available_tool_names`
    includes the launchers being decided.

    Args:
        template: The template whose launcher is being decided.

    Returns:
        The declared-but-unbound tool and job names it steps through, sorted; `[]` when every one of
        them is either bound here or declared by nothing.
    """
    from chemclaw.connectors.registry import connector_tool_names, declared_connector_tool_names

    unbound = set(declared_connector_tool_names()) - set(connector_tool_names())
    named = {
        step.tool if isinstance(step, ToolStep) else step.job
        for step in template.steps
        if isinstance(step, ToolStep | JobStep)
    }
    return sorted(named & unbound)


def profile_named_tools() -> frozenset[str]:
    """Every tool name any profile lists explicitly — the names a build would refuse to lose.

    `chemclaw_agent._reject_unknown_tool_names` raises when a profile lists a name the surface
    lacks, so a launcher some profile names must stay bound. A profile with `tool_names` unset lists
    nothing. Read from the profile registry rather than the files, since this sits under the
    frequently called `available_tool_names`; withholding is applied where the tool registry is read
    (`chemclaw_agent.withheld_tool_names`), so the answer stays current.
    """
    from chemclaw.agent.profiles import get_profile

    return frozenset(
        name
        for profile_name in registered_profile_names()
        for name in get_profile(profile_name).tool_names or ()
    )


def resolvable_signatures() -> dict[str, inspect.Signature]:
    """Every tool name whose parameters this tree can answer for, mapped to its signature.

    Two local sources: the in-process `@tool` registry, and each discovered bundle's
    `chemclaw.connectors.<name>.server.tools` module, whose function names are the declared tool
    names. A bundle with no server module (`results` is jobs-only), or a declared name the module
    lacks, is skipped; that is `make connector-validate`'s question. A bundle that fails to import
    raises (via `server_tools_module`, shared with `make connector-validate`) rather than silently
    shrinking the checked set.

    The agent import is load-bearing: `registered_tools()` is populated as a side effect of
    importing `chemclaw.agent.chemclaw_agent`, and without it the in-process half would silently go
    unchecked.
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

    Built both from a local `inspect.Signature` (this gate) and from a running server's schema
    (`chemclaw.cli.validate_template_args_live`), and both go through `argument_problems`, so "wrong
    key" and "missing required argument" have one definition.
    """

    accepted: frozenset[str]
    required: frozenset[str]
    takes_any_key: bool
    """True when the tool absorbs any keyword (`**kwargs`, or an open JSON schema): the unknown-key
    check is then vacuous and is skipped, while the missing-required check still applies."""

    @classmethod
    def of_schema(cls, schema: Mapping[str, Any]) -> "ToolArguments":
        """Read a *running* tool's advertised JSON schema — the live gate's authority.

        One reading for every live caller (the template argument gate and a hypothesis check's
        dispatcher), produced by `normalise_tool_schema`.
        """
        return cls(
            accepted=frozenset(schema.get("properties") or {}),
            required=frozenset(schema.get("required") or []),
            # Only a literal `True` makes the schema open (like `**kwargs`, absorbing any key); an
            # absent `additionalProperties` means closed for a tool declaration.
            takes_any_key=schema.get("additionalProperties") is True,
        )

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


def normalise_tool_schema(tool: Any) -> Mapping[str, Any] | None:
    """The JSON schema a running tool advertises, or `None` where it advertises none readably.

    `tool_call_schema` rather than `args_schema`, because injected arguments are already removed
    from it. Handles both MCP tools' plain JSON-schema dicts and in-process tools' pydantic models.
    """
    schema: Any = getattr(tool, "tool_call_schema", None)
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        schema = schema.model_json_schema()
    return schema if isinstance(schema, Mapping) else None


def argument_problems(template: Template, step: ToolStep, accepts: ToolArguments) -> list[str]:
    """Check one tool step's argument *keys* against the arguments the tool actually takes.

    Keys only: a value may be a `${...}` reference whose type is known only at run time, while a
    wrong key is wrong at every substitution.
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

    Invariant across templates, so it is computed once and passed down. Not memoised with
    `functools.cache`, because tests in `tests/test_templates.py` rely on `resolvable_signatures`
    raising on an unimportable bundle and on the result being independent of call order, which a
    process-wide cache would mask.
    """

    tools: set[str]
    jobs: set[str]
    profiles: set[str]
    signatures: dict[str, inspect.Signature]

    @classmethod
    def resolve(cls, *, with_signatures: bool = True, declared: bool = False) -> "TemplateSurface":
        """Derive the whole surface once. The call order matters — see `resolvable_signatures`.

        Registering the file profiles is part of resolving: `registered_profile_names()` holds only
        `default` until `load_profiles()` has run. The load is idempotent.

        Args:
            declared: Check against every tool this tree *declares* rather than the ones this
            deployment binds. True for `make template-validate`, False for the runtime launch gate —
            see `available_tools`.
            with_signatures: Whether to resolve each tool's parameters as well as its name. The
            runtime precondition (`templates.registry`) passes False: the signatures are the
            expensive half, and without them the argument check simply does not fire.

        Returns:
            The surface every template is checked against.

        Raises:
            ProfileError: When a profile file is malformed, or two claim one name. Both callers
            report it rather than raising.
        """
        from chemclaw.agent.profile_discovery import load_profiles

        load_profiles()
        return cls(
            tools=available_tools(declared=declared),
            jobs=available_jobs(),
            profiles=set(registered_profile_names()),
            signatures=resolvable_signatures() if with_signatures else {},
        )


def step_problems(template: Template, surface: TemplateSurface | None = None) -> list[str]:
    """Check every step's outward references — the tool, job or profile it names, and its args.

    `surface` is passed by `validate_templates`, which resolves it once per run; by default it is
    resolved here, so checking a single template needs no setup.
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


def _wave_ceiling(
    wave: tuple[Any, ...], ceilings: dict[str, tuple[float, str]], limit: int
) -> float:
    """What one wave may cost: each of its batches costs that batch's slowest member.

    Summed over the batches the run will actually dispatch (`templates/schedule.batches`, the same
    function `TemplateWorkflow._run_wave` uses), not the wave's slowest member times the batch
    count, which would charge a slow step to batches that do not hold it and refuse templates that
    would finish.

    Args:
        wave: The steps that may run together, in declared order.
        ceilings: `settings.template_step_ceilings()`, one `(seconds, why)` per step kind.
        limit: How many of a wave may be in flight at once — `orchestrator_max_parallel_children`,
        which is also what `TemplateRunInput.max_parallel_steps` pins into the run.

    Returns:
        The wave's ceiling in seconds.
    """
    return sum(max(ceilings[step.kind][0] for step in batch) for batch in batches(wave, limit))


#: How many of a wave's members the refusal names before stating the width instead; for a very wide
#: wave the full list would bury the width.
_NAMED_MEMBERS = 4


def _wave_reason(wave: tuple[Any, ...], ceilings: dict[str, tuple[float, str]], limit: int) -> str:
    """One wave, spelled so a reader adding the printed numbers gets the printed total.

    Concurrent members are joined with `" | "` inside brackets carrying the wave's own cost (`+`
    would read as addition); a wave of one prints as the bare step, and a wide wave states its width
    rather than listing itself.

    Args:
        wave: The steps that may run together, in declared order.
        ceilings: `settings.template_step_ceilings()`.
        limit: The per-wave concurrency bound.

    Returns:
        The wave as one term of the total.
    """
    named = wave[:_NAMED_MEMBERS]
    members = " | ".join(f"{step.id}={ceilings[step.kind][0]:,.0f}s" for step in named)
    if len(wave) == 1:
        return members
    if len(wave) > len(named):
        members += f" | … {len(wave)} steps in {len(batches(wave, limit))} batches"
    return f"[{_wave_ceiling(wave, ceilings, limit):,.0f}s: {members}]"


def run_ceiling_problems(template: Template) -> list[str]:
    """Check that this deployment's run ceiling covers every step this template declares.

    `core/config` can only check that `template_run_timeout_seconds` covers the longest single step,
    since `Settings` cannot see `data/templates/`. A template whose steps together exceed the
    ceiling would die silently: a workflow execution timeout is not delivered to workflow code, so
    no failure is notified or recorded.

    The cost is summed over waves (each costing its slowest member once per dispatched batch of
    `max_parallel_steps`, via `templates/schedule.batches`), not over steps: an over-stating bound
    would refuse templates that would finish, and an under-stating one would pass a very wide wave
    as if all members ran at once.

    Read by both `make template-validate` and `registry.unrunnable_reason`, so a file that cannot
    complete is refused at the gate and at launch.

    Args:
        template: The template to size, steps and all.

    Returns:
        One problem line when the run ceiling cannot hold the declared steps, or `[]`.
    """
    ceilings = settings.template_step_ceilings()
    # `KeyError` rather than a default, so an unsized step kind is never counted as free. Summed
    # over waves, since a wave's steps run together.
    waves = schedule(template)
    # The same bound the run enforces: `TemplateWorkflow._run_wave` runs a wave in batches of
    # `max_parallel_steps`, pinned from this setting at launch.
    limit = max(1, settings.orchestrator_max_parallel_children)
    needed = sum(_wave_ceiling(wave, ceilings, limit) for wave in waves)
    if needed <= settings.template_run_timeout_seconds:
        return []
    return [
        f"template {template.name!r} declares steps that cannot finish inside "
        f"template_run_timeout_seconds={settings.template_run_timeout_seconds:,.0f}: they may take "
        f"{needed:,.0f}s in total "
        f"({' + '.join(_wave_reason(wave, ceilings, limit) for wave in waves)}). Waves are added "
        f"up; the steps inside one `[…]` run together at most "
        f"{limit} at a time, so that wave costs its slowest member once per batch, not the sum of "
        "its members. A run that outlives that ceiling is terminated by "
        "Temporal without its workflow code running, so the chemist is told nothing and no failure "
        "row is written. Raise template_run_timeout_seconds above the total, or shorten the "
        "procedure."
    ]


def write_tool_problems(
    template: Template, step: AgentStep, tools: set[str], known_profile: bool
) -> list[str]:
    """Check an agent step's declared writes: each exists, actually writes, and is reachable.

    An `agent` step is read-only unless it declares writes, and the declaration is applied by set
    subtraction, which is silent about names it never removes. So three checks:

    1. **The name exists**, or the step would discover the missing write mid-run.
    2. **The name actually writes** (`chemclaw.agent.authz.side_effecting_tools`, the same
       classification the narrowing uses), so the list cannot become a general allow-list.
    3. **The step's own profile advertises it**, since `step_profile` intersects with what the
       profile offered and would silently drop it. Skipped when the profile is unknown, which is
       already reported.
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
