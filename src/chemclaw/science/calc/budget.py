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

__all__ = ["estimate_units", "require_within_budget"]

# What one member of an ensemble costs downstream, in remote primitives, at each refinement depth.
# `quick` is the search only; `standard` adds a relaxation per member; `thorough` adds its Hessian.
# These are the three `ReactionLevel` values the composites already take, so a caller reasoning
# about cost and a caller running the calculation name the same thing.
_PER_MEMBER: dict[str, int] = {"quick": 0, "standard": 1, "thorough": 2}

# One CREST search, plus the embed that seeds it. The search dominates by orders of magnitude, but
# it is counted as one unit like everything else: this budget bounds *fan-out*, and pretending a
# search is worth fifty single points would make the ceiling a cost model, which is the thing
# `connectors/calc/workflows.py` deleted for being wrong in both directions.
_PER_SPECIES_SEARCH = 2


def estimate_units(
    species: int, *, members_each: int = 0, level: str = "standard", searched: bool = True
) -> int:
    """How many remote primitives a fan-out over `species` will ask for.

    Args:
        species: How many distinct molecules the fan-out covers — tautomers, microstates,
            stereoisomers, or the two fragments of each bond in a survey.
        members_each: How many ensemble members each species is refined over; 0 for a species
            treated as a single geometry.
        level: `quick`, `standard` or `thorough` — the same vocabulary the reaction composites take.
        searched: Whether each species costs a CREST search.

    Returns:
        The number of remote calls, which is what the ceiling is expressed in.
    """
    per_species = _PER_SPECIES_SEARCH if searched else 1
    per_species += max(members_each, 1) * _PER_MEMBER.get(level, 1)
    return species * per_species


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
