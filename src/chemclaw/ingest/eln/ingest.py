"""Ingest one validated reaction into the corpus and the fingerprint index (plan 4.4/4.5).

The glue that makes an ELN entry both *findable by fingerprint* and *readable once found*. For one
canonical reaction it: (1) validates structure + mass balance and refuses to ingest an invalid
record; (2) indexes the reaction (DRFP) and each distinct molecule it names — its compounds *and*
its identified impurities (ECFP4) — into the fingerprint stores; (3) writes the transcription to
the reaction record store, which is what a structure hit expands into.

**All three are deterministic serving indexes, and none of them is PR-gated** (D-2026-08-25). That
used to be true of the first two only, while the third was proposed as a `created_by: agent` note
for a human to merge — a reviewer asked to approve a rendering of data the source system had
already signed off on. The argument the fingerprint half always made now covers the whole function:
nothing here infers anything, so there is nothing to decide. A knowledge *claim* about these runs
is still a playbook or a campaign, still gated, citing these records.

Stores are injected, so the flow is testable with in-memory ones. Every write is an id-keyed
upsert, so re-ingesting is safe and an amended entry simply overwrites its record.
"""

import logging

from chemclaw.core.chem import standard_smiles
from chemclaw.core.errors import ChemclawError
from chemclaw.ingest.eln.ord import OrdReaction
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
) -> ReactionRecord:
    """Validate, index (reaction + compounds + impurities + labels), store the record; return it.

    Raises `IngestError` (listing the problems) if the reaction is invalid, so a corrupt
    ELN entry never reaches the index or the corpus.

    Returns the stored record rather than a reference, because there is no longer anything to refer
    *to*: the transcription is the row, available the moment this returns instead of whenever
    somebody got round to merging a pull request.

    `label_index` and `source` are keyword-only and **required**, with no default, which is
    deliberate: the label index's record phase can only be written here, from the canonical record
    in hand (`ingest/labels/record.py` says why), so a default of `None` would let a caller quietly
    stop writing the half of the row that cannot be reconstructed afterwards.

    `source` is the registry source name, and it is the other half of the key of *both* rows this
    writes — two ELNs may legitimately use one entry id, and a facet count must not merge two
    sites' runs any more than one site's transcription may overwrite the other's. The transcription
    tier keyed on the bare id until
    `D-2026-08-26-a-transcription-is-keyed-by-its-source`, carrying the rendered provenance in a
    `source` column beside it — which recorded which site won rather than keeping both.

    **The fingerprint tables are still keyed on the bare id**, so two sites sharing one id still
    collapse to one structural row. That is a narrower harm — a hit resolves to a run that exists,
    and `ReactionRecordStore.read` now refuses rather than substituting when the citation is
    ambiguous — but it is not fixed here, and saying so is the point: this function writes four
    indexes and only three of them can tell the two sites apart.
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

    # The label index's record phase, from the record form — agents kept, conditions and workup in
    # columns. A fourth deterministic serving index beside the three above, derived from the same
    # validated record: what `reaction_records` holds is the transcription a hit expands into,
    # while this holds the facets a hit is *found* by, and neither can be reconstructed from the
    # other.
    await label_index.record(record_phase(reaction, source))

    record = record_from_ord_reaction(reaction)
    await record_store.record([record], source)
    return record


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
