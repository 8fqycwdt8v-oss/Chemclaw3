"""Read an optimization series as a *sequence* rather than a set (D-162).

`memory.optimization` groups runs by DRFP similarity, which has no time axis. This module adds
the two deterministic facts that make a series legible: the order runs were performed in, and
what differs from the run before each one. Both are read straight off the record. The judgment
(which change was the lever) belongs to the `experiment-progression` skill.

Causality is deliberately not derived: `performed_at` proves B came after A, not that B was run
because of A, so a `follows` edge is never minted from two dates.
"""

from datetime import date

from pydantic import BaseModel

from chemclaw.core.reagents import display_name, resolve_compound_name
from chemclaw.ingest.eln.ord import DateSource, OrdReaction, Role, RoleSpecies

# The roles diffed between consecutive runs: `RoleSpecies`' fields, so this list and the stored
# species projection cannot diverge.
DIFFED_ROLES: tuple[Role, ...] = tuple(Role(name) for name in RoleSpecies.model_fields)


class ConditionChange(BaseModel):
    """One condition that differs between a run and the run performed before it.

    `before`/`after` are rendered strings rather than typed values because the variables are not
    of one type — a temperature is a number, a solvent is a set of species — and every consumer
    (the campaign table, the skill reading it) wants them as text. "—" means the condition was
    absent on that side: unrecorded for a number, or nothing of that role for a species set.
    """

    variable: str
    before: str
    after: str

    def describe(self) -> str:
        """One-line rendering for a table cell or a sentence: `solvent DMF → 2-MeTHF`."""
        return f"{self.variable} {self.before} → {self.after}"


class ProgressionStep(BaseModel):
    """One run in the series, with what it changed relative to its predecessor.

    `changes` is empty for the first run (nothing precedes it) *and* for a run that repeats its
    predecessor's conditions exactly — two different facts that the campaign note distinguishes,
    because an intentional repeat is a reproducibility check and worth seeing as one.
    """

    reaction_id: str
    performed_at: date | None
    # Lets `ordering_caveat` weaken its sentence for a series dated from entry timestamps.
    date_source: DateSource = "stated"
    changes: list[ConditionChange]


class Progression(BaseModel):
    """A series of runs in the order they were performed, each with its delta.

    `is_timeline()` is the honesty check the campaign note prints: with no dates on the record
    the ordering is a stable id listing and nothing more, and a reader must not take the deltas
    as "what was tried next".
    """

    steps: list[ProgressionStep]

    def is_timeline(self) -> bool:
        """True when every run carries a date, so the order is genuinely the order run."""
        return bool(self.steps) and all(step.performed_at is not None for step in self.steps)

    def undated(self) -> list[str]:
        """The ids with no `performed_at`, in listing order — the runs with no place in time."""
        return [step.reaction_id for step in self.steps if step.performed_at is None]

    def entry_dated(self) -> list[str]:
        """The ids whose date is the entry's write time rather than a stated experiment date.

        These are ordered by when the records were written, which the reader has to be told.
        """
        return [
            step.reaction_id
            for step in self.steps
            if step.performed_at is not None and step.date_source == "entry"
        ]


def order_chronologically(reactions: list[OrdReaction]) -> list[OrdReaction]:
    """Sort runs by the date they were performed, undated ones last, ties broken by id.

    Total and deterministic, so re-synthesis of the same runs rewrites the same note. Undated runs
    go last: an unknown date is not "long ago".
    """
    return sorted(
        reactions,
        key=lambda r: (r.performed_at is None, r.performed_at or date.min, r.reaction_id),
    )


def progression(reactions: list[OrdReaction]) -> Progression:
    """Order the runs and name what changed at each step.

    Each run is diffed against the one immediately before it in time, not against a baseline.
    """
    ordered = order_chronologically(reactions)
    return Progression(
        steps=[
            ProgressionStep(
                reaction_id=run.reaction_id,
                performed_at=run.performed_at,
                date_source=run.date_source,
                changes=[] if previous is None else changes_between(previous, run),
            )
            for previous, run in zip([None, *ordered], ordered, strict=False)
        ]
    )


# An optional scalar, where `None` means "nobody wrote it down". Deliberately not a species set:
# an empty set is an answer, not a gap.
Recorded = float | str | None


def both_recorded(before: Recorded, after: Recorded) -> bool:
    """Whether a field was recorded on *both* sides, the precondition for diffing it.

    A field present on one side only differs in what was recorded, not in what was done, so a diff
    against absent would render a change nobody made. Absent is `None`, empty or whitespace; `0.0`
    is recorded.

    Applies to optional scalars only. Species sets are exempt: an empty `reagent` set states the run
    used no reagent, a real change.
    """
    return all(_recorded(value) for value in (before, after))


def _recorded(value: Recorded) -> bool:
    """Whether one side carries a value at all — `None` and blank text do not; `0.0` does."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def changes_between(previous: OrdReaction, current: OrdReaction) -> list[ConditionChange]:
    """The recorded conditions that differ between two runs, in a stable order.

    Covers the two headline setpoints and the species set of each non-product role. Amounts are
    out: they are optional on `Component` and often absent, so they would report spurious changes.
    """
    changes = [
        change
        for change in (
            number_change("temperature", previous.temperature_c, current.temperature_c, "°C"),
            number_change("time", previous.time_h, current.time_h, "h"),
        )
        if change is not None
    ]
    changes.extend(
        change
        for role in DIFFED_ROLES
        if (change := species_change(role, previous.species(role), current.species(role)))
        is not None
    )
    return changes


def number_change(
    variable: str, before: float | None, after: float | None, unit: str
) -> ConditionChange | None:
    """A setpoint change, or None when the two runs agree (including both being unrecorded).

    Public so the turn-time condenser renders setpoint moves with the same rule. A setpoint one side
    did not record is not a move (see `both_recorded`).
    """
    if not both_recorded(before, after) or before == after:
        return None
    return ConditionChange(
        variable=variable,
        before=_quantity(before, unit),
        after=_quantity(after, unit),
    )


def canonical_condition(species: str) -> str:
    """Fold a condition species to one canonical token.

    `DMF`, `N,N-dimethylformamide` and `CN(C)C=O` resolve to one token through
    `chemclaw.core.reagents`, so a campaign is not split by spelling. An unrecognised species folds
    to its trimmed, lowercased form rather than being dropped: it is still a real condition.
    """
    match = resolve_compound_name(species)
    return match.smiles if match is not None else species.strip().lower()


def text_change(variable: str, before: str | None, after: str | None) -> ConditionChange | None:
    """A change in a condition the record only carries as words, or None when they agree.

    The condenser's counterpart to `species_change` for a solvent read out of prose: both sides are
    resolved through `canonical_condition`, so two spellings of one solvent agree. What is displayed
    is what was written; the fold decides only whether anything moved. A side with no words is not a
    swap (see `both_recorded`).
    """
    if not both_recorded(before, after):
        return None
    if canonical_condition(before or "") == canonical_condition(after or ""):
        return None
    return ConditionChange(variable=variable, before=before or "—", after=after or "—")


def species_change(
    role: Role, before: frozenset[str], after: frozenset[str]
) -> ConditionChange | None:
    """The change in one role's species set, or None when the same structures are present.

    Public so the turn-time condenser diffs stored projections with the same rule. Reported as what
    went out -> what came in, with structural identity (canonical SMILES), so a respelling cannot
    fabricate a change.

    `both_recorded` deliberately does not apply: an empty set beside a full one means the run used
    none of that role, a real change. A record with no projection at all is the caller's to skip.
    """
    if before == after:
        return None
    return ConditionChange(
        variable=role.value,
        before=_species_label(before - after),
        after=_species_label(after - before),
    )


def _species_label(structures: frozenset[str]) -> str:
    """Name a set of structures for a human: known reagents by name, the rest by SMILES."""
    if not structures:
        return "—"
    return ", ".join(sorted(display_name(s) or s for s in structures))


def _quantity(value: float | None, unit: str) -> str:
    """A setpoint with its unit, or "—" when the run did not record it."""
    return "—" if value is None else f"{value:g} {unit}"
