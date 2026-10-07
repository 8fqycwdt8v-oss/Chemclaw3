"""A plan approval authorizes the tools its steps declared, and nothing else.

`D-2026-09-12-an-approval-that-names-no-tool-authorizes-every-tool`.
`test_the_surface_a_read_only_plans_approval_reaches` is the ratchet: under an approval for a plan
that declared nothing, every name in `authz.side_effecting_tools()` must be refused. The migration
and `agent/plan_scope.py` cite this test rather than stating a count.
"""

import asyncio
from collections.abc import Iterator
from typing import Any, cast

import pytest
from pydantic import BaseModel, ValidationError

from chemclaw.agent import plan_approval_store as store_module
from chemclaw.agent.authz import memory_write_verbs, side_effecting_call, side_effecting_tools
from chemclaw.agent.plan_approval_store import InMemoryPlanApprovalStore
from chemclaw.agent.plan_gate import PlanNotApprovedError, enforce_plan_approval, plan_identity
from chemclaw.agent.plan_scope import (
    ScopedTodo,
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

    As in `tests/test_plan_gate.py`: the sharing between reader and writer is under test, so a
    patched-in double would hide a broken cache.
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
    """Record a human approval of `steps`, scoped with `declared_scope` as both decision surfaces
    do.
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

    `arguments` matter only for `authz.memory_write_verbs()`, where the path decides gatedness.
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


def _plan(*steps: dict[str, Any]) -> list[ScopedTodo]:
    """`_step` output as the `ScopedTodo` list the argument schema takes.

    One cast here instead of per-call-site ignores: `_step` returns the loose dict `write_todos` is
    actually called with, which is what these tests drive.
    """
    return cast(list[ScopedTodo], list(steps))


def test_an_approval_for_one_tool_does_not_authorize_another(
    approvals: InMemoryPlanApprovalStore,
) -> None:
    """An approval for a plan naming tool A refuses tool B in the same session.

    An effect assertion: one live approval, two calls, differing only in the declaration the human
    read.
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
    """An approval of a read-only plan reaches none of the side-effecting surface.

    Iterates `side_effecting_tools()` itself, so a bundle enabled later is covered the day it is.
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
    """The gate also reaches calls whose gatedness depends on their arguments.

    `authz.side_effecting_call` covers named tools plus `memory_write_verbs()`, where `/memories/`
    writes outlive the deployment and `/scratch/` writes die with the turn. Derived from `authz` and
    `scratchpad`, so a new argument-driven verb is covered when added.
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
    """A plan is bounded in steps and in tools per step at argument validation.

    Both sizes outlive the call (the refusal sentence, logs, the audit row). The scope only narrows,
    so this is not an escalation. Bounds are read from `settings`, so the assertion is that they
    bind, whatever a deployment sets.
    """
    over_steps = _plan(*(_step("x") for _ in range(settings.plan_max_steps + 1)))
    with pytest.raises(ValidationError, match=r"at most \d+ are accepted"):
        ScopedWriteTodosInput(todos=over_steps)

    wide = _step("x", *[f"tool-{i}" for i in range(settings.plan_max_tools_per_step + 1)])
    with pytest.raises(ValidationError, match=r"at most \d+ are accepted per step"):
        ScopedWriteTodosInput(todos=_plan(_step("first"), wide))


def test_the_refusal_names_the_step_so_the_model_can_split_the_plan() -> None:
    """The refusal names the step and says to split the plan.

    A `TypedDict` bound could only be a literal and pydantic's `too_long` names neither; the model
    needs which step and what to do.
    """
    wide = _step("x", *[f"tool-{i}" for i in range(settings.plan_max_tools_per_step + 1)])
    with pytest.raises(ValidationError) as raised:
        ScopedWriteTodosInput(todos=_plan(_step("first"), _step("second"), wide))
    message = raised.value.errors()[0]["msg"]
    assert "step 3" in message, f"the refusal does not say which step is too broad: {message}"
    assert "Split it" in message


def test_an_ordinary_plan_is_nowhere_near_either_bound() -> None:
    """An ordinary plan is far below both bounds.

    Asserted as a ratio, since "an eight-step plan validates" would also pass at a bound of nine.
    """
    ordinary = _plan(*(_step(f"step {i}", "gather_evidence", "expand_note") for i in range(8)))
    assert ScopedWriteTodosInput(todos=ordinary).todos
    assert settings.plan_max_steps >= 4 * len(ordinary)
    assert settings.plan_max_tools_per_step >= 8 * 2


def test_widening_a_step_after_approval_does_not_widen_the_approval(
    approvals: InMemoryPlanApprovalStore,
) -> None:
    """The model cannot grant itself a tool by editing its own declaration.

    The gate reads the recorded scope, never the live one, and the identity covers each step's
    declaration, so a widened plan is a different plan with no decision against it.
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
    """An out-of-scope refusal says what was approved, distinct from "not approved yet".

    The two have different remedies, and the second would read as a broken gate on an approved plan.
    """
    from chemclaw.agent.plan_gate import out_of_scope_refusal, plan_approval_refusal

    outside = str(out_of_scope_refusal("watch_for", frozenset({"record_knowledge_note"})))
    assert "record_knowledge_note" in outside, "the refusal does not say what was approved"
    assert "watch_for" in outside, "the refusal does not name the tool it refused"
    assert str(plan_approval_refusal("watch_for")) != outside, (
        "an unapproved plan and an out-of-scope tool read identically to a chemist"
    )


def test_a_plan_step_cannot_be_written_without_declaring_its_tools() -> None:
    """A plan step cannot be written without declaring its tools.

    Required in the schema, so an omission is a tool-argument error the model retries, rather than
    an unbounded step or a confusing authorization refusal.
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
    """The scoped plan tool keeps the name and channel the gate and harness use.

    `plan_gate._PLAN_WRITE_TOOL`, `chemclaw_agent.harness_tool_names` and the `todos` channel spell
    them by hand; a rename would silently disable the gate's batch rule.
    """
    middleware = ScopedTodoListMiddleware()
    assert [tool.name for tool in middleware.tools] == ["write_todos"]
    assert middleware.state_schema.__name__ == "PlanningState"


def test_an_unreadable_declaration_narrows_rather_than_widens() -> None:
    """`declared_scope` fails closed: a malformed `tools` contributes nothing."""
    assert declared_scope([{"content": "a", "tools": "record_knowledge_note"}]) == frozenset()
    assert declared_scope([{"content": "a", "tools": 7}]) == frozenset()
    assert declared_scope([{"content": "a"}]) == frozenset()
    assert declared_scope([{"content": "a", "tools": ["x", 7, "y"]}]) == frozenset({"x", "y"})


def test_the_durable_backend_round_trips_a_scope_and_defaults_a_legacy_row_to_none() -> None:
    """The Postgres backend round-trips a scope and reads a legacy row as the empty set.

    The migration's `DEFAULT '{}'` means an approval in flight across the upgrade authorizes nothing
    and is asked for again, rather than authorizing everything.
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
    """`POST /sessions/{id}/plan/decision` stamps the plan's declared scope.

    The gate reads only what the route stamps, so a wrong stamp disables the feature or reopens the
    defect. `GET .../plan` also returns the declaration, since the person is approving the scope.
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


def test_the_stream_and_the_route_name_the_same_scope_for_one_plan(
    monkeypatch: pytest.MonkeyPatch, approvals: InMemoryPlanApprovalStore
) -> None:
    """The stream's `plan` event and the route name the same scope for one plan.

    Clients answer the plan they rendered from the stream, so the stream must carry the scope too.
    Asserted as equality, not presence, so a scope computed from a different reading of the plan
    fails. Driven through the real route and `graph_stream._from_update`.
    """
    import asyncio as _asyncio

    from fastapi.testclient import TestClient

    from chemclaw.api.app import create_app
    from chemclaw.api.auth import Principal, require_principal
    from chemclaw.api.graph_stream import _from_update
    from chemclaw.api.routes import plan as plan_routes
    from chemclaw.api.runner_trace import ToolCallTrace
    from tests.test_service import _FakeOwnerStore, _no_connectors

    steps = [
        _step("write up the result", "record_knowledge_note"),
        _step("watch the reactor", "watch_for", "record_knowledge_note"),
        _step("read the table"),
    ]

    async def _read(session_id: str, **_kwargs: Any) -> list[dict[str, Any]]:
        return steps

    monkeypatch.setattr(plan_routes, "session_plan", _read)
    app = create_app(owner_store=_FakeOwnerStore(), connector_factory=_no_connectors)
    app.state.plan_approvals = approvals
    app.dependency_overrides[require_principal] = lambda: Principal(
        oid="alice", upn="alice@corp", roles=frozenset()
    )
    client = TestClient(app)
    session_id = client.post("/sessions").json()["session_id"]
    fetched = client.get(f"/sessions/{session_id}/plan").json()

    async def _stream() -> list[Any]:
        trace = ToolCallTrace()
        return [
            event
            async for event in _from_update(
                {"agent": {"todos": steps}}, agent="", emit_plan=True, trace=trace, todos=[]
            )
        ]

    (streamed,) = [event for event in _asyncio.run(_stream()) if event.type == "plan"]
    assert streamed.scope, (
        "the streamed plan named no tool although its steps declare some, so a card rendered from "
        "the stream asks for a yes to an authorization it cannot display"
    )
    assert streamed.scope == fetched["scope"], (
        f"the stream says approving this plan authorizes {streamed.scope} and "
        f"`GET /sessions/{{id}}/plan` says {fetched['scope']}. One plan, two answers about what a "
        "chemist is being asked to authorize — whichever they read, the other surface is lying. "
        "Both must come from `plan_scope.declared_scope` over the same steps the identity is "
        "hashed from."
    )
    assert streamed.plan_hash == fetched["plan_hash"], (
        "the scopes agree and the identities do not, so the two surfaces are describing different "
        "plans and the agreement above is a coincidence"
    )


def test_a_rewrite_that_widens_a_declaration_does_not_pass_the_freshness_guard(
    monkeypatch: pytest.MonkeyPatch, approvals: InMemoryPlanApprovalStore
) -> None:
    """A rewrite that widens a declaration does not pass the decision route's freshness guard.

    A plan can be rewritten while its decision card is open (`out_of_scope_refusal` tells the model
    to do exactly that). Because the identity covers each step's declaration, the hash the card
    showed no longer matches and the route refuses. The test computes no identity itself; it posts
    the one the server rendered.
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
    """The plan's identity moves with its declaration and not with its progress.

    Sensitive to the declaration, or the freshness guard misses a widening rewrite; insensitive to
    `status`, or ticking a step revokes its own approval (`tests/test_plan_gate.py` drives that
    effect).
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
