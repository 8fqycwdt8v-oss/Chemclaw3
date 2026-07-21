"""The Nextflow launcher adapter drives a full run lifecycle offline (plan F5-T1/T3).

A stateful `httpx.MockTransport` stands in for the Seqera/Tower REST API: `launch_run` posts the
pipeline and gets a run id; `poll_run` walks SUBMITTED→RUNNING→SUCCEEDED; `fetch_artifacts` pulls
the QM output blob. Also proves the F5-T3 cache-key rule: a configured pipeline version enters
`qm_job_key` (a bump is a miss) while an empty version leaves the key unchanged.
"""

import asyncio

import httpx
import pytest

from chemclaw.config import settings
from workflows.hpc import nextflow
from workflows.models import HpcJobHandle, QMJobInput, qm_job_key


@pytest.fixture
def _launcher_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the adapter at a fake launcher + artifact store with a token."""
    monkeypatch.setattr(settings, "hpc_launch_interface", "nextflow")
    monkeypatch.setattr(settings, "hpc_api_base_url", "https://tower.test/api")
    monkeypatch.setattr(settings, "hpc_api_token", "tower-token")
    monkeypatch.setattr(settings, "hpc_pipeline_name", "qm-pipeline")
    monkeypatch.setattr(settings, "hpc_pipeline_version", "1.4.0")
    monkeypatch.setattr(settings, "hpc_artifact_store_url", "https://blobs.test")


def _job() -> QMJobInput:
    return QMJobInput(molecule_smiles="CCO", method="B3LYP", basis_set="def2-SVP")


class _FakeLauncher:
    """A stateful fake: a launched run reports SUBMITTED, then RUNNING, then SUCCEEDED."""

    def __init__(self) -> None:
        self.polls = 0
        self.launched: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/workflow/launch"):
            self.launched.append(request)
            return httpx.Response(200, json={"workflowId": "run-77"})
        if path.endswith("/qm_output.txt"):
            return httpx.Response(200, text="energy=-142.500000 converged=True")
        if "/workflow/run-77" in path:
            self.polls += 1
            status = ["SUBMITTED", "RUNNING", "SUCCEEDED"][min(self.polls - 1, 2)]
            return httpx.Response(200, json={"workflow": {"status": status}})
        return httpx.Response(404, text=f"unexpected {path}")


def test_full_lifecycle(_launcher_env: None) -> None:
    """Launch → poll to SUCCEEDED → fetch returns the QM output text, with auth + params sent."""
    launcher = _FakeLauncher()
    transport = httpx.MockTransport(launcher.handler)

    async def _drive() -> str:
        handle = await nextflow.launch_run(_job(), transport=transport)
        assert handle.scheduler_job_id == "run-77"
        states = [await nextflow.poll_run(handle, transport=transport) for _ in range(3)]
        assert states == [
            nextflow.RunState.SUBMITTED,
            nextflow.RunState.RUNNING,
            nextflow.RunState.SUCCEEDED,
        ]
        return await nextflow.fetch_artifacts(handle, transport=transport)

    output = asyncio.run(_drive())
    assert output == "energy=-142.500000 converged=True"
    launch = launcher.launched[0]
    assert launch.headers["Authorization"] == "Bearer tower-token"
    body = launch.content.decode()
    assert '"CCO"' in body and '"qm-pipeline"' in body and '"1.4.0"' in body


def test_unknown_status_is_an_error(_launcher_env: None) -> None:
    """An unrecognized launcher status raises, rather than looping forever as 'still running'."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"workflow": {"status": "WEIRD"}})

    handle = HpcJobHandle(scheduler_job_id="run-77")
    with pytest.raises(nextflow.NextflowError, match="unknown launcher status"):
        asyncio.run(nextflow.poll_run(handle, transport=httpx.MockTransport(handler)))


def test_unknown_status_is_non_terminal(_launcher_env: None) -> None:
    """Tower's transient `UNKNOWN` keeps polling (RUNNING), it does not fail a maybe-fine run."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"workflow": {"status": "UNKNOWN"}})

    handle = HpcJobHandle(scheduler_job_id="run-77")
    state = asyncio.run(nextflow.poll_run(handle, transport=httpx.MockTransport(handler)))
    assert state is nextflow.RunState.RUNNING  # non-terminal → the poll loop keeps going


def test_launch_rejection_is_an_error(_launcher_env: None) -> None:
    """A non-200 on launch surfaces as a typed error, not a bad handle."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(nextflow.NextflowError, match="launch failed"):
        asyncio.run(nextflow.launch_run(_job(), transport=httpx.MockTransport(handler)))


def test_pipeline_version_enters_cache_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured pipeline version changes the key (bump = miss); empty leaves it unchanged."""
    job = _job()
    monkeypatch.setattr(settings, "hpc_pipeline_version", "")
    base = qm_job_key(job)
    monkeypatch.setattr(settings, "hpc_pipeline_version", "1.4.0")
    v1 = qm_job_key(job)
    monkeypatch.setattr(settings, "hpc_pipeline_version", "1.5.0")
    v2 = qm_job_key(job)
    assert base != v1 != v2 and base != v2  # every distinct version is a distinct cache identity
