"""A launched job names the plan step it serves — without the plan ever being written.

The decision is D-2026-08-27-a-job-names-the-step-it-serves. The old link was a marker prefixed
into a todo's `content`, deleted twice because it revoked the approval keyed on the plan's
identity. What replaced it is a stamp on the *job*: a middleware
binds the current step ambiently per tool call, and the launch copies it onto the workflow input,
the durable record and the `job_started` announcement. So what is proven here is the whole chain —
the selection rule, the bind/reset discipline, the launch stamp, the signal — and the property the
whole design exists to protect: a launch leaves the plan's identity untouched.
"""

import asyncio
from typing import Any

import pytest

from chemclaw.agent.plan_gate import plan_identity
from chemclaw.agent.plan_link import plan_link_from_todos, stamp_plan_link
from chemclaw.core.plan_context import get_current_plan_link, set_current_plan_link
from chemclaw.core.turn_signals import JobSignal, record_job_started
from tests.middleware import run_middleware, tool_request
from tests.signals import collect_signals

_TODOS = [
    {"content": "compute the pKa of the amine", "status": "completed"},
    {"content": "run the conformer search", "status": "in_progress"},
    {"content": "propose a note with both results", "status": "pending"},
]


def test_the_link_is_the_first_in_progress_step_and_the_plans_own_identity() -> None:
    """The step is the one in flight; the hash is the same identity the approval row is keyed on."""
    step, plan_hash = plan_link_from_todos(_TODOS)
    assert step == "run the conformer search"
    assert plan_hash == plan_identity([str(t["content"]) for t in _TODOS])


def test_no_step_in_flight_stamps_the_empty_string_not_a_guess() -> None:
    """A plan whose steps are all pending or done gives no step to blame a launch on."""
    todos = [{"content": "a", "status": "pending"}, {"content": "b", "status": "completed"}]
    step, plan_hash = plan_link_from_todos(todos)
    assert step == ""
    # The plan still has an identity — a job launched between steps still belongs to a revision.
    assert plan_hash == plan_identity(["a", "b"])


def test_no_plan_at_all_stamps_the_empty_link() -> None:
    """`plan_identity` refuses the empty plan's hash, and the link honours that refusal."""
    assert plan_link_from_todos([]) == ("", "")


def test_the_middleware_binds_the_link_around_the_tool_body_and_resets_after() -> None:
    """The launcher inside the body reads the link; the next call cannot inherit it."""

    async def _run() -> None:
        seen: list[tuple[str, str]] = []

        async def _handler(_request: Any) -> Any:
            seen.append(get_current_plan_link())
            return "ok"

        request = tool_request("run_calculation")
        object.__setattr__(request, "state", {"todos": _TODOS})
        result = await run_middleware(stamp_plan_link, request, _handler)
        assert result == "ok"
        assert seen == [
            ("run the conformer search", plan_identity([str(t["content"]) for t in _TODOS]))
        ]
        # Reset on the way out: off the call, the ambient link is the empty default again.
        assert get_current_plan_link() == ("", "")

    asyncio.run(_run())


def test_a_raising_tool_body_still_resets_the_link() -> None:
    """`try/finally`, proven: one call's link must not leak into the next call's job."""

    async def _run() -> None:
        async def _handler(_request: Any) -> Any:
            raise RuntimeError("the tool fell over")

        request = tool_request("run_calculation")
        object.__setattr__(request, "state", {"todos": _TODOS})
        with pytest.raises(RuntimeError):
            await run_middleware(stamp_plan_link, request, _handler)
        assert get_current_plan_link() == ("", "")

    asyncio.run(_run())


def test_an_absent_todos_key_binds_the_empty_link_rather_than_reaching_elsewhere() -> None:
    """A subagent's stripped state (or a stateless caller) means "not launched from a step"."""

    async def _run() -> None:
        seen: list[tuple[str, str]] = []

        async def _handler(_request: Any) -> Any:
            seen.append(get_current_plan_link())
            return None

        await run_middleware(stamp_plan_link, tool_request("run_calculation"), _handler)
        assert seen == [("", "")]

    asyncio.run(_run())


def test_the_middleware_reads_the_plan_and_never_writes_it() -> None:
    """The whole reason the link lives on the job: a launch must not perturb `plan_identity`."""

    async def _run() -> None:
        todos = [dict(t) for t in _TODOS]
        before = plan_identity([str(t["content"]) for t in todos])

        async def _handler(_request: Any) -> Any:
            return None

        request = tool_request("run_calculation")
        object.__setattr__(request, "state", {"todos": todos})
        await run_middleware(stamp_plan_link, request, _handler)
        assert [str(t["content"]) for t in todos] == [str(t["content"]) for t in _TODOS]
        assert plan_identity([str(t["content"]) for t in todos]) == before

    asyncio.run(_run())


def test_the_job_started_announcement_carries_the_ambient_step() -> None:
    """Every launcher that announces a job announces its step — folded in at the one emit site."""

    async def _announce() -> None:
        record_job_started("job-1", "run_calculation")

    async def _run() -> None:
        token = set_current_plan_link("run the conformer search", "hash-1")
        try:
            _, signals = await collect_signals(_announce)
        finally:
            from chemclaw.core.plan_context import reset_current_plan_link

            reset_current_plan_link(token)
        assert signals == [
            JobSignal(job_id="job-1", kind="run_calculation", plan_step="run the conformer search")
        ]

    asyncio.run(_run())


def test_the_stamp_is_attached_exactly_when_the_harness_is() -> None:
    """The todo list exists in `execute` autonomy too, so the harness attaches it, not the gate."""
    from chemclaw.agent.langgraph_agent import tool_governance_middleware
    from chemclaw.agent.profiles import AgentProfile

    harness_on = AgentProfile(name="on", harness_enabled=True, harness_autonomy="execute")
    harness_off = AgentProfile(name="off", harness_enabled=False)
    audit = object()
    assert stamp_plan_link in tool_governance_middleware(audit, harness_on)
    assert stamp_plan_link not in tool_governance_middleware(audit, harness_off)
