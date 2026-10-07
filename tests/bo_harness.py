"""The BO drivers the suite needs and `src/` does not have: an ask/tell loop and a library problem.

They drive the production engine (`science.bo.engine`) and problem types for the convergence
checks, the Reizman quality bar and the campaign-identity fixtures. The campaign that ships is the
Temporal `BoCampaignWorkflow` (`connectors/bo/workflows.py`); nothing in production uses this loop.
"""

import asyncio
from collections.abc import Awaitable, Callable

from chemclaw.core.chem import require_canonical_smiles
from chemclaw.science.bo.engine import initial_candidates, propose_candidates
from chemclaw.science.bo.objectives import MOLECULE_KEY
from chemclaw.science.bo.problem import (
    MIN_SEED_OBSERVATIONS,
    CampaignResult,
    Candidate,
    CategoricalParameter,
    Observation,
    OptimizationProblem,
    ParamValue,
    best_of,
    discrete_candidate_count,
    require_problem_yields_one_best_point,
    require_rounds_within_ceiling,
    space_exhausted,
)
from chemclaw.science.bo.problem import (
    Objective as ObjectiveSpec,
)

# Evaluate a candidate's parameters to its objective value.
Evaluate = Callable[[dict[str, ParamValue]], Awaitable[float]]


def molecule_library_problem(smiles: list[str]) -> OptimizationProblem:
    """Build a candidate-set problem: pick the most soluble molecule from a library.

    The `solubility_max` objective reads its candidate from `params[MOLECULE_KEY]`, so the problem
    declares a categorical `molecule` parameter whose levels are SMILES. Every entry is
    canonicalized: an unparseable SMILES raises `InvalidSmilesError`, and duplicate spellings
    collapse to one level.
    """
    library = list(dict.fromkeys(require_canonical_smiles(entry) for entry in smiles))
    return OptimizationProblem(
        parameters=[CategoricalParameter(name=MOLECULE_KEY, categories=library)],
        objectives=[ObjectiveSpec(name="log_s", direction="maximize")],
    )


async def _evaluate(
    candidates: list[Candidate], evaluate: Evaluate, provenance: str
) -> list[Observation]:
    """Evaluate each candidate into an observation."""
    observations = []
    for candidate in candidates:
        value = await evaluate(candidate.params)
        observations.append(
            Observation(
                params=candidate.params,
                value=value,
                provenance=provenance,
                surrogate_sd=candidate.predicted_sd,
            )
        )
    return observations


async def optimize(
    problem: OptimizationProblem,
    evaluate: Evaluate,
    *,
    n_initial: int = 5,
    n_rounds: int = 10,
    batch: int = 1,
    provenance: str = "predicted",
    seed: int | None = None,
) -> CampaignResult:
    """Run a BO campaign in-process and return the best observation plus history.

    Seed with space-filling points, evaluate, then repeatedly propose → evaluate → tell. The two
    BoFire calls run in `asyncio.to_thread`, as on the production path, so the loop does not stall.

    Args:
        problem: What to optimize.
        evaluate: Async objective evaluation for one candidate's parameters.
        n_initial: Space-filling seed points; at least `MIN_SEED_OBSERVATIONS`.
        n_rounds: Model-guided rounds after seeding. Bounded by `bo_max_rounds`.
        batch: Candidates proposed (and evaluated) per round.
        provenance: Recorded on each observation (e.g. "predicted" vs "measured").
        seed: Per-campaign RNG seed for replicate runs; None uses the config default.

    Returns:
        The best observation found and the ordered evaluation history.
    """
    if n_initial < MIN_SEED_OBSERVATIONS:
        raise ValueError(
            f"n_initial must be >= {MIN_SEED_OBSERVATIONS}: the surrogate cannot fit on fewer"
        )
    require_rounds_within_ceiling(n_rounds)
    # Checked before any evaluation: a trade-off has no single best point, and finding that out
    # after the whole budget is spent would waste it.
    require_problem_yields_one_best_point(problem)
    seeds = await asyncio.to_thread(initial_candidates, problem, n_initial, seed)
    history = await _evaluate(seeds, evaluate, provenance)
    space = discrete_candidate_count(problem)
    for _ in range(n_rounds):
        # A purely discrete space can be exhausted: once too few distinct candidates
        # remain to propose a full batch, stop rather than crash inside BoFire.
        if space_exhausted(problem, space, history, batch):
            break
        proposed = await asyncio.to_thread(propose_candidates, problem, history, batch, seed)
        history.extend(await _evaluate(proposed, evaluate, provenance))
    return CampaignResult(best=best_of(problem, history), history=history)
