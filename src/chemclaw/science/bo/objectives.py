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

from chemclaw.core.chem import require_canonical_smiles
from chemclaw.science.bo.benchmarks.reizman_suzuki import load_benchmark
from chemclaw.science.bo.problem import (
    CategoricalParameter,
    OptimizationProblem,
    ParamValue,
)
from chemclaw.science.bo.problem import (
    Objective as ObjectiveSpec,
)

Objective = Callable[[dict[str, ParamValue]], Awaitable[float]]

# How a calculator-backed objective obtains one molecule's predicted log S.
#
# **Injected rather than imported**, for the reason `science/bo/featurize.py` states at length: the
# solubility model moved to `Chemclaw3-mcp` and the client that reaches it lives one package above
# this one, which `science` may not import (`tests/test_layering.py`). The binding lives in
# `connectors/bo/calculators.py`. The property this objective always advertised is now the client's:
# a molecule revisited during a search is served from the calculation store and never recomputed
# (D-011).
LogSFor = Callable[[str], Awaitable[float]]

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
        objectives=[ObjectiveSpec(name="log_s", direction="maximize")],
    )


def solubility_objective(log_s_for: LogSFor) -> Objective:
    """A BO objective that scores a candidate molecule by cached predicted log S.

    This is the calculator-backed objective of plan step 1d.3: each evaluation asks the calculator
    through the calculation store, so a molecule revisited during a search is served from the store
    and never recomputed (D-011). The scorer is injected so the objective is testable without a
    database or a server. The candidate molecule is read from `params[MOLECULE_KEY]`; pair it with
    `molecule_library_problem`.
    """

    async def evaluate(params: dict[str, ParamValue]) -> float:
        return await log_s_for(str(params[MOLECULE_KEY]))

    return evaluate


@cache
def _reizman_suzuki() -> Objective:
    """The Reizman Suzuki yield objective (surrogate fitted once per process)."""
    _, objective = load_benchmark()
    return objective


# Name → factory. Every factory takes the calculator seam, so the registry has one shape even
# though the benchmark objective — a surrogate fitted from a bundled dataset — needs no calculator
# at all. A per-entry signature would push the branch into `get_objective` and make adding a
# calculator-backed objective a change to the resolver rather than a row here.
_REGISTRY: dict[str, Callable[[LogSFor], Objective]] = {
    "reizman_suzuki": lambda _log_s_for: _reizman_suzuki(),
    "solubility_max": solubility_objective,
}


def get_objective(name: str, log_s_for: LogSFor) -> Objective:
    """Resolve a named objective, or raise with the known names (gate G4).

    `log_s_for` is the calculator a calculator-backed objective evaluates through; see `LogSFor`
    for why it arrives as an argument rather than as an import.
    """
    factory = _REGISTRY.get(name)
    if factory is None:
        raise ValueError(f"unknown objective {name!r}; known: {sorted(_REGISTRY)}")
    return factory(log_s_for)
