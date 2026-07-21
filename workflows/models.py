"""Typed payloads for the QM/DFT durable job (plan Phase 1).

These pydantic models are the single shared contract crossing two boundaries:
the MAF→Temporal boundary (the agent tool submits a `QMJobInput`) and the
activity boundary (handles and results passed between activities). One module so
no shape is duplicated between the agent, the workflow, and the activities.
"""

from pydantic import BaseModel, Field

from chemclaw.chem import require_canonical_smiles
from chemclaw.config import settings
from chemclaw.ids import stable_hash


class QMJobInput(BaseModel):
    """A request to run a quantum-mechanical calculation on one molecule.

    `method`/`basis_set` name the QM level of theory (e.g. "B3LYP" / "def2-SVP").
    Kept as free strings: the valid set is a chemistry concern for a later Skill,
    not something to hardcode as an enum here.

    `requested_by` carries the caller's Entra ID object id (`oid`) for the audit
    trail (plan step 1.9). The submit tool populates it via `require_actor`
    (F4-T3): under Entra it is the authenticated user and a run without one is
    rejected before submission; in local dev it is the configured service
    identity. The field keeps a safe default so tests and system-triggered runs
    can construct the input without a tenant. Excluded from `qm_job_key`: the
    science does not depend on who asked, so identical work dedupes across users.
    """

    molecule_smiles: str = Field(min_length=1)
    method: str = Field(min_length=1)
    basis_set: str = Field(min_length=1)
    requested_by: str = "unknown"
    # When true, the completed result is proposed as a PR-gated graph note (2.8).
    # Opt-in, so a calculation is only published to the graph when deliberately asked.
    publish_to_graph: bool = False
    # The conversation session to notify on completion (plan F3-T3), stamped from the turn's
    # ambient context (`agents.session_context`) at submit, never by the model. `None` off the
    # front-door path (tests/CLI) — then the job simply records no push-back. Deliberately excluded
    # from `qm_job_key`: identical science is still deduplicated across sessions (D-011), so a
    # completion notifies the session that actually started the workflow.
    session_id: str | None = None


def qm_job_key(job: QMJobInput) -> str:
    """Stable identity of a QM calculation: molecule + method + basis only.

    The SMILES is canonicalized first, so two spellings of the same molecule
    (`"CCO"` / `"OCC"`) share one workflow id and one cache entry rather than
    running the calculation twice (D-011). Raises `InvalidSmilesError` on an
    unparseable structure, so a malformed request is rejected at the durable
    boundary (G4) instead of flowing through the pipeline into a stored result.

    Deliberately excludes `requested_by` — the result of a calculation does not
    depend on who asked for it, so identical science shares one workflow id and
    one cache entry across users. Used for the workflow id (dedup), the mock
    scheduler handle, and the result cache key (plan step 1.10). One definition,
    three callers. Shares `chemclaw.ids.stable_hash` (SHA-256) with every other
    identity key in the system.

    Includes the HPC pipeline version **only when one is configured** (plan F5-T3):
    a real pipeline update changes the numbers, so it must be a cache *miss*, not a
    stale hit (D-011/D-033). An empty version (the mock/dev default) leaves the key
    byte-identical to before F5, so existing cached results and ids are unaffected.
    """
    payload = {
        "smiles": require_canonical_smiles(job.molecule_smiles),
        "method": job.method,
        "basis_set": job.basis_set,
    }
    if settings.hpc_pipeline_version:
        payload["pipeline_version"] = settings.hpc_pipeline_version
    return stable_hash(payload)


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
