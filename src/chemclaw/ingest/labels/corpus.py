"""Walk a bulk reaction corpus into the label index, as cited evidence rather than as knowledge.

A patent corpus is not an ELN, and the difference is not size. An ELN entry is a claim this
organisation makes about an experiment it ran, so it goes through the PR-gate and becomes a note a
human signed off. A patent reaction is *literature*: it is evidence, it cites a document anyone can
read, and nobody here is going to review 13 million of them.
`D-2026-08-06-a-share-is-mounted-not-called` drew that line for documents; this applies it to
reactions.

**So a corpus declares no `ingest:` half, and that is a design choice with five separate reasons.**
Each is a real path in this tree, not a hypothetical:

* `durable/memory_jobs.py::read_corpus` calls `fetch_new_entries(datetime.min)` on **every** active
  ingest half and materialises every `OrdReaction` into the worker's heap; three memory workflows
  do it per cycle.
* `memory/similarity.cluster_by_similarity` is then O(n²) pairwise over that list — the
  `DEFERRED.md` row whose stated trigger is ~10⁴ reactions.
* `ingest_reaction` ends in `propose_note` unconditionally: one git branch per reaction, through a
  gate a human is supposed to read.
* `sync_entries` calls `_merged_note_bodies()`, loading every merged note body once per run.
* A corpus release is a versioned load addressed by key, not a live feed addressed by datetime. The
  `ElnAdapter` cursor contract does not fit it.

Declaring no ingest half sidesteps all five with **no edits** to any of them, which is why there is
no "publish mode" on `ingest_reaction`: there is no ingest path to gate.

What a corpus source *does* declare is `retrieve:` — so its rows are reachable as evidence — and a
`corpus:` block in its warehouse binding, which this module drains.
"""

import logging
from typing import Any

from pydantic import BaseModel, Field

from chemclaw.core.chem import InvalidSmilesError, standard_smiles
from chemclaw.ingest.eln.warehouse import sql
from chemclaw.ingest.eln.warehouse.binding import CorpusBinding, FieldBinding
from chemclaw.ingest.eln.warehouse.driver import Warehouse
from chemclaw.ingest.eln.warehouse.expr import apply_transforms, as_text, resolve_path
from chemclaw.science.labels.molecules import CorpusMolecules
from chemclaw.science.labels.records import ReactionLabel, SpeciesLabel
from chemclaw.science.labels.store import LabelIndex

logger = logging.getLogger(__name__)

# The payload key one row lands under, so a binding says `root.COL` — the same convention the ELN
# adapter uses, and the same word, because a binding author reads both files.
ROOT = "root"

# Which side of `reactants>agents>products` a species came from, as a recorded `Role` value. The
# agent slot maps to `reagent` and not to `solvent`: the record form groups solvent, catalyst,
# ligand and base into one slot, and guessing which is exactly the labeller's job. Calling them all
# solvents here would be a wrong answer written into the column that says what the source claimed.
_SLOT_ROLES = ("reactant", "reagent", "product")


class CorpusReport(BaseModel):
    """What one drain pass read and wrote, and where the next one resumes."""

    read: int = Field(default=0, ge=0)
    recorded: int = Field(default=0, ge=0)
    skipped: int = Field(
        default=0,
        ge=0,
        description="Rows with no usable reaction SMILES or no citation. Counted, never silent.",
    )
    cursor: str = ""
    has_more: bool = False


async def drain_corpus(
    warehouse: Warehouse,
    binding: CorpusBinding,
    index: LabelIndex,
    source: str,
    *,
    molecules: CorpusMolecules | None = None,
    after: str = "",
    limit: int | None = None,
) -> CorpusReport:
    """Read one keyset page of the corpus and write its record phase into the label index.

    Args:
        warehouse: An open connection to the corpus's warehouse.
        binding: The `corpus:` block naming the relation and its columns.
        index: The label index to write into.
        source: The registry source name — half of every row's key.
        molecules: Where each distinct structure is fingerprinted, if similarity search over this
            corpus is wanted. Written *after* the reactions and from what was actually recorded, so
            a structure only enters `corpus_molecules` because some reaction row names it — which
            is what keeps every similarity hit resolvable back to a precedent.
        after: Resume strictly after this key; empty starts at the beginning.
        limit: Rows this pass may read; defaults to the binding's `fetch_limit`.

    Returns:
        Counts, the cursor the next pass resumes after, and whether more rows remain.

    Every write is an id-keyed upsert of the *record* phase only, so re-draining an unchanged
    release is a no-op and a row already labelled keeps its labels — `LabelIndex.record` holds that
    rule, and it is what makes a stopped drain resumable at any point with no bookkeeping.
    """
    page = limit if limit is not None else binding.fetch_limit
    statement, params = sql.corpus_statement(binding, warehouse.placeholder, after, page)
    async with warehouse.cursor() as cursor:
        await cursor.execute(statement, params)
        rows = await cursor.fetchall()
    if not rows:
        return CorpusReport(cursor=after)

    report = CorpusReport(read=len(rows), cursor=after, has_more=len(rows) == page)
    structures: set[str] = set()
    for row in rows:
        bundle = {ROOT: row}
        key = as_text(row.get(binding.key))
        if key:
            report.cursor = as_text(row.get(binding.cursor_column)) or key
        label = _record(bundle, binding, source, key)
        if label is None:
            report.skipped += 1
            continue
        await index.record(label)
        report.recorded += 1
        structures.update(s.smiles for s in label.species)
    if molecules is not None and structures:
        await molecules.add_many(sorted(structures))
    if report.skipped:
        logger.warning(
            "%s: %d of %d corpus row(s) carried no usable reaction SMILES, key or citation and "
            "were skipped; the drain still advanced past them",
            source,
            report.skipped,
            report.read,
        )
    return report


def _record(
    bundle: dict[str, Any], binding: CorpusBinding, source: str, key: str
) -> ReactionLabel | None:
    """One row as a record-phase label, or `None` when it lacks what a precedent needs.

    Three things are required and the rest are optional, which is the honest split: without a key
    there is no row to write, without a reaction there is nothing to label, and without a citation
    a hit is a precedent a chemist cannot follow back — which is not a precedent.
    """
    reaction = _field(bundle, binding.smiles)
    citation = _field(bundle, binding.citation)
    if not key or not reaction or not citation:
        return None
    species = _species(reaction)
    if not species:
        return None
    return ReactionLabel(
        source=source,
        reaction_id=key,
        record_smiles=reaction,
        citation=citation,
        performed_on=_date(bundle, binding.published_on),
        temperature_c=_number(bundle, binding.temperature_c),
        time_h=_number(bundle, binding.time_h),
        yield_percent=_number(bundle, binding.yield_percent),
        workup_text=_field(bundle, binding.workup_text) or None,
        species=species,
        named_reaction=_field(bundle, binding.named_reaction) or None,
        reaction_class=_field(bundle, binding.reaction_class) or None,
        rxno_id=_field(bundle, binding.rxno_id) or None,
        mapped_smiles=_field(bundle, binding.mapped_smiles) or None,
        # `method` says where a carried label came from, and it is set here rather than left to the
        # enricher because only this side knows: the corpus said so. A chemist reading a frequency
        # table is entitled to tell "Pistachio's NameRxn classified this" from "our SMIRKS matched".
        method="source" if _field(bundle, binding.named_reaction) else None,
    )


def _species(reaction_smiles: str) -> list[SpeciesLabel]:
    """Split `reactants>agents>products` into species rows carrying the slot they came from.

    Returns `[]` for a string that is not a three-part reaction or whose products are empty — a
    reaction with nothing on the right is not a reaction, and indexing it would put a row in the
    corpus that no facet query can ever answer usefully.

    A species RDKit cannot standardize is **kept, with its raw SMILES**. That is deliberate and it
    is the opposite of what the ELN path does: an ELN entry is a claim we are about to put through
    review, so a structure that will not parse is worth rejecting the entry over; a patent extract
    is evidence, one of whose fifty species may be a mangled OCR artefact, and dropping the whole
    reaction over it loses forty-nine good precedents. What it costs is that this species will not
    join `corpus_molecules` by value, which is a missing similarity hit rather than a wrong one.
    """
    parts = reaction_smiles.split(">")
    if len(parts) != 3 or not parts[2].strip():
        return []
    species: list[SpeciesLabel] = []
    for slot, role in zip(parts, _SLOT_ROLES, strict=True):
        for raw in slot.split("."):
            smiles = raw.strip()
            if not smiles:
                continue
            species.append(
                SpeciesLabel(ordinal=len(species), smiles=_standardized(smiles), role=role)
            )
    return species


def _standardized(smiles: str) -> str:
    """`standard_smiles` where it parses, the raw string where it does not — see `_species`."""
    try:
        return standard_smiles(smiles)
    except (InvalidSmilesError, ValueError):
        return smiles


def _field(bundle: dict[str, Any], field: FieldBinding | None) -> str:
    """One bound field as text; `""` when the binding omits it or the path resolves to nothing.

    The `None` check is load-bearing and is not defensive: `as_text` is `str()` for everything, so
    a NULL column becomes the literal string `"None"`. Caught by a test over a corpus row NameRxn
    could not classify — without it, every unclassified reaction would have been stored as a named
    reaction *called* "None", and then counted in a frequency table beside the real ones.
    """
    if field is None:
        return ""
    value = _resolve(bundle, field)
    return as_text(value) if value is not None else ""


def _number(bundle: dict[str, Any], field: FieldBinding | None) -> float | None:
    """One bound field as a float, or `None`. A value that will not convert is `None`, not a zero.

    Zero is a real temperature and a real yield, so coercing an unparseable one to it would put a
    fabricated number into a column a chemist reads as recorded fact.
    """
    if field is None:
        return None
    value = _resolve(bundle, field)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _date(bundle: dict[str, Any], field: FieldBinding | None) -> Any:
    """One bound field as whatever its `iso_date` transform produced, or `None`.

    Typed loosely on purpose: the transform vocabulary owns the conversion (`iso_date` /
    `iso_datetime`), and re-parsing here would be a second, disagreeing definition of what a date
    in this corpus looks like. Pydantic validates it into a `date` on the way into the model.
    """
    if field is None:
        return None
    return _resolve(bundle, field)


def _resolve(bundle: dict[str, Any], field: FieldBinding) -> Any:
    """A field's value after its transforms, falling back where the binding declares one."""
    value = apply_transforms(resolve_path(field.path, bundle), field.transform)
    if value is None and field.fallback is not None:
        return _resolve(bundle, field.fallback)
    return value
