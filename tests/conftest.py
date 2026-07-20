"""Shared pytest fixtures and test fakes.

`fast_mock` shrinks the mock-HPC sleep durations so server-backed workflow tests
finish in milliseconds; it is autouse but harmless to tests that don't touch
those settings, and it reverts cleanly via monkeypatch after each test.

`FakeSubmitter` is the one PR-gate test double: every test that exercises a
"propose a note" path imports it (`from tests.conftest import FakeSubmitter`)
instead of redefining an identical fake per file (DRY).
"""

import pytest

from chemclaw.config import settings
from kg.pr_gate import NoteSubmission


class FakeSubmitter:
    """Records PR-gate submissions instead of touching git, returning a stub PR ref."""

    def __init__(self) -> None:
        """Start with no captured submissions."""
        self.submissions: list[NoteSubmission] = []

    async def submit(self, submission: NoteSubmission) -> str:
        """Capture the submission and return a fake PR reference."""
        self.submissions.append(submission)
        return f"pr://{submission.branch}"


@pytest.fixture(autouse=True)
def fast_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the mock HPC job complete near-instantly for tests."""
    monkeypatch.setattr(settings, "hpc_mock_submit_seconds", 0.0)
    monkeypatch.setattr(settings, "hpc_mock_run_seconds", 0.02)
    monkeypatch.setattr(settings, "hpc_poll_interval_seconds", 0.01)
