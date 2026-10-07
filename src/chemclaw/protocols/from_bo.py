"""Turn an optimisation design space and the points it suggests into factors and arms.

A BoFire suggestion or screening design is a bare `{parameter: value}` mapping; this does the
mechanical translation into labelled `Factor`s and `ProtocolArm`s so the model never retypes
levels by hand. The protocol itself (charges, steps, analytics, hazards) is judgment and stays
the model's, so this returns the two collections `draft_experiment_protocol` takes rather than
an `ExperimentDesign`. It lives in `protocols/` because the layering only allows
`protocols -> science`.

It refuses rather than papers over: two parameters slugging to one factor name; a parameter with
more settings than `Factor.levels` allows; runs and problem disagreeing about which parameters
exist. A parameter the runs never vary is a setpoint, reported in `constants`, never dropped.

Units cannot be supplied: an `OptimizationProblem` carries none, so units come back empty and
`notes` says so per parameter.
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

    A dataclass rather than a `protocols.models` entry because nothing persists it.
    """

    #: Ready to pass to `draft_experiment_protocol(factors=...)`.
    factors: list[Factor]
    #: Ready to pass to `draft_experiment_protocol(arms=...)`, one per run, in the order given.
    arms: list[ProtocolArm]
    #: `{parameter name: the single value every run uses}` — a setpoint the design holds fixed, not
    #: a factor. The model puts these in `ProtocolBody.setpoints` or says them in the prose.
    constants: dict[str, str] = field(default_factory=dict)
    #: What a reader has to supply or check, one sentence each; always includes the units warning
    #: when a continuous parameter varies.
    notes: list[str] = field(default_factory=list)


def factors_and_arms(
    problem: OptimizationProblem,
    runs: Sequence[Mapping[str, ParamValue]],
    *,
    prefix: str = "arm",
) -> BoTranslation:
    """Translate an optimisation problem and the points it suggested into factors and arms.

    Args:
        problem: The campaign's design space; a categorical parameter's `structures` supply a
            level's SMILES.
        runs: The points to run, each `{parameter name: value}`. Order is preserved: arm *n* is run
            *n*.
        prefix: The arm-id stem (`arm` gives `arm1`, `arm2`); a second block uses another.

    Returns:
        The factors, the arms, the parameters that turned out to be constants, and what the model
        still has to supply.

    Raises:
        BoTranslationError: The runs and the problem disagree about which parameters exist, two
            parameter names slug to one factor name, or a parameter varies over more settings than
            a `Factor` may declare. Each names the parameter.
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

    Both directions mean runs from one campaign against another's problem. An extra key would be
    silently ignored; a missing one would only be caught later by a blocker check.
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

    A parameter at one setting is a setpoint (`Factor.levels` requires two). Order follows
    `problem.parameters`, so run-sheet columns are stable across revisions.
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

    The slug is lowercase with every other character run collapsed to `_`, prefixed if it would
    start with a digit. A collision raises: two parameters in one factor column would yield a design
    that passes every check and describes experiments nobody asked for.
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

    Levels come from the runs, not the declared range, so the design never states a grid the arms
    do not explore. `role` is `UNKNOWN`: a parameter says what may change, not what the species
    does; the model sets it when drafting.
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

    Repeats become `replicate_of` (what centre points and replicates are), which
    `arms_are_distinct` accepts. Replicates have identical levels by construction and identical
    setpoints because no arm here sets `setpoints`.
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

    Notes rather than errors: they make the translation incomplete, not wrong.
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

    Not sorted: the suggestion's order carries the optimiser's preference.
    """
    seen: dict[str, ParamValue] = {}
    for run in runs:
        seen.setdefault(_label(run[name]), run[name])
    return list(seen.values())


def _label(value: ParamValue) -> str:
    """One setting as a `FactorLevel.label` and a `ProtocolArm.levels` entry: the same string.

    One function for both because `checks.factor_levels_declared` matches them by string
    equality. `%.10g` matches `protocols/export.csv_cell`, so a level reads the same everywhere.
    """
    return f"{value:.10g}" if isinstance(value, float) else str(value)
