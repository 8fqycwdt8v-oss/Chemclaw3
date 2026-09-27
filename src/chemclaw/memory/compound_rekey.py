"""Carry compound notes across a `STANDARDIZATION_VERSION` bump that moves their id.

A compound note's id is a hash of its standardized structure (`core.chem.compound_id`), and the
version is not in the hash — deliberately, since folding it in would invalidate every stored id at
every bump and break every citation to one. So a bump that changes what a structure standardizes to
moves that compound to a new id and leaves the old note behind, current, with nothing linking the
two: two notes for one substance, and a citation to the old id resolving to a stale copy.

**The link is the ordinary supersede link, in the ordinary write order**
(`D-2026-09-27-a-compound-id-a-bump-moves-is-superseded-not-orphaned`). The note under the new id
records `supersedes` for every old id that standardizes onto it; each old note is retired —
`valid_to` closed, `superseded-by` the new id — by `memory.supersede.retire_note`; and both land
through `kg.record.record_note`, whose order (dependencies, subject, retirements) means the old note
never names a successor that is not yet in the graph. Nothing is rewritten or deleted: the old note
stays in Git, reachable by id, and `kg.graph.current_successor` is how a reader gets from it to the
note that replaced it.

**Derived from what the note already holds.** The old note's `compound_smiles` is the structure
standardized under the *old* version, and re-standardizing it under the new one is the new id. That
is exact for a bump that discards or neutralizes more than its predecessor did — which is every bump
so far, `std12` included — and it cannot be exact for one that would need something the old
standard form already threw away. Such a bump needs the source data again; this module says so by
never inventing an answer: it re-keys what the stored structure determines and nothing else.

Only structure-derived ids are candidates. A seed note filed under a slug (`compound-thf`) never had
a hash to go stale, and a note a person wrote is never retired in place — `git_writer` refuses that
amendment, so it is counted and left current, while the replacement still records that it
supersedes it.
"""

import asyncio
from collections import defaultdict
from collections.abc import Callable
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from chemclaw.core.chem import STANDARDIZATION_VERSION, InvalidSmilesError, compound_id
from chemclaw.ingest.eln.compound import STRUCTURAL_COMPOUND_ID, compound_note
from chemclaw.kg.graph import load_notes
from chemclaw.kg.note import Note, Relation
from chemclaw.kg.record import NoteWriter, record_note
from chemclaw.memory.supersede import retire_note
from chemclaw.science.fingerprints.molfp.fingerprint import molecule_definition
from chemclaw.science.fingerprints.rekey import (
    FingerprintRekeyCounts,
    rebuild_molecule,
    rebuild_reaction,
    rekey_fingerprints,
)
from chemclaw.science.fingerprints.rxnfp.fingerprint import reaction_definition
from chemclaw.science.fingerprints.store import FingerprintStore

#: The sentence a retired compound note carries, where a cluster note says its membership changed.
_RETIRED_BECAUSE = (
    f"under standardization {STANDARDIZATION_VERSION} this structure has a different compound id, "
    "so the note above is the same compound filed under its superseded id"
)


class CompoundRekey(BaseModel):
    """One new compound id and what moving onto it writes.

    `successor` is the note recorded under the new id, carrying a `supersedes` relation for every
    old id; `retired` are the old notes closed in place; `kept` are old ids a person wrote, which
    the successor supersedes and nothing retires.
    """

    model_config = ConfigDict(frozen=True)

    successor: Note
    retired: list[Note]
    kept: list[str]


class CompoundRekeyPlan(BaseModel):
    """Everything one pass would write, and the counts an operator reads before applying it.

    Counts rather than a boolean, per kind, in the shape `make user-erase` reports: a dry run is
    only reviewable if it says how many of each thing it would touch, and `apply` is only checkable
    against it if it reports the same numbers.
    """

    model_config = ConfigDict(frozen=True)

    #: Compound notes read.
    examined: int = 0
    #: Structure-derived compound notes whose id is still the one their structure hashes to.
    unchanged: int = 0
    #: Compound notes filed under a slug rather than a structure hash; never candidates.
    not_structural: int = 0
    #: Compound notes whose structure no longer parses, so no id can be derived for them.
    unreadable: int = 0
    #: New ids whose existing note a person wrote or was written for; nothing is recorded onto it.
    blocked: list[str] = []
    rekeys: list[CompoundRekey] = []

    def counts(self) -> dict[str, int]:
        """The per-kind counts, the same keys whether the pass previews or applies."""
        return {
            "examined": self.examined,
            "unchanged": self.unchanged,
            "not_structural": self.not_structural,
            "unreadable": self.unreadable,
            "successors": len(self.rekeys),
            "retired": sum(len(rekey.retired) for rekey in self.rekeys),
            "kept_current_human": sum(len(rekey.kept) for rekey in self.rekeys),
            "blocked_successor": len(self.blocked),
        }


def plan_compound_rekey(notes: list[Note], as_of: date) -> CompoundRekeyPlan:
    """What re-keying `notes` onto the current standardization writes; empty when nothing moved.

    **Idempotent by construction, so a second run is a read.** An old note is a candidate only
    while `valid_to` is open, which a first run closes; one a person wrote stays open and is then
    excluded because its successor already names it. A new id with nothing left to add is not in
    the plan at all.

    Grouped by new id, because a bump is exactly the change that makes two old ids one: the
    neutral and the ionic spelling of one salt were two notes under `std11` and are one compound
    under `std12`, so one successor supersedes both.

    Args:
        notes: The corpus, typically `kg.graph.load_notes` over the knowledge directory.
        as_of: The run's date, used as each retired note's `valid_to`.
    """
    by_id = {note.id: note for note in notes}
    moved: dict[str, list[Note]] = defaultdict(list)
    examined = unchanged = not_structural = unreadable = 0
    for note in notes:
        if note.type != "compound":
            continue
        examined += 1
        if not STRUCTURAL_COMPOUND_ID.fullmatch(note.id) or not note.compound_smiles:
            not_structural += 1
            continue
        try:
            current = compound_id(note.compound_smiles)
        except InvalidSmilesError:
            unreadable += 1
            continue
        if current == note.id:
            unchanged += 1
        elif note.valid_to is None:
            moved[current].append(note)
        # A structure whose id moved and whose note is already closed was re-keyed by an earlier
        # pass; it is neither unchanged nor a candidate, and counting it as either would be a lie.

    blocked: list[str] = []
    rekeys: list[CompoundRekey] = []
    for new_id in sorted(moved):
        old_notes = sorted(moved[new_id], key=lambda note: note.id)
        existing = by_id.get(new_id)
        # Recording onto a note a person wrote would forge their note, and onto one that names
        # the person it was written for would write it for somebody else — `record_note` refuses
        # both, so the plan does not offer them. The old notes stay current and are counted.
        if existing is not None and (not existing.authorship.by_agent or existing.actor):
            blocked.append(new_id)
            continue
        base = (
            existing if existing is not None else compound_note(old_notes[0].compound_smiles or "")
        )
        declared = {relation.to for relation in base.relations if relation.rel == "supersedes"}
        added = [
            Relation(rel="supersedes", to=old.id) for old in old_notes if old.id not in declared
        ]
        retired = [
            retire_note(old, [new_id], as_of, reason=_RETIRED_BECAUSE)
            for old in old_notes
            if old.authorship.by_agent
        ]
        kept = [old.id for old in old_notes if not old.authorship.by_agent]
        if not added and not retired:
            continue
        successor = base.model_copy(update={"relations": [*base.relations, *added]})
        rekeys.append(CompoundRekey(successor=successor, retired=retired, kept=kept))
    return CompoundRekeyPlan(
        examined=examined,
        unchanged=unchanged,
        not_structural=not_structural,
        unreadable=unreadable,
        blocked=blocked,
        rekeys=rekeys,
    )


async def apply_compound_rekey(
    plan: CompoundRekeyPlan, writer: NoteWriter, knowledge_dir: str | None = None
) -> None:
    """Write `plan` through the one write path, one successor and its retirements per record.

    One `record_note` per new id rather than one commit for the pass, so each unit is the ordinary
    one — the successor, then the notes it retires — and an interrupted run leaves every compound
    either wholly moved or untouched; the next run's plan is exactly what is left.
    """
    for rekey in plan.rekeys:
        await record_note(
            rekey.successor, writer, knowledge_dir=knowledge_dir, superseded=list(rekey.retired)
        )


class StandardizationRekeyReport(BaseModel):
    """What `rekey_standardization` found, per kind — a preview's numbers are an apply's numbers."""

    applied: bool
    version: str = STANDARDIZATION_VERSION
    notes: dict[str, int]
    molecules: FingerprintRekeyCounts
    reactions: FingerprintRekeyCounts


async def rekey_standardization(
    *,
    apply: bool,
    notes_dir: Path,
    writer: Callable[[], NoteWriter],
    molecule_store: FingerprintStore,
    reaction_store: FingerprintStore,
    as_of: date,
) -> StandardizationRekeyReport:
    """Carry the compound notes and both fingerprint indexes onto the current standardization.

    The job an operator runs after deploying a `STANDARDIZATION_VERSION` bump. Idempotent and
    interrupt-safe in both halves, and a preview unless `apply`: every count is computed before
    anything is written, and the same way either way.

    Args:
        apply: Write; otherwise count only.
        notes_dir: The knowledge tree to read (`settings.knowledge_path`).
        writer: Builds the note writer — called only when there is something to write, because
            the git writer insists on a dedicated checkout and a preview should not need one.
        molecule_store: The molecule index.
        reaction_store: The reaction index.
        as_of: The date a retired note's `valid_to` records.
    """
    notes = await asyncio.to_thread(load_notes, notes_dir)
    plan = plan_compound_rekey(notes, as_of)
    if apply and plan.rekeys:
        await apply_compound_rekey(plan, writer())
    molecules = await rekey_fingerprints(
        molecule_store, molecule_definition(), rebuild_molecule, apply=apply
    )
    reactions = await rekey_fingerprints(
        reaction_store, reaction_definition(), rebuild_reaction, apply=apply
    )
    return StandardizationRekeyReport(
        applied=apply, notes=plan.counts(), molecules=molecules, reactions=reactions
    )
