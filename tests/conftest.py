"""Shared pytest fixtures.

`fast_mock` shrinks the mock-HPC sleep durations so server-backed workflow tests
finish in milliseconds; it is autouse but harmless to tests that don't touch
those settings, and it reverts cleanly via monkeypatch after each test.
"""

import pytest

from chemclaw.config import settings


@pytest.fixture(autouse=True)
def fast_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the mock HPC job complete near-instantly for tests."""
    monkeypatch.setattr(settings, "hpc_mock_submit_seconds", 0.0)
    monkeypatch.setattr(settings, "hpc_mock_run_seconds", 0.02)
    monkeypatch.setattr(settings, "hpc_poll_interval_seconds", 0.01)
