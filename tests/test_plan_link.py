"""A launched job names the plan step it serves, without the plan ever being written.

Per `D-2026-08-27-a-job-names-the-step-it-serves`, a middleware binds the current step per tool
call and the launch copies it onto the workflow input, the durable record and the `job_started`
announcement. Proven: the selection rule, bind/reset, the launch stamp, the signal, and that a
launch leaves the plan's identity (and so its approval) untouched.
"""

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessage

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
    assert plan_hash == plan_identity(_TODOS)


def test_no_step_in_flight_stamps_the_empty_string_not_a_guess() -> None:
    """A plan whose steps are all pending or done gives no step to blame a launch on."""
    todos = [{"content": "a", "status": "pending"}, {"content": "b", "status": "completed"}]
    step, plan_hash = plan_link_from_todos(todos)
    assert step == ""
    # The plan still has an identity — a job launched between steps still belongs to a revision.
    assert plan_hash == plan_identity(todos)


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
        assert seen == [("run the conformer search", plan_identity(_TODOS))]
        # Reset on the way out: off the call, the ambient link is the empty default again.
        assert get_current_plan_link() == ("", "")

    asyncio.run(_run())


async def test_a_raising_tool_body_still_resets_the_link() -> None:
    """`try/finally`, proven: one call's link must not leak into the next call's job."""

    async def _handler(_request: Any) -> Any:
        raise RuntimeError("the tool fell over")

    request = tool_request("run_calculation")
    object.__setattr__(request, "state", {"todos": _TODOS})
    with pytest.raises(RuntimeError):
        await run_middleware(stamp_plan_link, request, _handler)
    assert get_current_plan_link() == ("", "")


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
        before = plan_identity(todos)

        async def _handler(_request: Any) -> Any:
            return None

        request = tool_request("run_calculation")
        object.__setattr__(request, "state", {"todos": todos})
        await run_middleware(stamp_plan_link, request, _handler)
        assert [str(t["content"]) for t in todos] == [str(t["content"]) for t in _TODOS]
        assert plan_identity(todos) == before

    asyncio.run(_run())


async def test_the_job_started_announcement_carries_the_ambient_step() -> None:
    """Every launcher that announces a job announces its step — folded in at the one emit site."""

    async def _announce() -> None:
        record_job_started("job-1", "run_calculation")

    token = set_current_plan_link("run the conformer search", "hash-1")
    try:
        _, signals = await collect_signals(_announce)
    finally:
        from chemclaw.core.plan_context import reset_current_plan_link

        reset_current_plan_link(token)
    assert signals == [
        JobSignal(job_id="job-1", kind="run_calculation", plan_step="run the conformer search")
    ]


def test_the_stamp_reads_the_batchs_own_rewrite_not_the_pre_batch_snapshot() -> None:
    """A "tick step N, do step N+1" batch stamps step N+1.

    `request.state["todos"]` is the pre-batch snapshot, so the stamp reads the plan the batch
    writes, as `enforce_plan_approval` does (`plan_after_batch`).
    """

    async def _run() -> None:
        seen: list[tuple[str, str]] = []

        async def _handler(_request: Any) -> Any:
            seen.append(get_current_plan_link())
            return None

        pre_batch_todos = [
            {"content": "compute the barrier", "status": "in_progress"},
            {"content": "propose the note", "status": "pending"},
        ]
        tick = {
            "name": "write_todos",
            "args": {
                "todos": [
                    {"content": "compute the barrier", "status": "completed"},
                    {"content": "propose the note", "status": "in_progress"},
                ]
            },
            "id": "c-plan",
        }
        this_call = {"name": "record_knowledge_note", "args": {}, "id": "c-write"}
        request = tool_request("record_knowledge_note", call_id="c-write")
        object.__setattr__(
            request,
            "state",
            {
                "todos": pre_batch_todos,
                "messages": [AIMessage(content="", tool_calls=[tick, this_call])],
            },
        )
        await run_middleware(stamp_plan_link, request, _handler)

        assert seen == [
            (
                "propose the note",
                plan_identity(tick["args"]["todos"]),  # type: ignore[index]
            )
        ]

    asyncio.run(_run())


def test_an_unanswerable_batch_rewrite_falls_back_to_the_pre_batch_snapshot() -> None:
    """Two concurrent rewrites have no answerable post-batch plan, so the stamp falls back.

    Unlike the plan gate, which fails closed there, the stamp protects no safety property, so the
    pre-batch snapshot is an acceptable best effort.
    """

    async def _run() -> None:
        seen: list[tuple[str, str]] = []

        async def _handler(_request: Any) -> Any:
            seen.append(get_current_plan_link())
            return None

        rewrite_one = {"name": "write_todos", "args": {"todos": []}, "id": "c-plan-1"}
        rewrite_two = {"name": "write_todos", "args": {"todos": []}, "id": "c-plan-2"}
        this_call = {"name": "record_knowledge_note", "args": {}, "id": "c-write"}
        request = tool_request("record_knowledge_note", call_id="c-write")
        object.__setattr__(
            request,
            "state",
            {
                "todos": _TODOS,
                "messages": [
                    AIMessage(content="", tool_calls=[rewrite_one, rewrite_two, this_call])
                ],
            },
        )
        await run_middleware(stamp_plan_link, request, _handler)

        assert seen == [("run the conformer search", plan_identity(_TODOS))]

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


def test_the_link_is_bound_inside_a_real_compiled_graph() -> None:
    """The link is bound inside a real compiled graph.

    The other tests hand-build `request.state`, which cannot show that a tool inside
    `build_langgraph_agent`'s graph receives `todos` at all; a missing channel is dropped silently
    and every job would stamp `("", "")`. This drives the real builder, `TodoListMiddleware`,
    `write_todos` and `ToolNode`, with a tool reading the ambient link as `connectors/jobs.py` does.
    `harness_autonomy="execute"` keeps the plan gate out of the way and proves the stamp follows the
    harness, not the gate.
    """
    from chemclaw.agent.audit import NullAuditSink
    from chemclaw.agent.langgraph_agent import build_langgraph_agent
    from chemclaw.agent.profiles import AgentProfile
    from chemclaw.agent.state import turn_config, turn_input
    from chemclaw.core.tool_registry import _REGISTRY, register_tool
    from tests.fakes_langgraph import ScriptedChatModel

    seen: list[tuple[str, str]] = []

    async def plan_link_probe() -> str:
        """Record the ambient plan link, exactly as a job launcher reads it."""
        seen.append(get_current_plan_link())
        return "recorded"

    # `tools` is required by the plan tool's schema (`agent/plan_scope.py`), so a `write_todos`
    # call omitting it never reaches state at all — the model gets a validation error instead.
    # The link itself reads only `content` and `status`.
    plan = [
        {"content": "gather the evidence", "status": "completed", "tools": []},
        {"content": "run the conformer search", "status": "in_progress", "tools": []},
        {"content": "propose a note", "status": "pending", "tools": ["record_knowledge_note"]},
    ]
    register_tool(plan_link_probe)
    try:
        graph = build_langgraph_agent(
            # `ScriptedChatModel` takes a tool call as `{"name", "args"}` and a plain string as
            # the final answer; it mints the call ids itself.
            ScriptedChatModel(
                [
                    # The model writes the plan, then acts on the step it just marked in flight —
                    # two assistant messages, which is what puts `todos` in state before the call.
                    {"name": "write_todos", "args": {"todos": plan}},
                    {"name": "plan_link_probe", "args": {}},
                    "done",
                ]
            ),
            profile=AgentProfile(
                name="plan-link-probe", harness_enabled=True, harness_autonomy="execute"
            ),
            audit_sink=NullAuditSink(),
        )
        asyncio.run(graph.ainvoke(turn_input("run the search"), turn_config()))
    finally:
        _REGISTRY.pop("plan_link_probe", None)

    assert seen == [("run the conformer search", plan_identity(plan))], (
        "the tool body saw no plan link, so `todos` did not reach it through the compiled graph"
    )
