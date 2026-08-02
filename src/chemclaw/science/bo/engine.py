"""BoFire adapter — the only module that touches BoFire (plan Phase 1d, D-012; D-092).

Maps our neutral `OptimizationProblem`/`Observation` types to BoFire's domain and
strategies, proposes candidates, and maps results back to our `Candidate` type.
Nothing BoFire leaks past this boundary (gate G6), so the engine could be swapped
without touching the campaign, agents, or skills. `factorial_design` (D-092) is the
same adapter shape for BoFire's classical `FractionalFactorialStrategy`, alongside
the Bayesian-optimization strategies — full grid by default, and a reduced two-level
design when the chemist's plate cannot hold the whole one.
"""

import itertools
import operator
import string
from functools import reduce
from typing import Any

import pandas as pd
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
    RandomStrategy,
    SoboStrategy,
)
from bofire.strategies import api as strategies
from bofire.utils.doe import get_generator

from chemclaw.core.config import settings
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
    params_key,
)


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
    """The problem's single objective as a BoFire output, whichever domain shape needs it."""
    objective = (
        MinimizeObjective(w=1.0)
        if problem.objective.direction == "minimize"
        else MaximizeObjective(w=1.0)
    )
    return ContinuousOutput(key=problem.objective.name, objective=objective)


def _to_domain(problem: OptimizationProblem) -> Domain:
    """Translate our problem into a BoFire `Domain` (inputs + one objective output)."""
    inputs = []
    for parameter in problem.parameters:
        if isinstance(parameter, ContinuousParameter):
            inputs.append(
                ContinuousInput(key=parameter.name, bounds=(parameter.lower, parameter.upper))
            )
        else:
            inputs.append(_categorical_input(parameter))
    return Domain(
        inputs=Inputs(features=inputs), outputs=Outputs(features=[_objective_output(problem)])
    )


def _cast(parameter: ContinuousParameter | CategoricalParameter, raw: Any) -> ParamValue:
    """Coerce a dataframe cell to the parameter's value type (float or category str)."""
    return float(raw) if isinstance(parameter, ContinuousParameter) else str(raw)


def _observations_to_frame(
    problem: OptimizationProblem, observations: list[Observation]
) -> pd.DataFrame:
    """Build the experiments dataframe BoFire's `tell` expects."""
    objective_key = problem.objective.name
    rows = []
    for obs in observations:
        row: dict[str, object] = dict(obs.params)
        row[objective_key] = obs.value
        row[f"valid_{objective_key}"] = 1
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
    """
    predicted, sd = f"{problem.objective.name}_pred", f"{problem.objective.name}_sd"
    return [
        Candidate(
            params={p.name: _cast(p, row[p.name]) for p in problem.parameters},
            predicted_value=float(row[predicted]) if predicted in frame.columns else None,
            # abs(): a posterior sd is non-negative by definition, and the field enforces it, but
            # a float round-trip through the surrogate can land a hair below zero.
            predicted_sd=abs(float(row[sd])) if sd in frame.columns else None,
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
    if space is None:
        return _frame_to_candidates(problem, strategy.ask(n))
    if n > space:
        raise ValueError(f"cannot seed {n} distinct points: the discrete space has only {space}")
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
    """Propose the next `n` candidates from past observations via SOBO.

    Requires at least `MIN_SEED_OBSERVATIONS` observations to fit the surrogate
    (BoFire's floor); call `initial_candidates` first to seed. Raises `ValueError`
    below that floor rather than surfacing an opaque BoFire error (gate G4).
    """
    if len(observations) < MIN_SEED_OBSERVATIONS:
        raise ValueError(
            f"propose_candidates needs at least {MIN_SEED_OBSERVATIONS} observations; seed first"
        )
    strategy = strategies.map(SoboStrategy(domain=_to_domain(problem), seed=_resolve_seed(seed)))
    strategy.tell(_observations_to_frame(problem, observations))
    return _frame_to_candidates(problem, strategy.ask(n))


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


def _fractional_design(problem: OptimizationProblem, n_generators: int) -> ScreeningDesign:
    """A reduced two-level screen: `2**-n_generators` of the grid, with its resolution stated.

    BoFire fractionates the *continuous* half of a domain and always crosses the categorical half
    in full (`FractionalFactorialStrategy._get_categorical_design` enumerates every combination and
    never consults `n_generators`) — measured: seven two-level `CategoricalInput`s give 128 runs at
    every `n_generators` value that validates at all. So the only way to express a reduced screen
    over categorical factors is to hand BoFire the factors as continuous inputs on [0, 1], let it
    build the fractional design at those two bounds, and map each bound back to its label. That is
    what this does; `n_center=0` because a centre point at 0.5 would decode to neither level.

    Two-level only, and a factor with a different number of levels is **refused** rather than
    quietly crossed in full: this is a two-level design by construction, and a three-level factor
    smuggled in would make the returned resolution describe only part of the design — the exact
    "looks complete while omitting a factor" failure `factorial_design` refuses continuous inputs
    to avoid.
    """
    # Total: `factorial_design` has already refused any continuous parameter before calling here.
    parameters = [p for p in problem.parameters if isinstance(p, CategoricalParameter)]
    wrong_levels = [p.name for p in parameters if len(p.categories) != 2]
    if wrong_levels:
        raise ValueError(
            f"a fractional design is a two-level design; {wrong_levels!r} have a different "
            "number of levels — give every factor exactly two levels, or ask for n_generators=0 "
            "to get the full grid"
        )
    # Raised here rather than from inside the strategy's validator so the caller sees a plain
    # ValueError ("Design not possible, as main factors are confounded with each other") instead of
    # a pydantic ValidationError wrapping it.
    generator = get_generator(n_factors=len(parameters), n_generators=n_generators)
    domain = Domain(
        inputs=Inputs(
            features=[ContinuousInput(key=p.name, bounds=(0.0, 1.0)) for p in parameters]
        ),
        outputs=Outputs(features=[_objective_output(problem)]),
    )
    frame = strategies.map(
        FractionalFactorialStrategy(domain=domain, generator=generator, n_center=0)
    ).ask()
    levels = {p.name: p.categories for p in parameters}
    runs: list[dict[str, ParamValue]] = [
        {name: options[0] if row[name] < 0.5 else options[1] for name, options in levels.items()}
        for _, row in frame.iterrows()
    ]
    return ScreeningDesign(runs=runs, resolution=_resolution(generator))


def factorial_design(problem: OptimizationProblem, n_generators: int = 0) -> ScreeningDesign:
    """Screen `problem`'s categorical factors — the full grid, or a reduced fraction of it.

    `n_generators=0` (the default) is every combination: the plain Cartesian product, which is what
    BoFire's `FractionalFactorialStrategy` returns on an all-categorical domain. Each generator
    beyond that halves the run count, so seven two-level factors go from 128 runs to 64, 32 or 16 —
    which is the difference between a design that fits a 96-well plate and one that does not.

    Raises `ValueError` if `problem` names any continuous parameter (D-092 research follow-up):
    the same class silently *fractionates* a continuous input to its two bounds instead of erroring,
    and a design that looks complete but quietly omits or fractionates a factor is worse than a
    clear refusal (gate G4). Reformulate a continuous factor as a small set of discrete levels
    (e.g. temperature as "low"/"high") to include it in a screen, or use
    `propose_candidates`/`initial_candidates` for a continuous decision space.

    The returned `ScreeningDesign` carries the design's `resolution` and a `summary` sentence
    naming it, so a reduced design cannot be presented as an exhaustive one — the same reason the
    continuous refusal above exists, applied to the reduction this function now performs itself.
    """
    continuous = [p.name for p in problem.parameters if isinstance(p, ContinuousParameter)]
    if continuous:
        raise ValueError(
            "factorial_design only supports categorical parameters; "
            f"{continuous!r} are continuous — discretize them into levels first"
        )
    if n_generators < 0:
        # Not left to BoFire: a negative count reaches `fracfact` as a malformed generator string
        # and comes back as a pydantic ValidationError about a generator the caller never wrote.
        raise ValueError(f"n_generators must be 0 (the full grid) or more; got {n_generators}")
    if n_generators:
        return _fractional_design(problem, n_generators)
    strategy = strategies.map(FractionalFactorialStrategy(domain=_to_domain(problem)))
    frame = strategy.ask()
    categorical_names = [p.name for p in problem.parameters]
    runs: list[dict[str, ParamValue]] = [
        {name: str(row[name]) for name in categorical_names} for _, row in frame.iterrows()
    ]
    return ScreeningDesign(runs=runs)
