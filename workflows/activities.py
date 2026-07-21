"""Activities for the QM job — every non-deterministic step and all I/O.

HPC has two backends selected by `hpc_launch_interface` (plan F5): the **mock** (default) simulates
a SLURM-style async job so the durable path (submit → heartbeat-poll → parse) is provable
end-to-end without a cluster, kept for CI/local; **nextflow** dispatches to the real launcher
(`workflows.hpc.nextflow`). Only this module changed to make compute real — the workflow and the
agent stay put, exactly as this module's original contract promised.

Activities may do anything (sleep, network, hashing); determinism is the workflow's concern, not
theirs.
"""

import asyncio
import re

from temporalio import activity

from chemclaw.chem import require_canonical_smiles
from chemclaw.config import settings
from workflows.hpc import nextflow
from workflows.models import HpcJobHandle, QMJobInput, QMJobResult, qm_job_key

# Format the mock scheduler emits; parsed by `parse_qm_output`. Kept next to the
# only two functions that produce/consume it so the contract stays local.
_MOCK_OUTPUT_TEMPLATE = "energy={energy:.6f} converged={converged}"
_ENERGY_RE = re.compile(r"energy=(-?\d+\.\d+)")
_CONVERGED_RE = re.compile(r"converged=(True|False)")


@activity.defn
async def prepare_input(job: QMJobInput) -> QMJobInput:
    """Validate and normalize the request before submission (plan step 1.2).

    The first activity in the spine and the durable-boundary validation gate (G4):
    it canonicalizes the SMILES via RDKit, which both rejects a structurally
    invalid molecule (`InvalidSmilesError`, non-retryable bad data) and normalizes
    equivalent spellings to one form — so a malformed request fails fast here rather
    than flowing through the mock into a stored result, and the same molecule always
    yields the same downstream workflow id / cache key (D-011).
    """
    smiles = require_canonical_smiles(job.molecule_smiles)
    return job.model_copy(update={"molecule_smiles": smiles})


@activity.defn
async def submit_to_hpc(job: QMJobInput) -> HpcJobHandle:
    """Enqueue the QM job and return a handle — via the real launcher or the mock (plan F5).

    `nextflow` launches the pipeline on the real backend; `mock` returns a deterministic
    inputs-derived id (reproducible in tests) after a short sleep that models submission latency so
    the step is visibly distinct in the Temporal UI. The handle shape is identical either way, so
    the workflow is agnostic.
    """
    if settings.hpc_launch_interface == "nextflow":
        return await nextflow.launch_run(job)
    await asyncio.sleep(settings.hpc_mock_submit_seconds)
    return HpcJobHandle(scheduler_job_id=f"mock-{qm_job_key(job)}")


@activity.defn
async def poll_hpc_status(handle: HpcJobHandle) -> str:
    """Poll until the job completes and return its raw output — real launcher or mock (plan F5).

    Either path heartbeats every `hpc_poll_interval_seconds` (plan step 1.3): the heartbeat is what
    lets Temporal notice a dead worker and retry the poll elsewhere within
    `qm_poll_heartbeat_timeout_seconds`. `nextflow` polls the launcher's status endpoint until a
    terminal state, then fetches the output artifact; `mock` sleeps for the simulated run time and
    synthesizes a deterministic output. Both return the same `energy=… converged=…` text shape.
    """
    if settings.hpc_launch_interface == "nextflow":
        return await _poll_nextflow(handle)
    elapsed = 0.0
    while elapsed < settings.hpc_mock_run_seconds:
        activity.heartbeat(f"{handle.scheduler_job_id}: running ({elapsed:.0f}s)")
        await asyncio.sleep(settings.hpc_poll_interval_seconds)
        elapsed += settings.hpc_poll_interval_seconds
    # Deterministic fake energy so results vary by molecule without a real QM run.
    fake_energy = -1.0 * (int(handle.scheduler_job_id[-4:], 16) % 1000) / 10.0
    return _MOCK_OUTPUT_TEMPLATE.format(energy=fake_energy, converged=True)


async def _poll_nextflow(handle: HpcJobHandle) -> str:
    """Heartbeat-poll the Nextflow launcher to a terminal state, then fetch the output artifact.

    Raises `NextflowError` on a failed run (surfaced through the workflow's retry policy) so a
    failed compute never silently becomes an unparseable-output error downstream.
    """
    while True:
        activity.heartbeat(f"{handle.scheduler_job_id}: polling")
        state = await nextflow.poll_run(handle)
        if state is nextflow.RunState.SUCCEEDED:
            return await nextflow.fetch_artifacts(handle)
        if state is nextflow.RunState.FAILED:
            raise nextflow.NextflowError(f"run {handle.scheduler_job_id} failed")
        await asyncio.sleep(settings.hpc_poll_interval_seconds)


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
