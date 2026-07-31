"""Read an optimization series as a *sequence* rather than a set (D-156).

`memory.optimization` answers "which runs are the same transformation"; it says nothing about
order, because DRFP similarity has no time axis and `cluster_by_similarity` returns ids sorted
lexically. That is the right answer for grouping and the wrong one for the way process
development actually happens: one technician, one step, one experiment a day for weeks, each
run chosen in response to yesterday's result. Handed those runs as an unordered table, the agent
could compare any two of them and could not see the trajectory — which variable was being walked,
what the last three days ruled out, what has not been touched since day one.

This module supplies the two deterministic facts that make a series legible: the order runs were
performed in, and, for each run, *what differs from the run before it*. Both are read straight
off the record — `performed_at` and the recorded conditions — so nothing here is inference. The
inference (which change was the lever, what to try next) is the `experiment-progression` skill's
judgment, layered on top exactly as `optimization-campaign-synthesis` sits on the comparative
table.

**What is deliberately not here: causality.** `performed_at` proves that run B came after run A.
It does not prove that B was run *because of* A, and the module never says it was — a `follows`
edge is minted by the agent (or a chemist) who can read the intent, never derived from two dates.
"""

from datetime import date

from pydantic import BaseModel

from chemclaw.core.chem import canonical_smiles
from chemclaw.core.reagents import display_name
from chemclaw.ingest.eln.ord import Component, OrdReaction, Role

# The roles whose species set is worth diffing between consecutive runs. `product` is excluded: a
# changed product is a different transformation, which is the grouping layer's business, not a
# condition the chemist turned.
_DIFFED_ROLES: tuple[Role, ...] = (Role.REACTANT, Role.REAGENT, Role.SOLVENT, Role.CATALYST)


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


def order_chronologically(reactions: list[OrdReaction]) -> list[OrdReaction]:
    """Sort runs by the date they were performed, undated ones last, ties broken by id.

    Total and deterministic, which matters because the result is rendered into a PR-gated note:
    the same set of runs must produce the same note or every re-synthesis is a spurious diff.
    Undated runs sort last rather than first — an unknown date is not "long ago", and putting
    them at the end keeps the dated prefix a clean timeline.
    """
    return sorted(
        reactions,
        key=lambda r: (r.performed_at is None, r.performed_at or date.min, r.reaction_id),
    )


def progression(reactions: list[OrdReaction]) -> Progression:
    """Order the runs and name what changed at each step.

    Each run is diffed against the one immediately before it in time — the comparison the
    chemist actually made — not against the first run or against a notional baseline.
    """
    ordered = order_chronologically(reactions)
    return Progression(
        steps=[
            ProgressionStep(
                reaction_id=run.reaction_id,
                performed_at=run.performed_at,
                changes=[] if previous is None else changes_between(previous, run),
            )
            for previous, run in zip([None, *ordered], ordered, strict=False)
        ]
    )


def changes_between(previous: OrdReaction, current: OrdReaction) -> list[ConditionChange]:
    """The recorded conditions that differ between two runs, in a stable order.

    Covers what an ELN reliably records and a chemist reliably turns: the two headline setpoints
    and the species set of each non-product role. Amounts (equivalents, loading) are deliberately
    out: they are optional on `Component` and frequently absent, so diffing them would report a
    change every time one run happened to record a mass and its neighbour did not.
    """
    changes = [
        change
        for change in (
            _number_change("temperature", previous.temperature_c, current.temperature_c, "°C"),
            _number_change("time", previous.time_h, current.time_h, "h"),
        )
        if change is not None
    ]
    changes.extend(
        change
        for role in _DIFFED_ROLES
        if (change := _species_change(role, previous, current)) is not None
    )
    return changes


def _number_change(
    variable: str, before: float | None, after: float | None, unit: str
) -> ConditionChange | None:
    """A setpoint change, or None when the two runs agree (including both being unrecorded)."""
    if before == after:
        return None
    return ConditionChange(
        variable=variable,
        before=_quantity(before, unit),
        after=_quantity(after, unit),
    )


def _species_change(
    role: Role, previous: OrdReaction, current: OrdReaction
) -> ConditionChange | None:
    """The change in one role's species set, or None when the same structures are present.

    Reported as *what went out* → *what came in*, not as the full set on each side: a run that
    swaps one of four reactants should read `reactant A → B`, not two four-item lists a reader
    has to diff by eye. Identity is structural (canonical SMILES), so a source spelling the same
    molecule differently cannot fabricate a change.
    """
    before = _species(previous, role)
    after = _species(current, role)
    if before == after:
        return None
    return ConditionChange(
        variable=role.value,
        before=_species_label(before - after),
        after=_species_label(after - before),
    )


def _species(reaction: OrdReaction, role: Role) -> frozenset[str]:
    """The canonical structures playing `role` in this run."""
    return frozenset(canonical_smiles(c.smiles) for c in _components(reaction, role))


def _components(reaction: OrdReaction, role: Role) -> list[Component]:
    """The run's components in `role`, including any a mid-procedure step introduced.

    A reagent added partway through the recipe lives on the step, not on `inputs` — and swapping
    it is exactly the kind of change this series is made of, so it must not be invisible here.
    """
    return [c for c in [*reaction.inputs, *reaction.step_components()] if c.role == role]


def _species_label(structures: frozenset[str]) -> str:
    """Name a set of structures for a human: known reagents by name, the rest by SMILES."""
    if not structures:
        return "—"
    return ", ".join(sorted(display_name(s) or s for s in structures))


def _quantity(value: float | None, unit: str) -> str:
    """A setpoint with its unit, or "—" when the run did not record it."""
    return "—" if value is None else f"{value:g} {unit}"
