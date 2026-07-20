"""BoFire adapter — the only module that touches BoFire (plan Phase 1d, D-012).

Maps our neutral `OptimizationProblem`/`Observation` types to BoFire's domain and
strategies, proposes candidates, and maps results back to our `Candidate` type.
Nothing BoFire leaks past this boundary (gate G6), so the engine could be swapped
without touching the campaign, agents, or skills.
"""

import pandas as pd
from bofire.data_models.domain.api import Domain, Inputs, Outputs
from bofire.data_models.features.api import ContinuousInput, ContinuousOutput
from bofire.data_models.objectives.api import MaximizeObjective, MinimizeObjective
from bofire.data_models.strategies.api import RandomStrategy, SoboStrategy
from bofire.strategies import api as strategies

from bo.problem import Candidate, Observation, OptimizationProblem


def _to_domain(problem: OptimizationProblem) -> Domain:
    """Translate our problem into a BoFire `Domain` (inputs + one objective output)."""
    inputs = [ContinuousInput(key=p.name, bounds=(p.lower, p.upper)) for p in problem.parameters]
    objective = (
        MinimizeObjective(w=1.0)
        if problem.objective.direction == "minimize"
        else MaximizeObjective(w=1.0)
    )
    output = ContinuousOutput(key=problem.objective.name, objective=objective)
    return Domain(inputs=Inputs(features=inputs), outputs=Outputs(features=[output]))


def _observations_to_frame(
    problem: OptimizationProblem, observations: list[Observation]
) -> pd.DataFrame:
    """Build the experiments dataframe BoFire's `tell` expects."""
    objective_key = problem.objective.name
    rows = []
    for obs in observations:
        row: dict[str, float] = dict(obs.params)
        row[objective_key] = obs.value
        row[f"valid_{objective_key}"] = 1
        rows.append(row)
    return pd.DataFrame(rows)


def _frame_to_candidates(problem: OptimizationProblem, frame: pd.DataFrame) -> list[Candidate]:
    """Extract the parameter columns of an ask() result into our `Candidate` type."""
    names = [p.name for p in problem.parameters]
    return [
        Candidate(params={name: float(row[name]) for name in names}) for _, row in frame.iterrows()
    ]


def initial_candidates(problem: OptimizationProblem, n: int) -> list[Candidate]:
    """Propose `n` space-filling starting points (random design, no model yet).

    Used to seed a campaign before any observations exist — a GP needs data before
    it can guide the search.
    """
    strategy = strategies.map(RandomStrategy(domain=_to_domain(problem)))
    return _frame_to_candidates(problem, strategy.ask(n))


def propose_candidates(
    problem: OptimizationProblem, observations: list[Observation], n: int = 1
) -> list[Candidate]:
    """Propose the next `n` candidates from past observations via SOBO.

    Requires at least one observation to fit the surrogate; call
    `initial_candidates` first to seed. Raises `ValueError` on empty observations
    rather than surfacing an opaque BoFire error (gate G4).
    """
    if not observations:
        raise ValueError("propose_candidates needs at least one observation; seed first")
    strategy = strategies.map(SoboStrategy(domain=_to_domain(problem)))
    strategy.tell(_observations_to_frame(problem, observations))
    return _frame_to_candidates(problem, strategy.ask(n))
