"""Shared helpers for Temporal-backed tests.

The time-skipping test server's binary is downloaded on first use; in a
network-restricted sandbox that fails, so `start_env_or_skip` turns that into a
skip (the tests run fully in CI). Kept in one place so every workflow/tool test
uses the same server bootstrap and pydantic-configured client (DRY).
"""

import pytest
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment


async def start_env_or_skip() -> WorkflowEnvironment:
    """Start the time-skipping test server, or skip if its binary can't be fetched."""
    try:
        return await WorkflowEnvironment.start_time_skipping()
    except RuntimeError as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"Temporal test server unavailable (offline sandbox): {exc}")


async def start_local_env_or_skip() -> WorkflowEnvironment:
    """Start a **real-time** local dev server, or skip if its binary can't be fetched.

    The time-skipping server above is the right instrument for a workflow whose behaviour is about
    timers; it is the wrong one for a test about *wall-clock* worker events — a terminate, a cache
    eviction, a graceful drain, or a client call that must return promptly on a run that is still
    going. Under time skipping the server advances the clock whenever every worker is idle, so a
    workflow parked on an unserved child queue is fast-forwarded to its own execution timeout
    instead of staying `RUNNING`, which is exactly the state those tests need to observe.
    """
    try:
        return await WorkflowEnvironment.start_local()
    except RuntimeError as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"Temporal dev server unavailable (offline sandbox): {exc}")


def pydantic_client(env: WorkflowEnvironment) -> Client:
    """Rebuild the env's client with our pydantic data converter."""
    config = env.client.config()
    config["data_converter"] = pydantic_data_converter
    return Client(**config)
