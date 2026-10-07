"""Walk a bulk reaction corpus into the label index, as cited evidence rather than as knowledge.

A patent reaction is literature, not this organisation's record of an experiment, so a corpus source
declares no `ingest:` half: the memory jobs would load every ingest half's reactions into memory and
cluster them pairwise, and a versioned release addressed by key does not fit the `ElnAdapter`
datetime cursor. It declares `retrieve:`, so rows are reachable as evidence, and a `corpus:` block
in its warehouse binding, which this module drains.
"""

import logging
from typing import Any

from pydantic import BaseModel, Field

from chemclaw.core.chem import InvalidSmilesError, standard_smiles
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.ingest.eln.warehouse import sql
from chemclaw.ingest.eln.warehouse.binding import CorpusBinding, FieldBinding
from chemclaw.ingest.eln.warehouse.driver import Warehouse
from chemclaw.ingest.eln.warehouse.expr import (
    apply_transforms,
    as_text,
    pattern_budget,
    resolve_path,
)
from chemclaw.science.fingerprints.rxnfp.search import record_for_reaction
from chemclaw.science.fingerprints.store import (
    FingerprintInputError,
    FingerprintRecord,
    FingerprintStore,
)
from chemclaw.science.labels.molecules import CorpusMolecules
from chemclaw.science.labels.reactions import transformation_of
from chemclaw.science.labels.records import ReactionLabel, SpeciesLabel
from chemclaw.science.labels.store import LabelIndex

logger = logging.getLogger(__name__)

# The payload key one row lands under, so a binding says `root.COL` — the same convention the ELN
# adapter uses, and the same word, because a binding author reads both files.
ROOT = "root"

# Which side of `reactants>agents>products` a species came from, as a recorded `Role`. The agent
# slot maps to `reagent`: it mixes solvents, catalysts, ligands and bases, and telling them apart is
# the labeller's job.
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
    unfingerprintable: int = Field(
        default=0,
        ge=0,
        description=(
            "Recorded reactions whose DRFP could not be built — in practice a degenerate "
            "transformation whose symmetric difference is empty, which is the one case measured "
            "to reach it. Notably *not* an unparseable species: `standard_smiles` returns a "
            "string RDKit cannot read unchanged, so DRFP shingles it and yields bits, where the "
            "molecule half raises. The reaction row is still written and still answers every "
            "facet query; what it loses is reaction *similarity*. Counted separately from "
            "`skipped` because the outcomes differ: a skipped row is not in the index at all, and "
            "conflating the two would report a corpus as less complete than it is."
        ),
    )
    unreadable_fields: int = Field(
        default=0,
        ge=0,
        description=(
            "Optional fields a row carried and this drain could not read — a temperature written "
            "'60 °C' or '333 K', a range '60-65', 'rt', 'reflux', a decimal comma. The column is "
            "written NULL, which is right (zero is a real temperature and coercing to it would "
            "fabricate a recorded fact) and was **silent**: measured, 8 of 11 realistic corpus "
            "cells became a NULL nobody counted. `search.py`'s facets filter on `temperature_c`, "
            "so a precedent search for a temperature window excludes every such row while "
            "`CorpusCoverage`'s verdict — which is about labelling coverage, not field coverage — "
            "says nothing about it. Counted, never silent, like `skipped`; a site that sees this "
            "rise declares a `transform:` for the column."
        ),
    )
    cursor: str = ""
    has_more: bool = False
    advanced: bool = Field(
        default=False,
        description=(
            "Whether this pass moved the keyset position at all. Computed here rather than by the "
            "caller comparing `cursor` against the `after` it passed, because those are no longer "
            "the same two values: for an append-only source the activity resolves a *stored* "
            "position, so the workflow's own `after` is `''` on the first page while the drain "
            "started somewhere else — and the comparison would read a bypassed guard as an "
            "advance. This is the one place both values are in scope."
        ),
    )


async def drain_corpus(
    warehouse: Warehouse,
    binding: CorpusBinding,
    index: LabelIndex,
    source: str,
    *,
    molecules: CorpusMolecules | None = None,
    reactions: FingerprintStore | None = None,
    after: str = "",
    limit: int | None = None,
) -> CorpusReport:
    """Read one keyset page of the corpus and write its record phase into the label index.

    Every write is an id-keyed upsert of the record phase only, so re-draining is a no-op, labelled
    rows keep their labels, and a stopped drain resumes anywhere. Every pass, including an empty
    one, books its rows on `chemclaw_ingest_records_total{source,outcome}`.

    Args:
        warehouse: An open connection to the corpus's warehouse.
        binding: The `corpus:` block naming the relation and its columns.
        index: The label index to write into.
        source: The registry source name, half of every row's key.
        molecules: Where each distinct structure is fingerprinted, if structure similarity is
            wanted. Written after the reactions, from what was recorded, so every hit resolves to a
            precedent.
        reactions: Where each recorded reaction's DRFP is written, if reaction similarity is wanted.
            Batched once per page; the cursor only advances per page anyway.
        after: Resume strictly after this key; empty starts at the beginning.
        limit: Rows this pass may read; defaults to the binding's `fetch_limit`.

    Returns:
        Counts, the cursor the next pass resumes after, and whether more rows remain.
    """
    report = await _drain_page(
        warehouse,
        binding,
        index,
        source,
        molecules=molecules,
        reactions=reactions,
        after=after,
        limit=limit,
    )
    _drained(source, report)
    return report


async def _drain_page(
    warehouse: Warehouse,
    binding: CorpusBinding,
    index: LabelIndex,
    source: str,
    *,
    molecules: CorpusMolecules | None = None,
    reactions: FingerprintStore | None = None,
    after: str = "",
    limit: int | None = None,
) -> CorpusReport:
    """Read one page and write it, and account for nothing.

    Split out so `drain_corpus` has one return and the metric cannot be skipped.
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
    fingerprints: list[FingerprintRecord] = []
    # One regex matching budget for the page, since the per-cell bound does not compose over many
    # rows; it charges matching time only, not the awaited writes.
    with pattern_budget():
        for row in rows:
            bundle = {ROOT: row}
            key = _text(row.get(binding.key))
            # Taken from the pagination column only, and only when it holds a value: a NULL must not
            # become `"None"`, and the key column is a different domain. A row without a value holds
            # the cursor, which stops the source with a "no cursor advance" warning naming
            # `order_by`.
            cursor_value = row.get(binding.cursor_column)
            if cursor_value is not None:
                report.cursor = as_text(cursor_value)
            label = _record(bundle, binding, source, key, report)
            if label is None:
                report.skipped += 1
                continue
            await index.record(label)
            report.recorded += 1
            structures.update(s.smiles for s in label.species)
            if reactions is not None:
                _collect_fingerprint(fingerprints, source, label, report)
    if reactions is not None and fingerprints:
        await reactions.add_many(fingerprints)
    if molecules is not None and structures:
        await molecules.add_many(sorted(structures))
    if report.unfingerprintable:
        logger.info(
            "%s: %d of %d recorded reaction(s) yielded no DRFP and are not searchable by "
            "reaction similarity; they are indexed and answerable by facet",
            source,
            report.unfingerprintable,
            report.recorded,
        )
    report.advanced = bool(report.cursor) and report.cursor != after
    if report.skipped:
        logger.warning(
            "%s: %d of %d corpus row(s) carried no usable reaction SMILES, key or citation and "
            "were skipped; the drain still advanced past them",
            source,
            report.skipped,
            report.read,
        )
    if report.unreadable_fields:
        logger.warning(
            "%s: %d optional field value(s) across %d row(s) could not be read and were stored as "
            "NULL, so a facet search on them excludes those rows; declare a `transform:` for the "
            "column if they matter",
            source,
            report.unreadable_fields,
            report.read,
        )
    return report


def _drained(source: str, report: CorpusReport) -> None:
    """Book what this pass did on `chemclaw_ingest_records_total`, whatever it did.

    Two outcomes that partition `read`: `ingested` (recorded) and `rejected` (no usable reaction
    SMILES, key or citation: deterministic bad data). There is no `skipped`, since no corpus row is
    deliberately passed over. `unfingerprintable` rows were recorded and are counted as `ingested`.
    Called on every return, so a healthy empty page books a zero and a silent series means the drain
    did not run.
    """
    record_metric(
        lambda m: m.increment(
            "chemclaw_ingest_records_total",
            report.recorded,
            {"source": source, "outcome": "ingested"},
        )
    )
    record_metric(
        lambda m: m.increment(
            "chemclaw_ingest_records_total",
            report.skipped,
            {"source": source, "outcome": "rejected"},
        )
    )


def _collect_fingerprint(
    into: list[FingerprintRecord], source: str, label: ReactionLabel, report: CorpusReport
) -> None:
    """Add one recorded reaction's DRFP to the page's batch, counting when it has none.

    Skips rather than raises: one degenerate reaction must not cost the page its good precedents,
    and the reaction row is already written, so a skip loses only a similarity hit, counted on
    `report.unfingerprintable`. Only `FingerprintInputError` is caught, since DRFP shingles even
    unparseable species. The source is set via `model_copy`, as in `ingest_reaction`. Bits are taken
    over the transformation form (see `transformation_of`).
    """
    try:
        record = record_for_reaction(
            label.reaction_id, transformation_of(label.record_smiles)
        ).model_copy(update={"source": source})
    except FingerprintInputError:
        report.unfingerprintable += 1
        return
    into.append(record)


def _record(
    bundle: dict[str, Any], binding: CorpusBinding, source: str, key: str, report: CorpusReport
) -> ReactionLabel | None:
    """One row as a record-phase label, or `None` when it lacks what a precedent needs.

    Key, reaction and citation are required: without a citation a chemist cannot follow the hit
    back.
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
        performed_on=_date(bundle, binding.published_on, report),
        temperature_c=_number(bundle, binding.temperature_c, report),
        time_h=_number(bundle, binding.time_h, report),
        yield_percent=_number(bundle, binding.yield_percent, report),
        workup_text=_field(bundle, binding.workup_text) or None,
        species=species,
        named_reaction=_field(bundle, binding.named_reaction) or None,
        reaction_class=_field(bundle, binding.reaction_class) or None,
        rxno_id=_field(bundle, binding.rxno_id) or None,
        mapped_smiles=_field(bundle, binding.mapped_smiles) or None,
        # Only this side knows a carried label came from the corpus, so chemists can tell a source's
        # classification from our SMIRKS match.
        method="source" if _field(bundle, binding.named_reaction) else None,
    )


def _species(reaction_smiles: str) -> list[SpeciesLabel]:
    """Split `reactants>agents>products` into species rows carrying the slot they came from.

    Returns `[]` for a string that is not a three-part reaction or has no products. A species RDKit
    cannot standardize is kept with its raw SMILES, unlike on the ELN path: a patent extract is
    evidence, and dropping a reaction over one mangled species loses the rest. It only misses
    joining `corpus_molecules`.
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


def _text(value: Any) -> str:
    """One raw column value as text, `""` when it is NULL, never the string `"None"`.

    Used for the key and the pagination cursor, read directly rather than through a field binding.
    """
    return as_text(value) if value is not None else ""


def _field(bundle: dict[str, Any], field: FieldBinding | None) -> str:
    """One bound field as text; `""` when the binding omits it or the path resolves to nothing.

    The `None` check matters: `as_text` would turn NULL into `"None"`, e.g. a named reaction called
    "None".
    """
    if field is None:
        return ""
    value = _resolve(bundle, field)
    return as_text(value) if value is not None else ""


def _number(
    bundle: dict[str, Any], field: FieldBinding | None, report: CorpusReport
) -> float | None:
    """One bound field as a float, or `None`. A value that will not convert is `None`, not a zero.

    A supplied value that cannot be read is counted on `report.unreadable_fields`, since its NULL
    would otherwise read as "not recorded".
    """
    if field is None:
        return None
    value = _resolve(bundle, field)
    # A blank cell is the source recording nothing, not an unreadable value.
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        report.unreadable_fields += 1
        return None


def _date(bundle: dict[str, Any], field: FieldBinding | None, report: CorpusReport) -> Any:
    """One bound field as whatever its `iso_date` transform produced, or `None`.

    The transform owns the conversion; pydantic validates the result into a `date`.
    """
    if field is None:
        return None
    value = _resolve(bundle, field)
    # A non-blank value that did not become a date is counted as unreadable, like in `_number`.
    raw = resolve_path(field.path, bundle)
    if value is None and raw is not None and not (isinstance(raw, str) and not raw.strip()):
        report.unreadable_fields += 1
    return value


def _resolve(bundle: dict[str, Any], field: FieldBinding) -> Any:
    """A field's value after its transforms, falling back where the binding declares one."""
    value = apply_transforms(resolve_path(field.path, bundle), field.transform)
    if value is None and field.fallback is not None:
        return _resolve(bundle, field.fallback)
    return value
