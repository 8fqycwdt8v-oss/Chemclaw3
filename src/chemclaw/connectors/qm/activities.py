"""Activities for the QM job — every non-deterministic step and all I/O.

HPC has two backends selected by `hpc_launch_interface` (plan F5): the **mock** (default) simulates
a SLURM-style async job so the durable path (submit → heartbeat-poll → parse) is provable
end-to-end without a cluster, kept for CI/local; **nextflow** dispatches to the real launcher
(`chemclaw.connectors.qm.hpc.nextflow`). Only this module changed to make compute real — the
workflow and
the agent stay put, exactly as this module's original contract promised.

Activities may do anything (sleep, network, hashing); determinism is the workflow's concern, not
theirs.

They run on this bundle's own worker (`chemclaw.connectors.qm.worker`), never core's: the HPC
launcher
credential, the artifact store and the 24-hour poll belong to this capability alone (D-118).
"""

import asyncio
import re

import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError

from chemclaw.connectors.qm.cache import calculation_key
from chemclaw.connectors.qm.hpc import nextflow
from chemclaw.connectors.qm.specs import (
    HpcJobHandle,
    QmCacheLookup,
    QMJobInput,
    QMJobResult,
    QmJobSpec,
    qm_job_key,
)
from chemclaw.connectors.queues import bundle_queue
from chemclaw.core.chem import require_canonical_smiles
from chemclaw.core.config import settings
from chemclaw.durable.registry import durable_activity
from chemclaw.science.calc.postgres_store import default_store
from chemclaw.science.calc.store import StoredResult

# Format the mock scheduler emits; parsed by `parse_qm_output`. Kept next to the
# only two functions that produce/consume it so the contract stays local.
_MOCK_OUTPUT_TEMPLATE = "energy={energy:.6f} converged={converged}"
_ENERGY_RE = re.compile(r"energy=(-?\d+\.\d+)")
_CONVERGED_RE = re.compile(r"converged=(True|False)")


@durable_activity(bundle_queue("qm"))
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


@durable_activity(bundle_queue("qm"))
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


@durable_activity(bundle_queue("qm"))
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

    Transient and terminal failures are deliberately kept apart: a launcher/network blip
    (`NextflowError` from a non-200 poll, or an `httpx` transport error) is absorbed by the loop —
    up to `hpc_poll_max_consecutive_errors` in a row — because during an up-to-24h run each blip
    would otherwise burn one of the activity's few shared retry attempts and fail a job whose HPC
    run actually succeeds. A run the launcher reports FAILED is terminal: it raises a
    *non-retryable* `ApplicationError`, since re-polling a failed run can never change the outcome,
    and it must never silently become an unparseable-output error downstream.
    """
    consecutive_errors = 0
    while True:
        activity.heartbeat(f"{handle.scheduler_job_id}: polling")
        try:
            state = await nextflow.poll_run(handle)
        except (nextflow.NextflowError, httpx.HTTPError) as exc:
            consecutive_errors += 1
            if consecutive_errors >= settings.hpc_poll_max_consecutive_errors:
                raise
            activity.logger.warning(
                "transient poll error for %s (%d consecutive): %s",
                handle.scheduler_job_id,
                consecutive_errors,
                exc,
            )
            await asyncio.sleep(settings.hpc_poll_interval_seconds)
            continue
        consecutive_errors = 0
        if state is nextflow.RunState.SUCCEEDED:
            return await nextflow.fetch_artifacts(handle)
        if state is nextflow.RunState.FAILED:
            raise ApplicationError(
                f"run {handle.scheduler_job_id} failed",
                type="NextflowRunFailed",
                non_retryable=True,
            )
        await asyncio.sleep(settings.hpc_poll_interval_seconds)


@durable_activity(bundle_queue("qm"))
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


@durable_activity(bundle_queue("qm"))
@activity.defn
async def lookup_qm_result(job: QMJobInput) -> QmCacheLookup:
    """Return this calculation's store key, and its result if it has already been computed (D-157).

    The read half of compute-once for QM. Without it, persistence alone would make an expensive
    result *durable* but never *reused*: the only thing stopping a repeat run was the deterministic
    workflow id, and Temporal frees that id once the execution ages out of retention — so the same
    request re-ran hours of cluster time and simply overwrote the identical row.

    Runs after `prepare_input`, not before, so the SMILES has already been through the durable
    boundary's validation gate (G4): a malformed structure still fails in the activity that owns
    that job, and the key derived here is over an already-canonical molecule.

    A miss and a disabled cache are the same shape — no result — because the caller's behaviour is
    identical either way; only the key differs, and an empty key is what disables the note's
    `calc_refs` downstream.

    **A hit is re-attributed to whoever asked this time.** `requested_by` rides on `QMJobResult`
    but is deliberately *not* part of the key: the energy of a molecule does not depend on who
    wanted it, which is why identical science shares one entry across users (`qm_job_key` excludes
    it for the same reason). Returning the stored value would therefore credit every future cache
    hit to whoever happened to compute it first — and that string becomes the note's `source`, so
    the audit trail would name the wrong chemist for a run they did not request. The science comes
    from the store; the attribution comes from this request.
    """
    if not settings.qm_persist_to_calc_store:
        return QmCacheLookup()
    key = calculation_key(job)
    hit = await default_store().get(key)
    if hit is None:
        return QmCacheLookup(calc_key=key.as_str())
    stored = QMJobResult.model_validate(hit.result)
    return QmCacheLookup(
        calc_key=key.as_str(),
        result=stored.model_copy(update={"requested_by": job.requested_by}),
    )


@durable_activity(bundle_queue("qm"))
@activity.defn
async def persist_qm_result(result: QMJobResult) -> str:
    """Persist the finished calculation in the shared calculation store; return its key (D-157).

    The write that makes an hours-long cluster run durable independently of two conditional
    things: whether a human merges the note's PR, and how long Temporal retains the execution.
    Until this existed, both had to hold or the result was gone and the next identical request
    re-ran the job (see `chemclaw.connectors.qm.cache`).

    Returns the flat `CalculationKey` string so the workflow can hand it to the note as a
    `calc_refs` entry — making this the *first* producer for the crosslink read side
    (`chemclaw.kg.crosslink`), which until now had no writer at all. Returns `""` when
    `qm_persist_to_calc_store` is off, which the note reads as "no reference to record".

    The flag is checked here rather than in the workflow deliberately: a workflow that branched on
    config would decide differently on replay if an operator flipped it mid-run, and determinism is
    the workflow's concern (this module's own contract). One activity round-trip when the feature
    is disabled is the price of that, and it is cheap next to a DFT run.

    `compute_seconds` is deliberately left unset. It records what *this process* spent computing,
    and the wall time here is a store write — the cluster's own runtime is not threaded back
    through the poll. Recording the write's duration would misreport the calculation's cost to
    every consumer of that column (`durable/artifact_eviction` orders by it).
    """
    if not settings.qm_persist_to_calc_store:
        return ""
    key = calculation_key(
        QmJobSpec(
            molecule_smiles=result.molecule_smiles,
            method=result.method,
            basis_set=result.basis_set,
        )
    )
    await default_store().put(
        StoredResult(key=key, result=result.model_dump(mode="json")),
    )
    return key.as_str()
