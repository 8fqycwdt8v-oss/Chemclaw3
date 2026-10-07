"""Ingest one validated reaction into the corpus and the fingerprint index.

For one canonical reaction: (1) validate structure and mass balance, refusing an invalid record; (2)
index the reaction (DRFP) and each distinct molecule it names, compounds and identified impurities
(ECFP4); (3) write the transcription to the reaction record store, which a structure hit expands
into. Step (2) runs only for a structured record; a citation-only record is stored and citable but
indexed nowhere a structure search looks.

All of these are deterministic serving indexes: nothing here infers anything, so nothing needs
review (D-2026-08-25-an-eln-transcription-is-data-not-a-claim). Stores are injected, and every write
is an id-keyed upsert, so re-ingesting is safe and an amended entry overwrites its record.
"""

import logging
from datetime import datetime

from chemclaw.core.chem import standard_smiles
from chemclaw.core.errors import ChemclawError
from chemclaw.ingest.eln.ord import OrdReaction, RecordTier
from chemclaw.ingest.eln.record import record_from_ord_reaction
from chemclaw.ingest.eln.records import ReactionRecord, ReactionRecordStore
from chemclaw.ingest.eln.validate import validate_ord
from chemclaw.ingest.labels.record import record_phase
from chemclaw.science.fingerprints.molfp.search import record_for
from chemclaw.science.fingerprints.rxnfp.search import record_for_reaction
from chemclaw.science.fingerprints.store import FingerprintError, FingerprintStore
from chemclaw.science.labels.store import LabelIndex

logger = logging.getLogger(__name__)


class IngestError(ChemclawError):
    """A reaction failed validation and was not ingested (carries the problems)."""


async def ingest_reaction(
    reaction: OrdReaction,
    reaction_store: FingerprintStore,
    molecule_store: FingerprintStore,
    record_store: ReactionRecordStore,
    *,
    label_index: LabelIndex,
    source: str,
    retracted_at: datetime | None = None,
) -> ReactionRecord:
    """Validate, index (reaction + compounds + impurities + labels), store the record; return it.

    Raises `IngestError` listing the problems if the reaction is invalid, so a corrupt entry never
    reaches the index or the corpus.

    `label_index` and `source` are keyword-only and required: the label record phase can only be
    written here, from the canonical record. `source` is half of the key of every row written about
    the reaction, since two ELNs may use one entry id. The molecule index is not keyed by source: a
    molecule's id is its standardized SMILES, and two sites charging one reagent share a row.
    """
    problems = validate_ord(reaction)
    if problems:
        raise IngestError(f"reaction {reaction.reaction_id!r} invalid: {'; '.join(problems)}")

    if reaction.tier is RecordTier.STRUCTURED:
        await _index_structure(reaction, reaction_store, molecule_store, label_index, source)
    # A citation-only record writes nothing to any structural index (no DRFP, molecule or label
    # row), only the record: a fingerprint of the structured subset would describe a reaction nobody
    # ran, and the labeller would infer from a partial structure.

    # The withdrawal is stamped here, not in `record_from_ord_reaction`: it is something the source
    # said about the entry (`RawEntry`), and keeping the mapping pure keeps it a deterministic
    # transcription.
    record = record_from_ord_reaction(reaction)
    if retracted_at is not None:
        record = record.model_copy(update={"retracted_at": retracted_at})
    await record_store.record([record], source)
    return record


async def _index_structure(
    reaction: OrdReaction,
    reaction_store: FingerprintStore,
    molecule_store: FingerprintStore,
    label_index: LabelIndex,
    source: str,
) -> None:
    """Write a structured reaction's three structural index rows: DRFP, ECFP4 and its label."""
    # `transformation_smiles`, never `reaction_smiles`: DRFP folds the agent slot back onto the
    # reactants. The source is set on the record rather than passed to the builder, which knows only
    # the fingerprint.
    fingerprint = record_for_reaction(reaction.reaction_id, reaction.transformation_smiles())
    await reaction_store.add(fingerprint.model_copy(update={"source": source}))
    for smiles in {standard_smiles(c.smiles) for c in reaction.compounds()}:
        await molecule_store.add(record_for(smiles, smiles))
    await _index_impurities(reaction, molecule_store)

    # The label index's record phase: the facets a hit is found by, derived from the same validated
    # record; `reaction_records` holds what a hit expands into, and neither is reconstructible from
    # the other.
    await label_index.record(record_phase(reaction, source))


async def _index_impurities(reaction: OrdReaction, molecule_store: FingerprintStore) -> None:
    """Index the identified impurity structures beside the compounds.

    "Have we seen this impurity before?" is a structure question, so impurities share the compounds'
    standardization and record shape. Skipped:

    * no `smiles` — an ELN often records only a name and RRT; not an error.
    * a `smiles` RDKit cannot parse — `validate_ord` does not check the impurity profile, so this is
      logged and skipped rather than aborting a valid experiment.
    """
    for smiles in {standard_smiles(i.smiles) for i in reaction.impurities if i.smiles}:
        try:
            record = record_for(smiles, smiles)
        except FingerprintError:
            # `%r` on both, because each is external text: repr escapes the control characters
            # that would otherwise let an ELN export forge a log line.
            logger.warning(
                "reaction %r: skipping unparseable impurity SMILES %r (the run is still ingested)",
                reaction.reaction_id,
                smiles,
            )
            continue
        await molecule_store.add(record)
