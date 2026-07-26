"""Named BO objectives (plan steps 1d.3, 1d.4).

A Temporal workflow cannot carry a Python callable across its boundary, so a
durable campaign references its objective by name and the evaluate activity
resolves it here. This registry is the generic-dispatch point that justifies a
lookup table (Rule of Three): the durable campaign resolves by name, and a
calculator-backed objective (1d.3) registers alongside the reaction benchmark.
Objectives are built lazily and cached per process where construction is
expensive (e.g. fitting a surrogate).
"""

from collections.abc import Awaitable, Callable
from functools import cache

from bo.benchmarks.reizman_suzuki import load_benchmark
from bo.problem import (
    CategoricalParameter,
    OptimizationProblem,
    ParamValue,
)
from bo.problem import (
    Objective as ObjectiveSpec,
)
from calc.postgres_store import PostgresStore
from calc.solubility import SolubilityInput, run_cached_solubility
from calc.store import ResultStore
from chemclaw.chem import require_canonical_smiles

Objective = Callable[[dict[str, ParamValue]], Awaitable[float]]

# The parameter key a molecule-scoring objective reads its candidate from.
MOLECULE_KEY = "molecule"


def molecule_library_problem(smiles: list[str]) -> OptimizationProblem:
    """Build a candidate-set problem: pick the most soluble molecule from a library.

    The categorical `molecule` parameter ranges over the given SMILES and the paired
    solubility objective is maximized. BoFire optimizes this discrete space by
    exhaustive acquisition search, so the value of BO is finding a top molecule
    *without* evaluating the whole library. The evaluation budget
    (`n_initial + n_rounds * batch`) must stay below the library size, else the
    unique-candidate pool is exhausted.

    Every entry is canonicalized up front: an unparseable SMILES raises
    `InvalidSmilesError` naming it *before* any budget is spent (otherwise the
    campaign would fail non-retryably only when the bad molecule is finally
    proposed, discarding all completed rounds), and duplicate spellings of one
    molecule collapse so the discrete-space accounting counts real candidates.
    """
    library = list(dict.fromkeys(require_canonical_smiles(entry) for entry in smiles))
    return OptimizationProblem(
        parameters=[CategoricalParameter(name=MOLECULE_KEY, categories=library)],
        objective=ObjectiveSpec(name="log_s", direction="maximize"),
    )


def solubility_objective(store: ResultStore) -> Objective:
    """A BO objective that scores a candidate molecule by cached predicted log S.

    This is the calculator-backed objective of plan step 1d.3: each evaluation runs
    the solubility calculator through the store, so a molecule revisited during a
    search is served from the store and never recomputed (D-011). The store is
    injected so the objective is testable without a database. The candidate molecule
    is read from `params[MOLECULE_KEY]`; pair it with `molecule_library_problem`.
    """

    async def evaluate(params: dict[str, ParamValue]) -> float:
        result, _ = await run_cached_solubility(
            store, SolubilityInput(smiles=str(params[MOLECULE_KEY]))
        )
        return result.log_s_mol_per_l

    return evaluate


@cache
def _reizman_suzuki() -> Objective:
    """The Reizman Suzuki yield objective (surrogate fitted once per process)."""
    _, objective = load_benchmark()
    return objective


def _solubility_max() -> Objective:
    """Calculator-backed solubility objective on the production (Postgres) store."""
    return solubility_objective(PostgresStore())


# Name → factory. Factories are cached where construction is expensive.
_REGISTRY: dict[str, Callable[[], Objective]] = {
    "reizman_suzuki": _reizman_suzuki,
    "solubility_max": _solubility_max,
}


def get_objective(name: str) -> Objective:
    """Resolve a named objective, or raise with the known names (gate G4)."""
    factory = _REGISTRY.get(name)
    if factory is None:
        raise ValueError(f"unknown objective {name!r}; known: {sorted(_REGISTRY)}")
    return factory()
