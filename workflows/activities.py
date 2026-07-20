"""Activities for the QM job — every non-deterministic step and all I/O.

HPC is **mocked** in Phase 1 (plan): `submit_to_hpc` / `poll_hpc_status`
simulate a SLURM-style async job so the durable path (submit → heartbeat-poll →
parse) is provable end-to-end without a real cluster. When a real backend lands,
only this module changes — the workflow and the agent stay put.

Activities may do anything (sleep, network, hashing); determinism is the
workflow's concern, not theirs.
"""

import asyncio
import re

from temporalio import activity

from chemclaw.config import settings
from workflows.models import HpcJobHandle, QMJobInput, QMJobResult, qm_job_key

# Format the mock scheduler emits; parsed by `parse_qm_output`. Kept next to the
# only two functions that produce/consume it so the contract stays local.
_MOCK_OUTPUT_TEMPLATE = "energy={energy:.6f} converged={converged}"
_ENERGY_RE = re.compile(r"energy=(-?\d+\.\d+)")
_CONVERGED_RE = re.compile(r"converged=(True|False)")


@activity.defn
async def prepare_input(job: QMJobInput) -> QMJobInput:
    """Validate and normalize the request before submission (plan step 1.2).

    Trivial by design (first activity in the spine), but does real work: trims
    the SMILES and rejects a whitespace-only value, so a malformed request fails
    fast at the durable boundary rather than deep inside the mock (gate G4).
    """
    smiles = job.molecule_smiles.strip()
    if not smiles:
        raise ValueError("molecule_smiles must not be blank")
    return job.model_copy(update={"molecule_smiles": smiles})


@activity.defn
async def submit_to_hpc(job: QMJobInput) -> HpcJobHandle:
    """MOCK: enqueue the QM job on the scheduler and return a handle.

    The id is a deterministic hash of the inputs (reproducible in tests). The
    short sleep models submission latency so the step is visibly distinct in the
    Temporal UI. A real impl would SSH / call the scheduler API here.
    """
    await asyncio.sleep(settings.hpc_mock_submit_seconds)
    return HpcJobHandle(scheduler_job_id=f"mock-{qm_job_key(job)}")


@activity.defn
async def poll_hpc_status(handle: HpcJobHandle) -> str:
    """MOCK long-running poll until the job 'completes'; returns raw output.

    Heartbeats every `hpc_poll_interval_seconds` (plan step 1.3): the heartbeat
    is what lets Temporal notice a dead worker and retry the poll elsewhere
    within `qm_poll_heartbeat_timeout_seconds`. A real impl would query
    squeue/sacct instead of sleeping.
    """
    elapsed = 0.0
    while elapsed < settings.hpc_mock_run_seconds:
        activity.heartbeat(f"{handle.scheduler_job_id}: running ({elapsed:.0f}s)")
        await asyncio.sleep(settings.hpc_poll_interval_seconds)
        elapsed += settings.hpc_poll_interval_seconds
    # Deterministic fake energy so results vary by molecule without a real QM run.
    fake_energy = -1.0 * (int(handle.scheduler_job_id[-4:], 16) % 1000) / 10.0
    return _MOCK_OUTPUT_TEMPLATE.format(energy=fake_energy, converged=True)


@activity.defn
async def parse_qm_output(job: QMJobInput, raw_output: str) -> QMJobResult:
    """Parse raw HPC output into a typed result (plan step 1.4).

    Real parsing against the mock output format; raises on unparseable output so
    a corrupt result never silently becomes a `converged=False`, energy-0 record.
    """
    energy_match = _ENERGY_RE.search(raw_output)
    converged_match = _CONVERGED_RE.search(raw_output)
    if energy_match is None or converged_match is None:
        raise ValueError(f"unparseable QM output: {raw_output!r}")
    return QMJobResult(
        molecule_smiles=job.molecule_smiles,
        method=job.method,
        basis_set=job.basis_set,
        total_energy_hartree=float(energy_match.group(1)),
        converged=converged_match.group(1) == "True",
        requested_by=job.requested_by,
    )
