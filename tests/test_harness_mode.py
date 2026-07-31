"""The pre-execution approval gate: the model cannot authorize its own execution (REV-1, D-137).

These tests exist because the two that were already here could not see the defect. One asserted
`mode_provider.default_mode` — the *initial* value — and one asserted the loop does not
*auto*-start.
Neither ever had the model call `mode_set`, which was the only thing that broke the gate. So the
tests below drive that call directly: a test for an access-control property has to attempt the
access.
"""

import asyncio
from typing import Any, cast

import pytest
from agent_framework import AgentSession
from agent_framework._harness._mode import AgentModeProvider

from chemclaw.agent.harness_mode import (
    EXECUTE_MODE,
    MODEL_MODE_TOOL,
    PLAN_MODE,
    PlanApprovalModeProvider,
    current_plan_hash,
    execute_is_authorized,
    grant_execute,
    plan_bound,
    revoke_execute,
    session_mode,
)
from chemclaw.agent.harness_todo import complete_awaiting_job, mark_awaiting_job
from chemclaw.agent.plan_approval_store import PlanApprovalStore
from tests.pg import migrated_db_or_skip


class _Context:
    """The slice of MAF's invocation context the mode provider writes to."""

    def __init__(self) -> None:
        self.tools: list[object] = []
        self.instructions: list[str] = []
        self.messages: list[object] = []

    def extend_tools(self, source_id: str, tools: list[object]) -> None:
        """Collect injected tools, as MAF's real context does."""
        del source_id
        self.tools.extend(tools)

    def extend_instructions(self, source_id: str, instructions: Any) -> None:
        """Collect injected instructions."""
        del source_id
        self.instructions.extend(instructions if isinstance(instructions, list) else [instructions])

    def extend_messages(self, source: object, messages: list[object]) -> None:
        """Collect injected messages."""
        del source
        self.messages.extend(messages)


def _tool_names(context: _Context) -> set[str]:
    """Advertised names of everything injected into the invocation."""
    return {str(getattr(t, "name", None) or getattr(t, "__name__", "")) for t in context.tools}


def test_upstream_provider_really_does_advertise_the_flip() -> None:
    """Pin the upstream behaviour this gate exists to remove.

    Without this, a future MAF that stops injecting `mode_set` would make the gate test below pass
    vacuously — it would assert the absence of something nobody adds. If this fails, the gate may
    no longer be needed; that is a decision to make deliberately, not to discover through a green
    suite.
    """
    session = AgentSession(session_id="upstream")
    context = _Context()
    asyncio.run(
        AgentModeProvider(default_mode=PLAN_MODE).before_run(
            agent=None, session=session, context=cast(Any, context), state={}
        )
    )
    assert MODEL_MODE_TOOL in _tool_names(context)


def test_the_model_is_not_offered_the_mode_tool() -> None:
    """The gated provider withholds `mode_set` — the model has no way to change its own mode."""
    session = AgentSession(session_id="gated")
    context = _Context()
    asyncio.run(
        PlanApprovalModeProvider(default_mode=PLAN_MODE).before_run(
            agent=None, session=session, context=cast(Any, context), state={}
        )
    )
    assert MODEL_MODE_TOOL not in _tool_names(context)


def test_the_rest_of_the_upstream_injection_survives() -> None:
    """Only the one tool is retracted: `mode_get` and the mode instructions still arrive.

    Retracting rather than reimplementing is the point — a reimplementation would quietly lose
    whatever upstream injects next.
    """
    session = AgentSession(session_id="rest")
    context = _Context()
    asyncio.run(
        PlanApprovalModeProvider(default_mode=PLAN_MODE).before_run(
            agent=None, session=session, context=cast(Any, context), state={}
        )
    )
    assert "mode_get" in _tool_names(context)
    assert context.instructions


def test_only_grant_execute_moves_the_session_into_execute() -> None:
    """The session starts in plan and reaches execute solely through the human-only path."""
    session = AgentSession(session_id="grant")
    assert session_mode(session) == PLAN_MODE
    asyncio.run(grant_execute(session))
    assert session_mode(session) == EXECUTE_MODE


def test_a_changed_plan_has_a_different_hash() -> None:
    """An approval is bound to the plan that was shown, so a mutated plan is unapproved.

    This is what stops "present a modest plan, get it approved, then rewrite the todo list and run
    something else under the same authorization".
    """

    async def _hashes() -> tuple[str, str]:
        session = AgentSession(session_id="plan-hash")
        before = await current_plan_hash(session)
        await mark_awaiting_job(session, "job-1", title="run something expensive")
        return before, await current_plan_hash(session)

    before, after = asyncio.run(_hashes())
    assert before != after


def test_the_same_plan_hashes_stably() -> None:
    """Re-reading an unchanged plan gives the same hash, so approval is not spuriously lost."""

    async def _hashes() -> tuple[str, str]:
        session = AgentSession(session_id="plan-stable")
        await mark_awaiting_job(session, "job-1", title="a step")
        return await current_plan_hash(session), await current_plan_hash(session)

    first, second = asyncio.run(_hashes())
    assert first == second


@pytest.mark.parametrize("mode", [PLAN_MODE, EXECUTE_MODE])
def test_the_gate_does_not_depend_on_the_starting_mode(mode: str) -> None:
    """`mode_set` is withheld in every mode — including one already in execute.

    A gate that only applied while in plan mode would be no gate at all: the model could flip
    itself once and keep the tool forever after.
    """
    session = AgentSession(session_id=f"mode-{mode}")
    context = _Context()
    asyncio.run(
        PlanApprovalModeProvider(default_mode=mode).before_run(
            agent=None, session=session, context=cast(Any, context), state={}
        )
    )
    assert MODEL_MODE_TOOL not in _tool_names(context)


# --- the durable half: the decision has to survive the process that took it -------------------


async def _store_or_skip() -> "PlanApprovalStore":
    """A store over a migrated database, or skip when none is reachable."""
    await migrated_db_or_skip()
    return PlanApprovalStore()


def test_a_decision_is_recorded_against_the_exact_plan() -> None:
    """An approval is readable back with who gave it, and only for the plan it was given for."""

    async def _run() -> None:
        store = await _store_or_skip()
        await store.record("s-1", "hash-a", "chemist@example.com", True)
        assert await store.decision("s-1", "hash-a") == (True, "chemist@example.com")
        # A different plan in the same session is a different question, and unanswered.
        assert await store.decision("s-1", "hash-b") is None
        # The same plan in a different session is also unanswered — approvals do not leak sideways.
        assert await store.decision("s-2", "hash-a") is None

    asyncio.run(_run())


def test_a_later_rejection_revokes_an_earlier_approval() -> None:
    """The latest decision wins, so clicking "no" second means no.

    Rows are append-only because each is a GxP record of something a person did; the read path
    takes the most recent rather than the first, which is what a human would expect.
    """

    async def _run() -> None:
        store = await _store_or_skip()
        await store.record("s-revoke", "hash-a", "first@example.com", True)
        await store.record("s-revoke", "hash-a", "second@example.com", False)
        assert await store.decision("s-revoke", "hash-a") == (False, "second@example.com")

    asyncio.run(_run())


def _loop(approved: bool) -> Any:
    """The composed loop predicate over a stub inner that always wants to continue."""

    async def _inner(**_kwargs: Any) -> bool:
        return True

    return plan_bound(_inner) if approved else _inner


def test_one_approval_does_not_authorize_every_later_plan() -> None:
    """The escalation this fix exists for: execute mode was granted once and never revoked.

    D-137 bound the approval *record* to a plan hash so "approve a modest plan, then rewrite it"
    would be a different key — but nothing consulted that binding at execution time.
    `set_agent_mode` had one caller and no counterpart, so a session that reached execute mode
    stayed there for the rest of its life and every later plan looped with no human in the loop.
    """

    async def _run() -> tuple[bool, object]:
        session = AgentSession(session_id="escalation")
        await mark_awaiting_job(session, "job-1", title="the approved, modest step")
        await grant_execute(session)
        still_the_approved_plan = await execute_is_authorized(session)

        # The model rewrites its plan — same session, same mode, different work.
        await mark_awaiting_job(session, "job-2", title="something else entirely")
        return still_the_approved_plan, await execute_is_authorized(session)

    approved, after_rewrite = asyncio.run(_run())
    assert approved is True
    assert after_rewrite is False


def test_the_loop_stops_when_the_plan_is_rewritten() -> None:
    """The authorization check is wired into the predicate, not merely available to be called."""

    async def _run() -> tuple[object, object]:
        session = AgentSession(session_id="loop-bound")
        await mark_awaiting_job(session, "job-1", title="approved step")
        await grant_execute(session)
        predicate = _loop(approved=True)
        before = await predicate(session=session, agent=object())
        await mark_awaiting_job(session, "job-2", title="unapproved step")
        return before, await predicate(session=session, agent=object())

    before, after = asyncio.run(_run())
    assert before is True
    # Denials carry MAF's `(False, reason)` form so the explanation is not discarded.
    assert isinstance(after, tuple) and after[0] is False and "approved" in str(after[1])


def test_ticking_a_step_off_does_not_revoke_the_approval() -> None:
    """Progress is not a rewrite — binding to the displayed hash would stop the loop after step one.

    `current_plan_hash` deliberately includes completion state, because that is what the human saw
    and re-approval is correct for the handshake. Authorization asks the other question: is this
    still the same plan? `todo_steps` answers it, and this test is why the two exist separately.
    """

    async def _run() -> bool:
        session = AgentSession(session_id="progress")
        await mark_awaiting_job(session, "job-1", title="a step")
        await grant_execute(session)
        await complete_awaiting_job(session, "job-1", reason="done")
        return await execute_is_authorized(session)

    assert asyncio.run(_run()) is True


def test_a_rejection_after_an_approval_revokes_execute() -> None:
    """`plan_approvals` keeps every decision and reads the latest, so "no" after "yes" must revoke.

    Without `revoke_execute` the durable row said rejected while the session kept executing.
    """

    async def _run() -> tuple[str, bool]:
        session = AgentSession(session_id="revoke")
        await mark_awaiting_job(session, "job-1", title="a step")
        await grant_execute(session)
        revoke_execute(session)
        return session_mode(session), await execute_is_authorized(session)

    mode, authorized = asyncio.run(_run())
    assert mode == PLAN_MODE
    assert authorized is False


def test_an_unapproved_session_never_loops() -> None:
    """No approval on file means no execution, stated independently of the mode check."""

    async def _run() -> object:
        session = AgentSession(session_id="never-approved")
        await mark_awaiting_job(session, "job-1", title="a step")
        return await _loop(approved=True)(session=session, agent=object())

    outcome = asyncio.run(_run())
    assert outcome is False or (isinstance(outcome, tuple) and outcome[0] is False)
