"""A plan approval authorizes the tools its steps declared, and nothing else.

`D-2026-09-12-an-approval-that-names-no-tool-authorizes-every-tool`. The defect these tests exist
for was driven, not argued: with a standing approval for the one-line, read-only plan
`["look up the melting point of aspirin"]`, **every** name in `authz.side_effecting_tools()`
executed — every knowledge-graph write, every durable launcher, every enabled bundle's
state-changing surface, nothing refused. D-137 made the decision durable and D-167 made it bind an
act rather than latch onto a session; neither bounded what the act could be.

`test_the_surface_a_read_only_plans_approval_reaches` is that measurement, now as a ratchet: it
drives the whole of `side_effecting_tools()` through the gate under an approval for a plan that
declared nothing, and requires **every one** to be refused. That is the assertion the figure in
`infra/sql/095_plan_approval_scope.sql` and `agent/plan_scope.py` delegates to, and why neither of
them writes a count.
"""

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from chemclaw.agent import plan_approval_store as store_module
from chemclaw.agent.authz import memory_write_verbs, side_effecting_call, side_effecting_tools
from chemclaw.agent.plan_approval_store import InMemoryPlanApprovalStore
from chemclaw.agent.plan_gate import PlanNotApprovedError, enforce_plan_approval, plan_identity
from chemclaw.agent.plan_scope import (
    ScopedTodoListMiddleware,
    ScopedWriteTodosInput,
    declared_scope,
)
from chemclaw.agent.scratchpad import MEMORY_ROOT
from chemclaw.core.config import settings
from chemclaw.core.session_context import reset_current_session_id, set_current_session_id
from tests.middleware import run_middleware, tool_request
from tests.pg import migrated_db_or_skip

# The read-only plan the live repro used. One line, and nothing about it suggests a write.
_READ_ONLY_PLAN = ["look up the melting point of aspirin"]


@pytest.fixture
def approvals(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemoryPlanApprovalStore]:
    """The real factory's in-memory store, obtained the way every caller obtains it.

    The same fixture `tests/test_plan_gate.py` uses and for the same reason: the sharing between
    the gate that reads and the decision surface that writes is part of what is under test, so a
    patched-in double would pass even if the factory's cache were removed.
    """
    monkeypatch.setattr(settings, "session_store", "memory")
    factory = store_module.plan_approval_store
    factory.cache_clear()
    store = factory()
    assert isinstance(store, InMemoryPlanApprovalStore)
    yield store
    factory.cache_clear()


async def _approve(
    store: InMemoryPlanApprovalStore, session_id: str, steps: list[dict[str, Any]]
) -> None:
    """Record a human approval of `steps`, scoped the way the two decision surfaces scope it.

    `declared_scope` rather than a literal set, so this drives the same derivation
    `api/routes/plan.py` and `cli/chat.py` record with: a test that passed its own scope would
    prove the gate reads the column and nothing about what the column ever gets.
    """
    plan_hash = plan_identity(steps)
    assert plan_hash is not None
    await store.record(session_id, plan_hash, "chemist-1", True, declared_scope(steps))


async def _ran(
    tool: str,
    session_id: str,
    steps: list[dict[str, Any]],
    arguments: dict[str, Any] | None = None,
) -> bool:
    """Drive one call through the gate under `steps`; True when the tool body ran.

    `arguments` defaults to empty because for all but two tools the name settles whether the gate
    applies. The exceptions are `authz.memory_write_verbs()`, where the *path* decides, and passing
    them is the only way to drive that half.
    """
    ran = False

    async def _handler(_request: Any) -> Any:
        nonlocal ran
        ran = True

    request = tool_request(tool, arguments or {})
    object.__setattr__(request, "state", {"todos": steps})
    token = set_current_session_id(session_id)
    try:
        await run_middleware(enforce_plan_approval, request, _handler)
    except PlanNotApprovedError:
        return False
    finally:
        reset_current_session_id(token)
    return ran


def _step(content: str, *tools: str) -> dict[str, Any]:
    """One plan step as `write_todos` writes it, declaring `tools`."""
    return {"content": content, "status": "pending", "tools": list(tools)}


def test_an_approval_for_one_tool_does_not_authorize_another(
    approvals: InMemoryPlanApprovalStore,
) -> None:
    """The effect assertion: approve a plan naming A, watch B refused in the same session.

    Not "the scope column is populated" — that is a shape, and a shape assertion is what
    `tasks/lessons.md` records surviving mutation. One session, one live approval, two calls, and
    the difference between them is the declaration the human read.
    """

    async def _run() -> tuple[bool, bool]:
        steps = [_step("write up what we know about aspirin", "record_knowledge_note")]
        await _approve(approvals, "scoped", steps)
        declared = await _ran("record_knowledge_note", "scoped", steps)
        # The approval is still live: `consume_all` has not run, and the plan has not changed.
        undeclared = await _ran("remember_preference", "scoped", steps)
        return declared, undeclared

    declared, undeclared = asyncio.run(_run())
    assert declared, "the tool the approved plan declared was refused"
    assert not undeclared, (
        "a tool no step of the approved plan declared executed under that plan's approval"
    )


def test_the_surface_a_read_only_plans_approval_reaches(
    approvals: InMemoryPlanApprovalStore,
) -> None:
    """The live repro, as a ratchet over the whole side-effecting surface.

    Before this decision, an approval of this exact plan permitted every one of these names. The
    assertion is over `side_effecting_tools()` itself rather than a list, so a bundle enabled next
    year is covered the day it is enabled — and no number appears here or in the prose that cites
    this test.
    """

    async def _run() -> list[str]:
        steps = [_step(line) for line in _READ_ONLY_PLAN]
        await _approve(approvals, "read-only", steps)
        return [
            name for name in sorted(side_effecting_tools()) if await _ran(name, "read-only", steps)
        ]

    permitted = asyncio.run(_run())
    assert side_effecting_tools(), "the surface under test is empty; this ratchet proves nothing"
    assert permitted == [], (
        f"a plan declaring no tools authorized {len(permitted)} of them: {permitted}"
    )


def test_the_gate_also_reaches_the_half_of_the_surface_a_name_cannot_enumerate(
    approvals: InMemoryPlanApprovalStore,
) -> None:
    """The ratchet above walks `side_effecting_tools()`, and that is not the whole gated surface.

    `authz.side_effecting_call` is the union of two halves: the names a gate can enumerate, and the
    calls whose gatedness is a function of their *arguments*. `memory_write_verbs()` is the second
    half — one name serving two roots, `/scratch/` dying with the turn and `/memories/` outliving
    the deployment — so the tools whose classification is hardest are precisely the ones the
    enumeration walks past.

    Measured before this existed: both verbs are outside the 49-name surface, and
    `side_effecting_call(verb, {"file_path": "/memories/x.md"})` is `True` for each. So the gate did
    refuse them and **nothing held that it would** — which is the shape `CLAUDE.md` names as a claim
    that a control exists. The backlog row that found it named one verb; there are two.

    Derived from `authz` and `scratchpad` rather than listing either name, for the reason the
    ratchet above gives about bundles: a third argument-driven verb is covered the day it is added.
    """

    async def _run() -> tuple[list[str], list[str]]:
        steps = [_step(line) for line in _READ_ONLY_PLAN]
        await _approve(approvals, "argument-driven", steps)
        durable, scratch = [], []
        for verb in sorted(memory_write_verbs()):
            if await _ran(verb, "argument-driven", steps, {"file_path": f"{MEMORY_ROOT}probe.md"}):
                durable.append(verb)
            if not await _ran(verb, "argument-driven", steps, {"file_path": "/scratch/probe.md"}):
                scratch.append(verb)
        return durable, scratch

    durable, scratch = asyncio.run(_run())
    assert memory_write_verbs(), "the argument-driven half is empty; this proves nothing"
    assert durable == [], (
        f"a plan declaring no tools authorized {durable} to write durable memory — these are gated "
        "by `side_effecting_call` and the name-only ratchet above cannot see them"
    )
    # The other direction, and it is what stops this being a test that a blunt name-gate would pass:
    # a turn's own scratchpad must stay reachable under an approval that declared nothing, or the
    # gate has refused the agent its notepad rather than its memory.
    assert scratch == [], (
        f"{scratch} were refused for a `/scratch/` write, so the gate is matching on the name "
        "rather than the path and a turn cannot put down intermediate work"
    )
    for verb in memory_write_verbs():
        assert side_effecting_call(verb, {"file_path": f"{MEMORY_ROOT}probe.md"}), (
            f"{verb} is no longer classified as a durable write by its arguments, so the arms "
            "above pass for a reason other than the one this test is about"
        )
        assert verb not in side_effecting_tools(), (
            f"{verb} is now in the name-enumerable half, so the ratchet above covers it and this "
            "test is measuring the same thing twice — move it or delete it deliberately"
        )


def test_a_plan_is_bounded_in_both_directions_at_argument_validation() -> None:
    """An unpriced write a model can repeat, refused where the model can read why.

    Both halves were unbounded and each sizes something that outlives the call. Measured on the
    shipped schema before this: **50,000** ten-character names in one step validated, and
    `plan_gate.out_of_scope_refusal` built a **600,192-character** sentence out of the union —
    bounded to 60,000 by `agent/tool_authz._refusal_message` before the model reads it, and
    unbounded everywhere before that (the exception, the log, the audit row). **20,000 steps
    validated too**, which is the half the backlog row that found this did not name.

    Not an escalation: the scope only ever *narrows* what a call may do, and a name no tool answers
    to is refused by `enforce_tool_authz` regardless.

    Both numbers are read off `settings` rather than written here, so the assertion is that the
    bound *binds* rather than that it is 32 — a deployment that raises either is still tested.
    """
    over_steps = [_step("x") for _ in range(settings.plan_max_steps + 1)]
    with pytest.raises(ValidationError, match=r"at most \d+ are accepted"):
        ScopedWriteTodosInput(todos=over_steps)  # type: ignore[arg-type]

    wide = _step("x", *[f"tool-{i}" for i in range(settings.plan_max_tools_per_step + 1)])
    with pytest.raises(ValidationError, match=r"at most \d+ are accepted per step"):
        ScopedWriteTodosInput(todos=[_step("first"), wide])  # type: ignore[arg-type]


def test_the_refusal_names_the_step_so_the_model_can_split_the_plan() -> None:
    """Refused at argument validation is the whole point, and a message it cannot act on wastes it.

    The alternative designs both fail here rather than in principle: a bound in the `TypedDict`
    could only carry a literal (its annotations are evaluated at class definition, so no setting
    reaches it) and pydantic's own `too_long` message names neither the step nor what to do about
    it. So the assertion is on the two things a model needs — *which* step, and the instruction.
    """
    wide = _step("x", *[f"tool-{i}" for i in range(settings.plan_max_tools_per_step + 1)])
    with pytest.raises(ValidationError) as raised:
        ScopedWriteTodosInput(todos=[_step("first"), _step("second"), wide])  # type: ignore[arg-type]
    message = raised.value.errors()[0]["msg"]
    assert "step 3" in message, f"the refusal does not say which step is too broad: {message}"
    assert "Split it" in message


def test_an_ordinary_plan_is_nowhere_near_either_bound() -> None:
    """The half that decides whether these defaults are a control or an obstacle.

    A step declares nought to a handful of tools and a plan a person approves runs to a handful of
    steps, so the bounds have to be far enough above real use that nobody meets them. Asserted as
    the *ratio* rather than by accepting one plan, because "an eight-step plan validates" would
    stay true at a bound of nine.
    """
    ordinary = [_step(f"step {i}", "gather_evidence", "expand_note") for i in range(8)]
    assert ScopedWriteTodosInput(todos=ordinary).todos  # type: ignore[arg-type]
    assert settings.plan_max_steps >= 4 * len(ordinary)
    assert settings.plan_max_tools_per_step >= 8 * 2


def test_widening_a_step_after_approval_does_not_widen_the_approval(
    approvals: InMemoryPlanApprovalStore,
) -> None:
    """The model cannot grant itself a tool by editing its own declaration.

    **Two independent reasons, and this test used to assert the weaker one as its precondition.**
    The gate reads the recorded scope and never the live one, so a widened declaration gains nothing
    even where the approval still stands — and since
    `D-2026-09-13-a-plan-identity-that-omits-the-scope-approves-a-plan-nobody-read` the approval no
    longer stands either: the identity covers each step's declaration, so the widened plan is a
    different plan with no decision against it at all.

    The precondition therefore runs the other way now. It asserted that widening leaves the identity
    *unchanged* — which was true, and was the hole: the decision route's 409 freshness guard read
    that same identity, so a rewrite made between the chemist reading the card and posting their
    decision was stamped as what they had approved
    (`tests/test_plan_scope.py::test_a_rewrite_that_widens_a_declaration_does_not_pass_the_freshness_guard`
    drives it). The effect assertion is untouched, and is what this test is for.
    """

    async def _run() -> bool:
        approved = [_step("write up what we know about aspirin", "record_knowledge_note")]
        await _approve(approvals, "widened", approved)
        widened = [
            _step("write up what we know about aspirin", "record_knowledge_note", "watch_for")
        ]
        assert plan_identity(approved) != plan_identity(widened), (
            "widening a step's declaration left the plan's identity unchanged, so the decision "
            "route's freshness guard cannot see the rewrite"
        )
        return await _ran("watch_for", "widened", widened)

    assert not asyncio.run(_run()), (
        "the agent widened its own authorization by rewriting a step's declaration"
    )


def test_a_refusal_outside_the_scope_names_what_was_approved() -> None:
    """The two refusals are different sentences because they have different remedies.

    A chemist reading "has not been approved yet" while the plan is visibly approved would
    reasonably conclude the gate was broken. `out_of_scope_refusal` says what the approval covers
    and what to do instead, and this is what stops the two collapsing into one message.
    """
    from chemclaw.agent.plan_gate import out_of_scope_refusal, plan_approval_refusal

    outside = str(out_of_scope_refusal("watch_for", frozenset({"record_knowledge_note"})))
    assert "record_knowledge_note" in outside, "the refusal does not say what was approved"
    assert "watch_for" in outside, "the refusal does not name the tool it refused"
    assert str(plan_approval_refusal("watch_for")) != outside, (
        "an unapproved plan and an out-of-scope tool read identically to a chemist"
    )


def test_a_plan_step_cannot_be_written_without_declaring_its_tools() -> None:
    """The decision on "a step declares no tools": the schema makes the omission unrepresentable.

    The alternative designs both fail. An optional field with a fall-through bounds nothing until
    the model volunteers to be bounded; an optional field that refuses when absent turns the first
    omission into what looks, to the chemist, like an authorization decision about a plan they have
    approved. Required makes it a tool-argument error the model reads and retries, which is the
    ordinary loop and not a security surface at all.
    """
    schema = ScopedTodoListMiddleware().tools[0].args_schema
    # The tool advertises a pydantic model, which is what makes the omission a *validation* error
    # the model is handed rather than a plan with a missing field.
    assert isinstance(schema, type) and issubclass(schema, BaseModel)
    with pytest.raises(ValidationError):
        schema.model_validate({"todos": [{"content": "do the thing", "status": "pending"}]})
    # The counterweight: declaring nothing is expressible, and means "this step changes nothing".
    schema.model_validate({"todos": [{"content": "look it up", "status": "pending", "tools": []}]})


def test_the_scoped_plan_tool_is_still_the_one_the_gate_and_the_harness_know() -> None:
    """Everything that reads the plan reads it by name; widening the schema must not move any name.

    `plan_gate._PLAN_WRITE_TOOL`, `chemclaw_agent.harness_tool_names` and the `todos` channel all
    spell this out by hand. A subclass that renamed the tool or wrote a different channel would
    disable the gate's batch rule silently, which is the failure mode `tests/test_upstream_surface`
    exists for one level up.
    """
    middleware = ScopedTodoListMiddleware()
    assert [tool.name for tool in middleware.tools] == ["write_todos"]
    assert middleware.state_schema.__name__ == "PlanningState"


def test_an_unreadable_declaration_narrows_rather_than_widens() -> None:
    """`declared_scope` fails closed, because it is what a call is checked *against*.

    A `tools` that is a bare string, a number, or absent contributes nothing — so a malformed plan
    authorizes less, never more. The opposite direction would make a malformed `write_todos` a way
    to be granted everything.
    """
    assert declared_scope([{"content": "a", "tools": "record_knowledge_note"}]) == frozenset()
    assert declared_scope([{"content": "a", "tools": 7}]) == frozenset()
    assert declared_scope([{"content": "a"}]) == frozenset()
    assert declared_scope([{"content": "a", "tools": ["x", 7, "y"]}]) == frozenset({"x", "y"})


def test_the_durable_backend_round_trips_a_scope_and_defaults_a_legacy_row_to_none() -> None:
    """The shipped backend is Postgres, and the in-memory mirror proves nothing about the SQL.

    Two things only a real database can answer, and both decide whether the control holds on a
    deployment: that `plan_approvals.scope` survives the write and the read, and that a row written
    *without* one — every approval recorded before
    `infra/sql/095_plan_approval_scope.sql` ran — comes back as the empty set rather than as NULL.
    The migration's `DEFAULT '{}'` is what makes the second true, and it is the direction of that
    default that is the decision: an approval an upgrade found in flight authorizes nothing and is
    asked for again, rather than standing as a permanent authorization for everything.
    """

    async def _run() -> tuple[frozenset[str] | None, frozenset[str] | None]:
        await migrated_db_or_skip()
        from chemclaw.agent.plan_approval_store import PlanApprovalStore
        from chemclaw.core import db

        store = PlanApprovalStore()
        await store.record("pg-scope", "hash-a", "chemist", True, {"record_knowledge_note"})
        stamped = await store.decision("pg-scope", "hash-a")
        # A row inserted the way the previous image's statement did — no `scope` column at all.
        async with db.connection(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO plan_approvals (session_id, plan_hash, actor, approved) "
                    "VALUES (%s, %s, %s, %s)",
                    ("pg-scope", "hash-legacy", "chemist", True),
                )
            await conn.commit()
        legacy = await store.decision("pg-scope", "hash-legacy")
        return (
            stamped.scope if stamped else None,
            legacy.scope if legacy else None,
        )

    stamped, legacy = asyncio.run(_run())
    assert stamped == frozenset({"record_knowledge_note"}), (
        f"the durable backend did not round-trip the approval's scope: {stamped}"
    )
    assert legacy == frozenset(), (
        f"an approval recorded before migration 095 came back authorizing {legacy}"
    )


def test_the_decision_route_records_what_the_plan_declared(
    monkeypatch: pytest.MonkeyPatch, approvals: InMemoryPlanApprovalStore
) -> None:
    """`POST /sessions/{id}/plan/decision` stamps the scope; the gate reads only what it stamps.

    The other half of the control. The gate could be perfect and the front door could still record
    an approval that authorizes nothing (every call refused, the feature dead) or everything (the
    defect back, with the column present and meaningless). So this drives the real route against a
    plan whose steps declare, and asserts the row.

    It also asserts the same plan comes back with its declaration on `GET .../plan`: the person is
    being asked to approve the scope, so a surface that showed only the steps would be collecting a
    yes to something it had not displayed.
    """
    from fastapi.testclient import TestClient

    from chemclaw.api.app import create_app
    from chemclaw.api.auth import Principal, require_principal
    from chemclaw.api.routes import plan as plan_routes
    from tests.test_service import _FakeOwnerStore, _no_connectors

    steps = [_step("write up the result", "record_knowledge_note"), _step("check the table")]

    async def _plan(session_id: str, **_kwargs: Any) -> list[dict[str, Any]]:
        return steps

    monkeypatch.setattr(plan_routes, "session_plan", _plan)
    app = create_app(owner_store=_FakeOwnerStore(), connector_factory=_no_connectors)
    app.state.plan_approvals = approvals
    app.dependency_overrides[require_principal] = lambda: Principal(
        oid="alice", upn="alice@corp", roles=frozenset()
    )
    client = TestClient(app)
    session_id = client.post("/sessions").json()["session_id"]
    shown = client.get(f"/sessions/{session_id}/plan").json()
    assert shown["scope"] == ["record_knowledge_note"], (
        f"the plan was shown without what approving it would authorize: {shown}"
    )
    res = client.post(
        f"/sessions/{session_id}/plan/decision",
        json={"approved": True, "plan_hash": shown["plan_hash"]},
    )
    assert res.status_code == 204, res.text

    async def _read() -> Any:
        return await approvals.decision(session_id, shown["plan_hash"])

    recorded = asyncio.run(_read())
    assert recorded is not None and recorded.scope == frozenset({"record_knowledge_note"}), (
        f"the route recorded an approval authorizing {recorded and sorted(recorded.scope)}"
    )


def test_a_rewrite_that_widens_a_declaration_does_not_pass_the_freshness_guard(
    monkeypatch: pytest.MonkeyPatch, approvals: InMemoryPlanApprovalStore
) -> None:
    """The approval-time widening window, driven through the real route.

    `D-2026-09-13-a-plan-identity-that-omits-the-scope-approves-a-plan-nobody-read`. Stamping the
    scope at decide time (above) stops a rewrite widening an approval that has already been given.
    It does nothing about a rewrite made *while the decision card is open*, because the scope the
    route stamps is read off the **live** plan once the posted hash has matched — and the hash
    matched, since the identity covered step text only. So: show a plan declaring nothing, keep
    every step's text, widen its `tools`, and the chemist's own hash still satisfies the 409 guard.

    Measured on the pre-fix code through this exact sequence: shown scope `[]`, rewritten scope
    `['record_knowledge_note', 'watch_for']`, identity unchanged, **204**, and the row came back
    authorizing both. No concurrency is involved — an unapproved plan is not a hold, so any
    follow-up message takes a turn while the card is open, and `out_of_scope_refusal` tells the
    model in as many words to rewrite the plan so a step declares the tool it wants.

    The assertion is the route's answer and the absence of a row, not the hash: a hash comparison
    here would be a second copy of `plan_identity`'s rule, which is the vacuous shape
    `tasks/lessons.md` records. What makes it non-vacuous is that nothing in this test computes an
    identity at all — it posts the one the server rendered on the card, and asks what the server
    does with it.
    """
    from fastapi.testclient import TestClient

    from chemclaw.api.app import create_app
    from chemclaw.api.auth import Principal, require_principal
    from chemclaw.api.routes import plan as plan_routes
    from tests.test_service import _FakeOwnerStore, _no_connectors

    shown = [_step("look up the melting point of aspirin")]
    widened = [_step("look up the melting point of aspirin", "record_knowledge_note", "watch_for")]
    live: list[list[dict[str, Any]]] = [shown]

    async def _plan(_session_id: str, **_kwargs: Any) -> list[dict[str, Any]]:
        return live[0]

    monkeypatch.setattr(plan_routes, "session_plan", _plan)
    app = create_app(owner_store=_FakeOwnerStore(), connector_factory=_no_connectors)
    app.state.plan_approvals = approvals
    app.dependency_overrides[require_principal] = lambda: Principal(
        oid="alice", upn="alice@corp", roles=frozenset()
    )
    client = TestClient(app)
    session_id = client.post("/sessions").json()["session_id"]
    card = client.get(f"/sessions/{session_id}/plan").json()
    assert card["scope"] == [], card
    live[0] = widened
    res = client.post(
        f"/sessions/{session_id}/plan/decision",
        json={"approved": True, "plan_hash": card["plan_hash"]},
    )
    assert res.status_code == 409, (
        f"the widened plan was stamped approved on the hash of the plan the chemist read: "
        f"{res.status_code}"
    )

    async def _read() -> Any:
        return await approvals.decision(session_id, card["plan_hash"])

    recorded = asyncio.run(_read())
    assert recorded is None, (
        f"an approval was recorded authorizing {recorded and sorted(recorded.scope)}"
    )


def test_the_plans_identity_moves_with_its_declaration_and_not_with_its_progress() -> None:
    """The two sensitivities the identity has to have, asserted directly and in one place.

    **Sensitive to the declaration**, or the decision route's freshness guard cannot see a widening
    rewrite — the hole
    `D-2026-09-13-a-plan-identity-that-omits-the-scope-approves-a-plan-nobody-read` closes, driven
    through the route above. **Insensitive to `status`**, or the canonical "tick the completed step,
    run the next one" batch revokes its own approval and an approved multi-step plan livelocks
    against the repeat guard (`tests/test_plan_gate.py` drives that half as an effect).

    It replaces a test in `tests/test_langgraph_agent.py` that claimed both engines hashed a plan to
    one identity, which survived this whole change green and could not have failed it: its assertion
    was
    `plan_identity([t["content"] for t in todos]) == plan_identity(titles)` over `todos` built from
    `titles` one line above — a value compared with itself, and about a second engine that no longer
    exists. That is the vacuous shape `tasks/lessons.md` records.
    """
    narrow = [_step("write up what we know", "record_knowledge_note")]
    widened = [_step("write up what we know", "record_knowledge_note", "watch_for")]
    assert plan_identity(narrow) != plan_identity(widened), (
        "a step's declaration is outside the plan's identity, so a widening rewrite is invisible "
        "to the decision route's 409 guard"
    )
    ticked = [{**narrow[0], "status": "completed"}]
    assert plan_identity(ticked) == plan_identity(narrow), (
        "ticking a step changed the plan's identity, which revokes the approval it is making "
        "progress under"
    )
    # Neither sensitivity is a property of the *order* a step declares its tools in, which nothing
    # about authorization depends on — an identity that moved on a reorder would revoke a live
    # approval for a change a chemist could not see.
    assert plan_identity(
        [_step("write up what we know", "watch_for", "record_knowledge_note")]
    ) == (plan_identity(widened))
    assert plan_identity([]) is None, "the empty plan is a constant every session shares"
