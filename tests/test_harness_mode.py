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

from agents.harness_mode import (
    EXECUTE_MODE,
    MODEL_MODE_TOOL,
    PLAN_MODE,
    PlanApprovalModeProvider,
    current_plan_hash,
    grant_execute,
    session_mode,
)
from agents.harness_todo import mark_awaiting_job
from agents.plan_approval_store import PlanApprovalStore
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
    grant_execute(session)
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
