"""Ingest one validated reaction into the graph and the fingerprint index (plan 4.4/4.5).

The glue that makes an ELN entry both *findable by fingerprint* and *citable in the graph*
(CHECKMATE 4). For one canonical reaction it: (1) validates structure + mass balance and
refuses to ingest an invalid record; (2) indexes the reaction (DRFP) and each distinct
compound (ECFP4) into the fingerprint stores — a deterministic serving index, so it is not
PR-gated; (3) proposes a `reaction` note through the PR-gate — the knowledge claim a human
signs off. Stores and submitter are injected, so the whole flow is testable in-memory with
no database or git. Indexing is idempotent (id-keyed upserts), so re-ingesting is safe.
"""

from chemclaw.chem import canonical_smiles
from chemclaw.errors import ChemclawError
from eln.note import note_from_ord_reaction
from eln.ord import OrdReaction
from eln.validate import validate_ord
from kg.pr_gate import NoteSubmitter, propose_note
from mcp_servers.fpstore import FingerprintStore
from mcp_servers.molfp.search import record_for
from mcp_servers.rxnfp.search import record_for_reaction


class IngestError(ChemclawError):
    """A reaction failed validation and was not ingested (carries the problems)."""


async def ingest_reaction(
    reaction: OrdReaction,
    reaction_store: FingerprintStore,
    molecule_store: FingerprintStore,
    submitter: NoteSubmitter,
) -> str:
    """Validate, index (reaction + compounds), and PR-gate a reaction; return the note ref.

    Raises `IngestError` (listing the problems) if the reaction is invalid, so a corrupt
    ELN entry never reaches the index or the graph.
    """
    problems = validate_ord(reaction)
    if problems:
        raise IngestError(f"reaction {reaction.reaction_id!r} invalid: {'; '.join(problems)}")

    await reaction_store.add(record_for_reaction(reaction.reaction_id, reaction.reaction_smiles()))
    for smiles in {canonical_smiles(c.smiles) for c in reaction.compounds()}:
        await molecule_store.add(record_for(smiles, smiles))

    return await propose_note(note_from_ord_reaction(reaction), submitter)
