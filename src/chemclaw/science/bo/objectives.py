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
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class RegisteredObjective:
    """A named objective a durable campaign can run: how to build it, and which way is better.

    **The direction is here because it is a property of the objective, not of the request.** A
    campaign carries the direction twice — once in `CampaignSpec.problem.objectives[0].direction`,
    which is what BoFire optimizes, and once implicitly in what the registered function *means* —
    and nothing compared them. A caller pairing `solubility_max` with `direction="minimize"` got a
    campaign that ran to completion, wrote a `bo-candidate` note, and recommended the **least**
    soluble molecule in the library as its best point. Every number in it is correct; the
    recommendation is inverted, which is the class of wrongness a reviewer is least able to catch
    from the note alone.

    So the registry states it, `require_campaign_startable` checks it, and the mismatch becomes a
    refusal at launch instead of a plausible answer hours later.
    """

    factory: Callable[[LogSFor], Objective]
    #: `"maximize"` or `"minimize"` — the same vocabulary `ObjectiveSpec.direction` uses, because
    #: the whole point is that the two are compared as equals.
    direction: str


#: The objective that is not a function: the numbers come back from a bench, not from a process.
#:
#: **This is the name that makes a real screening campaign expressible.** Every entry in the
#: registry below is `Callable[..., Awaitable[float]]`, which is exactly what a *simulated* campaign
#: needs and exactly what a chemist's campaign is not — BO's value to a process chemist is proposing
#: eight conditions, waiting a week for the plates, and proposing eight more. The registry cannot
#: hold that, because there is no function to register; the durable workflow suspends on a wait
#: instead (`durable/awaiting.py`), so this name is deliberately absent from `_REGISTRY` and is
#: recognised by `is_measured` rather than resolved by `get_objective`.
MEASURED_OBJECTIVE = "measured"


def is_measured(name: str) -> bool:
    """Whether this campaign's values come from people rather than from a registered function."""
    return name == MEASURED_OBJECTIVE


# Name → the objective it stands for. Every factory takes the calculator seam, so the registry has
# one shape even though the benchmark objective — a surrogate fitted from a bundled dataset — needs
# no calculator at all. A per-entry signature would push the branch into `get_objective` and make
# adding a calculator-backed objective a change to the resolver rather than a row here.
_REGISTRY: dict[str, RegisteredObjective] = {
    # Reaction yield: more is better.
    "reizman_suzuki": RegisteredObjective(lambda _log_s_for: _reizman_suzuki(), "maximize"),
    # Predicted log S. The name says `_max` and the direction says it again, checkably.
    "solubility_max": RegisteredObjective(solubility_objective, "maximize"),
}


def get_objective(name: str, log_s_for: LogSFor) -> Objective:
    """Resolve a named objective, or raise with the known names (gate G4).

    `log_s_for` is the calculator a calculator-backed objective evaluates through; see `LogSFor`
    for why it arrives as an argument rather than as an import.
    """
    if is_measured(name):
        raise ValueError(
            f"objective {name!r} is measured rather than computed, so it has no function to "
            "resolve; a campaign naming it suspends on a durable wait instead of evaluating "
            "(chemclaw.durable.awaiting)"
        )
    registered = _REGISTRY.get(name)
    if registered is None:
        raise ValueError(f"unknown objective {name!r}; known: {sorted(_REGISTRY)}")
    return registered.factory(log_s_for)


def registered_direction(name: str) -> str:
    """Which way this registered objective is better, or raise with the known names.

    Split from `get_objective` because the caller that needs it — the launch-time precondition —
    must answer the question *without building the objective*: `solubility_objective` closes over a
    calculator client the precondition has no business constructing, and `_reizman_suzuki` fits a
    surrogate. A campaign refused for a direction mismatch should cost nothing.
    """
    registered = _REGISTRY.get(name)
    if registered is None:
        raise ValueError(f"unknown objective {name!r}; known: {sorted(_REGISTRY)}")
    return registered.direction
