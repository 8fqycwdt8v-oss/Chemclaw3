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

from connectors.qm.specs import QMJobResult, QmJobSpec, qm_job_key
from kg.note import Note


def note_from_qm_result(result: QMJobResult) -> Note:
    """Map a QM job result to an agent-authored `job-result` note.

    The molecule is identified structurally via the `compound_smiles` field and named in the body.
    It deliberately does *not* wikilink to a compound note that may not exist — a dangling link
    would fail `kg.validate` on the very PR this opens; linking compound notes is a separate step
    once they are created. The note id is the calculation key, so re-writing the same calculation
    is idempotent.
    """
    spec = QmJobSpec(
        molecule_smiles=result.molecule_smiles,
        method=result.method,
        basis_set=result.basis_set,
    )
    body = (
        f"Calculation for `{result.molecule_smiles}`, "
        f"method {result.method}/{result.basis_set}.\n\n"
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
