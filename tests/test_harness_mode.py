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
    EMPTY_PLAN_HASH,
    EXECUTE_MODE,
    MODEL_MODE_TOOL,
    PLAN_MODE,
    PlanApprovalModeProvider,
    approvable_plan_hash,
    current_plan_hash,
    grant_execute,
    session_mode,
)
from chemclaw.agent.harness_todo import mark_awaiting_job
from chemclaw.agent.plan_approval_store import (
    ApprovalStore,
    InMemoryPlanApprovalStore,
    PlanApprovalStore,
)
from tests.pg import migrated_db_or_skip


async def _set_plan(
    session: AgentSession, titles: list[str], *, complete: set[int] | None = None
) -> None:
    """Write `titles` as the session's plan, marking the listed indices complete.

    Straight into MAF's own todo store, because that is where the model's `todo_write` puts them —
    a test for "what happens when the plan changes" has to change the plan the same way the model
    does, not through a helper that writes a different kind of row.
    """
    from agent_framework import DEFAULT_TODO_SOURCE_ID, TodoItem, TodoSessionStore

    done = complete or set()
    items = [
        TodoItem(id=index + 1, title=title, is_complete=index in done)
        for index, title in enumerate(titles)
    ]
    await TodoSessionStore().save_state(
        session, items, next_id=len(items) + 1, source_id=DEFAULT_TODO_SOURCE_ID
    )


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

    Driven through the *todo store* rather than through `mark_awaiting_job`, which this test used
    to use. That helper writes the system-authored `awaiting-job:` row, and D-167 excludes those
    from the identity on purpose — see `test_a_launched_job_does_not_revoke_its_own_approval`. What
    is being asserted here is that a change to the **work items** changes the hash, so the change
    has to be a work item.
    """

    async def _hashes() -> tuple[str, str]:
        session = AgentSession(session_id="plan-hash")
        before = await current_plan_hash(session)
        await _set_plan(session, ["screen the species"])
        return before, await current_plan_hash(session)

    before, after = asyncio.run(_hashes())
    assert before != after


def test_ticking_a_box_does_not_change_the_plans_identity() -> None:
    """Working through an approved plan must not revoke the approval it is working under (D-167).

    The hash used to cover the rendered `[x]`/`[ ] title` lines, so it moved on the first completed
    step. An approval could therefore be recorded but never checked against the plan being
    executed — which is precisely what the system did, and why the gate was decorative. Binding to
    the work items makes "the plan proceeded" and "the plan changed" different events.
    """

    async def _hashes() -> tuple[str, str]:
        session = AgentSession(session_id="plan-tick")
        await _set_plan(session, ["screen the species", "find precedent"])
        before = await current_plan_hash(session)
        await _set_plan(session, ["screen the species", "find precedent"], complete={0})
        return before, await current_plan_hash(session)

    before, after = asyncio.run(_hashes())
    assert before == after


def test_a_launched_job_does_not_revoke_its_own_approval() -> None:
    """A durable launch inside an approved plan appends a todo — and must not unapprove the plan.

    `mark_awaiting_job` adds an `awaiting-job:` row from the launcher
    (`chemclaw.connectors.jobs._mark_awaiting_if_harness`), so counting it in the identity would
    mean every approved plan revoked itself the first time it started a job. It is also not work a
    human agreed to: it records that work already agreed to is in flight.
    """

    async def _hashes() -> tuple[str, str]:
        session = AgentSession(session_id="plan-awaiting")
        await _set_plan(session, ["compute the energy"])
        before = await current_plan_hash(session)
        await mark_awaiting_job(session, "calc-7", title="awaiting compute_xtb_energy")
        return before, await current_plan_hash(session)

    before, after = asyncio.run(_hashes())
    assert before == after


def test_no_work_items_is_no_approvable_identity() -> None:
    """The empty plan hashes to a constant, and a constant cannot be an authorization.

    `EMPTY_PLAN_HASH` is the same string for every session in every deployment for all time, so a
    decision recorded against it says nothing about *this* session's plan — and a session that has
    lost its todo state (a rehydrate) proposes it again for free. `approvable_plan_hash` answers
    None there, which is what the gate and the decision route ask; `current_plan_hash` stays total
    for the display route.
    """

    async def _identities() -> tuple[str | None, str, str | None]:
        session = AgentSession(session_id="plan-empty")
        empty, displayed = await approvable_plan_hash(session), await current_plan_hash(session)
        # A row nobody agreed to: the launcher's bookkeeping, which `todo_plan_items` strips. The
        # display is non-empty, the plan is not.
        await mark_awaiting_job(session, "job-1", title="awaiting the DFT run")
        return empty, displayed, await approvable_plan_hash(session)

    empty, displayed, bookkeeping_only = asyncio.run(_identities())
    assert empty is None, "the empty plan was offered as an identity to approve"
    assert displayed == EMPTY_PLAN_HASH, "the display route lost its hash for a planless session"
    assert bookkeeping_only is None, "an `awaiting-job:` row alone made the plan approvable"


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


# --- consumption is durable evidence too, and both backends must agree about it ----------------
#
# Parametrized over the two real backends rather than tested once against the easy one. They are not
# a implementation and a double: `session_store="memory"` is a deployment (the CLI is one), so a
# disagreement about when an approval is spent would be a control that behaves differently depending
# on whether a database happens to be configured. The Postgres case skips where no server is
# reachable; the in-memory case always runs, so a regression in the shared semantics is caught
# offline and the SQL is checked wherever a database exists.


async def _consumable_store_or_skip(backend: str) -> ApprovalStore:
    """One of the two real approval stores, skipping the Postgres one when no server is up."""
    if backend == "memory":
        return InMemoryPlanApprovalStore()
    await migrated_db_or_skip()
    return PlanApprovalStore()


@pytest.mark.parametrize("backend", ["memory", "postgres"])
def test_a_consumed_approval_reads_back_as_unapproved(backend: str) -> None:
    """Spending an approval must change what the *store* says, not a marker beside it.

    This is the defect D-167 left open. The `plan_approvals` row was durable and the marker saying
    it had been spent lived in `session.state`, which an LRU eviction or a pod roll drops — so the
    two halves of one control had different lifetimes and a reconstructed plan met a spent approval
    looking fresh. `consumed_at` puts the second half on the row, and `decision` reports the
    *effective* verdict, so no caller can read one without the other.

    The actor still comes back: "approved earlier, already used" is a different thing for a surface
    to show than "nobody has decided", and `GET /sessions/{id}/plan` shows exactly that difference.
    """

    async def _run() -> None:
        store = await _consumable_store_or_skip(backend)
        await store.record("s-spend", "hash-a", "chemist@example.com", True)
        assert await store.decision("s-spend", "hash-a") == (True, "chemist@example.com")

        await store.consume("s-spend", "hash-a")
        assert await store.decision("s-spend", "hash-a") == (False, "chemist@example.com")

        # Idempotent: turn teardown reaches this on two paths (answered, and failed-after-running),
        # and spending twice must not cost a second plan's authorization or raise.
        await store.consume("s-spend", "hash-a")
        assert await store.decision("s-spend", "hash-a") == (False, "chemist@example.com")

        # Untouched plans are untouched — consumption is scoped to the one identity, like the read.
        assert await store.decision("s-spend", "hash-b") is None

    asyncio.run(_run())


@pytest.mark.parametrize("backend", ["memory", "postgres"])
def test_re_approving_a_spent_plan_re_arms_it(backend: str) -> None:
    """A person saying "yes, again" is a fresh decision, and needs no second operation to be one.

    Append-only plus latest-wins means a new row is unspent by construction, which is why the
    `rearm_plan` that used to sit beside every decision path is gone rather than reimplemented here.
    """

    async def _run() -> None:
        store = await _consumable_store_or_skip(backend)
        await store.record("s-again", "hash-a", "chemist@example.com", True)
        await store.consume("s-again", "hash-a")
        assert await store.decision("s-again", "hash-a") == (False, "chemist@example.com")

        await store.record("s-again", "hash-a", "reviewer@example.com", True)
        assert await store.decision("s-again", "hash-a") == (True, "reviewer@example.com")

    asyncio.run(_run())


@pytest.mark.parametrize("backend", ["memory", "postgres"])
def test_consuming_never_spends_a_rejection(backend: str) -> None:
    """Nothing to spend means nothing happens — a "no" must not be quietly stamped as used.

    The distinction is not cosmetic: a rejection that had been marked consumed would still read
    `approved=False`, but the record of what a person did would say the system had *used* their
    refusal. Turn teardown calls `consume` on paths where the latest decision may well be a no.
    """

    async def _run() -> None:
        store = await _consumable_store_or_skip(backend)
        await store.record("s-no", "hash-a", "chemist@example.com", False)
        await store.consume("s-no", "hash-a")
        assert await store.decision("s-no", "hash-a") == (False, "chemist@example.com")
        # And an approval recorded afterwards is live, rather than having been consumed early.
        await store.record("s-no", "hash-a", "chemist@example.com", True)
        assert await store.decision("s-no", "hash-a") == (True, "chemist@example.com")

    asyncio.run(_run())
