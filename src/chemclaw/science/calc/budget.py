"""Counting the calculations a fan-out will make, before it makes the first one.

Every composite added for the multi-step protocols multiplies: a species ranking is one CREST
search plus one optimization plus one Hessian *per species*, a bond-dissociation survey is one
reaction energy *per bond*. Against the measured anchors — a 33-atom conformer search is 1142 s,
one 76-atom optimization-plus-Hessian is minutes — a request that looks like one tool call is
routinely hours of saturated CPU, and `xtb_job_timeout_seconds` is four hours for *every* calc job.

**The fence is a preflight, not a clock.** Two reasons it has to count before it computes rather
than stopping when it has spent too long:

- A timeout that fires after three hours has already spent three hours. The work is not wasted —
  every completed primitive is in the D-011 cache and a retry resumes past it — but nobody was told
  at the point where they could have chosen a cheaper question.
- The timeout is one number shared by every job in the bundle. Raising it to fit the worst fan-out
  degrades failure detection for the two-second reaction that shares it, which is the trade
  `xtb_job_timeout_seconds` already documents refusing.

So the shape is `xtb_scan_max_points`': a configured ceiling on *units of work*, checked where the
work is composed, refusing with the count in the message so the caller can act on it. The refusal
is a `ValueError`, which `durable/publish.py::BAD_DATA_RETRY` treats as non-retryable — an
over-budget request fails in the first second rather than burning the activity budget three times.

**A unit is one remote primitive**, not one second. Duration depends on the molecule and this
module cannot see the molecule; the call count is exactly what a composite knows before it starts,
and it is the quantity that scales with the fan-out the caller chose.
"""

from chemclaw.core.config import settings
from chemclaw.science.calc.models import ReactionLevel

__all__ = ["estimate_units", "require_within_budget"]

# What one species costs, in remote primitives, at each level — read off `_species_energy` rather
# than guessed. That function is `embed` -> (thorough: `conformer_ensemble` -> lowest) -> `relax`
# -> (not quick: `hessian`), and `reaction_energy`'s own docstring states the same ladder:
#
#   quick     embed + relax                                 = 2
#   standard  embed + relax + hessian                       = 3
#   thorough  embed + search + relax + hessian (+ the search's own embed)
#
# The first version of this said `{"quick": 0, "standard": 1, "thorough": 2}` and commented that
# "`thorough` adds its Hessian". It does not — the Hessian is already paid at `standard`, and what
# `thorough` adds is a **CREST search**, the most expensive call in the system. So the fence
# under-counted at every level, in the one direction a fence must not.
_PER_SPECIES: dict[str, int] = {"quick": 2, "standard": 3, "thorough": 5}


def estimate_units(species: int, *, level: ReactionLevel = "standard") -> int:
    """How many remote primitives a fan-out over `species` will ask for.

    Args:
        species: How many distinct molecules the fan-out covers — tautomers, microstates,
            stereoisomers, the members of an ensemble, or the parent-and-fragments of each bond in
            a survey.
        level: `quick`, `standard` or `thorough` — the same vocabulary the reaction composites
            take. Typed as `ReactionLevel` rather than `str`, because the previous `.get(level, 1)`
            silently answered "standard" for a typo and handed back an estimate for a cheaper
            calculation than the caller asked for.

    Returns:
        The number of remote calls, which is what the ceiling is expressed in.
    """
    return species * _PER_SPECIES[level]


def require_within_budget(units: int, what: str) -> None:
    """Refuse a fan-out larger than the configured ceiling, naming the count.

    The message carries the number rather than saying "too large", because the caller's next move
    depends on it: eleven tautomers at `thorough` is a smaller question asked at a cheaper level,
    while two hundred is an enumeration that needs narrowing first. A refusal that does not say
    which is a refusal nobody can act on.

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
