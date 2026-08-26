"""Read an optimization series as a *sequence* rather than a set (D-162).

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

from chemclaw.core.chem import standard_smiles
from chemclaw.core.reagents import display_name, resolve_compound_name
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


# What this rule can be asked about: an optional scalar, where `None` means "nobody wrote it down".
# Deliberately *not* `frozenset[str]` — a species set is derived from a components list that is
# present either way, so "empty" is an answer rather than a gap, and mypy rejecting the call is what
# keeps that distinction from being erased by someone tidying two similar-looking guards into one.
Recorded = float | str | None


def both_recorded(before: Recorded, after: Recorded) -> bool:
    """Whether a field was recorded on *both* sides, which is the precondition for diffing it.

    **The one rule, defined once.** A field present on one side and absent on the other differs in
    what was *recorded*, never in what was done — and the two are indistinguishable to a reader once
    they are in the same column. So a diff against absent is a change nobody made, rendered exactly
    like one they did.

    It lived in `agent/condense._changes` first, where an arbitrary set of protocols made the
    fabrication constant: three runs with identical conditions and one failed extraction rendered
    `solvent 2-MeTHF → —` then `solvent — → 2-MeTHF`, two swaps that never happened. It belongs
    here, because `changes_between` has the same hole for the same reason — bounded rather than
    absent, since a campaign's members are all `OrdReaction`s from one DRFP cluster and usually
    record the same fields. Two rules for one question is how the bounded half stayed open.

    Absent is `None`, the empty string or whitespace. `0.0` is a recorded temperature and passes.

    **It applies to optional scalars and to nothing else** — the two setpoints and the solvent the
    condenser reads out of prose. `_species_change` is deliberately outside it: a role's species set
    is derived from a components list that is present either way, so an empty `reagent` set is the
    record stating that the run used no reagent, not a gap in it. `BACKLOG.md` asked for the rule
    over the species sets too; measured against `_components`, that would have erased the most
    common real change a run-to-run series carries — a reagent added mid-procedure — to suppress a
    fabrication that needs a *partially transcribed* source to happen at all.
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

    Covers what an ELN reliably records and a chemist reliably turns: the two headline setpoints
    and the species set of each non-product role. Amounts (equivalents, loading) are deliberately
    out: they are optional on `Component` and frequently absent, so diffing them would report a
    change every time one run happened to record a mass and its neighbour did not — which is
    `both_recorded`'s rule, stated here about amounts before it was applied to anything.
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
        for role in _DIFFED_ROLES
        if (change := _species_change(role, previous, current)) is not None
    )
    return changes


def number_change(
    variable: str, before: float | None, after: float | None, unit: str
) -> ConditionChange | None:
    """A setpoint change, or None when the two runs agree (including both being unrecorded).

    Public because the turn-time condenser diffs the same two setpoints off note frontmatter, where
    it has numbers but not the `OrdReaction` species sets `changes_between` also walks. One rule for
    "did this setpoint move, and how is that written" — two copies would render `90 °C -> 70 °C` in
    the campaign note and something subtly different in the comparison a chemist reads beside it.

    A setpoint one side did not record is not a move: see `both_recorded`.
    """
    if not both_recorded(before, after) or before == after:
        return None
    return ConditionChange(
        variable=variable,
        before=_quantity(before, unit),
        after=_quantity(after, unit),
    )


def canonical_condition(species: str) -> str:
    """Fold a condition species to one canonical token (gap KNW-4).

    `DMF`, `N,N-dimethylformamide` and `CN(C)C=O` are the same solvent and were three unrelated
    tokens to every lexical and grouping path, so an optimization campaign could be split in two by
    spelling alone. Resolution reuses the one identity table (`chemclaw.core.reagents`), so the
    vocabulary here cannot drift from the one every other in-process caller uses. That guarantee is
    now bounded by the process: after `D-2026-08-16-the-physics-leaves-the-cache-stays` the
    calculators and the hazard screen answer from `Chemclaw3-mcp`, each carrying its own reagent
    table, and a shared import no longer holds them together.

    An unrecognised species folds to its own trimmed, lowercased form rather than being dropped:
    an unknown reagent is still a real condition, and losing it would silently merge campaigns that
    genuinely differ.

    **It lives here because `text_change` is its caller**, and it had none. Defined next to the
    campaign builder, it was reachable only from a test that called it directly — a control that
    exists as a function and not as behaviour, which is the `reject_widening` shape `CLAUDE.md`
    names by that name. `memory.optimization` imports this module already, so this is also the
    direction that has no cycle in it.
    """
    match = resolve_compound_name(species)
    return match.smiles if match is not None else species.strip().lower()


def text_change(variable: str, before: str | None, after: str | None) -> ConditionChange | None:
    """A change in a condition the record only carries as words, or None when they agree.

    The condenser's counterpart to `_species_change`: a solvent read out of a procedure is a name,
    not a structure, so it cannot be compared as a graph the way `_species` does — but it can be
    *resolved*, and `canonical_condition` is the one table that does it. Two spellings of one
    solvent therefore agree here: `DMF` and `N,N-dimethylformamide`, `DIPEA` and
    `N,N-diisopropylethylamine`, a name and its SMILES. Before that fold this compared casefolded,
    whitespace-collapsed prose, so a technician writing the long name in one entry and the acronym
    in the next produced `solvent DMF → N,N-dimethylformamide` in the "Changed vs previous" column
    — a fabricated lever in the one artifact built for reading levers off. The casefold is not lost:
    it is what `canonical_condition` falls back to for a species the table does not know, so
    "2-MeTHF" and "2-methf " still agree and `Mystery-A` and `Mystery-B` still differ.

    What is *displayed* is what was written; the fold decides only whether anything moved.

    A side that recorded no words at all is not a swap either: see `both_recorded`.
    """
    if not both_recorded(before, after):
        return None
    if canonical_condition(before or "") == canonical_condition(after or ""):
        return None
    return ConditionChange(variable=variable, before=before or "—", after=after or "—")


def _species_change(
    role: Role, previous: OrdReaction, current: OrdReaction
) -> ConditionChange | None:
    """The change in one role's species set, or None when the same structures are present.

    Reported as *what went out* → *what came in*, not as the full set on each side: a run that
    swaps one of four reactants should read `reactant A → B`, not two four-item lists a reader
    has to diff by eye. Identity is structural (canonical SMILES), so a source spelling the same
    molecule differently cannot fabricate a change.

    **`both_recorded` deliberately does not apply here**, and that asymmetry is the whole point of
    where the rule is drawn. A setpoint is an optional scalar, so `None` means *nobody wrote it
    down*. A role's species set is derived from a components list that is present either way — so an
    empty `reagent` set beside a full one is the record saying "this run used no reagent", which is
    a real change a chemist made and the most common one a series is built out of
    (`test_a_reagent_added_mid_procedure_is_diffed_too`). Suppressing it would trade a rare
    fabrication for a routine erasure.
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
    return frozenset(standard_smiles(c.smiles) for c in _components(reaction, role))


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
