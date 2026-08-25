"""Ingest one validated reaction into the graph and the fingerprint index (plan 4.4/4.5).

The glue that makes an ELN entry both *findable by fingerprint* and *citable in the graph*
(CHECKMATE 4). For one canonical reaction it: (1) validates structure + mass balance and
refuses to ingest an invalid record; (2) indexes the reaction (DRFP) and each distinct
molecule it names — its compounds *and* its identified impurities (ECFP4) — into the
fingerprint stores, a deterministic serving index, so it is not PR-gated; (3) proposes a
`reaction` note through the PR-gate — the knowledge claim a human signs off. Stores and
submitter are injected, so the whole flow is testable in-memory with no database or git.
Indexing is idempotent (id-keyed upserts), so re-ingesting is safe.
"""

import logging

from chemclaw.core.chem import standard_smiles
from chemclaw.core.errors import ChemclawError
from chemclaw.ingest.eln.note import note_from_ord_reaction
from chemclaw.ingest.eln.ord import OrdReaction
from chemclaw.ingest.eln.validate import validate_ord
from chemclaw.ingest.labels.record import record_phase
from chemclaw.kg.pr_gate import propose_note
from chemclaw.kg.submission import NoteSubmitter
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
    submitter: NoteSubmitter,
    *,
    label_index: LabelIndex,
    source: str,
) -> str:
    """Validate, index (reaction + compounds + impurities + labels), PR-gate; return the ref.

    Raises `IngestError` (listing the problems) if the reaction is invalid, so a corrupt
    ELN entry never reaches the index or the graph.

    `label_index` and `source` are keyword-only and **required**, with no default, which is
    deliberate: the label index's record phase can only be written here, from the canonical record
    in hand (`ingest/labels/record.py` says why), so a default of `None` would let a caller quietly
    stop writing the half of the row that cannot be reconstructed afterwards. `source` is the
    registry source name, and it is the other half of the label row's key — two ELNs may
    legitimately use one entry id, which the fingerprint tables, keyed on the bare id, cannot
    represent.
    """
    problems = validate_ord(reaction)
    if problems:
        raise IngestError(f"reaction {reaction.reaction_id!r} invalid: {'; '.join(problems)}")

    # `transformation_smiles`, never `reaction_smiles`: the row is a fingerprint, and the agent
    # slot only changes the bits by being *left out* (DRFP folds it back onto the reactants).
    await reaction_store.add(
        record_for_reaction(reaction.reaction_id, reaction.transformation_smiles())
    )
    for smiles in {standard_smiles(c.smiles) for c in reaction.compounds()}:
        await molecule_store.add(record_for(smiles, smiles))
    await _index_impurities(reaction, molecule_store)

    # The record phase, from the record form — agents kept, conditions and workup in columns. It
    # is written before the PR-gate for the same reason the fingerprints are: this is a
    # deterministic serving index derived from a validated record, not a knowledge claim a human
    # signs off, and holding it behind review would leave every unmerged reaction unsearchable.
    await label_index.record(record_phase(reaction, source))

    return await propose_note(note_from_ord_reaction(reaction), submitter)


async def _index_impurities(reaction: OrdReaction, molecule_store: FingerprintStore) -> None:
    """Index the identified impurity structures beside the compounds (gap KNW-2).

    "Have we seen this impurity before?" is a structure question, and the answer used to be no
    matter what: an impurity's SMILES reached the note *text* only, so it was findable by lexical
    search and invisible to `similar_molecules`/`substructure_matches` — the exact inverse of what
    the question needs. Same standardization and same record shape as the compounds, so an
    impurity in one run and the same molecule charged as a reactant in another land on one row.

    Two kinds of impurity are skipped rather than indexed, both because `Impurity` is deliberately
    lenient about identification:

    * no `smiles` — an ELN routinely records only a chromatographic name and an RRT; there is no
      structure to fingerprint and that is not an error.
    * a `smiles` RDKit cannot parse — `validate_ord` checks the *reaction's* components, not the
      impurity profile, so a malformed trace-impurity string would otherwise abort ingestion of an
      entirely valid experiment. Logged, never silent: the run is kept, the bad structure is not.
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
