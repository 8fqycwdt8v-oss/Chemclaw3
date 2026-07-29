"""Map a finished QM calculation to a knowledge-graph note (plan step 2.8).

A completed calculation becomes an agent-authored `job-result` note and is proposed through the
**same** PR-gate every other agent note uses (D-005) — there is no second write path.

This module is the *mapping only*, which is the connector split (the shape `connectors/bo/
knowledge.py` already has): turning a QM result into a note is this domain's knowledge, so it lives
in the bundle; pushing that note through the PR-gate is the GxP boundary, so it stays in core —
`ConnectorJobWorkflow` publishes whatever note the result envelope carries, via the one
`publish_memory_note_activity`. The `write_knowledge_node` activity that used to do both is gone: a
connector must not be able to reach around the gate, and now it structurally cannot (D-118).
"""

from chemclaw.connectors.qm.specs import QMJobResult, QmJobSpec, qm_job_key
from chemclaw.ingest.eln.compound import compound_id
from chemclaw.kg.note import Note


def note_from_qm_result(result: QMJobResult) -> Note:
    """Map a QM job result to an agent-authored `job-result` note.

    **The note links its compound.** It used to refuse to, and said so: a wikilink to a compound
    note that might not exist yet would dangle and fail `chemclaw.kg.validate` on the very PR this
    opens.
    The consequence was that every computed result was a graph island — the calculation store and
    the knowledge graph, the two halves of the system's memory, could not reference each other in
    either direction (STO-7).

    What changed is not this module's confidence but the PR-gate's shape: a `NoteSubmission` now
    carries a note *with its dependencies*, and
    `chemclaw.ingest.eln.compound.compound_dependencies` mints the
    compound note into the same PR. So the link resolves on the branch it is proposed on. Because
    `compound_id` is derived from the canonical structure, the target here is the same id that
    helper will produce — one derivation, used twice.

    The note id is the calculation key, so re-writing the same calculation is idempotent.
    """
    spec = QmJobSpec(
        molecule_smiles=result.molecule_smiles,
        method=result.method,
        basis_set=result.basis_set,
    )
    body = (
        f"Calculation for [[{compound_id(result.molecule_smiles)}]] "
        f"(`{result.molecule_smiles}`), method {result.method}/{result.basis_set}.\n\n"
        f"- total energy: {result.total_energy_hartree:.6f} Hartree\n"
        f"- converged: {result.converged}\n"
    )
    return Note(
        id=f"job-{qm_job_key(spec)}",
        type="job-result",
        compound_smiles=result.molecule_smiles,
        created_by="agent",
        source=f"qm:{result.requested_by}",
        body=body,
    )
