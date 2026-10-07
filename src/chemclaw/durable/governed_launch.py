"""Launching a connector job through the same governed chain a chat turn's launch goes through.

A job launched by a template or by the hypothesis tournament, without a human in the loop, gets
the same audit row and authorization as one asked for in conversation. The pre-flight (resolution
and validation before Temporal starts the workflow) is wrapped in a tool built on the spot and
named after the job, so its audit row reads like a chat launch's. A refusal is recorded as an
`error` outcome and then propagates.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.tools import tool as tool_decorator

from chemclaw.agent.profiles import get_profile
from chemclaw.agent.tool_invocation import invoke_governed


async def audited_launch(
    job: str,
    arguments: dict[str, Any],
    action: Callable[[], dict[str, Any]],
    *,
    actor: str,
    correlation_id: str,
) -> dict[str, Any]:
    """Run a job launch's pre-flight through the governed chain and return its validated payload.

    `action` is the pre-flight itself, normally `prepare_job_launch` (validate arguments, authorize
    the trigger, run the job's precondition). Passed in so this module stays a governance wrapper.
    """

    @tool_decorator(name_or_callable=job, description=f"launch the {job!r} job")
    async def _launch(**_kwargs: Any) -> dict[str, Any]:
        return action()

    payload: dict[str, Any] = await invoke_governed(
        _launch,
        arguments,
        correlation_id=correlation_id,
        actor=actor,
        profile=get_profile(None),
    )
    return payload
