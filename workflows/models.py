"""Typed payloads for the QM/DFT durable job (plan Phase 1).

These pydantic models are the single shared contract crossing two boundaries:
the MAF→Temporal boundary (the agent tool submits a `QMJobInput`) and the
activity boundary (handles and results passed between activities). One module so
no shape is duplicated between the agent, the workflow, and the activities.
"""

import hashlib

from pydantic import BaseModel, Field


class QMJobInput(BaseModel):
    """A request to run a quantum-mechanical calculation on one molecule.

    `method`/`basis_set` name the QM level of theory (e.g. "B3LYP" / "def2-SVP").
    Kept as free strings: the valid set is a chemistry concern for a later Skill,
    not something to hardcode as an enum here.

    `requested_by` carries the caller's Entra ID object id (`oid`) for the audit
    trail (plan step 1.9). It is a v1 placeholder — populated for real once auth
    lands in Phase 6 — but present in the data model now so provenance flows all
    the way to the knowledge-graph note without a later schema change.
    """

    molecule_smiles: str = Field(min_length=1)
    method: str = Field(min_length=1)
    basis_set: str = Field(min_length=1)
    requested_by: str = "unknown"


def qm_job_key(job: QMJobInput) -> str:
    """Stable identity of a QM calculation: molecule + method + basis only.

    Deliberately excludes `requested_by` — the result of a calculation does not
    depend on who asked for it, so identical science shares one workflow id and
    one cache entry across users. Used for the workflow id (dedup), the mock
    scheduler handle, and the result cache key (plan step 1.10). One definition,
    three callers.
    """
    raw = f"{job.molecule_smiles.strip()}|{job.method}|{job.basis_set}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


class HpcJobHandle(BaseModel):
    """Opaque handle the (mock) scheduler returns from submit, used to poll.

    A real backend would carry the SLURM job id and cluster; the mock carries a
    deterministic id derived from the input so runs are reproducible in tests.
    """

    scheduler_job_id: str = Field(min_length=1)


class QMJobResult(BaseModel):
    """Structured result parsed from the (mock) HPC output (plan step 1.4).

    Echoes the identifying inputs so a stored result is self-describing when it
    later becomes a knowledge-graph note (Phase 2).
    """

    molecule_smiles: str
    method: str
    basis_set: str
    total_energy_hartree: float
    converged: bool
    # Provenance for the audit trail (mirrors QMJobInput.requested_by).
    requested_by: str


class QMJobStatus(BaseModel):
    """A non-blocking status view of a submitted job (plan step 1.6).

    `status` is Temporal's execution status name (RUNNING/COMPLETED/FAILED/…).
    `result` is populated only once the job has completed, so the agent can poll
    with one call and read the outcome the moment it is ready.
    """

    job_id: str
    status: str
    result: QMJobResult | None = None
