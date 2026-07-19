"""Typed payloads for the QM/DFT durable job (plan Phase 1).

These pydantic models are the single shared contract crossing two boundaries:
the MAF→Temporal boundary (the agent tool submits a `QMJobInput`) and the
activity boundary (handles and results passed between activities). One module so
no shape is duplicated between the agent, the workflow, and the activities.
"""

from pydantic import BaseModel, Field


class QMJobInput(BaseModel):
    """A request to run a quantum-mechanical calculation on one molecule.

    `method`/`basis_set` name the QM level of theory (e.g. "B3LYP" / "def2-SVP").
    Kept as free strings: the valid set is a chemistry concern for a later Skill,
    not something to hardcode as an enum here.
    """

    molecule_smiles: str = Field(min_length=1)
    method: str = Field(min_length=1)
    basis_set: str = Field(min_length=1)


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
