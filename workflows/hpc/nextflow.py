"""The Nextflow launcher adapter: launch a run, poll it, fetch its artifacts (plan F5-T1).

One seam behind the QM activities so the durable workflow never changes when the compute backend
becomes real (ADR D-A5a chose the Seqera Platform / Tower REST API — status is a simple GET, no SSH
session to keep alive across a durable poll). Each function takes an injectable `httpx` transport so
the whole launch→poll→fetch lifecycle is proven offline against a fake endpoint, with no cluster.

The three functions map one-to-one onto what the poll activity needs: `launch_run` returns the same
`HpcJobHandle` the mock returns (so the workflow is agnostic), `poll_run` returns a coarse run state
the activity loops on, and `fetch_artifacts` pulls the raw QM output text a finished run produced.
"""

from enum import StrEnum

import httpx

from chemclaw.config import settings
from chemclaw.http import error_detail
from workflows.models import HpcJobHandle, QMJobInput


class NextflowError(RuntimeError):
    """The launcher was misconfigured, rejected a request, or returned an unusable response."""


class RunState(StrEnum):
    """A coarse launcher run state the poll activity loops on (terminal: SUCCEEDED/FAILED)."""

    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


# Launcher status strings that map onto our terminal/non-terminal states. An *unrecognized* status
# is an error (surfaces loudly, never hangs), but Tower's `UNKNOWN` is treated as non-terminal
# (transient before a run resolves): failing a run that may still succeed is worse than polling on —
# the run timeout bounds a genuinely stuck run either way (review finding).
_STATE_BY_LAUNCHER_STATUS = {
    "SUBMITTED": RunState.SUBMITTED,
    "PENDING": RunState.SUBMITTED,
    "UNKNOWN": RunState.RUNNING,
    "RUNNING": RunState.RUNNING,
    "SUCCEEDED": RunState.SUCCEEDED,
    "COMPLETED": RunState.SUCCEEDED,
    "FAILED": RunState.FAILED,
    "CANCELLED": RunState.FAILED,
}


def _auth_headers() -> dict[str, str]:
    """Bearer auth for the launcher (token arrives via the HPC bridge / a mounted secret, F4-T6)."""
    return {"Authorization": f"Bearer {settings.hpc_api_token}"} if settings.hpc_api_token else {}


async def _client(transport: httpx.AsyncBaseTransport | None) -> httpx.AsyncClient:
    """Build an httpx client for the launcher, timeout-bounded from config; transport for tests."""
    return httpx.AsyncClient(
        base_url=settings.hpc_api_base_url,
        headers=_auth_headers(),
        timeout=settings.hpc_http_timeout_seconds,
        transport=transport,
    )


async def launch_run(
    job: QMJobInput, *, transport: httpx.AsyncBaseTransport | None = None
) -> HpcJobHandle:
    """Submit the QM pipeline for `job` and return a handle carrying the launcher's run id.

    Raises:
        NextflowError: When the launcher rejects the launch or returns no run id.
    """
    payload = {
        "pipeline": settings.hpc_pipeline_name,
        "revision": settings.hpc_pipeline_version,
        "params": {
            "smiles": job.molecule_smiles,
            "method": job.method,
            "basis_set": job.basis_set,
        },
    }
    async with await _client(transport) as client:
        response = await client.post("/workflow/launch", json=payload)
    if response.status_code != httpx.codes.OK:
        raise NextflowError(f"launch failed: {error_detail(response)}")
    run_id = response.json().get("workflowId")
    if not run_id:
        raise NextflowError("launcher returned no workflowId")
    return HpcJobHandle(scheduler_job_id=str(run_id))


async def poll_run(
    handle: HpcJobHandle, *, transport: httpx.AsyncBaseTransport | None = None
) -> RunState:
    """Return the current `RunState` of the run, mapping the launcher's status string.

    Raises:
        NextflowError: When the run is unknown or its status is not one we recognize.
    """
    async with await _client(transport) as client:
        response = await client.get(f"/workflow/{handle.scheduler_job_id}")
    if response.status_code != httpx.codes.OK:
        raise NextflowError(f"poll failed: {error_detail(response)}")
    status = str(response.json().get("workflow", {}).get("status", "")).upper()
    state = _STATE_BY_LAUNCHER_STATUS.get(status)
    if state is None:
        raise NextflowError(f"unknown launcher status {status!r}")
    return state


async def fetch_artifacts(
    handle: HpcJobHandle, *, transport: httpx.AsyncBaseTransport | None = None
) -> str:
    """Fetch the finished run's raw QM output text from the artifact store.

    Returns the same `energy=… converged=…` text shape `parse_qm_output` already parses, so parsing
    is unchanged whether the output came from the mock or a real run.

    Raises:
        NextflowError: When the artifact cannot be fetched.
    """
    url = f"{settings.hpc_artifact_store_url}/{handle.scheduler_job_id}/qm_output.txt"
    async with await _client(transport) as client:
        response = await client.get(url)
    if response.status_code != httpx.codes.OK:
        raise NextflowError(f"artifact fetch failed: {error_detail(response)}")
    return response.text
