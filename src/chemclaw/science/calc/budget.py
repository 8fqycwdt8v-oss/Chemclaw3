"""Counting the calculations a fan-out will make, before it makes the first one.

A preflight, not a clock: composites multiply remote primitives per species, and a timeout fires
only after the time is spent. Refusals are `ValueError`s naming the count, non-retryable under
`durable/publish.py::BAD_DATA_RETRY`. `require_hessian_affordable` counts atoms instead.
"""

from chemclaw.core.config import settings
from chemclaw.science.calc.models import ReactionLevel

__all__ = [
    "estimate_units",
    "require_hessian_affordable",
    "require_within_budget",
    "rotation_units",
]

# Remote primitives per species at each level, read off `_species_energy`:
#
#   quick     embed + relax                                 = 2
#   standard  embed + relax + hessian                       = 3
#   thorough  embed + search + relax + hessian (+ the search's own embed)
_PER_SPECIES: dict[str, int] = {"quick": 2, "standard": 3, "thorough": 5}


def estimate_units(species: int, *, level: ReactionLevel = "standard") -> int:
    """How many remote primitives a fan-out over `species` molecules will ask for at `level`."""
    return species * _PER_SPECIES[level]


def rotation_units(points: int, passes: int, *, level: ReactionLevel = "quick") -> int:
    """How many remote primitives a rotational profile will ask for, counted before anything runs.

    Per `compose.rotation_profile`: every level runs one constrained optimization per coarse point,
    refinement points per maximum and one released optimization per well; `standard` adds a Hessian
    and re-optimization per well; `thorough` also a constrained optimization and Hessian per pass.
    Wells are counted at `passes`, their upper bound.
    """
    per_well = {"quick": 1, "standard": 3, "thorough": 3}[level]
    per_pass = 2 if level == "thorough" else 0
    return points + passes * (per_well + per_pass)


def require_within_budget(units: int, what: str) -> None:
    """Refuse a fan-out larger than the configured ceiling, naming the count.

    Raises:
        ValueError: the request needs more primitives than `calc_max_primitive_calls` allows.
    """
    ceiling = settings.calc_max_primitive_calls
    if units <= ceiling:
        return
    raise ValueError(
        f"{what} would run {units} calculations, over the {ceiling} this deployment allows for one "
        "job. Narrow the species set, lower the refinement level, or raise "
        "CHEMCLAW_CALC_MAX_PRIMITIVE_CALLS if the cost is understood — a conformer search is "
        "minutes of saturated CPU each and they do not run in parallel here."
    )


def require_hessian_affordable(atom_count: int, what: str) -> None:
    """Refuse a Hessian on a molecule too large for one, naming the routes this system has.

    The message points at `level="quick"` (no Hessian) or a truncated model system.

    Raises:
        ValueError: the molecule has more atoms than `calc_hessian_max_atoms` allows
            (non-retryable).
    """
    ceiling = settings.calc_hessian_max_atoms
    if atom_count <= ceiling:
        return
    raise ValueError(
        f"{what} needs second derivatives on {atom_count} atoms, over the {ceiling} this "
        f"deployment allows: a Hessian costs 6N single points, so this one is {6 * atom_count} of "
        'them. There is no larger-molecule route to escalate to — ask at level="quick", which '
        "differences electronic energies and takes no Hessian, or put the question to a truncated "
        "model system, or raise CHEMCLAW_CALC_HESSIAN_MAX_ATOMS if the cost is understood."
    )
