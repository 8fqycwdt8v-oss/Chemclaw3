"""Shared helpers for Temporal-backed tests.

The time-skipping test server's binary is downloaded on first use; where that fails,
`start_env_or_skip` skips (the tests run fully in CI). One server bootstrap and client for every
workflow test.
"""

from collections.abc import Sequence

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


async def start_local_env_or_skip(dynamic_config: Sequence[str] = ()) -> WorkflowEnvironment:
    """Start a **real-time** local dev server, or skip if its binary can't be fetched.

    For wall-clock worker events (terminate, eviction, drain, a prompt return on a running run): a
    time-skipping server fast-forwards an idle workflow to its execution timeout instead of leaving
    it `RUNNING`. `dynamic_config` entries (`key=value`) set the server's dynamic config, e.g. a
    namespace rate limit a test needs the broker to enforce.
    """
    extra = [arg for entry in dynamic_config for arg in ("--dynamic-config-value", entry)]
    try:
        return await WorkflowEnvironment.start_local(dev_server_extra_args=extra)
    except RuntimeError as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"Temporal dev server unavailable (offline sandbox): {exc}")


def pydantic_client(env: WorkflowEnvironment) -> Client:
    """Rebuild the env's client with our pydantic data converter."""
    config = env.client.config()
    config["data_converter"] = pydantic_data_converter
    return Client(**config)
