"""BoFire adapter — the only module that touches BoFire (plan Phase 1d, D-012; D-092).

Maps our neutral `OptimizationProblem`/`Observation` types to BoFire's domain and
strategies, proposes candidates, and maps results back to our `Candidate` type.
Nothing BoFire leaks past this boundary (gate G6), so the engine could be swapped
without touching the campaign, agents, or skills. `factorial_design` (D-092) is the
same adapter shape for BoFire's classical `FractionalFactorialStrategy`, alongside
the Bayesian-optimization strategies — full grid by default, and a reduced two-level
design when the chemist's plate cannot hold the whole one.

Errors leak nowhere past this boundary either (Science-4): `_fractional_design` catches
BoFire's own validator error and re-raises a plain `ValueError`, and `initial_candidates`/
`propose_candidates` catch the botorch/gpytorch/linear-algebra exceptions a degenerate fit
or acquisition step can raise and re-raise `SurrogateFitError` — see
`_translating_surrogate_errors`.
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
from bofire.data_models.domain.api import Domain, Inputs, Outputs
from bofire.data_models.features.api import (
    CategoricalDescriptorInput,
    CategoricalInput,
    ContinuousInput,
    ContinuousOutput,
)
from bofire.data_models.objectives.api import MaximizeObjective, MinimizeObjective
from bofire.data_models.strategies.api import (
    FractionalFactorialStrategy,
    MoboStrategy,
    RandomStrategy,
    SoboStrategy,
)
from bofire.strategies import api as strategies
from bofire.utils.doe import get_generator
from botorch.exceptions.errors import BotorchError, ModelFittingError
from linear_operator.utils.errors import NanError, NotPSDError

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.science.bo.problem import (
    MIN_SEED_OBSERVATIONS,
    Candidate,
    CategoricalParameter,
    ContinuousParameter,
    Observation,
    OptimizationProblem,
    ParamValue,
    ScreeningDesign,
    discrete_candidate_count,
    observed_value,
    params_key,
)


class SurrogateFitError(ChemclawError):
    """BoFire's Bayesian strategy could not fit or query its surrogate (Science-4).

    Raised in place of whatever `botorch`/`gpytorch`/`linear_operator` exception the fit or the
    acquisition step actually threw. The classical path (`_fractional_design`) already had this
    boundary — it catches BoFire's validator error and re-raises a plain `ValueError` "so the
    caller sees a plain ValueError instead of a pydantic ValidationError wrapping it" — and the
    Bayesian path had no equivalent, so a duplicate observation or a degenerate kernel propagated
    a raw library exception straight through the Temporal activity or the in-process campaign
    loop. `chemclaw.core.errors.ChemclawError` is what `agent.tool_authz.surface_domain_errors`
    catches for the in-process seam; for the durable one, this class's name is listed in
    `chemclaw.durable.publish._BAD_DATA_TYPES`, because the failure is a property of the *data*
    (the same observations will fail the same way again) rather than a transient one a retry
    could fix.
    """


# The library exceptions a GP fit or an acquisition-optimization step is known to raise on
# degenerate input: a near-singular kernel (duplicate/near-duplicate points), a covariance matrix
# that stays non-positive-definite after every jitter attempt, or a fit that produces NaNs.
# `ModelFittingError` does not subclass `BotorchError` (botorch's own hierarchy), so it is listed
# separately rather than assumed to be covered by it.
_SURROGATE_FAILURES: tuple[type[Exception], ...] = (
    BotorchError,
    ModelFittingError,
    NotPSDError,
    NanError,
    numpy.linalg.LinAlgError,
    torch.linalg.LinAlgError,  # type: ignore[attr-defined] # torch's stubs omit this re-export
)


@contextmanager
def _translating_surrogate_errors(context: str) -> Iterator[None]:
    """Turn a known BoFire/botorch numerical failure into `SurrogateFitError`.

    `context` names the step in the caller's own words (e.g. "fitting the surrogate to 3
    observation(s)"), so the translated message says what was being attempted without this
    helper needing to know which of `tell`/`ask` raised.
    """
    try:
        yield
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
    """Map a categorical to BoFire, using its descriptors when it has been featurized (U1).

    The distinction is the whole point of featurization and it is invisible at the call site,
    so it is worth stating: a `CategoricalInput` is encoded ordinally inside a BoTorch
    surrogate — the model sees an index and can only learn each label independently — while a
    `CategoricalDescriptorInput` is descriptor-encoded, so the model sees the molecule's
    position in descriptor space and can generalize to a category it has never been told
    about. `tests/test_bo_featurize.py` pins that encoding, because it is a BoFire default we
    depend on rather than one we set.
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

    Kept for the classical design paths, which have no objective at all: `factorial_design` builds a
    domain only because BoFire requires outputs, and its direction is never read. A screen over a
    multi-objective problem is still one screen.
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


def _to_domain(problem: OptimizationProblem) -> Domain:
    """Translate our problem into a BoFire `Domain` (inputs + one output per objective)."""
    inputs = []
    for parameter in problem.parameters:
        if isinstance(parameter, ContinuousParameter):
            inputs.append(
                ContinuousInput(key=parameter.name, bounds=(parameter.lower, parameter.upper))
            )
        else:
            inputs.append(_categorical_input(parameter))
    return Domain(inputs=Inputs(features=inputs), outputs=Outputs(features=_outputs(problem)))


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

    BoFire returns `<objective>_pred`, `<objective>_sd` and `<objective>_des` beside the parameter
    columns whenever a model backs the proposal; a `RandomStrategy` returns the parameters alone.
    Reading them conditionally is what lets one adapter serve both, and recovering the sd is the
    point: it is computed on every model-guided ask and was dropped here, one function before it
    could reach the `bo-candidate` note a human signs off on (F8-T1 follow-up).

    `_des` is deliberately left behind. It is the acquisition/desirability score — a ranking
    quantity in the strategy's own units, not a statement about the chemistry — and carrying it
    would invite reading it as a confidence.

    **A multi-objective ask returns the same columns per objective** (measured, M-1:
    `yield_pred, impurity_pred, yield_sd, impurity_sd, …`), so the per-objective vectors are filled
    from the same reader. The scalars keep the lead objective, which is what every persisted row and
    every existing consumer already holds.
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

    Used to seed a campaign before any observations exist — a GP needs data before
    it can guide the search. In a finite (all-categorical) space the points are
    made distinct — a duplicate seed would spend budget re-running an identical
    experiment — and `n` beyond the space size is rejected because that many
    distinct points cannot exist.
    """
    strategy = strategies.map(RandomStrategy(domain=_to_domain(problem), seed=_resolve_seed(seed)))
    space = discrete_candidate_count(problem)
    with _translating_surrogate_errors("sampling initial candidates"):
        if space is None:
            return _frame_to_candidates(problem, strategy.ask(n))
        if n > space:
            raise ValueError(
                f"cannot seed {n} distinct points: the discrete space has only {space}"
            )
        # Re-ask until `n` distinct points are collected; each ask advances the
        # strategy's RNG, and n <= space guarantees enough fresh points exist.
        candidates: list[Candidate] = []
        seen: set[tuple[tuple[str, ParamValue], ...]] = set()
        while len(candidates) < n:
            for candidate in _frame_to_candidates(problem, strategy.ask(n - len(candidates))):
                key = params_key(candidate.params)
                if key not in seen:
                    seen.add(key)
                    candidates.append(candidate)
        return candidates


def propose_candidates(
    problem: OptimizationProblem,
    observations: list[Observation],
    n: int = 1,
    seed: int | None = None,
) -> list[Candidate]:
    """Propose the next `n` candidates from past observations — SOBO, or MOBO for a trade-off.

    Requires at least `MIN_SEED_OBSERVATIONS` observations to fit the surrogate
    (BoFire's floor); call `initial_candidates` first to seed. Raises `ValueError`
    below that floor rather than surfacing an opaque BoFire error (gate G4).

    **The strategy follows the problem, not a setting** (W3). One objective gets `SoboStrategy`;
    more than one gets `MoboStrategy`, whose default acquisition is `qLogNEHVI`. Measured (M-1):
    `MoboStrategy` validates with `ref_point` unset — it derives a *moving* reference per objective
    (`AbsoluteMovingReferenceValue`) from the data rather than hiding a fixed one — and it fits at
    two observations, the same floor SOBO has, so `MIN_SEED_OBSERVATIONS` is unchanged.
    """
    if len(observations) < MIN_SEED_OBSERVATIONS:
        raise ValueError(
            f"propose_candidates needs at least {MIN_SEED_OBSERVATIONS} observations; seed first"
        )
    domain = _to_domain(problem)
    resolved = _resolve_seed(seed)
    specification = (
        MoboStrategy(domain=domain, seed=resolved)
        if len(problem.objectives) > 1
        else SoboStrategy(domain=domain, seed=resolved)
    )
    strategy = strategies.map(specification)
    context = f"fitting the surrogate to {len(observations)} observation(s)"
    with _translating_surrogate_errors(context):
        strategy.tell(_observations_to_frame(problem, observations))
        candidates = strategy.ask(n)
    return _frame_to_candidates(problem, candidates)


def _resolution(generator: str) -> int:
    """The resolution of a two-level design with this generator: its shortest defining word.

    Computed rather than reported as a run count, because the run count does not say what was
    given up. A generator string names one word per factor — a single letter for a base factor,
    a product like `abc` for a factor aliased onto that interaction — so each derived factor
    contributes the defining word `abc·d`, and the defining relation is every product of those.
    The shortest word in that group *is* the resolution, which is the number a chemist needs to
    know whether a main effect they read off the screen could really be a two-factor interaction.

    Derived here rather than taken from BoFire because BoFire only exposes it as a formatted alias
    listing (`bofire.utils.doe.get_alias_structure`), and parsing prose to recover a number is a
    worse dependency than restating a three-line definition.
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
    """Refuse the two knobs BoFire silently ignores rather than passing them into a no-op (W2).

    Measured (M-5): on an all-categorical domain `n_center` and `n_repetitions` are **inert** —
    three two-level categoricals give 8 runs at every value of either, exactly as `n_generators`
    does. Threading an argument into a call that ignores it is how `n_generators` came to be
    documented, imported and dead; a refusal naming the reason is the only honest alternative,
    because there is no partial behaviour to fall back on.

    A centre point also has to *mean* something. On the reduced path a categorical factor is
    re-encoded onto [0, 1], so a centre row would put it at 0.5 — which decodes to neither of its
    levels, and is why `n_center=0` was forced there from the start (D-2026-08-02).
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

    BoFire fractionates the *continuous* half of a domain and always crosses the categorical half
    in full (`FractionalFactorialStrategy._get_categorical_design` enumerates every combination and
    never consults `n_generators`) — measured: seven two-level `CategoricalInput`s give 128 runs at
    every `n_generators` value that validates at all. So the only way to express a reduced screen
    over categorical factors is to hand BoFire the factors as continuous inputs on [0, 1], let it
    build the fractional design at those two bounds, and map each bound back to its label.

    **A continuous factor joins that set on its own bounds, and the union fractionates as one**
    (measured, M-8): two real continuous factors beside three re-encoded categoricals give 32, 16
    and 8 runs at `n_generators` 0, 1 and 2, with every factor at exactly two levels and the real
    ones at their declared bounds. So `n_generators` counts against the *total* factor count, and
    the generator — hence the resolution derived from it — describes the whole design rather than
    part of it, which is what makes returning a resolution honest at all.

    Two-level only, and a *categorical* factor with a different number of levels is **refused**
    rather than quietly crossed in full: this is a two-level design by construction, and a
    three-level factor smuggled in would make the returned resolution describe only part of the
    design.
    """
    categoricals = [p for p in problem.parameters if isinstance(p, CategoricalParameter)]
    wrong_levels = [p.name for p in categoricals if len(p.categories) != 2]
    if wrong_levels:
        raise ValueError(
            f"a fractional design is a two-level design; {wrong_levels!r} have a different "
            "number of levels — give every factor exactly two levels, or ask for n_generators=0 "
            "to get the full grid"
        )
    # Raised here rather than from inside the strategy's validator so the caller sees a plain
    # ValueError ("Design not possible, as main factors are confounded with each other") instead of
    # a pydantic ValidationError wrapping it.
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

    `n_center` is passed explicitly on **every** path, including the default of 0, because BoFire's
    own default is **1** (measured, M-5) — leaving it unset would have this function silently start
    returning midpoint rows nobody asked for the moment a continuous factor was admitted.
    """
    frame = strategies.map(
        FractionalFactorialStrategy(
            domain=_to_domain(problem), n_center=n_center, n_repetitions=n_repetitions
        )
    ).ask()
    runs: list[dict[str, ParamValue]] = [
        {p.name: _cast(p, row[p.name]) for p in problem.parameters} for _, row in frame.iterrows()
    ]
    return ScreeningDesign(
        runs=runs,
        two_level_continuous=_two_level_names(problem),
        n_center=n_center,
        n_repetitions=n_repetitions,
    )


def _randomized(design: ScreeningDesign, seed: int | None) -> ScreeningDesign:
    """Shuffle the run order reproducibly, and record that it was shuffled.

    Done here rather than through `FractionalFactorialStrategy.randomize_runorder`, which exists
    and works (measured: seed-reproducible and seed-sensitive). Two reasons for the boundary: the
    two design paths construct their strategies differently, so shuffling once here is what makes
    them randomize identically under one `bo_seed` default; and it keeps the guarantee ours if a
    future BoFire release changes what that argument seeds from.
    """
    shuffled = list(design.runs)
    random.Random(_resolve_seed(seed)).shuffle(shuffled)
    return design.model_copy(update={"runs": shuffled, "randomized": True})


def factorial_design(
    problem: OptimizationProblem,
    n_generators: int = 0,
    n_center: int = 0,
    n_repetitions: int = 1,
    randomize: bool = False,
    seed: int | None = None,
) -> ScreeningDesign:
    """Screen `problem`'s factors — the full grid, or a reduced fraction of it.

    `n_generators=0` (the default) is every combination of the categorical levels crossed with each
    continuous factor's two bounds. Each generator beyond that halves the run count, so seven
    two-level factors go from 128 runs to 64, 32 or 16 — the difference between a design that fits a
    96-well plate and one that does not.

    **A continuous factor is admitted and held at its two bounds** (W2). This used to be refused
    (D-092), because the class silently fractionates a continuous input to its two bounds and a
    design that looks complete while quietly reshaping a factor is worse than a clear refusal. That
    was right while nothing in the return could say what had been done; `ScreeningDesign` now
    carries `two_level_continuous` and a `summary` naming every collapsed factor, so the condition
    the refusal was waiting for is met. A screen still says nothing about what happens *between*
    those bounds — use `propose_candidates` for that.

    `n_center` adds centre runs at the midpoint of every continuous factor, which is what detects
    curvature a two-level design cannot see; **BoFire adds them per categorical combination**, so
    the total is not `corners + n_center` (measured: 4·2^k + n_center·2^k over k categoricals).
    `n_repetitions` replicates the factorial part, which is what gives the screen a pure-error
    estimate. `randomize` shuffles the run order against a drift over the session, reproducibly
    under `seed`.

    Both `n_center` and `n_repetitions` are **refused** on an all-categorical problem rather than
    passed into a call that ignores them — see `_require_knobs_are_honoured`.

    The returned `ScreeningDesign` carries the design's `resolution` and a `summary` naming it, so
    a reduced design cannot be presented as an exhaustive one.
    """
    if n_generators < 0:
        # Not left to BoFire: a negative count reaches `fracfact` as a malformed generator string
        # and comes back as a pydantic ValidationError about a generator the caller never wrote.
        raise ValueError(f"n_generators must be 0 (the full grid) or more; got {n_generators}")
    if n_center < 0:
        raise ValueError(f"n_center must be 0 or more; got {n_center}")
    if n_repetitions < 1:
        raise ValueError(f"n_repetitions must be 1 or more; got {n_repetitions}")
    _require_knobs_are_honoured(problem, n_center, n_repetitions, reduced=bool(n_generators))
    design = (
        _fractional_design(problem, n_generators, n_center, n_repetitions)
        if n_generators
        else _full_design(problem, n_center, n_repetitions)
    )
    return _randomized(design, seed) if randomize else design
