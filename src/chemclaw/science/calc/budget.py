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

**One fence here counts atoms instead, and it is the same shape for a different runaway.** A Hessian
is 6N single points on a *single* molecule, so no fan-out count catches it — and the calculation
server, which does refuse above its own ceiling, answered with a route that no longer exists
(`require_hessian_affordable`). The unit differs; what does not is that the refusal names what to do
instead, before anything is spent.
"""

from chemclaw.core.config import settings
from chemclaw.science.calc.models import ReactionLevel

__all__ = [
    "estimate_units",
    "require_hessian_affordable",
    "require_within_budget",
    "rotation_units",
]

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


def rotation_units(points: int, passes: int, *, level: ReactionLevel = "quick") -> int:
    """How many remote primitives a rotational profile will ask for.

    Counted from the *shape of the request* rather than from what the profile turns out to contain,
    because a preflight has to count before the first calculation runs. `passes` is therefore the
    most maxima a period can hold at this step, not the number found.

    The ladder, read off `connectors/calc/compose.py::rotation_profile`:

        every level    one constrained optimization per coarse point, plus the refinement points
                       around each maximum, plus one released optimization per well
        standard       a Hessian and a re-optimization per well
        thorough       also a constrained optimization and a Hessian per pass

    A period can hold at most as many wells as maxima, so wells are counted at `passes`.
    """
    per_well = {"quick": 1, "standard": 3, "thorough": 3}[level]
    per_pass = 2 if level == "thorough" else 0
    return points + passes * (per_well + per_pass)


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


def require_hessian_affordable(atom_count: int, what: str) -> None:
    """Refuse a Hessian on a molecule too large for one, naming the routes this system has.

    The second fence in this module, and the only one counted in atoms rather than in calls: a
    Hessian is 6N single points on *one* molecule, so its runaway is the molecule and not the
    fan-out.

    **It exists for the message as much as for the refusal.** The calculation server has its own
    atom ceiling, and its refusal used to tell the model to "submit it through Chemclaw3's durable
    QM job path instead" — a route `D-2026-08-26-semiempirical-is-the-whole-tier` deleted, and one
    that would not have helped if it existed: every durable job here composes the *same*
    `compute_hessian` primitive under the same ceiling, so escalating a 200-atom Hessian to Temporal
    changes which process waits and nothing else. A false instruction handed to a model is worse
    than a bare refusal, because the model acts on it.

    So this side, which is the side that knows what this system offers, says it: `level="quick"`
    differences electronic energies and takes no Hessian at all, and a truncated model system is the
    chemistry answer to a molecule whose remote substituents cannot matter to the mode in question.
    Neither is a workaround — they are the two things a chemist does here.

    Raises:
        ValueError: the molecule has more atoms than `calc_hessian_max_atoms` allows. Non-retryable
            by `durable/publish.py::BAD_DATA_RETRY`, because the molecule will not get smaller.
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
