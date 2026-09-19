"""The harness's plan approval gates an action, and stops latching a session (DARK-1, D-167).

The defect these tests exist for was reproduced live, not hypothesised. With `harness_enabled` and
`harness_autonomy="plan_only"`: approve a four-item plan, then ask a *completely different*
question in the same session, and the turn autonomously ran `compute_xtb_energy` and
`record_knowledge_note` — a knowledge-graph write — while `GET /sessions/{id}/plan` reported the
new plan as `approved=false`. The approval had authorized the session, not the plan.

The first test below is that sequence, reduced to its mechanism. The tests around it pin the
boundaries the fix must not overrun: a read tool still works (or `plan_only` is unusable and gets
turned off), and a deployment that asked for autonomy still gets it.
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

    Deliberately not a patched-in double. The sharing is part of what is being tested: the gate
    reads through `plan_approval_store()` and the front-door route writes through it, and under the
    in-memory backend they are only the same decisions because the factory is `@cache`d. A fixture
    that injected its own object would pass even if that cache were removed, and the symptom in
    production would be "approving does nothing".

    The cache is cleared on both sides so this neither inherits a store another test filled nor
    leaves one behind.
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

    Under MAF the plan lived in a todo store hanging off an `AgentSession`, so a test wrote it
    there and the gate read it back through the same object. `enforce_plan_approval` reads
    `request.state["todos"]` — this turn's live view, owned by `TodoListMiddleware` — and takes the
    session id from the ambient contextvar. So a case here is two independent facts, and this
    carries both rather than pretending they still travel together.
    """

    def __init__(self, session_id: str, titles: list[str] | None = None) -> None:
        self.session_id = session_id
        self.titles: list[str] = list(titles or [])
        # What this session's plan steps declare they will call, and so what an approval of it
        # authorizes (`agent/plan_scope.py`). Every case in *this* file is about **when** an
        # approval stands — a rewrite, a spend, an eviction — so the declaration is fixed at the
        # one gated tool they all drive and stays out of the way. What an approval *covers* is
        # `tests/test_plan_scope.py`, which varies it.
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

    Over the steps, not the titles: the identity covers each step's declaration as well as its
    content (`D-2026-09-13-a-plan-identity-that-omits-the-scope-approves-a-plan-nobody-read`), and
    `_todos` is the one place this file builds a step, so the hash the test approves and the plan
    the gate is driven with cannot diverge.
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

    Deliberately not `_approve`: these cases record against an identity the decision route now
    refuses to write, because the gate must hold against a row that exists however it got there —
    written before the route was fixed, or by a path that never went through it.
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
    """The live turn ran two things it should not have, and they are gated by different routes.

    `record_knowledge_note` is an in-process write, listed in `STATE_CHANGING_TOOLS`.
    `compute_xtb_energy` is a `calc` **endpoint** tool — not a job — so it is covered only because
    a bundle declares its own `state_changing` subset. That distinction is the point of this test:
    a gated set built from in-process names plus declared jobs looks complete, passes every test
    anyone would think to write, and still misses half of the finding it was written for.
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
    """Research has to work before approval, or nothing can build the plan being approved.

    MAF's plan-mode instructions tell the agent to run exploratory checks, and a gate over every
    tool would make `plan_only` a mode in which the agent can neither answer nor plan. Deployments
    would turn it off, which is a worse outcome than the defect.
    """

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
    """With no session there is no plan to approve, so this gate has nothing to decide about.

    The behaviour, pinned. **What the docstring here used to say about it was false**, and the same
    sentence sat in `plan_gate.py` and in `cli/chat.py`: that a session-less call is "not
    ungoverned — `enforce_tool_authz` and `authorize_trigger` still decide, which is what governs
    them". Measured, those two reach 6 of the 15 side-effecting registry tools and nine are refused
    by neither; the test below holds that residual. What governs each session-less path is named in
    `plan_gate.py` now, and it is a different thing per path — a reviewed, git-committed template
    file for a `tool` step, a surface the step's agent was never given for an `agent` step, and
    possession of the terminal for the CLI.
    """
    assert asyncio.run(_call("record_knowledge_note", None))


@pytest.mark.parametrize("blank", ["   ", "\t", " \t "])
def test_a_blank_session_id_is_no_session_rather_than_a_session_of_its_own(
    approvals: InMemoryPlanApprovalStore, blank: str
) -> None:
    r"""The early return above tests `not session_id`, and whitespace is truthy.

    `core/session_context.get_current_session_id` returned the contextvar verbatim, so `""` took the
    early return and `"   "` did not — it went on to `approved_scope(session_id, …)` and was used as
    the `plan_approvals` lookup key. Measured before the fix: a whitespace session was refused with
    `plan_approval_refusal`, which reads as "nobody has approved this plan" for a session no chemist
    can see, approve, or find in the inbox; and had anything ever *written* an approval under that
    key it would have been a second, unreachable approval namespace for one conversation.

    Asserted as "the call runs" rather than "the call is refused", because that is what
    distinguishes the two behaviours: with the reader fixed, a blank id is *absent* and takes the
    same path as no session at all, which is the answer the gate already argues for.
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


#: The side-effecting registry tools that **neither** other write gate reaches, so for these the
#: plan gate is the only one — and it is the one that returns early when there is no session.
#:
#: A register rather than a count, for `DEFAULT_WRITE_TOOL_GATES`' own reason: "nine tools are
#: uncovered" and "these nine tools are uncovered, and here is why that was accepted" are different
#: documents, and only the second survives somebody adding a tenth. Each of these is a write whose
#: blast radius is one actor's own conversation state — a preference, a watch, a draft, a workflow
#: document keyed `(owner, name)` — which is why they were never role-gated; `run_composed_workflow`
#: is the one that could reach further and it re-checks at run time
#: (`workflow_tools.py`: `authored_problems(document, side_effecting_tools())` plus
#: `unapproved_jobs` over a fingerprint recomputed from the stored document).
_UNGATED_WITHOUT_A_SESSION = frozenset(
    {
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
    """The residual the early return leaves, held as a set so a tenth tool cannot join it quietly.

    `enforce_plan_approval` returns early when `get_current_session_id()` is empty, and for these
    nine names it is the only write gate in the tree: `DEFAULT_WRITE_TOOL_GATES` does not name them,
    so `authorize_tool` refuses nothing for a role-less authenticated actor, and
    `expensive_actions()` does not either, so `authorize_trigger` does not run. Measured under
    `entra_required=True` with `entra_privileged_roles` configured: 12 of the 15 side-effecting
    registry tools executed for an
    actor holding no application role, and three of those twelve were covered by the trigger gate.

    **Both directions are asserted, and the second is the one that pays.** A new side-effecting tool
    that no gate covers reds this test on the day it is registered rather than the day somebody
    re-runs an audit. A name *leaving* the register reds it too — if a tool becomes role-gated the
    register has to lose it, and a stale entry would be this file claiming a gap that is closed.

    **Over `STATE_CHANGING_TOOLS` rather than `side_effecting_tools()`, and that is not a narrowing
    of the claim but the only way to state it as an equality.** The other two thirds of that union
    are discovery: a connector's own declared `state_changing` names and one launcher per *enabled*
    template. Both are a deployment's choice, so an equality over them asserts whichever registries
    a test run happened to warm — measured, this test passed alone and failed after
    `tests/test_authz.py` had enabled templates in the same process, with nine `run_*` launchers
    added. That difference is a real fact about the residual and it is recorded rather than
    asserted: a deployment that enables the shipped templates puts nine more uncovered writes behind
    this early return, each one a launcher for a git-committed, reviewed template, which is the
    artefact the gate's own comment names as what governs a `tool` step.
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
    """The half of `plan_gate.py`'s corrected comment that is a fact about another module.

    The old comment named "a template activity's tool step" as a path this early return governs. An
    **agent** step does not reach the line at all, and that is stronger rather than weaker:
    `step_profile` returns the profile with `harness_enabled=False`, so `gate_applies` is `False`
    and this middleware is never attached — and the same function subtracts every side-effecting
    tool the step did not declare from the surface *before the graph is built*, so an undeclared
    write is a tool the step's agent never held rather than a call something refused.

    Asserted here, beside the comment that relies on it, because the two facts are one claim: "the
    gate does not apply" is only acceptable while "the surface was narrowed instead" is true.
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

    Read off `tool_call_middleware` rather than off a built agent: the chain is what this asks
    about, and building a whole graph to inspect its list would need a model. The MAF version
    passed `chat_client=object()` for the same reason and got a whole `Agent` anyway.
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
    """`announce_tool_failures` must be *outside* the gate, and this assertion used to say inside.

    Nesting is list order, so a middleware cannot see an exception raised by one above it.
    `enforce_plan_approval` raises before calling its handler, so while the announcer sat innermost
    it never ran for a refusal — and a gated call reached the chemist only as a `tool_result` whose
    text begins "Refused:", which a surface renders as a step that worked.

    The old assertion pinned that ordering with the comment "not innermost (that is
    announce_tool_failures)", and `tests/test_m12_probes.py` asserted the opposite behaviour in
    prose — "they arrive on the stream as the same event type". Both were believed; the live M12
    plan-gate suite settled it by scoring **0** refusals in a run whose front-door log recorded two.
    The gate held the whole time and nothing downstream could see it hold.

    So the invariant is ordering-as-visibility: everything that refuses must nest *inside* the
    thing that announces refusals, and the announcer must stay inside both converters so it still
    sees the raw exception rather than the prose either one turns it into.
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
    """Stated as a fact rather than assumed: the gate ships on, with the harness.

    **This asserted the opposite until D-2026-09-13, and the inversion is the point.** The gate
    shipped off in code while `deploy/helm/chemclaw/values.yaml` turned it on, so the posture every
    supported deployment ran was the one no test here measured — and
    `D-2026-09-06-the-write-gate-is-three-names-and-the-plan-gate-carries-the-rest` had already made
    that gate the only cover over the 29 write tools outside `DEFAULT_WRITE_TOOL_GATES`. A default
    that is less safe than every deployment of it is not a safe fallback.

    Both halves are asserted because the gate is their conjunction: `gate_applies` is
    `harness_enabled and autonomy == 'plan_only'`, so either one drifting silently removes it.
    """
    assert settings.harness_enabled is True
    assert settings.harness_autonomy == "plan_only"
    assert gate_applies(get_profile("default")) is True


# --- an approval authorizes one request, not a standing session (the live finding) -------------


def test_an_approval_is_spent_by_the_turn_that_used_it(
    approvals: InMemoryPlanApprovalStore, monkeypatch: pytest.MonkeyPatch
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
        # Re-approving the same unchanged plan is a person saying "yes, again" — and recording it
        # is the whole of that act, because the store is append-only and reads the latest row, so a
        # fresh decision is an unspent one. It used to need a separate `rearm_plan` call against
        # session state, which every future writer of a decision path had to remember.
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
    """A profile that overrides autonomy must not get the gate without the spend.

    `build_langgraph_agent` attaches the middleware from the *profile's* resolved autonomy while
    the runner decided whether to spend the approval from `settings` alone. A profile setting
    `harness_autonomy="plan_only"` under a global `execute` therefore got a gate whose approval was
    never spent — one decision authorizing every later turn, which is DARK-1 again for exactly the
    sessions a deployment had narrowed on purpose. Both now call `gate_applies`.
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
    """A decision recorded against the empty plan must authorize nothing, ever.

    `current_plan_hash([])` is a *constant* — the same string in every session of every deployment
    for all time — so an approval keyed on it is not a fact about this session's plan. The gate
    treated it as one, which is what let the second half of this finding compose: see the
    rehydration test below.
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
    """An eviction must not re-arm an authorization a turn already used.

    The two halves of the composition, in order. The consumed marker lives in `session.state`,
    which `chemclaw.api.deps._rehydrate_session` drops when the live LRU evicts a session or the pod
    rolls — it rebuilds the handle over the durable history alone, so the todo state goes with it.
    The `plan_approvals` row does not go: it is durable. A rehydrated session therefore proposes
    the empty plan, and while that constant was an approvable identity a spent approval of it came
    back armed, with no human act, outside the single-turn limit D-167 states.

    Modelled the way the front door does it — a new `TurnSession` over the same session id — since
    that *is* the rehydration.
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

    `plan_after_batch` reads the *batch* off `state["messages"]` rather than off the
    state's `todos`, because that is the only place the other calls in the same assistant message
    are visible — `ToolNode` builds every call's runtime from one pre-batch snapshot, so state
    cannot answer "what else is running right now" by construction. So a test of that rule has to
    supply the messages; `_call` supplies only the plan.
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
    """DARK-1's remaining shape, and the branch nothing exercised.

    Turn 1 writes plan A and a chemist approves it. Turn 2 emits `write_todos(plan B)` and
    `record_knowledge_note(...)` in **one** assistant message. `request.state` is the snapshot
    taken before the whole batch, so the gate sees plan A, the approval stands, and the note is
    pushed to the knowledge repository under an approval given for a different plan — and
    `consume_turn_approval` then hashes plan B, finds no decision, and leaves the approval unspent
    for the next turn as well.

    The approval here is *live for the plan in state*, deliberately: that is what makes this a test
    of the batch rule rather than of the ordinary approval check. Deleting the two-line refusal in
    `enforce_plan_approval` left 204 tests green; only the `return True` control failed, which
    proved the function was reached and its true branch untested.
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
    """Consumption is session-wide, so a mid-turn reword cannot leave the old approval armed.

    The hash-targeted form leaked: the turn ends holding plan B, hashes it, finds no decision,
    and returns — while plan A's approval stays live indefinitely, re-authorizing any future turn
    whose todo list hashes back to A. D-167's limit is one turn, whatever the plan drifted to.
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
    """The canonical harness shape must pass on its standing approval — the livelock this closes.

    "Tick the completed step and do the next one" is `TodoListMiddleware`'s own pattern: one
    assistant message carrying `write_todos` (a status flip, same `content` list) beside the next
    step's tool call. The blanket batch refusal denied it on *every* step — the model retried, an
    identical retry tripped `refuse_repeated_calls`, and a fully approved multi-step plan could
    burn its loop allowance making no progress. A status flip does not perturb `plan_identity`
    (the hash covers each step's `content` and its declaration, never its `status`), so judging
    against the plan the batch *writes* lets this through while the DARK-1 rewrite above still
    refuses on its own unapproved hash.

    The flip carries each step's `tools` because the tool's own schema requires it
    (`plan_scope.ScopedTodoListMiddleware`) — a rewrite that dropped the declaration would be a
    different plan, which is the point of
    `D-2026-09-13-a-plan-identity-that-omits-the-scope-approves-a-plan-nobody-read` and is why this
    fixture cannot be written without it.
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
    """The twin, without which the refusal above would break every legitimate re-issue.

    Refusing costs a legitimate turn one retry: the model re-issues the call in the *next* message,
    against the plan it just wrote, and a human approves that plan. If the gate refused that too,
    `plan_only` would be a mode in which a plan can never be acted on — so the boundary is pinned
    from both sides, exactly as the read-tool case is.
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
    """The abandonment half of D-167: a torn-down turn that acted has used its authorization.

    The runner's cancellation path may not await (an `await` re-raises the cancellation and skips
    the teardown after it), so the spend runs on a task of its own — the `turn_cost` pattern. The
    assertion drains the loop before reading the store, exactly as the runner's own teardown
    allows the write to finish after the turn is gone.
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
