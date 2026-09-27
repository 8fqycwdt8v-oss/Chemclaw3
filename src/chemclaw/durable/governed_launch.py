"""Launching a connector job through the same governed chain a chat turn's launch goes through.

**Here rather than beside either caller, because there are now two.** `template_activities` wrote
this first, for a finding worth restating: a template resolving and launching a job *directly* did
so with no audit row and no authorization, while the same job asked for in a conversation was
audited and gated. The hypothesis tournament is the second path that chooses a job's arguments and
starts it without a human in the loop, so it needs the identical treatment — and a second
hand-rolled copy is how the two would drift the first time the audit shape changed.

The pre-flight is wrapped in a tool built on the spot rather than found on the surface, because
what is audited is not a tool the model can call: it is the resolution and validation done before
Temporal starts the workflow. Naming it after the job is what makes the row legible in the trail,
and what makes it read the same as a chat launch's row.

A refusal propagates after being recorded as an `error` outcome, exactly as a denied chat tool call
is.
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

    `action` is the pre-flight itself — normally `prepare_job_launch`, which validates the
    arguments against the job's declared params model, authorizes the expensive trigger and runs
    the job's own precondition. It is passed in rather than called here so this module stays a
    governance wrapper and does not become a second place that knows how a job is prepared.
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
