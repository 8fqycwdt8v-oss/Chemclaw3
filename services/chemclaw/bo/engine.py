"""BoFire adapter — the only module that touches BoFire (plan Phase 1d, D-012).

Maps our neutral `OptimizationProblem`/`Observation` types to BoFire's domain and
strategies, proposes candidates, and maps results back to our `Candidate` type.
Nothing BoFire leaks past this boundary (gate G6), so the engine could be swapped
without touching the campaign, agents, or skills.
"""

from typing import Any

import pandas as pd
from bofire.data_models.domain.api import Domain, Inputs, Outputs
from bofire.data_models.features.api import (
    CategoricalInput,
    ContinuousInput,
    ContinuousOutput,
)
from bofire.data_models.objectives.api import MaximizeObjective, MinimizeObjective
from bofire.data_models.strategies.api import RandomStrategy, SoboStrategy
from bofire.strategies import api as strategies

from bo.problem import (
    MIN_SEED_OBSERVATIONS,
    Candidate,
    CategoricalParameter,
    ContinuousParameter,
    Observation,
    OptimizationProblem,
    ParamValue,
    discrete_candidate_count,
    params_key,
)
from chemclaw.config import settings


def _resolve_seed(seed: int | None) -> int:
    """Per-call seed, falling back to the config default for reproducible runs."""
    return settings.bo_seed if seed is None else seed


def _to_domain(problem: OptimizationProblem) -> Domain:
    """Translate our problem into a BoFire `Domain` (inputs + one objective output)."""
    inputs = []
    for parameter in problem.parameters:
        if isinstance(parameter, ContinuousParameter):
            inputs.append(
                ContinuousInput(key=parameter.name, bounds=(parameter.lower, parameter.upper))
            )
        else:
            inputs.append(CategoricalInput(key=parameter.name, categories=parameter.categories))
    objective = (
        MinimizeObjective(w=1.0)
        if problem.objective.direction == "minimize"
        else MaximizeObjective(w=1.0)
    )
    output = ContinuousOutput(key=problem.objective.name, objective=objective)
    return Domain(inputs=Inputs(features=inputs), outputs=Outputs(features=[output]))


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
    """Extract the parameter columns of an ask() result into our `Candidate` type."""
    return [
        Candidate(params={p.name: _cast(p, row[p.name]) for p in problem.parameters})
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
