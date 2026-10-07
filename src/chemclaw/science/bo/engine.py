"""BoFire adapter — the only module that touches BoFire (D-012).

Maps the neutral `OptimizationProblem`/`Observation` types to BoFire domains and strategies and maps
results back to `Candidate`, so the engine could be swapped without touching campaigns, agents or
skills. `factorial_design` and `optimal_design` use BoFire's classical design strategies through the
same boundary.

No BoFire, botorch or gpytorch exception leaks past it: validator errors become `ValueError`, and
numerical fit/acquisition failures become `SurrogateFitError` (`_translating_surrogate_errors`).
"""

import itertools
import operator
import random
import string
from collections.abc import Iterator
from contextlib import contextmanager
from functools import reduce
from typing import Any

import numpy.linalg
import pandas as pd
import torch
from bofire.data_models.constraints.api import (
    CategoricalExcludeConstraint,
    LinearEqualityConstraint,
    LinearInequalityConstraint,
    SelectionCondition,
)
from bofire.data_models.domain.api import Constraints, Domain, Inputs, Outputs
from bofire.data_models.enum import RegressionMetricsEnum
from bofire.data_models.features.api import (
    CategoricalDescriptorInput,
    CategoricalInput,
    ContinuousInput,
    ContinuousOutput,
)
from bofire.data_models.objectives.api import MaximizeObjective, MinimizeObjective
from bofire.data_models.strategies.api import (
    DoEStrategy as DoESpec,
)
from bofire.data_models.strategies.api import (
    FractionalFactorialStrategy,
    MoboStrategy,
    RandomStrategy,
    SoboStrategy,
)
from bofire.data_models.strategies.doe import (
    AOptimalityCriterion,
    DOptimalityCriterion,
    IOptimalityCriterion,
    SpaceFillingCriterion,
)
from bofire.strategies import api as strategies
from bofire.strategies.doe.utils import get_formula_from_string
from bofire.surrogates import api as surrogate_api
from bofire.utils.doe import get_generator
from botorch.exceptions.errors import BotorchError, InfeasibilityError, ModelFittingError
from linear_operator.utils.errors import NanError, NotPSDError

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.science.bo.problem import (
    DESIGN_CRITERIA,
    DESIGN_FORMULAE,
    MIN_SEED_OBSERVATIONS,
    Candidate,
    CategoricalParameter,
    Constraint,
    ContinuousParameter,
    ExcludeConstraint,
    FitQuality,
    LinearConstraint,
    Observation,
    OptimalDesign,
    OptimizationProblem,
    ParamValue,
    Prediction,
    ScreeningDesign,
    discrete_candidate_count,
    discrete_space_size,
    distinct_feasible_candidate_count,
    observed_value,
    params_key,
    point_in_domain,
)

# Rejection-sampling rounds `initial_candidates` spends per requested point before giving up.
# A feasible domain needs one; the allowance covers exclusions that make feasible points rare.
# It bounds the loop on the inline path, which has no Temporal budget above it.
_SEED_DRAW_ROUNDS = 8


class SurrogateFitError(ChemclawError):
    """BoFire's Bayesian strategy could not fit or query its surrogate.

    Raised in place of the `botorch`/`gpytorch`/`linear_operator` exception the fit or acquisition
    step threw. A `ChemclawError`, so the in-process seam surfaces it to the model, and its name is
    in `chemclaw.durable.publish._BAD_DATA_TYPES` (matched as a string, so do not rename) because
    the same input fails the same way on retry. It also covers constraints that admit no point on
    the seeding path, where there is no surrogate; `_translating_surrogate_errors` words the two
    causes differently.
    """


# Library exceptions a GP fit or acquisition step raises on degenerate input (near-singular
# kernel, non-PD covariance after jitter, NaNs). `ModelFittingError` is not a `BotorchError`,
# so it is listed separately.
_SURROGATE_FAILURES: tuple[type[Exception], ...] = (
    BotorchError,
    ModelFittingError,
    NotPSDError,
    NanError,
    numpy.linalg.LinAlgError,
    torch.linalg.LinAlgError,  # type: ignore[attr-defined] # torch's stubs omit this re-export
)


@contextmanager
def _translating_surrogate_errors(problem: OptimizationProblem, context: str) -> Iterator[None]:
    """Turn a known BoFire/botorch numerical failure into `SurrogateFitError`.

    `context` names the step in the caller's words. An empty polytope (botorch's
    `InfeasibilityError`, matched by type) is checked first: it arises on the seeding path with no
    surrogate and no observations, so the message names the constraints via `describe()` rather than
    advising to vary measured values.
    """
    try:
        yield
    except InfeasibilityError as error:
        stated = "; ".join(constraint.describe() for constraint in problem.constraints)
        raise SurrogateFitError(
            f"no point satisfies this problem's constraints, so there is nothing to propose while "
            f"{context}: {error} The constraints are {stated or '(none declared)'}, over the "
            "parameter bounds as declared. This is a contradiction between the limits themselves, "
            "not a problem with the runs — relax or remove one of them; supplying different "
            "measurements cannot help."
        ) from error
    except _SURROGATE_FAILURES as error:
        raise SurrogateFitError(
            f"the Bayesian surrogate failed while {context}: {error}. This is usually duplicate "
            "or near-duplicate observations collapsing the model's kernel, or an objective with "
            "no spread across the points seen so far — vary the inputs, or the measured values, "
            "before retrying; the same data will fail the same way again."
        ) from error


def _resolve_seed(seed: int | None) -> int:
    """Per-call seed, falling back to the config default for reproducible runs."""
    return settings.bo_seed if seed is None else seed


def _categorical_input(
    parameter: CategoricalParameter,
) -> CategoricalInput | CategoricalDescriptorInput:
    """Map a categorical to BoFire, using its descriptors when it has been featurized.

    A `CategoricalInput` is ordinally encoded by a BoTorch surrogate (each label learned
    independently); a `CategoricalDescriptorInput` places the molecule in descriptor space so the
    model can generalize. `tests/test_bo_featurize.py` pins that BoFire default.
    """
    if parameter.descriptors is None:
        return CategoricalInput(key=parameter.name, categories=parameter.categories)
    names = parameter.descriptor_names()
    return CategoricalDescriptorInput(
        key=parameter.name,
        categories=parameter.categories,
        descriptors=names,
        # Row order must follow `categories`, column order `names` — BoFire matches by
        # position, not by label, so a mismatch here would silently mislabel the chemistry.
        values=[
            [parameter.descriptors[category][name] for name in names]
            for category in parameter.categories
        ],
    )


def _objective_output(problem: OptimizationProblem) -> ContinuousOutput:
    """The problem's **lead** objective as a BoFire output.

    For the classical design paths, where BoFire requires an output but its direction is never read.
    """
    return _outputs(problem)[0]


def _outputs(problem: OptimizationProblem) -> list[ContinuousOutput]:
    """Every objective as a BoFire output, in declaration order (W3)."""
    return [
        ContinuousOutput(
            key=objective.name,
            objective=(
                MinimizeObjective(w=1.0)
                if objective.direction == "minimize"
                else MaximizeObjective(w=1.0)
            ),
        )
        for objective in problem.objectives
    ]


def _exclusion(constraint: ExcludeConstraint) -> CategoricalExcludeConstraint:
    """Map a forbidden pairing of categorical options.

    BoFire's conditions are positional and combined by `logical_op`, so an `AND` of two selections
    excludes exactly the cross product of the two option lists.
    """
    return CategoricalExcludeConstraint(
        features=list(constraint.parameters),
        conditions=[SelectionCondition(selection=list(options)) for options in constraint.options],
        logical_op="AND",
    )


def _constraint(
    constraint: Constraint,
) -> LinearEqualityConstraint | LinearInequalityConstraint | CategoricalExcludeConstraint:
    """Map one neutral constraint, normalizing `>=` by negation.

    BoFire's inequality is `coefficients · x <= rhs`; a test asserts that sense, since getting it
    backwards silently inverts a stated limit. `>=` is the same inequality with every sign flipped:
    `a + b >= 3` is `-a - b <= -3`.
    """
    if isinstance(constraint, ExcludeConstraint):
        return _exclusion(constraint)
    if constraint.relation == "==":
        return LinearEqualityConstraint(
            features=list(constraint.parameters),
            coefficients=list(constraint.coefficients),
            rhs=constraint.rhs,
        )
    sign = -1.0 if constraint.relation == ">=" else 1.0
    return LinearInequalityConstraint(
        features=list(constraint.parameters),
        coefficients=[sign * coefficient for coefficient in constraint.coefficients],
        rhs=sign * constraint.rhs,
    )


def _to_domain(problem: OptimizationProblem) -> Domain:
    """Translate our problem into a BoFire `Domain` (inputs, one output per objective, limits)."""
    inputs = []
    for parameter in problem.parameters:
        if isinstance(parameter, ContinuousParameter):
            inputs.append(
                ContinuousInput(key=parameter.name, bounds=(parameter.lower, parameter.upper))
            )
        else:
            inputs.append(_categorical_input(parameter))
    return Domain(
        inputs=Inputs(features=inputs),
        outputs=Outputs(features=_outputs(problem)),
        # Honoured by both strategies that see this domain (`RandomStrategy` seeding and SOBO
        # proposals), so no rejection-sampling path is needed for linear constraints.
        constraints=Constraints(
            constraints=[_constraint(constraint) for constraint in problem.constraints]
        ),
    )


def _cast(parameter: ContinuousParameter | CategoricalParameter, raw: Any) -> ParamValue:
    """Coerce a dataframe cell to the parameter's value type (float or category str)."""
    return float(raw) if isinstance(parameter, ContinuousParameter) else str(raw)


def _observations_to_frame(
    problem: OptimizationProblem, observations: list[Observation]
) -> pd.DataFrame:
    """Build the experiments dataframe BoFire's `tell` expects, one column pair per objective."""
    rows = []
    for obs in observations:
        row: dict[str, object] = dict(obs.params)
        for objective in problem.objectives:
            row[objective.name] = observed_value(problem, obs, objective.name)
            row[f"valid_{objective.name}"] = 1
        rows.append(row)
    return pd.DataFrame(rows)


def _frame_to_candidates(problem: OptimizationProblem, frame: pd.DataFrame) -> list[Candidate]:
    """Extract an ask() result into our `Candidate` type, the surrogate's belief included.

    A model-backed proposal carries `<objective>_pred` and `<objective>_sd` columns (per objective
    for MOBO); a `RandomStrategy` returns parameters only, so they are read conditionally. The
    scalars hold the lead objective. `_des` (the acquisition score) is dropped, since it is a
    ranking quantity that would be misread as confidence.
    """
    predicted_values, predicted_sds = {}, {}
    for objective in problem.objectives:
        name = objective.name
        if f"{name}_pred" in frame.columns:
            predicted_values[name] = f"{name}_pred"
        if f"{name}_sd" in frame.columns:
            predicted_sds[name] = f"{name}_sd"
    lead = problem.objective.name
    multi = len(problem.objectives) > 1
    return [
        Candidate(
            params={p.name: _cast(p, row[p.name]) for p in problem.parameters},
            predicted_value=(
                float(row[predicted_values[lead]]) if lead in predicted_values else None
            ),
            # abs(): a posterior sd is non-negative by definition, and the field enforces it, but
            # a float round-trip through the surrogate can land a hair below zero.
            predicted_sd=abs(float(row[predicted_sds[lead]])) if lead in predicted_sds else None,
            # Empty on a single-objective problem: the scalars above are the whole answer there, and
            # a duplicate of them would be a second place for the same number to drift.
            predicted_values=(
                {name: float(row[column]) for name, column in predicted_values.items()}
                if multi
                else {}
            ),
            predicted_sds=(
                {name: abs(float(row[column])) for name, column in predicted_sds.items()}
                if multi
                else {}
            ),
        )
        for _, row in frame.iterrows()
    ]


def initial_candidates(
    problem: OptimizationProblem, n: int, seed: int | None = None
) -> list[Candidate]:
    """Propose `n` space-filling starting points (random design, no model yet).

    Seeds a campaign before observations exist. In a finite (all-categorical) space the points are
    distinct, and `n` beyond the space size is rejected because that many distinct points cannot
    exist.
    """
    _require_batch_fits_the_ceiling(n)
    strategy = strategies.map(RandomStrategy(domain=_to_domain(problem), seed=_resolve_seed(seed)))
    # `discrete_space_size` is `None` only for an infinite space; `discrete_candidate_count` is
    # also `None` for a finite space too large to enumerate. Branch on the first, or a large finite
    # space loses both guarantees above.
    size = discrete_space_size(problem)
    feasible = discrete_candidate_count(problem)
    with _translating_surrogate_errors(problem, "sampling initial candidates"):
        if size is None:
            return _frame_to_candidates(problem, strategy.ask(n))
        # Refuse against the feasible count when known; otherwise the raw size, with the rejection
        # loop's own bound stopping an impossible ask.
        space = feasible if feasible is not None else size
        if n > space:
            raise ValueError(
                f"cannot seed {n} distinct points: the discrete space has only {space}"
            )
        # Re-ask until `n` distinct points are collected; each ask advances the strategy's RNG.
        # Bounded by `_SEED_DRAW_ROUNDS`, because exclusions can make feasible points rare and an
        # `ask` returning nothing would otherwise spin forever.
        candidates: list[Candidate] = []
        seen: set[tuple[tuple[str, ParamValue], ...]] = set()
        for _attempt in range(max(_SEED_DRAW_ROUNDS, _SEED_DRAW_ROUNDS * n)):
            if len(candidates) >= n:
                return candidates
            for candidate in _frame_to_candidates(problem, strategy.ask(n - len(candidates))):
                key = params_key(candidate.params)
                if key not in seen:
                    seen.add(key)
                    candidates.append(candidate)
        if len(candidates) < n:
            raise ValueError(
                f"cannot seed {n} distinct points: the sampler returned only {len(candidates)} "
                f"distinct of the {space} the discrete space enumerates, after "
                f"{max(_SEED_DRAW_ROUNDS, _SEED_DRAW_ROUNDS * n)} rounds — the domain's exclusion "
                "constraints may leave too few feasible points. Seed fewer points, or relax them."
            )
        return candidates


def propose_candidates(
    problem: OptimizationProblem,
    observations: list[Observation],
    n: int = 1,
    seed: int | None = None,
) -> list[Candidate]:
    """Propose the next `n` candidates from past observations — SOBO, or MOBO for a trade-off.

    Requires at least `MIN_SEED_OBSERVATIONS` observations (BoFire's floor) and raises `ValueError`
    below it; call `initial_candidates` first. One objective gets `SoboStrategy`; several get
    `MoboStrategy` (`qLogNEHVI`, with a moving reference point derived from the data).
    """
    if len(observations) < MIN_SEED_OBSERVATIONS:
        raise ValueError(
            f"propose_candidates needs at least {MIN_SEED_OBSERVATIONS} observations; seed first"
        )
    _require_batch_fits_the_ceiling(n)
    _require_fresh_points_exist(problem, observations)
    strategy, _ = _fitted_strategy(problem, observations, seed)
    with _translating_surrogate_errors(problem, f"asking for {n} candidate(s)"):
        candidates = strategy.ask(n)
    # Fewer than `n` is allowed: a discrete space with two fresh cells answers a request for three
    # with two. Only zero is a failure, refused above.
    return _frame_to_candidates(problem, candidates)


def _require_fresh_points_exist(
    problem: OptimizationProblem, observations: list[Observation]
) -> None:
    """Refuse an ask a finite space cannot answer, before BoFire fails on it obscurely.

    When every feasible cell has been run, BoFire's discrete acquisition hands an empty frame on and
    raises a bare `KeyError`, which reaches the model as an opaque internal error. `KeyError` is
    deliberately not in `_SURROGATE_FAILURES`: that would misdiagnose code defects as bad data.
    """
    space = discrete_candidate_count(problem)
    if space is None:
        return
    # Counted under the same feasibility filter as `space`: an observation of an excluded point is
    # not one of the enumerated cells.
    run = distinct_feasible_candidate_count(problem, observations)
    if run < space:
        # Fresh cells remain. The threshold is zero fresh points, not `space_exhausted`'s "cannot
        # fill a batch", which is the durable loop's stop signal and would refuse an ask that can
        # answer partly.
        return
    raise ValueError(
        f"this decision space holds {space} distinct condition(s) and all {run} have been run, so "
        "there is no fresh point left to propose. The screen is complete — report the best runs "
        "rather than asking for another, or widen the space (add an option, relax a bound, or "
        "drop a constraint), which makes it a new campaign."
    )


def _fitted_strategy(
    problem: OptimizationProblem, observations: list[Observation], seed: int | None
) -> tuple[Any, pd.DataFrame]:
    """Build the strategy the problem calls for and fit it to the observations.

    Shared by propose, predict and cross-validate so all three describe the same surrogate (BoFire
    picks its class from the domain). The fitted frame is returned too, so cross-validation uses the
    same rows.
    """
    if len(observations) < MIN_SEED_OBSERVATIONS:
        raise ValueError(
            f"a surrogate needs at least {MIN_SEED_OBSERVATIONS} observations; seed first"
        )
    domain = _to_domain(problem)
    resolved = _resolve_seed(seed)
    specification = (
        MoboStrategy(domain=domain, seed=resolved)
        if len(problem.objectives) > 1
        else SoboStrategy(domain=domain, seed=resolved)
    )
    strategy = strategies.map(specification)
    frame = _observations_to_frame(problem, observations)
    context = f"fitting the surrogate to {len(observations)} observation(s)"
    with _translating_surrogate_errors(problem, context):
        strategy.tell(frame)
    return strategy, frame


def _predictions_from(
    problem: OptimizationProblem, strategy: Any, points: list[dict[str, ParamValue]]
) -> list[Prediction]:
    """Query an already-fitted strategy at the caller's points.

    Takes the fitted strategy so the prediction and its fit quality describe the same model.
    """
    frame = pd.DataFrame(
        [{p.name: point.get(p.name) for p in problem.parameters} for point in points]
    )
    with _translating_surrogate_errors(problem, f"predicting at {len(points)} point(s)"):
        predicted = strategy.predict(frame)
    return [
        Prediction(
            params={p.name: _cast(p, frame.iloc[index][p.name]) for p in problem.parameters},
            values={
                objective.name: float(predicted.iloc[index][f"{objective.name}_pred"])
                for objective in problem.objectives
            },
            sds={
                objective.name: float(predicted.iloc[index][f"{objective.name}_sd"])
                for objective in problem.objectives
            },
            in_domain=point_in_domain(problem, points[index]),
        )
        for index in range(len(points))
    ]


def _metric(results: Any, metric: RegressionMetricsEnum) -> float:
    """One number from a `CvResults`, pooled across folds rather than averaged over them.

    `get_metric`'s default `combine_folds=True` scores all held-out predictions together, so uneven
    small folds are not weighted equally.
    """
    return float(results.get_metric(metric).iloc[0])


def _is_flat(spread: float, column: pd.Series) -> bool:
    """Whether these observations carry too little range for "variance explained" to mean anything.

    Relative to the response's magnitude (`abs(mean)`), since R² is scale-free; a column centred on
    zero needs an exactly flat spread.
    """
    return spread <= abs(float(column.mean())) * settings.bo_flat_response_relative_spread


def _resolve_folds(folds: int | None, n_observations: int) -> int:
    """How many folds to cross-validate over: the caller's number, or one the data can carry.

    A defaulted count (`bo_cv_folds`) adapts to the run count so early campaigns still get a score;
    a count the caller stated is not adjusted, and more folds than runs raises. `FitQuality.folds`
    records what was used.
    """
    if folds is None:
        return max(2, min(settings.bo_cv_folds, n_observations))
    if folds < 2:
        raise ValueError(f"cross-validation needs at least 2 folds; got {folds}")
    if n_observations < folds:
        raise ValueError(
            f"cannot cross-validate {n_observations} observation(s) over {folds} folds: each fold "
            "would hold out less than one run. Supply more runs, or ask for fewer folds."
        )
    return folds


def _fit_quality_from(
    problem: OptimizationProblem, strategy: Any, frame: pd.DataFrame, folds: int, n: int
) -> list[FitQuality]:
    """Cross-validate the surrogates an already-fitted strategy chose, one score per objective."""
    # Matched on the surrogate's output key, not position: `strict=True` catches a count mismatch
    # but not an ordering one, and a mislabelled R² is worse than a missing one.
    by_output = {spec.outputs[0].key: spec for spec in strategy.surrogate_specs.surrogates}
    missing = sorted({o.name for o in problem.objectives} - set(by_output))
    if missing:
        raise SurrogateFitError(
            f"BoFire returned no surrogate for objective(s) {missing}; it fitted "
            f"{sorted(by_output)}. The fit cannot be attributed, so no score is reported."
        )
    scores = []
    with _translating_surrogate_errors(problem, f"cross-validating over {folds} folds"):
        for objective in problem.objectives:
            surrogate = surrogate_api.map(by_output[objective.name])
            _, test, _ = surrogate.cross_validate(frame, folds=folds)
            # R² has no denominator without variance (`get_metric` answers 1.0 for a flat response).
            # The spread is taken off the same frame the folds are cut from and reported beside the
            # score, since R² is scale-free.
            column = frame[objective.name]
            spread = float(column.max() - column.min())
            scores.append(
                FitQuality(
                    objective=objective.name,
                    # Relative, not `spread == 0.0`: a drift below the assay's own noise can still
                    # score a high R². See `bo_flat_response_relative_spread`.
                    r2=(
                        None
                        if _is_flat(spread, column)
                        else _metric(test, RegressionMetricsEnum.R2)
                    ),
                    mae=_metric(test, RegressionMetricsEnum.MAE),
                    folds=folds,
                    n_observations=n,
                    response_range=spread,
                )
            )
    return scores


def interrogate_surrogate(
    problem: OptimizationProblem,
    observations: list[Observation],
    points: list[dict[str, ParamValue]],
    folds: int | None = None,
    assess_fit: bool = True,
    seed: int | None = None,
) -> tuple[list[Prediction], list[FitQuality]]:
    """Ask one fitted surrogate both questions: what it expects here, and how well it predicts.

    One fit, so the score quoted beside a prediction describes the model that made it.

    Raises:
        ValueError: Below the observation floor, when the caller named more folds than runs, or when
            neither a point nor a fit assessment was asked for.
    """
    if not points and not assess_fit:
        raise ValueError("interrogate_surrogate was asked for neither a prediction nor a fit score")
    resolved_folds = _resolve_folds(folds, len(observations)) if assess_fit else 0
    strategy, frame = _fitted_strategy(problem, observations, seed)
    predictions = _predictions_from(problem, strategy, points) if points else []
    quality = (
        _fit_quality_from(problem, strategy, frame, resolved_folds, len(observations))
        if assess_fit
        else []
    )
    return predictions, quality


def _resolution(generator: str) -> int:
    """The resolution of a two-level design with this generator: its shortest defining word.

    A generator names one word per factor (a letter for a base factor, a product like `abc` for an
    aliased one), so each derived factor contributes the defining word `abc·d`; the shortest product
    in that group is the resolution, which tells whether a main effect could be a two-factor
    interaction. BoFire exposes this only as a formatted alias listing.
    """
    words = generator.split()
    # A factor whose word is a single letter is a base factor and aliases nothing.
    defining = [
        frozenset(word) ^ {letter}
        for letter, word in zip(string.ascii_lowercase, words, strict=False)
        if len(word) > 1
    ]
    return min(
        len(reduce(operator.xor, combination))
        for size in range(1, len(defining) + 1)
        for combination in itertools.combinations(defining, size)
    )


def _two_level_names(problem: OptimizationProblem) -> list[str]:
    """The continuous factors a screen holds at their two bounds, in declaration order."""
    return [p.name for p in problem.parameters if isinstance(p, ContinuousParameter)]


def _require_knobs_are_honoured(
    problem: OptimizationProblem, n_center: int, n_repetitions: int, reduced: bool
) -> None:
    """Refuse the two knobs BoFire silently ignores rather than passing them into a no-op.

    On an all-categorical domain `n_center` and `n_repetitions` do not change the design. A centre
    point also means nothing for a categorical re-encoded onto [0, 1]: 0.5 decodes to neither level.
    """
    continuous = _two_level_names(problem)
    if n_center and not continuous:
        raise ValueError(
            "n_center needs at least one continuous factor: a centre point is the midpoint of a "
            "range, and this problem declares only categorical factors, which have no midpoint. "
            "BoFire ignores the argument on such a design rather than erroring, so it is refused "
            "here instead of silently doing nothing."
        )
    if n_center and reduced and len(continuous) != len(problem.parameters):
        raise ValueError(
            "a reduced design encodes each categorical factor onto two numeric levels, so a centre "
            "run would place it halfway between them, which is not one of its categories. Ask for "
            "centre points on the full grid (n_generators=0), or drop the categorical factors."
        )
    if n_repetitions > 1 and not continuous:
        raise ValueError(
            "n_repetitions needs at least one continuous factor: BoFire replicates the continuous "
            "half of a design and crosses the categorical half in full, so on an all-categorical "
            "problem it is ignored rather than honoured. Repeat the returned runs yourself if you "
            "want replicates of a categorical screen."
        )


def _fractional_design(
    problem: OptimizationProblem, n_generators: int, n_center: int, n_repetitions: int
) -> ScreeningDesign:
    """A reduced two-level screen: `2**-n_generators` of the grid, with its resolution stated.

    BoFire fractionates only the continuous half of a domain and crosses categoricals in full, so
    categorical factors are handed to it as continuous inputs on [0, 1] and each bound is mapped
    back to its label. Real continuous factors join on their own bounds and the union fractionates
    as one, so `n_generators` counts against the total factor count and the resolution describes the
    whole design. A categorical with other than two levels is refused, or the resolution would
    describe only part of the design.
    """
    categoricals = [p for p in problem.parameters if isinstance(p, CategoricalParameter)]
    wrong_levels = [p.name for p in categoricals if len(p.categories) != 2]
    if wrong_levels:
        raise ValueError(
            f"a fractional design is a two-level design; {wrong_levels!r} have a different "
            "number of levels — give every factor exactly two levels, or ask for n_generators=0 "
            "to get the full grid"
        )
    # Raised here so the caller sees a plain ValueError rather than a pydantic ValidationError
    # wrapping it.
    generator = get_generator(n_factors=len(problem.parameters), n_generators=n_generators)
    domain = Domain(
        inputs=Inputs(
            features=[
                ContinuousInput(key=p.name, bounds=(p.lower, p.upper))
                if isinstance(p, ContinuousParameter)
                # [0, 1] rather than the labels: this is the encoding BoFire will fractionate.
                else ContinuousInput(key=p.name, bounds=(0.0, 1.0))
                for p in problem.parameters
            ]
        ),
        outputs=Outputs(features=[_objective_output(problem)]),
    )
    frame = strategies.map(
        FractionalFactorialStrategy(
            domain=domain,
            generator=generator,
            n_center=n_center,
            n_repetitions=n_repetitions,
        )
    ).ask()
    levels = {p.name: p.categories for p in categoricals}
    runs: list[dict[str, ParamValue]] = [
        {
            p.name: (
                (levels[p.name][0] if row[p.name] < 0.5 else levels[p.name][1])
                if p.name in levels
                else float(row[p.name])
            )
            for p in problem.parameters
        }
        for _, row in frame.iterrows()
    ]
    return ScreeningDesign(
        runs=runs,
        resolution=_resolution(generator),
        two_level_continuous=_two_level_names(problem),
        n_center=n_center,
        n_repetitions=n_repetitions,
    )


def _full_design(
    problem: OptimizationProblem, n_center: int, n_repetitions: int
) -> ScreeningDesign:
    """Every combination: categorical levels crossed with each continuous factor's two bounds.

    Built with `itertools.product` rather than asked of BoFire: for a mixed domain BoFire tiles both
    frames instead of crossing them, which enumerates the product only when `gcd(N, C) == 1` and
    otherwise drops and duplicates rows, confounding factors. Homogeneous problems give the same
    result as BoFire.

    `n_center` adds midpoint rows for the continuous factors per categorical combination;
    `n_repetitions` replicates the factorial part.
    """
    levels: list[list[ParamValue]] = []
    for parameter in problem.parameters:
        if isinstance(parameter, CategoricalParameter):
            levels.append(list(parameter.categories))
        else:
            # A continuous factor is screened at its two bounds and nothing between, which is what
            # `two_level_continuous` discloses and what BoFire did for this case too.
            levels.append([parameter.lower, parameter.upper])

    factorial = [
        {p.name: _cast(p, value) for p, value in zip(problem.parameters, combination, strict=True)}
        for combination in itertools.product(*levels)
    ]
    runs: list[dict[str, ParamValue]] = list(factorial) * n_repetitions

    if n_center:
        categoricals = [p for p in problem.parameters if isinstance(p, CategoricalParameter)]
        continuous = [p for p in problem.parameters if not isinstance(p, CategoricalParameter)]
        if continuous:
            midpoints = {p.name: _cast(p, (p.lower + p.upper) / 2) for p in continuous}
            cat_combinations = list(itertools.product(*(list(p.categories) for p in categoricals)))
            for combination in cat_combinations or [()]:
                labelled = {
                    p.name: _cast(p, value)
                    for p, value in zip(categoricals, combination, strict=True)
                }
                runs.extend([{**labelled, **midpoints}] * n_center)

    return ScreeningDesign(
        runs=runs,
        two_level_continuous=_two_level_names(problem),
        n_center=n_center,
        n_repetitions=n_repetitions,
    )


def _randomized(design: ScreeningDesign, seed: int | None) -> ScreeningDesign:
    """Shuffle the run order reproducibly, and record that it was shuffled.

    Done here rather than via `FractionalFactorialStrategy.randomize_runorder` so both design paths
    randomize identically under one `bo_seed`, independent of how BoFire seeds.
    """
    shuffled = list(design.runs)
    random.Random(_resolve_seed(seed)).shuffle(shuffled)
    return design.model_copy(update={"runs": shuffled, "randomized": True})


def _require_batch_fits_the_ceiling(n: int) -> None:
    """Refuse an ask beyond `bo_max_candidates_per_ask`, before the optimizer runs.

    In the engine so in-process and durable callers are bounded too, and before the strategy is
    built because the acquisition optimization is the cost being bounded.

    Raises:
        ValueError: Naming the request, the ceiling and the setting that moves it.
    """
    if n > settings.bo_max_candidates_per_ask:
        raise ValueError(
            f"cannot propose {n} candidates in one ask: the ceiling is "
            f"{settings.bo_max_candidates_per_ask}. Acquisition cost is linear in the batch — "
            "seconds per candidate unconstrained, and several times that with constraints — so a "
            "batch this size outlives the request that asked for it. Ask for a plate's worth at "
            "most, run them, and come back with the results; raise "
            "`CHEMCLAW_BO_MAX_CANDIDATES_PER_ASK` if this deployment genuinely proposes more than "
            "that at once."
        )


def _require_design_fits_the_ceiling(
    problem: OptimizationProblem, n_generators: int, n_center: int, n_repetitions: int
) -> None:
    """Refuse a screen whose run count exceeds `bo_max_design_runs`, before building it.

    A full factorial's size is exponential in model-supplied level counts, so it is counted before
    any row is materialized. The arithmetic mirrors `_full_design`: corners (categorical levels
    times two bounds per continuous factor) times `n_repetitions`, plus `n_center` rows per
    categorical combination; a reduced design is checked after halving per generator. The product is
    computed in full (Python ints are unbounded), since a truncated product shifted by
    `n_generators` could falsely pass. In the engine so in-process callers are bounded too.

    Raises:
        ValueError: Naming the run count, the ceiling, and the two ways to get under it.
    """
    if n_generators and any(
        len(p.categories) != 2 for p in problem.parameters if isinstance(p, CategoricalParameter)
    ):
        # A reduced design over a non-two-level factor is refused by `_fractional_design`, whose
        # error is the one the caller can act on; this arithmetic would describe a design that
        # cannot be built.
        return
    corners = 1
    categorical_combinations = 1
    for parameter in problem.parameters:
        if isinstance(parameter, CategoricalParameter):
            corners *= len(parameter.categories)
            categorical_combinations *= len(parameter.categories)
        else:
            # A continuous factor is screened at its two bounds, exactly as `_full_design` does.
            corners *= 2
    if n_generators:
        corners = max(corners >> n_generators, 1)
    runs = corners * n_repetitions + n_center * categorical_combinations
    if runs > settings.bo_max_design_runs:
        raise ValueError(
            f"this screen would generate {runs} runs, beyond the configured ceiling of "
            f"{settings.bo_max_design_runs}. A full factorial is the product of every factor's "
            "level count, so it grows exponentially in the number of factors — screen fewer "
            "factors at a time, or ask for a reduced design with `n_generators` (each one halves "
            "the run count). A design this size is not one anybody runs; it is a decision space "
            "that needs narrowing first."
        )


def factorial_design(
    problem: OptimizationProblem,
    n_generators: int = 0,
    n_center: int = 0,
    n_repetitions: int = 1,
    randomize: bool = False,
    seed: int | None = None,
) -> ScreeningDesign:
    """Screen `problem`'s factors — the full grid, or a reduced fraction of it.

    `n_generators=0` is every categorical combination crossed with each continuous factor's two
    bounds; each generator halves the run count. Continuous factors are held at their bounds, and
    `ScreeningDesign` names them (`two_level_continuous`, `summary`); use `propose_candidates` for
    what happens between bounds.

    `n_center` adds midpoint runs per categorical combination (detecting curvature); `n_repetitions`
    replicates the factorial part (pure-error estimate); `randomize` shuffles run order reproducibly
    under `seed`. Both knobs are refused on an all-categorical problem
    (`_require_knobs_are_honoured`). The result carries its `resolution`, so a reduced design cannot
    pass as exhaustive.
    """
    if problem.constraints:
        # `FractionalFactorialStrategy` rejects every constraint class; this refusal exists to give
        # a message the caller can act on instead of a pydantic error naming a BoFire class.
        stated = "; ".join(constraint.describe() for constraint in problem.constraints)
        raise ValueError(
            f"a factorial screen cannot honour a constraint ({stated}): it enumerates the corners "
            "of the space, and BoFire refuses a constrained design outright. Drop the constraint "
            "and filter the returned runs yourself — saying that you did — or use "
            "`suggest_next_experiment`, which does honour it."
        )
    if n_generators < 0:
        # Not left to BoFire: a negative count reaches `fracfact` as a malformed generator string
        # and comes back as a pydantic ValidationError about a generator the caller never wrote.
        raise ValueError(f"n_generators must be 0 (the full grid) or more; got {n_generators}")
    if n_center < 0:
        raise ValueError(f"n_center must be 0 or more; got {n_center}")
    if n_repetitions < 1:
        raise ValueError(f"n_repetitions must be 1 or more; got {n_repetitions}")
    _require_knobs_are_honoured(problem, n_center, n_repetitions, reduced=bool(n_generators))
    _require_design_fits_the_ceiling(problem, n_generators, n_center, n_repetitions)
    design = (
        _fractional_design(problem, n_generators, n_center, n_repetitions)
        if n_generators
        else _full_design(problem, n_center, n_repetitions)
    )
    return _randomized(design, seed) if randomize else design


#: BoFire's criterion class for each name `DESIGN_CRITERIA` exposes; a mapping so a BoFire
#: rename fails here rather than via a model-written string.
_CRITERIA = {
    "d-optimal": DOptimalityCriterion,
    "a-optimal": AOptimalityCriterion,
    "i-optimal": IOptimalityCriterion,
    "space-filling": SpaceFillingCriterion,
}


def _model_terms(problem: OptimizationProblem, formula: str) -> int:
    """How many coefficients `formula` has over this problem's factors.

    BoFire's own count rather than re-derived arithmetic: a categorical contributes one column per
    level minus one, where a naive formula diverges.
    """
    return len(get_formula_from_string(model_type=formula, inputs=_to_domain(problem).inputs))


def _require_design_can_estimate_its_model(
    problem: OptimizationProblem, n_experiments: int, formula: str
) -> None:
    """Refuse a design with fewer runs than the model it claims to be optimal for.

    BoFire returns such a design without error, but its information matrix is singular and no
    coefficient is estimable. The bound is `n_experiments >= n_terms`, the condition for
    estimability; at exactly n_terms there are no residual degrees of freedom, which the message
    states.

    Raises:
        ValueError: Naming the run count, the term count and the formula that set it.
    """
    terms = _model_terms(problem, formula)
    if n_experiments < terms:
        raise ValueError(
            f"{n_experiments} run(s) cannot estimate a {formula!r} model over these factors, "
            f"which has {terms} term(s): the design would be singular and none of its "
            f"coefficients estimable. Ask for at least {terms} runs, or choose a simpler formula "
            f"— 'linear' has {_model_terms(problem, 'linear')} term(s) here. Note that exactly "
            f"{terms} runs leaves no residual degrees of freedom, so the model would fit perfectly "
            "and tell you nothing about how well."
        )


#: How far outside a declared linear constraint a returned run may sit before it is a failure.
#:
#: BoFire's DoE solves a continuous optimization (SLSQP via scipy here), so an active constraint
#: is met only to solver tolerance, and that varies across scipy builds. 1e-4 is well above that
#: and far below anything a chemist can set, so a breach is a real infeasibility.
_CONSTRAINT_TOLERANCE = 1e-4


#: How close to a bound, as a fraction of the parameter's range, a solved value is solver noise
#: rather than a condition: well above solver residue, far below anything a chemist can dial.
_BOUND_SNAP_FRACTION = 1e-9

#: Significant digits a solved interior value keeps. Relative rounding can exceed the absolute
#: `_CONSTRAINT_TOLERANCE` at large magnitudes, so breaches are checked on the solver's values
#: and a run whose cleaned form breaches is returned as solved (`_verified_clean_run`).
_DESIGN_SIGNIFICANT_DIGITS = 10


def _clean(parameter: ContinuousParameter | CategoricalParameter, value: ParamValue) -> ParamValue:
    """A solved value with its solver noise removed: snapped onto a bound it meant, else rounded.

    Makes replicates detectable by equality and conditions readable. Not bounded in absolute terms,
    so `_verified_clean_run` re-checks the cleaned run.
    """
    if not isinstance(parameter, ContinuousParameter) or not isinstance(value, float):
        return value
    snap = (parameter.upper - parameter.lower) * _BOUND_SNAP_FRACTION
    for bound in (parameter.lower, parameter.upper):
        if abs(value - bound) <= snap:
            return bound
    return float(f"{value:.{_DESIGN_SIGNIFICANT_DIGITS}g}")


def _constraint_breaches(
    problem: OptimizationProblem, runs: list[dict[str, ParamValue]]
) -> list[str]:
    """Every run sitting outside a declared linear constraint by more than the tolerance.

    Honouring limits is `optimal_design`'s reason to exist, so feasibility is verified rather than
    assumed. `point_is_feasible` answers a different question (cell consumption, `ExcludeConstraint`
    only).
    """
    breaches: list[str] = []
    for index, run in enumerate(runs, start=1):
        for constraint in problem.constraints:
            if not isinstance(constraint, LinearConstraint):
                continue
            total = sum(
                coefficient * float(run[name])
                for name, coefficient in zip(
                    constraint.parameters, constraint.coefficients, strict=True
                )
                if name in run
            )
            slack = {
                "<=": constraint.rhs - total,
                ">=": total - constraint.rhs,
                "==": -abs(total - constraint.rhs),
            }[constraint.relation]
            if slack < -_CONSTRAINT_TOLERANCE:
                breaches.append(f"run {index} gives {total:g} against {constraint.describe()}")
    return breaches


def _verified_clean_run(
    problem: OptimizationProblem, run: dict[str, ParamValue]
) -> dict[str, ParamValue]:
    """`run` cleaned by `_clean`, unless cleaning breaks a constraint — then `run` as solved.

    Every returned run is one `_constraint_breaches` passed, which is what `honoured_constraints`
    claims.
    """
    cleaned = {p.name: _clean(p, run[p.name]) for p in problem.parameters}
    return run if _constraint_breaches(problem, [cleaned]) else cleaned


def optimal_design(
    problem: OptimizationProblem,
    n_experiments: int,
    criterion: str = "d-optimal",
    formula: str = "linear",
    seed: int | None = None,
) -> OptimalDesign:
    """Lay out `n_experiments` runs over `problem`, honouring its constraints.

    For a chemist with real constraints and a fixed run budget, which a factorial cannot honour. An
    optimality criterion builds the design that best estimates the stated `formula` (a `linear`
    design is blind to curvature); `space-filling` assumes no model and covers the region.

    Args:
        problem: The decision space, with any constraints it declares.
        n_experiments: The run budget. Filled exactly.
        criterion: One of `DESIGN_CRITERIA`.
        formula: One of `DESIGN_FORMULAE`. Ignored by `space-filling`, which assumes no model.
        seed: Reproducibility for the optimizer's own starting points.

    Returns:
        The runs, with the formula, the term count, the duplicate count and a `summary` stating what
        the design cannot do.

    Raises:
        ValueError: An unknown criterion or formula, a non-positive budget, a budget over
            `bo_max_design_runs`, a budget too small to estimate the stated model, or an
            `ExcludeConstraint`, which the DoE solver cannot honour.
    """
    if criterion not in _CRITERIA:
        raise ValueError(
            f"unknown criterion {criterion!r}; this deployment offers {', '.join(DESIGN_CRITERIA)}"
        )
    space_filling = criterion == "space-filling"
    if not space_filling and formula not in DESIGN_FORMULAE:
        raise ValueError(
            f"unknown formula {formula!r}; this deployment offers {', '.join(DESIGN_FORMULAE)}"
        )
    if n_experiments < 1:
        raise ValueError(f"n_experiments must be 1 or more; got {n_experiments}")
    if n_experiments > settings.bo_max_design_runs:
        raise ValueError(
            f"{n_experiments} runs is over this deployment's bo_max_design_runs "
            f"({settings.bo_max_design_runs}). Raise the setting, or ask for fewer."
        )
    # Refused before the solver: BoFire's DoE strategy cannot take a categorical exclusion, and
    # `_constraint_breaches` could not verify one.
    exclusions = [c for c in problem.constraints if isinstance(c, ExcludeConstraint)]
    if exclusions:
        raise ValueError(
            "optimal_design honours linear constraints only; an exclusion ("
            + "; ".join(c.describe() for c in exclusions)
            + ") is not supported by the DoE solver under any criterion. Remove the exclusion, "
            "design over the full space (the factorial lists every pairing), and strike the "
            "excluded pairings from the returned runs."
        )
    if not space_filling:
        _require_design_can_estimate_its_model(problem, n_experiments, formula)
    spec = DoESpec(
        domain=_to_domain(problem),
        criterion=SpaceFillingCriterion()
        if space_filling
        else _CRITERIA[criterion](formula=formula),
        seed=_resolve_seed(seed),
    )
    try:
        frame = strategies.map(spec).ask(candidate_count=n_experiments)
    except Exception as error:
        raise SurrogateFitError(
            f"the {criterion} design could not be solved for {n_experiments} run(s) over this "
            f"space: {error}. A tighter constraint set leaves less room, so try more runs, a "
            "simpler formula, or space-filling."
        ) from error
    solved: list[dict[str, ParamValue]] = [
        {p.name: _cast(p, row[p.name]) for p in problem.parameters} for _, row in frame.iterrows()
    ]
    breaches = _constraint_breaches(problem, solved)
    if breaches:
        raise SurrogateFitError(
            "the solver returned run(s) outside a declared constraint, so this design is not "
            f"feasible and must not be run: {'; '.join(breaches)}. Honouring a limit is the one "
            "thing this offers over a factorial screen, so it refuses rather than returning them."
        )
    runs = [_verified_clean_run(problem, run) for run in solved]
    seen: set[tuple[tuple[str, ParamValue], ...]] = set()
    duplicates = 0
    for run in runs:
        key = tuple(sorted(run.items()))
        if key in seen:
            duplicates += 1
        seen.add(key)
    return OptimalDesign(
        runs=runs,
        criterion=criterion,
        formula=None if space_filling else formula,
        n_terms=0 if space_filling else _model_terms(problem, formula),
        duplicate_runs=duplicates,
        # Only constraints `_constraint_breaches` verified on the returned runs; exclusions are
        # refused above, so this is every constraint the design carries.
        honoured_constraints=sum(isinstance(c, LinearConstraint) for c in problem.constraints),
    )
