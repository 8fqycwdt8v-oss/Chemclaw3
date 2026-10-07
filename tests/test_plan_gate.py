"""Plan approval gates an action and does not latch a session (D-167).

The first test is the core sequence: under `plan_only`, approving one plan must not authorize
side-effecting tools in a later, unrelated turn whose new plan is unapproved. The rest pin the
boundaries: read tools still work before approval, and a deployment that asked for autonomy still
gets it.
"""

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from chemclaw.agent import plan_approval_store as store_module
from chemclaw.agent import plan_gate as plan_gate_module
from chemclaw.agent.authz import side_effecting_tools
from chemclaw.agent.plan_approval_store import InMemoryPlanApprovalStore
from chemclaw.agent.plan_gate import (
    EMPTY_PLAN_HASH,
    PlanNotApprovedError,
    consume_turn_approval,
    enforce_plan_approval,
    gate_applies,
    plan_identity,
)
from chemclaw.agent.profiles import get_profile
from chemclaw.core.config import settings
from chemclaw.core.session_context import reset_current_session_id, set_current_session_id
from tests.middleware import run_middleware, tool_request


@pytest.fixture
def approvals(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemoryPlanApprovalStore]:
    """The real factory's in-memory store, obtained the way every caller obtains it.

    Not a patched-in double: the gate and the front-door route share decisions only because
    `plan_approval_store()` is cached, and that sharing is under test. The cache is cleared on both
    sides of the test.
    """
    monkeypatch.setattr(settings, "session_store", "memory")
    # Bound up front, so teardown clears *this* cache even if a test swaps the module attribute
    # for a stand-in — otherwise one test replacing the factory breaks the next test's fixture.
    factory = store_module.plan_approval_store
    factory.cache_clear()
    store = factory()
    assert isinstance(store, InMemoryPlanApprovalStore)
    yield store
    factory.cache_clear()


class _Session:
    """A session under test: its id, and the plan it is currently proposing.

    `enforce_plan_approval` reads the plan from `request.state["todos"]` and the session id from the
    ambient contextvar, so a case carries both facts.
    """

    def __init__(self, session_id: str, titles: list[str] | None = None) -> None:
        self.session_id = session_id
        self.titles: list[str] = list(titles or [])
        # What this session's plan steps declare they will call. This file is about when an approval
        # stands, so the declaration is fixed at the one gated tool; what an approval covers is
        # `tests/test_plan_scope.py`.
        self.declares: list[str] = ["record_knowledge_note"]


async def _set_plan(session: _Session, titles: list[str]) -> None:
    """Set what the session is proposing — what the model's `write_todos` would have written."""
    session.titles = list(titles)


async def _approve(store: InMemoryPlanApprovalStore, session: _Session) -> None:
    """Record a human approval for the plan the session is proposing right now."""
    await store.record(session.session_id, _hash(session), "chemist-1", True, session.declares)


async def _steps(session: _Session) -> list[dict[str, Any]]:
    """The session's plan, as `plan_state.session_plan` would return it."""
    return _todos(session)


def _hash(session: _Session) -> str:
    """The identity of the session's current plan, or the empty-plan constant.

    Hashed over the steps, which include each step's declaration; `_todos` builds every step, so the
    approved hash and the gated plan cannot diverge.
    """
    return plan_identity(_todos(session)) or EMPTY_PLAN_HASH


def _todos(session: _Session | None) -> list[dict[str, Any]]:
    """The session's plan as `write_todos` would have written it — steps and their declarations."""
    if session is None:
        return []
    return [
        {"content": t, "status": "pending", "tools": list(session.declares)} for t in session.titles
    ]


async def _call(tool: str, session: _Session | None) -> bool:
    """Drive one tool call through the gate; return whether the tool body ran."""
    ran = False

    async def _handler(_request: Any) -> Any:
        nonlocal ran
        ran = True
        return None

    request = tool_request(tool)
    object.__setattr__(request, "state", {"todos": _todos(session)})
    token = set_current_session_id(session.session_id) if session is not None else None
    try:
        await run_middleware(enforce_plan_approval, request, _handler)
    finally:
        if token is not None:
            reset_current_session_id(token)
    return ran


async def _record(store: InMemoryPlanApprovalStore, session: _Session) -> None:
    """Record an approval for whatever identity the session hashes to right now.

    Bypasses `_approve`, because the gate must hold against a row however it got into the store.
    """
    await store.record(session.session_id, _hash(session), "chemist", True, session.declares)


async def _try_call(tool: str, session: _Session) -> bool:
    """`_call` with the refusal reported as "the tool did not run" rather than raised.

    For the cases that assert *whether* a write happened across several attempts, where a raise
    would end the sequence before the interesting one.
    """
    try:
        return await _call(tool, session)
    except PlanNotApprovedError:
        return False


def test_an_approved_plan_does_not_authorize_the_next_one(
    approvals: InMemoryPlanApprovalStore,
) -> None:
    """The live defect: approve plan A, execute plan B. This is the whole finding.

    The session is left in execute mode throughout — as it was live — so the assertion is not that
    some flag flipped, but that the *write* is refused while that stale mode is still in place.
    """

    async def _run() -> tuple[bool, bool]:
        session = _Session("dark-1")
        await _set_plan(session, ["screen the species", "find precedent"])
        await _approve(approvals, session)
        approved_write = await _call("record_knowledge_note", session)

        # A completely different question: the model rewrites its own todo list mid-session.
        await _set_plan(session, ["compute the energy of every candidate"])
        with pytest.raises(PlanNotApprovedError):
            await _call("record_knowledge_note", session)
        # No mode to check. Under MAF an approval also flipped a session mode, and that mode
        # outlived the approval — so this asserted the demotion as well. The gate reads the plan
        # and the durable decision, and nothing else says "may this session act".
        return approved_write, True

    approved_write, demoted = asyncio.run(_run())
    assert approved_write, "the approved plan's own write was refused; the gate is too tight"
    assert demoted, "the session kept an execute mode it is not entitled to"


def test_both_tools_the_unapproved_turn_ran_are_gated() -> None:
    """Both an in-process write and a connector endpoint tool are gated.

    `record_knowledge_note` is in `STATE_CHANGING_TOOLS`; `compute_xtb_energy` is a `calc` endpoint
    tool covered only by the bundle's declared `state_changing` subset. A gated set of in-process
    names plus jobs would miss the second.
    """
    gated = side_effecting_tools()
    assert "record_knowledge_note" in gated
    assert "compute_xtb_energy" in gated
    # A declared job, gated structurally — no bundle has to remember to list one.
    assert "sample_conformers" in gated
    # And the reads a plan is built from are not.
    assert "resolve_compound" not in gated
    assert "screen_hazards" not in gated


def test_a_read_tool_is_not_gated(approvals: InMemoryPlanApprovalStore) -> None:
    """A read tool is not gated: research has to work before there is a plan to approve."""

    async def _run() -> bool:
        session = _Session("reads")
        await _set_plan(session, ["work out what to do"])
        return await _call("gather_evidence", session)

    assert asyncio.run(_run())


async def test_a_session_with_no_plan_cannot_write(approvals: InMemoryPlanApprovalStore) -> None:
    """No plan is not an approved plan: the agent proposes before it acts, by design."""
    session = _Session("no-plan")
    with pytest.raises(PlanNotApprovedError):
        await _call("record_knowledge_note", session)


async def test_a_rejection_after_an_approval_revokes_it(
    approvals: InMemoryPlanApprovalStore,
) -> None:
    """Migration 020 says the latest decision wins. Nothing acted on that until the gate did."""
    session = _Session("revoked")
    await _set_plan(session, ["do the thing"])
    await _approve(approvals, session)
    assert await _call("record_knowledge_note", session)
    await approvals.record(session.session_id, _hash(session), "chemist-1", False, session.declares)
    with pytest.raises(PlanNotApprovedError):
        await _call("record_knowledge_note", session)


def test_no_session_means_no_gate(approvals: InMemoryPlanApprovalStore) -> None:
    """With no session there is no plan to approve, so this gate does not decide.

    What governs each session-less path is named in `plan_gate.py`: a reviewed template file for a
    `tool` step, a narrowed surface for an `agent` step, possession of the terminal for the CLI. The
    residual that no other gate covers is held by the test below.
    """
    assert asyncio.run(_call("record_knowledge_note", None))


@pytest.mark.parametrize("blank", ["   ", "\t", " \t "])
def test_a_blank_session_id_is_no_session_rather_than_a_session_of_its_own(
    approvals: InMemoryPlanApprovalStore, blank: str
) -> None:
    r"""A whitespace session id is no session, not a session of its own.

    `get_current_session_id` normalises blanks, so a whitespace id takes the no-session path rather
    than becoming an approval key no chemist can see or approve. Asserted as "the call runs", which
    is what distinguishes the two behaviours.
    """
    token = set_current_session_id(blank)
    try:
        request = tool_request("record_knowledge_note")
        object.__setattr__(request, "state", {"todos": []})
        ran = False

        async def _handler(_request: Any) -> Any:
            nonlocal ran
            ran = True
            return None

        asyncio.run(run_middleware(enforce_plan_approval, request, _handler))
    finally:
        reset_current_session_id(token)

    assert ran, (
        f"a session id of {blank!r} was treated as a session, so the gate consulted "
        "`plan_approvals` under a key no chemist can approve against"
    )


#: The side-effecting registry tools no other write gate reaches, so without a session nothing gates
#: them. A register with reasons rather than a count: each write's blast radius is one actor's own
#: conversation state (a preference, a watch, a draft, an `(owner, name)` workflow document).
#: `run_composed_workflow` could reach further and re-checks at run time (`workflow_tools.py`).
_UNGATED_WITHOUT_A_SESSION = frozenset(
    {
        "attach_plate_results",
        "compose_workflow",
        "draft_experiment_protocol",
        "forget_preference",
        "propose_skill",
        "remember_preference",
        "run_composed_workflow",
        "stop_watching",
        "structure_experiment_request",
        "watch_for",
    }
)


def test_what_governs_a_session_less_write_is_registered_rather_than_asserted() -> None:
    """The session-less residual is held as an exact set, so no tool joins or leaves it quietly.

    `DEFAULT_WRITE_TOOL_GATES` and `expensive_actions()` do not name these, so for a role-less actor
    the plan gate's early return is the only gate. A new uncovered tool, or a stale entry for one
    since covered, fails here. Stated over `STATE_CHANGING_TOOLS` because connector names and
    enabled template launchers depend on deployment (and on which registries a test run warmed);
    enabling templates adds reviewed `run_*` launchers to this residual.
    """
    from chemclaw.agent import tool_modules  # noqa: F401  (registers the in-process tools)
    from chemclaw.agent.authz import (
        DEFAULT_WRITE_TOOL_GATES,
        STATE_CHANGING_TOOLS,
        expensive_actions,
    )
    from chemclaw.core.tool_registry import registered_tools

    registry = {function.__name__ for function in registered_tools()}
    writes = STATE_CHANGING_TOOLS & registry
    residual = writes - DEFAULT_WRITE_TOOL_GATES - expensive_actions()

    assert writes, "no side-effecting tool is registered; this test is measuring nothing"
    assert residual == _UNGATED_WITHOUT_A_SESSION, (
        "the set of writes that only the plan gate reaches has changed. Added names are reachable "
        "with no session id and no approval by anything that can enqueue a template run or type at "
        f"the CLI; removed names mean this register overstates the gap. added="
        f"{sorted(residual - _UNGATED_WITHOUT_A_SESSION)} "
        f"removed={sorted(_UNGATED_WITHOUT_A_SESSION - residual)}"
    )


def test_a_template_agent_step_is_narrowed_rather_than_gated() -> None:
    """A template agent step is narrowed rather than gated.

    `step_profile` disables the harness, so this middleware is not attached, and it removes every
    side-effecting tool the step did not declare before the graph is built. "The gate does not
    apply" is acceptable only while that narrowing holds.
    """
    from chemclaw.durable.template_activities import step_profile

    undeclared = step_profile(None, [])
    declared = step_profile(None, ["record_knowledge_note"])

    assert not gate_applies(undeclared), (
        "a template agent step is now plan-gated, so the early return this comment describes is "
        "reachable from it and the narrowing below is no longer the whole story"
    )
    assert "record_knowledge_note" not in (undeclared.tool_names or frozenset()), (
        "a step that declared no write tools was given one anyway, so the structural narrowing "
        "that stands in for the plan gate is not happening"
    )
    assert "record_knowledge_note" in (declared.tool_names or frozenset()), (
        "a step that declared a write tool did not get it, so this test passes for the wrong reason"
    )


def _proceed(result: Any) -> bool:
    """Normalize a `should_continue` result the way MAF's loop does."""
    return bool(result[0]) if isinstance(result, tuple) else bool(result)


# --- the gate is attached only where it means something ---------------------------------------


def _middleware_names() -> list[str]:
    """The advertised names of a profile's tool-call middleware chain.

    Read off `tool_call_middleware` without building a graph.
    """
    from chemclaw.agent.audit import NullAuditSink, make_audit_middleware
    from chemclaw.agent.langgraph_agent import tool_call_middleware
    from chemclaw.agent.profiles import get_profile

    # A real audit middleware, because its *position* is part of what this asserts and it is the
    # one entry built per agent rather than imported — a stand-in would show up as `object`.
    audit = make_audit_middleware(correlation_id="-", actor="-", sink=NullAuditSink())
    return [type(m).__name__ for m in tool_call_middleware(audit, get_profile(None))]


def test_the_gate_is_absent_from_the_classic_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """`harness_enabled` is off by default, and the default path must be untouched."""
    monkeypatch.setattr(settings, "harness_enabled", False)
    assert "enforce_plan_approval" not in _middleware_names()


def test_the_gate_is_absent_under_execute_autonomy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment that configured autonomy has said it does not want an approval-first posture.

    Attaching the gate there would refuse every write on a path that has no approval route at all,
    which is not a safer deployment — it is a broken one.
    """
    monkeypatch.setattr(settings, "harness_enabled", True)
    monkeypatch.setattr(settings, "harness_autonomy", "execute")
    assert "enforce_plan_approval" not in _middleware_names()


def test_the_gate_is_attached_under_plan_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The configuration the shipped Helm chart sets is the one that gets the gate."""
    monkeypatch.setattr(settings, "harness_enabled", True)
    monkeypatch.setattr(settings, "harness_autonomy", "plan_only")
    names = _middleware_names()
    assert "enforce_plan_approval" in names
    # Inside audit, so a refusal is recorded on the trail.
    assert names.index("enforce_plan_approval") > names.index("audit_tool_calls")


def test_a_refusal_is_announced_because_the_announcer_wraps_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`announce_tool_failures` wraps the gate, so a refusal is announced.

    Nesting is list order and the gate raises before calling its handler, so an announcer inside it
    never sees a refusal, which then reaches the chemist as a step that worked. Invariant:
    everything that refuses nests inside the announcer, and the announcer stays inside both
    converters so it sees the raw exception.
    """
    monkeypatch.setattr(settings, "harness_enabled", True)
    monkeypatch.setattr(settings, "harness_autonomy", "plan_only")
    names = _middleware_names()
    announcer = names.index("announce_tool_failures")
    for refuser in (
        "enforce_plan_approval",
        "enforce_tool_authz",
        "refuse_writes_on_dry_run",
        "refuse_repeated_calls",
    ):
        assert announcer < names.index(refuser), (
            f"{refuser} raises before its handler, so an announcer nested inside it never runs; "
            f"the refusal would reach the chemist as a tool_result reading 'Refused: …'"
        )
    for converter in ("surface_authorization_denials", "surface_domain_errors"):
        assert names.index(converter) < announcer, (
            f"{converter} turns an exception into prose for the model; the announcer must stay "
            "inside it to see the raw exception"
        )


def test_the_default_deployment_has_the_plan_gate() -> None:
    """The plan gate ships on by default, with the harness.

    It is the only cover for writes outside `DEFAULT_WRITE_TOOL_GATES`. `gate_applies` is
    `harness_enabled and autonomy == 'plan_only'`, so both halves are asserted.
    """
    assert settings.harness_enabled is True
    assert settings.harness_autonomy == "plan_only"
    assert gate_applies(get_profile("default")) is True


# --- an approval authorizes one request, not a standing session (the live finding) -------------


def test_an_approval_is_spent_by_the_turn_that_used_it(
    approvals: InMemoryPlanApprovalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An approval is spent by the turn that used it.

    A model can answer a different question without touching its todo list, so the plan identity
    alone cannot bound an approval. The harness runs a plan to completion within one `agent.run`,
    and the next user message needs its own decision.
    """

    async def _run() -> tuple[bool, bool, bool]:
        session = _Session("one-shot")
        await _set_plan(session, ["screen the species"])
        # `consume_turn_approval` reads the plan off the checkpointer, which this test has none of
        # — the session here is a fixture, not a turn that ran. Pointed at the same titles the gate
        # is driven with, so both halves ask about one plan.
        monkeypatch.setattr(plan_gate_module, "session_plan", lambda _sid, **_kw: _steps(session))
        await _approve(approvals, session)
        during = await _call("record_knowledge_note", session)
        await consume_turn_approval(session.session_id)  # the turn ends
        after = False
        try:
            after = await _call("record_knowledge_note", session)
        except PlanNotApprovedError:
            after = False
        # Re-approving the unchanged plan is a new decision; the store is append-only and reads the
        # latest row, so recording it is all re-arming takes.
        await _approve(approvals, session)
        again = await _call("record_knowledge_note", session)
        return during, after, again

    during, after, again = asyncio.run(_run())
    assert during, "the approved turn's own write was refused"
    assert not after, "a second, unrelated request ran on a spent approval"
    assert again, "re-approving an unchanged plan did not re-authorize it"


async def test_consuming_is_silent_when_nothing_was_approved(
    approvals: InMemoryPlanApprovalStore,
) -> None:
    """Turn teardown runs on every path, so this must never fail a turn on its way out."""
    session = _Session("never-approved")
    await _set_plan(session, ["a step"])
    await consume_turn_approval(session.session_id)
    await consume_turn_approval(session.session_id)


# --- review fixes: one predicate, a non-fatal spend, and an honest display --------------------


def test_the_gate_and_the_spend_ask_the_same_question(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate and the spend both call `gate_applies`.

    A profile setting `plan_only` under a global `execute` must get both, or one approval would
    authorize every later turn.
    """
    from chemclaw.agent.profiles import AgentProfile

    monkeypatch.setattr(settings, "harness_enabled", False)
    monkeypatch.setattr(settings, "harness_autonomy", "execute")

    narrowed = AgentProfile(name="p", harness_enabled=True, harness_autonomy="plan_only")
    assert gate_applies(narrowed), "a profile that asks for the approval-first posture is gated"

    autonomous = AgentProfile(name="p", harness_enabled=True, harness_autonomy="execute")
    assert not gate_applies(autonomous), "a profile that asks for autonomy is not gated"

    assert not gate_applies(AgentProfile(name="p")), "the default follows the deployment"


async def test_spending_never_raises_when_the_store_is_unreachable(
    approvals: InMemoryPlanApprovalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn must not fail on its way out because the approval store hiccupped.

    The gate still fails closed on the next call regardless — an unreadable decision is not an
    approval — so a swallowed error costs one extra approval rather than authorizing anything.
    """

    class _Broken:
        async def decision(self, *_: Any) -> None:
            raise RuntimeError("store is down")

        async def record(self, *_: Any) -> None:
            raise RuntimeError("store is down")

    monkeypatch.setattr(store_module, "plan_approval_store", lambda: _Broken())

    session = _Session("broken-store")
    await _set_plan(session, ["a step"])
    await consume_turn_approval(session.session_id)  # must not raise


# --- "nothing" is not an approvable plan, and a spent approval stays spent ---------------------


def test_the_empty_plan_is_not_an_approvable_identity(
    approvals: InMemoryPlanApprovalStore,
) -> None:
    """An approval recorded against the empty plan authorizes nothing.

    `current_plan_hash([])` is the same constant in every session, so it is not a fact about this
    session's plan.
    """

    async def _run() -> bool:
        session = _Session("empty-plan")
        # Recorded directly, as a row written before this was refused (or by any other path):
        # the gate must not depend on the decision route having filtered it out.
        await _record(approvals, session)
        return await _try_call("record_knowledge_note", session)

    assert not asyncio.run(_run()), "an approval of the empty plan authorized a knowledge write"


def test_a_spent_approval_stays_spent_across_a_rehydrate(
    approvals: InMemoryPlanApprovalStore,
) -> None:
    """A spent approval stays spent across a rehydrate.

    Eviction rebuilds a session from durable history, dropping the spent marker and the todo list,
    while the `plan_approvals` row survives. The rehydrated session proposes the empty plan, which
    must not re-arm the old approval. Modelled as the front door does it: a new `TurnSession` over
    the same id.
    """

    async def _run() -> tuple[bool, bool]:
        session = _Session("evicted")
        await _record(approvals, session)
        before = await _try_call("record_knowledge_note", session)
        await consume_turn_approval(session.session_id)

        rehydrated = _Session("evicted")  # the LRU evicted it; its plan is gone with it
        assert not rehydrated.titles, "a rehydrated session has no plan by construction"
        return before, await _try_call("record_knowledge_note", rehydrated)

    before, after = asyncio.run(_run())
    assert not before, "the empty plan authorized a write even before the eviction"
    assert not after, "a spent approval re-armed itself when the session was rehydrated"


async def _call_with_messages(tool: str, session: _Session, messages: list[Any]) -> bool:
    """`_call`, but with the assistant messages this call arrives among.

    `plan_after_batch` reads the batch from `state["messages"]`: `ToolNode` builds every call's
    runtime from one pre-batch snapshot, so the state cannot show what else is in the batch.
    """
    ran = False

    async def _handler(_request: Any) -> Any:
        nonlocal ran
        ran = True
        return None

    request = tool_request(tool, call_id="c-write")
    object.__setattr__(request, "state", {"todos": _todos(session), "messages": messages})
    token = set_current_session_id(session.session_id)
    try:
        await run_middleware(enforce_plan_approval, request, _handler)
    finally:
        reset_current_session_id(token)
    return ran


def _batch(*calls: dict[str, Any]) -> AIMessage:
    """One assistant message issuing `calls` together — what `ToolNode` fans out in one batch."""
    return AIMessage(content="", tool_calls=list(calls))


_WRITE_TODOS = {"name": "write_todos", "args": {"todos": []}, "id": "c-plan"}
_GATED = {"name": "record_knowledge_note", "args": {"type": "insight"}, "id": "c-write"}


async def test_a_gated_call_beside_a_plan_rewrite_is_refused_even_with_a_live_approval(
    approvals: InMemoryPlanApprovalStore,
) -> None:
    """A gated call beside a plan rewrite is refused even with a live approval.

    In one assistant message, `write_todos(plan B)` beside `record_knowledge_note` would otherwise
    run under plan A's approval, since state is the pre-batch snapshot. The approval is deliberately
    live for the plan in state, so this tests the batch rule rather than the ordinary check.
    """
    session = _Session("dark-1-batch")
    await _set_plan(session, ["screen the species", "find precedent"])
    await _approve(approvals, session)
    # The control: alone in its own message, this exact call is allowed right now.
    assert await _call_with_messages("record_knowledge_note", session, [_batch(_GATED)])

    with pytest.raises(PlanNotApprovedError):
        await _call_with_messages("record_knowledge_note", session, [_batch(_WRITE_TODOS, _GATED)])


def test_a_drifted_plans_old_approval_is_spent_at_turn_end(
    approvals: InMemoryPlanApprovalStore,
) -> None:
    """A drifted plan's old approval is spent at turn end.

    Consumption is session-wide; a hash-targeted spend would leave plan A's approval armed for a
    future turn whose list hashes back to A.
    """

    async def _run() -> bool:
        session = _Session("drift-leak")
        await _set_plan(session, ["plan A step"])
        await _approve(approvals, session)
        # The model rewords the plan mid-turn; the turn ends holding plan B.
        await _set_plan(session, ["plan B step"])
        await consume_turn_approval(session.session_id)
        # A later turn drifts back to plan A. Its old approval must be spent, not waiting.
        await _set_plan(session, ["plan A step"])
        return await _try_call("record_knowledge_note", session)

    assert not asyncio.run(_run()), (
        "a mid-turn reword left the old plan's approval live past the turn that ran under it"
    )


async def test_ticking_a_step_beside_the_steps_own_call_is_allowed(
    approvals: InMemoryPlanApprovalStore,
) -> None:
    """Ticking a step beside that step's own call is allowed on the standing approval.

    "Tick the completed step and do the next" puts `write_todos` (a status flip) beside the next
    call. `plan_identity` ignores `status`, so judging against the plan the batch writes allows it
    while a real rewrite is still refused; refusing would livelock the plan into
    `refuse_repeated_calls`. The flip carries each step's `tools` because the schema requires it.
    """
    session = _Session("tick-and-act")
    plan = ["compute the barrier", "propose the note"]
    await _set_plan(session, plan)
    await _approve(approvals, session)
    tick = {
        "name": "write_todos",
        "args": {
            "todos": [
                {
                    "content": "compute the barrier",
                    "status": "completed",
                    "tools": list(session.declares),
                },
                {
                    "content": "propose the note",
                    "status": "in_progress",
                    "tools": list(session.declares),
                },
            ]
        },
        "id": "c-plan",
    }
    assert await _call_with_messages("record_knowledge_note", session, [_batch(tick, _GATED)]), (
        "a status-flip write_todos beside the step's own call was refused — the livelock shape"
    )

    # A *content* rewrite in the same shape is a different plan, and refuses on its own hash.
    reword = {
        "name": "write_todos",
        "args": {
            "todos": [
                {
                    "content": "something else entirely",
                    "status": "pending",
                    "tools": list(session.declares),
                }
            ]
        },
        "id": "c-plan-2",
    }
    with pytest.raises(PlanNotApprovedError):
        await _call_with_messages("record_knowledge_note", session, [_batch(reword, _GATED)])


async def test_an_unanswerable_batch_rewrite_still_refuses(
    approvals: InMemoryPlanApprovalStore,
) -> None:
    """Two rewrites in one batch, or unparseable arguments, fail closed without asking the store."""
    session = _Session("unanswerable-batch")
    await _set_plan(session, ["step one"])
    await _approve(approvals, session)
    two = {"name": "write_todos", "args": {"todos": []}, "id": "c-plan-b"}
    with pytest.raises(PlanNotApprovedError):
        await _call_with_messages(
            "record_knowledge_note", session, [_batch(_WRITE_TODOS, two, _GATED)]
        )
    garbled = {"name": "write_todos", "args": {"todos": "not-a-list"}, "id": "c-plan-c"}
    with pytest.raises(PlanNotApprovedError):
        await _call_with_messages("record_knowledge_note", session, [_batch(garbled, _GATED)])


async def test_the_same_call_is_allowed_in_the_message_after_the_plan_was_rewritten(
    approvals: InMemoryPlanApprovalStore,
) -> None:
    """The same call is allowed in the message after the plan was rewritten and approved.

    Otherwise `plan_only` would be a mode in which a plan can never be acted on.
    """
    session = _Session("dark-1-next-message")
    await _set_plan(session, ["compute the barrier"])
    await _approve(approvals, session)
    messages = [_batch(_WRITE_TODOS), _batch(_GATED)]
    assert await _call_with_messages("record_knowledge_note", session, messages), (
        "a re-issued call in the next message was refused; the batch rule has overrun into "
        "the retry it exists to leave open"
    )


def test_a_teardown_spend_lands_without_awaiting(
    approvals: InMemoryPlanApprovalStore,
) -> None:
    """A torn-down turn that acted has used its authorization, spent without awaiting.

    The cancellation path may not await, so the spend runs on its own task; the test drains the loop
    before reading the store.
    """
    from chemclaw.agent.plan_gate import spend_approval_after_teardown

    async def _run() -> bool:
        session = _Session("torn-down")
        await _set_plan(session, ["start the screen"])
        await _approve(approvals, session)
        spend_approval_after_teardown(session.session_id)
        # Let the spend task run; the caller never awaits it, the loop does.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return await _try_call("record_knowledge_note", session)

    assert not asyncio.run(_run()), (
        "an abandoned turn's approval stayed live; 'drop the connection after the tools ran' "
        "re-authorizes a second turn under one human decision"
    )
