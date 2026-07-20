"""Named BO objectives (plan step 1d.4).

A Temporal workflow cannot carry a Python callable across its boundary, so a
durable campaign references its objective by name and the evaluate activity
resolves it here. This registry is the generic-dispatch point that justifies a
lookup table (Rule of Three): the durable campaign resolves by name today, and
calculator-backed objectives will register here next. Objectives are built lazily
and cached per process, so an expensive setup (e.g. fitting a surrogate) happens
once per worker.
"""

from collections.abc import Awaitable, Callable
from functools import cache

from bo.benchmarks.reizman_suzuki import load_benchmark
from bo.problem import ParamValue

Objective = Callable[[dict[str, ParamValue]], Awaitable[float]]


@cache
def _reizman_suzuki() -> Objective:
    """The Reizman Suzuki yield objective (surrogate fitted once per process)."""
    _, objective = load_benchmark()
    return objective


# Name → factory. Factories are cached, so lookups are cheap after the first.
_REGISTRY: dict[str, Callable[[], Objective]] = {
    "reizman_suzuki": _reizman_suzuki,
}


def get_objective(name: str) -> Objective:
    """Resolve a named objective, or raise with the known names (gate G4)."""
    factory = _REGISTRY.get(name)
    if factory is None:
        raise ValueError(f"unknown objective {name!r}; known: {sorted(_REGISTRY)}")
    return factory()
