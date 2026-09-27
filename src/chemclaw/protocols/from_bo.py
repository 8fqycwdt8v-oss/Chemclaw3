"""Turn an optimisation design space and the points it suggests into factors and arms.

**The gap this closes is retyping, not judgment.** A BoFire suggestion hands back
`Candidate.params` — a bare `dict[str, float | str]` keyed by parameter name, with no units, no
level labels and no roles — and a screening design hands back `ScreeningDesign.runs`, the same shape
in bulk. `draft_experiment_protocol` needs `Factor`s whose levels carry labels and structures, and
`ProtocolArm`s mapping factor name to level *label*. Nothing connected the two, so the model read
the candidate table and typed the arms out by hand, one level label at a time, and a transposed
value there is a different experiment run at a different condition with nobody able to see it.

Everything here is mechanical: which parameters vary, what their distinct settings are, which runs
repeat. **What is deliberately not here is the protocol** — the charge table, the steps, the
analytics, the hazards. Those are judgment over the chemistry and stay the model's, which is why
this returns the two collections `draft_experiment_protocol` takes as arguments rather than an
`ExperimentDesign` it could not honestly fill.

**Why this lives in `protocols/` and not beside the optimiser.** `tests/test_layering.py` allows
`chemclaw.protocols -> chemclaw.science` and allows neither `chemclaw.science -> chemclaw.protocols`
nor `chemclaw.connectors -> chemclaw.protocols`, so the translation can only sit on this side. That
is the right side on the argument the layering comment already makes: it says `protocols` imports
neither `ingest` nor `kg` because "a design is prescriptive and their shapes are descriptive". An
`OptimizationProblem` is a design space — prescriptive, like everything else here — so reading one
is the rule applying rather than an exception to it.

**Four things it refuses rather than papers over**, because each would otherwise reach a chemist as
a plausible-looking plate:

- Two parameters whose names slug to one `Factor.name`. Silently merging them puts two variables in
  one column and the arms stop describing the runs.
- A parameter over more than `Factor.levels`' 96 settings. A continuous parameter sampled freely can
  do this on a large campaign, and the model would otherwise meet a validation error with no idea
  which parameter caused it.
- A parameter the runs never vary. That is a *setpoint*, not a factor — `Factor.levels` requires
  two — so it is reported in `constants` for the model to put in `ProtocolBody.setpoints` or the
  prose, never dropped.
- A run naming a parameter the problem does not declare, or omitting one it does. Either means the
  runs and the problem are not from the same campaign, and `factor_levels_declared` is a *blocker*
  check that would refuse the resulting design anyway — later, and with a worse message.

**Units are the one thing this cannot supply and the one a reader will assume.** An
`OptimizationProblem` carries none: a `ContinuousParameter` is a name and two bounds, and whether 80
means °C or mol% lives only in whoever wrote the campaign. So `Factor.unit` and `FactorLevel.unit`
come back empty and `notes` says so per parameter. `quantities_are_plausible` bands temperature and
time but reads the *setpoints*, not a factor's levels, so nothing downstream catches a missing unit
either — the chemist or the model supplies it before the design is drafted.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from chemclaw.core.errors import ChemclawError
from chemclaw.protocols.models import Factor, FactorLevel, ProtocolArm
from chemclaw.science.bo.problem import (
    CategoricalParameter,
    ContinuousParameter,
    OptimizationProblem,
    Parameter,
    ParamValue,
)

#: What a `Factor.name` may be, restated here only to explain the slugging below — the model's own
#: pattern is the authority and `Factor` raises if this ever disagrees with it.
_SLUG_ILLEGAL = re.compile(r"[^a-z0-9]+")


class BoTranslationError(ChemclawError):
    """The runs and the problem cannot be turned into a design without guessing."""


@dataclass(frozen=True)
class BoTranslation:
    """What a campaign's points become, and what the model still has to decide.

    A dataclass rather than a `protocols.models` entry because nothing persists it: it is the return
    of one function, consumed in the same turn by the tool that renders it. Adding it to the model
    module would put it in a document schema that stores it nowhere.
    """

    #: Ready to pass to `draft_experiment_protocol(factors=...)`.
    factors: list[Factor]
    #: Ready to pass to `draft_experiment_protocol(arms=...)`, one per run, in the order given.
    arms: list[ProtocolArm]
    #: `{parameter name: the single value every run uses}` — a setpoint the design holds fixed, not
    #: a factor. The model puts these in `ProtocolBody.setpoints` or says them in the prose.
    constants: dict[str, str] = field(default_factory=dict)
    #: What a reader has to supply or check, one sentence each. Never empty of the units warning
    #: when a continuous parameter varies, because a number without one is the failure this
    #: translation cannot prevent.
    notes: list[str] = field(default_factory=list)


def factors_and_arms(
    problem: OptimizationProblem,
    runs: Sequence[Mapping[str, ParamValue]],
    *,
    prefix: str = "arm",
) -> BoTranslation:
    """Translate an optimisation problem and the points it suggested into factors and arms.

    Args:
        problem: The campaign's design space — the parameters are what may vary and, for a
            categorical one, `structures` is where a level's SMILES comes from.
        runs: The points to run, each `{parameter name: value}`. A BO suggestion's
            `Candidate.params`, or a `ScreeningDesign.runs` entry. Order is preserved: arm *n* is
            run *n*, so a randomised screening design keeps the order its randomisation chose.
        prefix: The arm-id stem. `arm` gives `arm1`, `arm2`; a second block on one design uses
            another so the ids do not collide.

    Returns:
        The factors, the arms, the parameters that turned out to be constants, and what the model
        still has to supply.

    Raises:
        BoTranslationError: The runs and the problem disagree about which parameters exist, two
            parameter names slug to one factor name, or a parameter varies over more settings than
            a `Factor` may declare. Each names the parameter, because the alternative is a pydantic
            error against a field the model never wrote.
    """
    if not runs:
        raise BoTranslationError(
            "no runs to translate: a campaign that has suggested nothing yet has no arms, and an "
            "empty design would pass `is_a_protocol` only to fail `factor_levels_declared`"
        )
    declared = {parameter.name: parameter for parameter in problem.parameters}
    _require_runs_match_the_problem(declared, runs)

    varying, constants = _split_by_variation(declared, runs)
    slugs = _slugs_for(varying)

    factors = [_factor_for(declared[name], slugs[name], runs) for name in varying]
    arms = _arms_for(runs, varying, slugs, prefix=prefix)
    return BoTranslation(
        factors=factors,
        arms=arms,
        constants=constants,
        notes=_notes_for(declared, varying, constants, slugs),
    )


def _require_runs_match_the_problem(
    declared: Mapping[str, Parameter], runs: Sequence[Mapping[str, ParamValue]]
) -> None:
    """Refuse runs that name a parameter the problem does not, or omit one it declares.

    Both directions, because they are different mistakes with the same cause — runs from one
    campaign against another's problem — and only one of them would be caught downstream. An extra
    key is silently ignored by every dict read below; a missing one produces an arm that does not
    set a declared factor, which `checks.factor_levels_declared` refuses as a **blocker**, later and
    against a design the model has already written a protocol body for.
    """
    for index, run in enumerate(runs, start=1):
        extra = sorted(set(run) - set(declared))
        missing = sorted(set(declared) - set(run))
        if extra or missing:
            raise BoTranslationError(
                f"run {index} does not match the problem's parameters"
                + (f"; it names {extra}, which the problem does not declare" if extra else "")
                + (f"; it omits {missing}, which the problem declares" if missing else "")
                + ". The runs and the problem are from different campaigns"
            )


def _split_by_variation(
    declared: Mapping[str, Parameter], runs: Sequence[Mapping[str, ParamValue]]
) -> tuple[list[str], dict[str, str]]:
    """Separate the parameters the runs actually vary from the ones they hold fixed.

    A parameter at one setting across every run is a **setpoint**, and calling it a factor is not
    merely redundant: `Factor.levels` has `min_length=2`, so it cannot be expressed, and
    `coverage_is_stated` would compare a grid against arms that never explore it.

    Order follows `problem.parameters` rather than the runs, so two translations of one campaign
    put the factors in the same order and the run sheet's columns do not move between revisions.
    """
    varying: list[str] = []
    constants: dict[str, str] = {}
    for name in declared:
        settings = {_label(run[name]) for run in runs}
        if len(settings) > 1:
            varying.append(name)
        else:
            constants[name] = settings.pop()
    return varying, constants


def _slugs_for(names: Sequence[str]) -> dict[str, str]:
    """Map each varying parameter name onto a legal, distinct `Factor.name`.

    A parameter is named by whoever wrote the campaign — "Pd source", "T (degC)", "equiv. base" —
    and `Factor.name` is `^[a-z][a-z0-9_]*$`. The slug is lowercase with every other run of
    characters collapsed to one underscore, prefixed when it would otherwise start with a digit.

    **A collision raises.** Two parameters slugging to one name would silently become one factor
    column holding two variables, and every arm after the first would overwrite the other's level —
    so the design would be internally consistent, pass every check, and describe experiments nobody
    asked for. Nothing downstream could detect it, because by then there is only one factor.
    """
    slugs: dict[str, str] = {}
    taken: dict[str, str] = {}
    for name in names:
        slug = _SLUG_ILLEGAL.sub("_", name.lower()).strip("_")
        if not slug:
            raise BoTranslationError(
                f"parameter {name!r} has no letters or digits in its name, so there is no legal "
                "factor name to derive from it; rename it in the campaign"
            )
        if slug[0].isdigit():
            slug = f"f_{slug}"
        if slug in taken:
            raise BoTranslationError(
                f"parameters {taken[slug]!r} and {name!r} both become the factor name {slug!r}. "
                "One factor cannot hold two variables — every arm would overwrite one of them and "
                "the design would look consistent while describing experiments nobody planned. "
                "Rename one in the campaign"
            )
        taken[slug] = name
        slugs[name] = slug
    return slugs


def _factor_for(
    parameter: Parameter, slug: str, runs: Sequence[Mapping[str, ParamValue]]
) -> Factor:
    """One `Factor`, with a level for each distinct setting the runs actually use.

    **Levels come from the runs, not from the parameter's declared range**, and that is the choice
    that makes the design honest. A `CategoricalParameter` may declare eight catalysts where the
    suggestion picks three; declaring all eight would state a grid the arms do not explore, which is
    exactly what `coverage_is_stated` reports on. What the design varies is what the runs vary.

    `role` is `UNKNOWN` throughout: an optimisation parameter says what may change, never what the
    species *does*, and `SpeciesRole.UNKNOWN` is a member precisely so "nothing has decided" stays
    distinguishable from a decision. The model sets it when it drafts the body.
    """
    settings = _distinct_in_order(parameter.name, runs)
    if len(settings) > 96:
        raise BoTranslationError(
            f"parameter {parameter.name!r} takes {len(settings)} distinct values across these "
            "runs, and a factor may declare at most 96 levels — a screen varying one factor that "
            "widely is not one anybody runs on a plate. Reduce the suggestion count, or bin the "
            "values before translating"
        )
    structures = parameter.structures or {} if isinstance(parameter, CategoricalParameter) else {}
    continuous = isinstance(parameter, ContinuousParameter)
    return Factor(
        name=slug,
        kind="continuous" if continuous else "categorical",
        levels=[
            FactorLevel(
                label=_label(value),
                smiles=structures.get(str(value), ""),
                value=float(value) if continuous else None,
            )
            for value in settings
        ],
    )


def _arms_for(
    runs: Sequence[Mapping[str, ParamValue]],
    varying: Sequence[str],
    slugs: Mapping[str, str],
    *,
    prefix: str,
) -> list[ProtocolArm]:
    """One arm per run, with a repeated run pointing at the first that set those levels.

    **Repeats are `replicate_of` rather than duplicate arms**, because they are what a screening
    design's centre points and replicates *are*, and `arms_are_distinct` reports two arms at
    identical settings as a warning unless one declares itself a replicate. Doing it here is the
    whole reason: the model reading a run table cannot see that run 7 repeats run 2 without
    comparing every column by eye, which is the error this module exists to remove.

    `ProtocolArm` requires a replicate to carry *identical* levels and identical effective
    setpoints. The first holds by construction — the runs are equal — and the second because no arm
    here sets `setpoints`, so every one inherits the same body.
    """
    arms: list[ProtocolArm] = []
    first_seen: dict[tuple[tuple[str, str], ...], str] = {}
    for index, run in enumerate(runs, start=1):
        levels = {slugs[name]: _label(run[name]) for name in varying}
        key = tuple(sorted(levels.items()))
        arm_id = f"{prefix}{index}"
        arms.append(ProtocolArm(arm_id=arm_id, levels=levels, replicate_of=first_seen.get(key, "")))
        first_seen.setdefault(key, arm_id)
    return arms


def _notes_for(
    declared: Mapping[str, Parameter],
    varying: Sequence[str],
    constants: Mapping[str, str],
    slugs: Mapping[str, str],
) -> list[str]:
    """What the model still has to supply, one sentence each and none of them optional.

    Written as notes rather than raised, because none of them makes the translation wrong — they
    make it incomplete in ways only a chemist can finish, and refusing would leave the model with
    no arms at all rather than with arms and a list of what to add.
    """
    notes: list[str] = []
    numeric = [name for name in varying if isinstance(declared[name], ContinuousParameter)]
    if numeric:
        notes.append(
            "An optimisation problem carries no units, so these factors came back with none: "
            + ", ".join(f"{slugs[name]} (from {name!r})" for name in numeric)
            + ". Set `unit` on each before drafting — nothing downstream checks a factor's units, "
            "so a number without one reaches a chemist unqualified."
        )
    if constants:
        notes.append(
            "These parameters are the same in every run, so they are setpoints rather than "
            "factors: "
            + ", ".join(f"{name}={value}" for name, value in constants.items())
            + ". Put them in the protocol body's setpoints or say them in its prose; they are not "
            "in the arms."
        )
    notes.append(
        "Every factor's `role` is `unknown` and every level's `smiles` is empty unless the "
        "campaign declared a structure for it. Set both where the level is a species, so the "
        "hazard screen and the precedent questions can see it."
    )
    return notes


def _distinct_in_order(name: str, runs: Sequence[Mapping[str, ParamValue]]) -> list[ParamValue]:
    """The settings one parameter takes, deduplicated, in the order the runs first use them.

    Order matters and sorting would be worse: a run sheet's level order is what a chemist reads
    down, and the suggestion's own order carries the optimiser's preference — the first candidate is
    the one it most wants run.
    """
    seen: dict[str, ParamValue] = {}
    for run in runs:
        seen.setdefault(_label(run[name]), run[name])
    return list(seen.values())


def _label(value: ParamValue) -> str:
    """One setting as a `FactorLevel.label` and a `ProtocolArm.levels` entry — the same string.

    The two must be produced by one function: `checks.factor_levels_declared` is a **blocker** that
    matches an arm's level against the factor's declared labels by string equality, so a float
    formatted one way in the factor and another in the arm fails a design that is in fact correct.

    `%.10g` is the format `protocols/export._cell` writes a float to the run sheet in, so a level
    reads the same in the factor table, the arm and the CSV a chemist opens.
    """
    return f"{value:.10g}" if isinstance(value, float) else str(value)
