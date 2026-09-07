"""The BO drivers the suite needs and `src/` does not have: an ask/tell loop and a library problem.

Both used to live in `src/chemclaw/science/bo/` with **zero** callers there, which is the
`map_to_hpc_identity` shape this repository has deleted three times already — code that reads as
capability, is maintained as capability (one of these two had a 16-second event-loop stall measured
and fixed in it the day before it was found unreachable), and no configuration can enter.

They are not deleted, because they are not dead here: they drive the *production* engine
(`science.bo.engine`) and the production problem types, and three suites lean on them — the
convergence checks in `test_bo.py`, `test_reizman.py`'s beat-the-median quality bar, and the
campaign-identity fixtures.
`D-2026-08-27-a-bound-that-multiplies-and-a-record-that-survives-the-cancel` kept `optimize` in
`src/` for exactly that reason, and its objection was to *inlining* the loop into
three test files. One definition, in `tests/`, answers the objection without leaving an unenterable
path in the shipped package.

**What ships instead.** The durable, resumable campaign is the Temporal `BoCampaignWorkflow`
(`connectors/bo/workflows.py`), which reuses the same engine and the same `best_of` reducer and
threads the same two BoFire calls off its event loop (`connectors/bo/activities.py`). A campaign a
chemist runs goes down that path; nothing goes down this one.
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

    The shape a `solubility_max` campaign has to have — that objective reads its candidate from
    `params[MOLECULE_KEY]`, so a campaign naming it declares a categorical `molecule` parameter
    whose levels are SMILES. A chemist's `CampaignSpec` carries that shape directly; this builds it
    for the suites that need one without writing the same four lines out each time.

    Every entry is canonicalized up front: an unparseable SMILES raises `InvalidSmilesError` naming
    it, and duplicate spellings of one molecule collapse so the discrete-space accounting counts
    real candidates.
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
    BoFire calls cross `asyncio.to_thread` because a GP fit and its acquisition optimisation are
    pure synchronous CPU, the same reason `connectors/bo/activities.py` threads the identical pair
    on the path that ships — kept here so a suite driving this loop is not measuring a stall the
    production path does not have.

    Args:
        problem: What to optimize.
        evaluate: Async objective evaluation for one candidate's parameters.
        n_initial: Space-filling points to seed the surrogate before it can guide. Must be at least
            `MIN_SEED_OBSERVATIONS` (BoFire's fitting floor).
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
    # Checked here, not at the `best_of` call below: this loop returns a single best observation, so
    # a trade-off has no answer for it — and discovering that *after* n_initial + n_rounds*batch
    # evaluations would spend the whole budget to raise. Shared with `require_campaign_startable`
    # rather than restated, because this loop and the durable workflow need the same three things.
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
