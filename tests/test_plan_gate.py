"""The harness's plan approval gates an action, and stops latching a session (DARK-1, D-157).

The defect these tests exist for was reproduced live, not hypothesised. With `harness_enabled` and
`harness_autonomy="plan_only"`: approve a four-item plan, then ask a *completely different*
question in the same session, and the turn autonomously ran `compute_xtb_energy` and
`propose_knowledge_note` — a knowledge-graph write — while `GET /sessions/{id}/plan` reported the
new plan as `approved=false`. The approval had authorized the session, not the plan.

The first test below is that sequence, reduced to its mechanism. The tests around it pin the
boundaries the fix must not overrun: a read tool still works (or `plan_only` is unusable and gets
turned off), and a deployment that asked for autonomy still gets it.
"""

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from agent_framework import DEFAULT_TODO_SOURCE_ID, AgentSession, TodoItem, TodoSessionStore

from chemclaw.agent import plan_approval_store as store_module
from chemclaw.agent.harness_mode import (
    EXECUTE_MODE,
    PLAN_MODE,
    current_plan_hash,
    grant_execute,
    rearm_plan,
    session_mode,
)
from chemclaw.agent.plan_approval_store import InMemoryPlanApprovalStore
from chemclaw.agent.plan_gate import (
    PlanNotApprovedError,
    approved_todos_remaining,
    consume_turn_approval,
    enforce_plan_approval,
    gated_tools,
)
from chemclaw.core.config import settings


@pytest.fixture
def approvals(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemoryPlanApprovalStore]:
    """The real factory's in-memory store, obtained the way every caller obtains it.

    Deliberately not a patched-in double. The sharing is part of what is being tested: the gate
    reads through `plan_approval_store()` and the front-door route writes through it, and under the
    in-memory backend they are only the same decisions because the factory is `@cache`d. A fixture
    that injected its own object would pass even if that cache were removed, and the symptom in
    production would be "approving does nothing".

    The cache is cleared on both sides so this neither inherits a store another test filled nor
    leaves one behind.
    """
    monkeypatch.setattr(settings, "session_store", "memory")
    store_module.plan_approval_store.cache_clear()
    store = store_module.plan_approval_store()
    assert isinstance(store, InMemoryPlanApprovalStore)
    yield store
    store_module.plan_approval_store.cache_clear()


class _Function:
    """The one attribute the gate reads off an invocation context's function: its name."""

    def __init__(self, name: str) -> None:
        self.name = name


class _Context:
    """The slice of `FunctionInvocationContext` the gate touches."""

    def __init__(self, tool: str, session: AgentSession | None) -> None:
        self.function = _Function(tool)
        self.session = session
        self.arguments: dict[str, Any] = {}
        self.result: Any = None


async def _set_plan(session: AgentSession, titles: list[str]) -> None:
    """Write `titles` as the session's plan, the way the model's own todo tool does."""
    items = [TodoItem(id=index + 1, title=title) for index, title in enumerate(titles)]
    await TodoSessionStore().save_state(
        session, items, next_id=len(items) + 1, source_id=DEFAULT_TODO_SOURCE_ID
    )


async def _approve(store: InMemoryPlanApprovalStore, session: AgentSession) -> None:
    """Record a human approval for the plan the session is proposing right now."""
    await store.record(session.session_id, await current_plan_hash(session), "chemist-1", True)
    grant_execute(session)


async def _call(tool: str, session: AgentSession | None) -> bool:
    """Drive one tool call through the gate; return whether the tool body ran."""
    ran = False

    async def _body() -> None:
        nonlocal ran
        ran = True

    context = _Context(tool, session)
    await enforce_plan_approval(context, _body)  # type: ignore[arg-type]
    return ran


def test_an_approved_plan_does_not_authorize_the_next_one(
    approvals: InMemoryPlanApprovalStore,
) -> None:
    """The live defect: approve plan A, execute plan B. This is the whole finding.

    The session is left in execute mode throughout — as it was live — so the assertion is not that
    some flag flipped, but that the *write* is refused while that stale mode is still in place.
    """

    async def _run() -> tuple[bool, bool]:
        session = AgentSession(session_id="dark-1")
        await _set_plan(session, ["screen the species", "find precedent"])
        await _approve(approvals, session)
        approved_write = await _call("propose_knowledge_note", session)

        # A completely different question: the model rewrites its own todo list mid-session.
        await _set_plan(session, ["compute the energy of every candidate"])
        assert session_mode(session) == EXECUTE_MODE, "the stale mode is the precondition"
        with pytest.raises(PlanNotApprovedError):
            await _call("propose_knowledge_note", session)
        return approved_write, session_mode(session) == PLAN_MODE

    approved_write, demoted = asyncio.run(_run())
    assert approved_write, "the approved plan's own write was refused; the gate is too tight"
    assert demoted, "the session kept an execute mode it is not entitled to"


def test_both_tools_the_unapproved_turn_ran_are_gated() -> None:
    """The live turn ran two things it should not have, and they are gated by different routes.

    `propose_knowledge_note` is an in-process write, listed in `STATE_CHANGING_TOOLS`.
    `compute_xtb_energy` is a `calc` **endpoint** tool — not a job — so it is covered only because
    a bundle declares its own `state_changing` subset. That distinction is the point of this test:
    a gated set built from in-process names plus declared jobs looks complete, passes every test
    anyone would think to write, and still misses half of the finding it was written for.
    """
    gated = gated_tools()
    assert "propose_knowledge_note" in gated
    assert "compute_xtb_energy" in gated
    # A declared job, gated structurally — no bundle has to remember to list one.
    assert "compute_dft_energy" in gated
    # And the reads a plan is built from are not.
    assert "resolve_compound" not in gated
    assert "screen_hazards" not in gated


def test_a_read_tool_is_not_gated(approvals: InMemoryPlanApprovalStore) -> None:
    """Research has to work before approval, or nothing can build the plan being approved.

    MAF's plan-mode instructions tell the agent to run exploratory checks, and a gate over every
    tool would make `plan_only` a mode in which the agent can neither answer nor plan. Deployments
    would turn it off, which is a worse outcome than the defect.
    """

    async def _run() -> bool:
        session = AgentSession(session_id="reads")
        await _set_plan(session, ["work out what to do"])
        return await _call("gather_evidence", session)

    assert asyncio.run(_run())


def test_a_session_with_no_plan_cannot_write(approvals: InMemoryPlanApprovalStore) -> None:
    """No plan is not an approved plan: the agent proposes before it acts, by design."""

    async def _run() -> None:
        session = AgentSession(session_id="no-plan")
        with pytest.raises(PlanNotApprovedError):
            await _call("propose_knowledge_note", session)

    asyncio.run(_run())


def test_a_rejection_after_an_approval_revokes_it(approvals: InMemoryPlanApprovalStore) -> None:
    """Migration 020 says the latest decision wins. Nothing acted on that until the gate did."""

    async def _run() -> None:
        session = AgentSession(session_id="revoked")
        await _set_plan(session, ["do the thing"])
        await _approve(approvals, session)
        assert await _call("propose_knowledge_note", session)
        await approvals.record(
            session.session_id, await current_plan_hash(session), "chemist-1", False
        )
        with pytest.raises(PlanNotApprovedError):
            await _call("propose_knowledge_note", session)

    asyncio.run(_run())


def test_no_session_means_no_gate(approvals: InMemoryPlanApprovalStore) -> None:
    """Off the harness there is no plan and no autonomous loop, so there is nothing to gate.

    A template activity's tool step and a one-shot CLI call land here. They are not ungoverned:
    `enforce_tool_authz` and `authorize_trigger` still decide, which is what governs them.
    """
    assert asyncio.run(_call("propose_knowledge_note", None))


def test_the_loop_stops_when_the_plan_is_not_approved(
    approvals: InMemoryPlanApprovalStore,
) -> None:
    """An unapproved session must not spin the runaway budget having every write refused."""

    async def _always_continue(**_kwargs: Any) -> bool:
        return True

    async def _run() -> tuple[bool, bool]:
        session = AgentSession(session_id="loop")
        await _set_plan(session, ["a step"])
        predicate = approved_todos_remaining(_always_continue)
        unapproved = await predicate(session=session, agent=None)
        await _approve(approvals, session)
        approved = await predicate(session=session, agent=None)
        return _proceed(unapproved), _proceed(approved)

    unapproved, approved = asyncio.run(_run())
    assert not unapproved, "the loop kept iterating on a plan nobody approved"
    assert approved, "the loop stopped on a plan that *was* approved"


def test_the_loop_wrapper_preserves_the_inner_predicates_feedback(
    approvals: InMemoryPlanApprovalStore,
) -> None:
    """MAF routes a predicate's feedback string to `next_message`; dropping it breaks the loop.

    `todos_remaining_message` is what tells the model which todos are still open between
    iterations. A wrapper that returned a bare bool would silently disable it and leave the loop
    re-invoking the model with nothing new to work from.
    """

    async def _continue_with_feedback(**_kwargs: Any) -> tuple[bool, str]:
        return True, "two todos still open"

    async def _run() -> Any:
        session = AgentSession(session_id="loop-feedback")
        await _set_plan(session, ["a step"])
        await _approve(approvals, session)
        return await approved_todos_remaining(_continue_with_feedback)(session=session, agent=None)

    assert asyncio.run(_run()) == (True, "two todos still open")


def _proceed(result: Any) -> bool:
    """Normalize a `should_continue` result the way MAF's loop does."""
    return bool(result[0]) if isinstance(result, tuple) else bool(result)


# --- the gate is attached only where it means something ---------------------------------------


def _middleware_names(agent: Any) -> list[str]:
    """The advertised names of an agent's function middleware chain."""
    return [getattr(m, "__name__", type(m).__name__) for m in (agent.middleware or [])]


def test_the_gate_is_absent_from_the_classic_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """`harness_enabled` is off by default, and the default path must be untouched."""
    from chemclaw.agent.chemclaw_agent import build_agent

    monkeypatch.setattr(settings, "harness_enabled", False)
    agent = build_agent(chat_client=object())
    assert "enforce_plan_approval" not in _middleware_names(agent)


def test_the_gate_is_absent_under_execute_autonomy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment that configured autonomy has said it does not want an approval-first posture.

    Attaching the gate there would refuse every write on a path that has no approval route at all,
    which is not a safer deployment — it is a broken one.
    """
    from chemclaw.agent.chemclaw_agent import build_agent

    monkeypatch.setattr(settings, "harness_enabled", True)
    monkeypatch.setattr(settings, "harness_autonomy", "execute")
    agent = build_agent(chat_client=object())
    assert "enforce_plan_approval" not in _middleware_names(agent)


def test_the_gate_is_attached_under_plan_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The configuration the shipped Helm chart sets is the one that gets the gate."""
    from chemclaw.agent.chemclaw_agent import build_agent

    monkeypatch.setattr(settings, "harness_enabled", True)
    monkeypatch.setattr(settings, "harness_autonomy", "plan_only")
    agent = build_agent(chat_client=object())
    names = _middleware_names(agent)
    assert "enforce_plan_approval" in names
    # Inside audit (so a refusal is recorded) and not innermost (that is announce_tool_failures).
    assert names.index("enforce_plan_approval") > names.index("audit_tool_calls")
    assert names.index("enforce_plan_approval") < names.index("announce_tool_failures")


def test_the_default_deployment_has_no_plan_gate() -> None:
    """Stated as a fact rather than assumed: the gate ships off, with the harness."""
    assert settings.harness_enabled is False
    assert settings.harness_autonomy == "plan_only"


def test_plan_mode_is_where_a_gated_session_ends_up() -> None:
    """The mode constants this module reasons about are the ones the harness actually uses."""
    assert (PLAN_MODE, EXECUTE_MODE) == ("plan", "execute")


# --- an approval authorizes one request, not a standing session (the live finding) -------------


def test_an_approval_is_spent_by_the_turn_that_used_it(
    approvals: InMemoryPlanApprovalStore,
) -> None:
    """The gap the *first* version of this fix left, found live rather than reasoned about.

    Binding the approval to the plan's work items made it checkable. It also made it durable in a
    way nobody approved: the live run showed the model answering a completely different question
    **without touching its todo list**, so the plan identity never changed, the approval never
    lapsed, and `compute_xtb_energy` ran under an authorization given for a hazard-screening plan.
    A plan-shaped identity cannot detect that on its own — the todo list is genuinely unchanged.

    What changed is the request. So the approval is spent by the turn it authorizes: the harness
    loop runs a plan to completion inside one `agent.run`, which is exactly the scope of "execute
    the approved plan", and the next user message needs its own decision.
    """

    async def _run() -> tuple[bool, bool, bool]:
        session = AgentSession(session_id="one-shot")
        await _set_plan(session, ["screen the species"])
        await _approve(approvals, session)
        during = await _call("propose_knowledge_note", session)
        await consume_turn_approval(session)  # the turn ends
        after = False
        try:
            after = await _call("propose_knowledge_note", session)
        except PlanNotApprovedError:
            after = False
        # Re-approving the same unchanged plan is a person saying "yes, again".
        await _approve(approvals, session)
        rearm_plan(session, await current_plan_hash(session))
        again = await _call("propose_knowledge_note", session)
        return during, after, again

    during, after, again = asyncio.run(_run())
    assert during, "the approved turn's own write was refused"
    assert not after, "a second, unrelated request ran on a spent approval"
    assert again, "re-approving an unchanged plan did not re-authorize it"


def test_consuming_is_silent_when_nothing_was_approved(
    approvals: InMemoryPlanApprovalStore,
) -> None:
    """Turn teardown runs on every path, so this must never fail a turn on its way out."""

    async def _run() -> None:
        session = AgentSession(session_id="never-approved")
        await _set_plan(session, ["a step"])
        await consume_turn_approval(session)
        await consume_turn_approval(session)

    asyncio.run(_run())
