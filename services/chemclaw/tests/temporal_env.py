"""Shared helpers for Temporal-backed tests.

The time-skipping test server's binary is downloaded on first use; in a
network-restricted sandbox that fails, so `start_env_or_skip` turns that into a
skip (the tests run fully in CI). Kept in one place so every workflow/tool test
uses the same server bootstrap and pydantic-configured client (DRY).
"""

from collections.abc import Callable, Sequence
from typing import Any

import pytest
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

from workflows.activities import (
    parse_qm_output,
    poll_hpc_status,
    prepare_input,
    submit_to_hpc,
)

# The full activity set the QM workflow needs, registered on every test worker.
QM_ACTIVITIES: Sequence[Callable[..., Any]] = [
    prepare_input,
    submit_to_hpc,
    poll_hpc_status,
    parse_qm_output,
]


async def start_env_or_skip() -> WorkflowEnvironment:
    """Start the time-skipping test server, or skip if its binary can't be fetched."""
    try:
        return await WorkflowEnvironment.start_time_skipping()
    except RuntimeError as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"Temporal test server unavailable (offline sandbox): {exc}")


def pydantic_client(env: WorkflowEnvironment) -> Client:
    """Rebuild the env's client with our pydantic data converter."""
    config = env.client.config()
    config["data_converter"] = pydantic_data_converter
    return Client(**config)
