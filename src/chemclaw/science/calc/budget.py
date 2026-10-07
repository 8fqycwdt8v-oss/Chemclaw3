"""Counting the calculations a fan-out will make, before it makes the first one.

Composites multiply: a species ranking is a search, an optimization and a Hessian *per species*, so
one tool call can be hours of CPU. The fence is a preflight, not a clock: a timeout fires only after
the time is spent, and raising the shared `xtb_job_timeout_seconds` would weaken failure detection
for every other job.

A unit is one remote primitive, which a composite can count before it starts. The refusal is a
`ValueError` naming the count, non-retryable under `durable/publish.py::BAD_DATA_RETRY`, so an
over-budget request fails at once. `require_hessian_affordable` counts atoms instead, since a
Hessian's runaway is the size of one molecule.
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
    """How many remote primitives a fan-out over `species` will ask for.

    Args:
        species: How many distinct molecules the fan-out covers — tautomers, microstates,
            stereoisomers, ensemble members, or the parent-and-fragments of each bond in a survey.
        level: `quick`, `standard` or `thorough`, as the reaction composites take it.

    Returns:
        The number of remote calls, which is what the ceiling is expressed in.
    """
    return species * _PER_SPECIES[level]


def rotation_units(points: int, passes: int, *, level: ReactionLevel = "quick") -> int:
    """How many remote primitives a rotational profile will ask for.

    Counted from the shape of the request, before anything runs, so `passes` is the most maxima a
    period can hold at this step. The ladder, from `connectors/calc/compose.py::rotation_profile`:

        every level    one constrained optimization per coarse point, plus the refinement points
                       around each maximum, plus one released optimization per well
        standard       a Hessian and a re-optimization per well
        thorough       also a constrained optimization and a Hessian per pass

    A period holds at most as many wells as maxima, so wells are counted at `passes`.
    """
    per_well = {"quick": 1, "standard": 3, "thorough": 3}[level]
    per_pass = 2 if level == "thorough" else 0
    return points + passes * (per_well + per_pass)


def require_within_budget(units: int, what: str) -> None:
    """Refuse a fan-out larger than the configured ceiling, naming the count.

    The count tells the caller whether to ask at a cheaper level or narrow the enumeration first.

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

    Counted in atoms: a Hessian is 6N single points on one molecule. The message names what this
    system offers instead — `level="quick"` (electronic energies, no Hessian) or a truncated model
    system — because every path here uses the same `compute_hessian` primitive under the same
    ceiling.

    Raises:
        ValueError: the molecule has more atoms than `calc_hessian_max_atoms` allows. Non-retryable
            by `durable/publish.py::BAD_DATA_RETRY`.
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
