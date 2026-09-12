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
from chemclaw.agent.authz import side_effecting_tools
from chemclaw.agent.plan_approval_store import InMemoryPlanApprovalStore
from chemclaw.agent.plan_gate import PlanNotApprovedError, enforce_plan_approval, plan_identity
from chemclaw.agent.plan_scope import ScopedTodoListMiddleware, declared_scope
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
    plan_hash = plan_identity([str(step["content"]) for step in steps])
    assert plan_hash is not None
    await store.record(session_id, plan_hash, "chemist-1", True, declared_scope(steps))


async def _ran(tool: str, session_id: str, steps: list[dict[str, Any]]) -> bool:
    """Drive one call through the gate under `steps`; True when the tool body ran."""
    ran = False

    async def _handler(_request: Any) -> Any:
        nonlocal ran
        ran = True

    request = tool_request(tool)
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


def test_widening_a_step_after_approval_does_not_widen_the_approval(
    approvals: InMemoryPlanApprovalStore,
) -> None:
    """The model cannot grant itself a tool by editing its own declaration.

    `plan_identity` hashes `content` only — deliberately, so the canonical "tick the step, run its
    tool" batch keeps its approval — which means a plan whose text is unchanged and whose `tools`
    have grown hashes to the *same approved plan*. The gate must therefore read the recorded scope
    and never the live one. If it read the live one this test's second call would run.
    """

    async def _run() -> bool:
        approved = [_step("write up what we know about aspirin", "record_knowledge_note")]
        await _approve(approvals, "widened", approved)
        widened = [
            _step("write up what we know about aspirin", "record_knowledge_note", "watch_for")
        ]
        assert plan_identity([s["content"] for s in approved]) == plan_identity(
            [s["content"] for s in widened]
        ), "the precondition is that widening a declaration does not change the plan's identity"
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
