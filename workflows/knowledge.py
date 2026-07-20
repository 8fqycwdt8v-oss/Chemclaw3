"""Bridge job results into the knowledge graph via the PR-gate (plan step 2.8).

A completed QM calculation becomes an agent-authored `job-result` note and is
proposed through the **same** PR-gate every other agent note uses (D-005) — there
is no second write path. `note_from_qm_result` is the pure mapping (tested
directly); `write_knowledge_node` is the Temporal activity that runs it and
submits, on the `background-jobs` queue.
"""

from temporalio import activity

from kg.git_submitter import default_submitter
from kg.note import Note
from kg.pr_gate import propose_note
from workflows.models import QMJobInput, QMJobResult, qm_job_key


def note_from_qm_result(result: QMJobResult) -> Note:
    """Map a QM job result to an agent-authored `job-result` note.

    The molecule is identified structurally via the `compound_smiles` field and
    named in the body. It deliberately does *not* wikilink to a compound note that
    may not exist — a dangling link would fail `kg.validate` on the very PR this
    opens; linking compound notes is a separate step once they are created. The
    note id is the calculation key, so re-writing the same calculation is idempotent.
    """
    job = QMJobInput(
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
        id=f"job-{qm_job_key(job)}",
        type="job-result",
        compound_smiles=result.molecule_smiles,
        created_by="agent",
        source=f"qm:{result.requested_by}",
        body=body,
    )


@activity.defn
async def write_knowledge_node(result: QMJobResult) -> str:
    """Write a QM result to the graph as a PR-gated note; return the PR reference."""
    return await propose_note(note_from_qm_result(result), default_submitter())
