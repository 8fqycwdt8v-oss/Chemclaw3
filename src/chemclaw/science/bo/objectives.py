"""Named BO objectives.

A Temporal workflow cannot carry a callable, so a durable campaign names its objective and the
evaluate activity resolves it here. Objectives are built lazily and cached per process where
construction is expensive (e.g. fitting a surrogate).
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import cache

from chemclaw.science.bo.benchmarks.reizman_suzuki import load_benchmark
from chemclaw.science.bo.problem import ParamValue

Objective = Callable[[dict[str, ParamValue]], Awaitable[float]]

# How a calculator-backed objective obtains one molecule's predicted log S. Injected for the
# reason `featurize.PropertiesFor` is; bound in `connectors/bo/calculators.py`.
LogSFor = Callable[[str], Awaitable[float]]

# The parameter key a molecule-scoring objective reads its candidate from.
MOLECULE_KEY = "molecule"


def solubility_objective(log_s_for: LogSFor) -> Objective:
    """A BO objective that scores a candidate molecule by cached predicted log S.

    Each evaluation goes through the calculation store, so a revisited molecule is never recomputed.
    The candidate is read from `params[MOLECULE_KEY]`, so a campaign naming this objective declares
    a categorical of that name whose levels are SMILES.
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

    `direction` and `requires` are properties of the objective, declared here so
    `require_campaign_startable` can refuse a mismatched direction (which would recommend the worst
    point) or a decision space missing a parameter the function reads (which would fail hours in
    with a `KeyError`) at launch.
    """

    factory: Callable[[LogSFor], Objective]
    #: `"maximize"` or `"minimize"` — the same vocabulary `problem.Objective.direction` uses,
    #: because the whole point is that the two are compared as equals.
    direction: str
    #: The parameter names this objective reads out of a candidate's `params`. Never empty, or the
    #: launch check would pass vacuously.
    requires: tuple[str, ...]


#: The objective that is not a function: results come back from the bench. Deliberately absent
#: from `_REGISTRY`; the durable workflow suspends on a wait instead (`durable/awaiting.py`), and
#: `is_measured` recognises the name.
MEASURED_OBJECTIVE = "measured"


def is_measured(name: str) -> bool:
    """Whether this campaign's values come from people rather than from a registered function."""
    return name == MEASURED_OBJECTIVE


# Name → objective. Every factory takes the calculator seam, even where unused, so adding an
# objective is a row here rather than a change to `get_objective`.
_REGISTRY: dict[str, RegisteredObjective] = {
    # Reaction yield: more is better. The four names are the emulator's own encoding order
    # (`benchmarks.reizman_suzuki.YieldSurrogate._encode`), which is what a candidate must supply.
    "reizman_suzuki": RegisteredObjective(
        lambda _log_s_for: _reizman_suzuki(),
        "maximize",
        ("catalyst", "t_res", "temperature", "catalyst_loading"),
    ),
    # Predicted log S. The name says `_max` and the direction says it again, checkably.
    "solubility_max": RegisteredObjective(solubility_objective, "maximize", (MOLECULE_KEY,)),
}


def get_objective(name: str, log_s_for: LogSFor) -> Objective:
    """Resolve a named objective, or raise with the known names.

    `log_s_for` is the calculator a calculator-backed objective evaluates through (see `LogSFor`).
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


def registered_parameters(name: str) -> tuple[str, ...]:
    """The parameter names this registered objective reads, or raise with the known names.

    Answers without building the objective, so a refused launch costs nothing.
    """
    registered = _REGISTRY.get(name)
    if registered is None:
        raise ValueError(f"unknown objective {name!r}; known: {sorted(_REGISTRY)}")
    return registered.requires


def registered_direction(name: str) -> str:
    """Which way this registered objective is better, or raise with the known names.

    Answers without building the objective (which may need a calculator client or a fitted
    surrogate), so a refused launch costs nothing.
    """
    registered = _REGISTRY.get(name)
    if registered is None:
        raise ValueError(f"unknown objective {name!r}; known: {sorted(_REGISTRY)}")
    return registered.direction
