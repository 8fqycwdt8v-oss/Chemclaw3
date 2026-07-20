"""Bridge job results into the knowledge graph via the PR-gate (plan step 2.8).

A completed QM calculation becomes an agent-authored `job-result` note and is
proposed through the **same** PR-gate every other agent note uses (D-005) — there
is no second write path. `note_from_qm_result` is the pure mapping (tested
directly); `write_knowledge_node` is the Temporal activity that runs it and
submits, on the `background-jobs` queue.
"""

import hashlib

from temporalio import activity

from kg.git_submitter import GitNoteSubmitter
from kg.note import Note
from kg.pr_gate import NoteSubmitter, propose_note
from workflows.models import QMJobInput, QMJobResult, qm_job_key


def _compound_id(smiles: str) -> str:
    """A stable compound-note id from a SMILES (method-independent)."""
    return "compound-" + hashlib.sha1(smiles.strip().encode()).hexdigest()[:12]


def note_from_qm_result(result: QMJobResult) -> Note:
    """Map a QM job result to an agent-authored `job-result` note.

    The note links to the compound it describes (a method-independent id) so it is
    reachable by graph traversal, and carries the requester as provenance. The note
    id is the calculation key, so re-writing the same calculation is idempotent.
    """
    job = QMJobInput(
        molecule_smiles=result.molecule_smiles,
        method=result.method,
        basis_set=result.basis_set,
    )
    compound_id = _compound_id(result.molecule_smiles)
    body = (
        f"Calculation for [[{compound_id}]] (`{result.molecule_smiles}`), "
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


def _default_submitter() -> NoteSubmitter:
    """The production submitter (git feature branch). Overridden in tests."""
    return GitNoteSubmitter()


@activity.defn
async def write_knowledge_node(result: QMJobResult) -> str:
    """Write a QM result to the graph as a PR-gated note; return the PR reference."""
    return await propose_note(note_from_qm_result(result), _default_submitter())
