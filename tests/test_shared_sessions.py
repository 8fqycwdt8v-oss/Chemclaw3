"""Several people in one session: the sender governs, a plan's author decides, the owner admits.

`D-2026-09-27-in-a-shared-session-the-sender-governs`. Every test here drives the production entry
point — `create_app` over `TestClient`, the real plan gate middleware, the real erasure — because
each property is a claim about who a request or a turn *is*, and a unit that takes the identity as
an argument proves only that it uses the argument it was given.

The security floor, one test (or more) each:

- a stranger is answered 404 on every session route, before and after somebody else is admitted;
- a member's turn runs as the member — their oid, their roles, their memories — never the owner's;
- a member cannot approve the owner's plan, the owner can, and the owner cannot approve a member's;
- an approval authorizes only its approver's turns, so nobody acts under another person's yes;
- the owner's acts (delete, fork, admit, remove another, stop another's turn) refuse a member;
- membership is revocable, and revocation takes effect on the next request;
- and over a real database: the membership rows cascade, and an erasure takes a member's words and
  standing out of a session somebody else owns while claiming that session for its duration.
"""

import asyncio
import math
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from chemclaw.agent import plan_approval_store as store_module
from chemclaw.agent import session_members as members_module
from chemclaw.agent.plan_approval_store import InMemoryPlanApprovalStore
from chemclaw.agent.plan_gate import (
    PlanNotApprovedError,
    approved_scope,
    enforce_plan_approval,
    plan_identity,
)
from chemclaw.agent.preferences import _STORE as PREFERENCES
from chemclaw.agent.preferences import recall_preferences
from chemclaw.agent.scratchpad import memory_namespace
from chemclaw.agent.session import TurnSession
from chemclaw.agent.session_members import InMemorySessionMemberStore
from chemclaw.api.app import create_app
from chemclaw.api.auth import Principal, require_principal
from chemclaw.api.routes import plan as plan_routes
from chemclaw.api.state import TurnLease
from chemclaw.core.config import settings
from chemclaw.core.identity_context import (
    get_current_actor,
    get_current_roles,
    reset_current_identity,
    set_current_identity,
)
from chemclaw.core.session_context import reset_current_session_id, set_current_session_id
from tests.fakes_turn import Piece, ScriptedTurn
from tests.middleware import run_middleware, tool_request
from tests.test_service import _FakeOwnerStore, _no_connectors

_ANA = Principal(oid="ana-shared", upn="ana@corp", roles=frozenset({"process-chemist"}))
_BEN = Principal(oid="ben-shared", upn="ben@corp", roles=frozenset({"analyst"}))
_CAT = Principal(oid="cat-shared", upn="cat@corp", roles=frozenset({"process-chemist"}))

_STEPS = [{"content": "write up the result", "status": "pending", "tools": ["watch_for"]}]


@pytest.fixture(autouse=True)
def _stores(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fresh in-process membership and approval stores, obtained through their real factories."""
    monkeypatch.setattr(settings, "session_store", "memory")
    monkeypatch.setattr(settings, "entra_required", True)
    members_module.session_member_store.cache_clear()
    store_module.plan_approval_store.cache_clear()
    yield
    members_module.session_member_store.cache_clear()
    store_module.plan_approval_store.cache_clear()


class _Recorder(ScriptedTurn):
    """A turn that answers once and records who it ran as, from inside the running graph."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[dict[str, Any]] = []

    def create_session(self, *, session_id: str) -> TurnSession:
        """The one non-streaming method the front door calls on an agent."""
        return TurnSession(session_id=session_id)

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        """Read the turn's ambient identity and memories the way a tool body would read them."""
        actor = get_current_actor()
        recalled = await recall_preferences()
        self.seen.append(
            {
                "actor": actor,
                "roles": get_current_roles(),
                "memories": memory_namespace(actor) if actor else None,
                "preferences": {p.key: p.value for p in recalled},
            }
        )
        yield "ok"


class _Stoppable:
    """Stands in for a running turn: records whether the stop route reached it."""

    def __init__(self) -> None:
        self.stopped = False

    async def stop(self) -> None:
        """What `DetachableTurn.stop` is called for."""
        self.stopped = True


def _app(turn: ScriptedTurn | None = None) -> Any:
    """The production app, durable-ownership registry faked, model faked."""
    return create_app(
        owner_store=_FakeOwnerStore(),
        connector_factory=_no_connectors,
        graph_factory=(turn or _Recorder()).graph_factory,
    )


def _as(app: Any, principal: Principal) -> TestClient:
    """A client whose every request arrives as `principal` — authentication is not under test."""
    app.dependency_overrides[require_principal] = lambda: principal
    return TestClient(app)


def _shared(app: Any) -> str:
    """A session Ana owns, with Ben admitted — the fixture every sharing test starts from."""
    session_id = str(_as(app, _ANA).post("/sessions").json()["session_id"])
    res = _as(app, _ANA).put(f"/sessions/{session_id}/members/{_BEN.oid}")
    assert res.status_code == 204, res.text
    return session_id


def _post(app: Any, principal: Principal, session_id: str, message: str) -> None:
    """Run one turn to the end of its stream as `principal`."""
    with _as(app, principal).stream(
        "POST", f"/sessions/{session_id}/messages", json={"message": message}
    ) as res:
        assert res.status_code == 200, res.read()
        for _line in res.iter_lines():
            pass


# --- who may reach a session -------------------------------------------------------------------


def test_a_stranger_is_answered_404_everywhere_and_a_member_is_let_in() -> None:
    """Membership is the only way past the gate, and a stranger learns nothing about the id."""
    app = _app()
    session_id = str(_as(app, _ANA).post("/sessions").json()["session_id"])
    paths = (f"/sessions/{session_id}/messages", f"/sessions/{session_id}/members")

    for path in paths:
        assert _as(app, _BEN).get(path).status_code == 404, f"{path} leaked to a non-member"

    assert _as(app, _ANA).put(f"/sessions/{session_id}/members/{_BEN.oid}").status_code == 204
    for path in paths:
        assert _as(app, _BEN).get(path).status_code == 200, f"{path} refused a member"
        assert _as(app, _CAT).get(path).status_code == 404, f"{path} leaked to a non-member"

    listed = _as(app, _BEN).get(f"/sessions/{session_id}/members").json()
    assert listed["owner"] == _ANA.oid
    assert [m["actor"] for m in listed["members"]] == [_BEN.oid]
    shared = _as(app, _BEN).get("/sessions/shared").json()
    assert [row["session_id"] for row in shared] == [session_id]
    assert _as(app, _CAT).get("/sessions/shared").json() == []


def test_removing_a_member_takes_effect_on_their_next_request() -> None:
    """Membership is read per request, so revocation does not wait for a cache eviction."""
    app = _app()
    session_id = _shared(app)
    assert _as(app, _BEN).get(f"/sessions/{session_id}/messages").status_code == 200

    assert _as(app, _ANA).delete(f"/sessions/{session_id}/members/{_BEN.oid}").status_code == 204
    assert _as(app, _BEN).get(f"/sessions/{session_id}/messages").status_code == 404
    assert _as(app, _ANA).delete(f"/sessions/{session_id}/members/{_BEN.oid}").status_code == 404


def test_the_owners_acts_refuse_a_member_and_a_member_may_leave() -> None:
    """Admitting, removing another, deleting and forking are the owner's; leaving is a member's."""
    app = _app()
    session_id = _shared(app)
    ben = _as(app, _BEN)

    assert ben.put(f"/sessions/{session_id}/members/{_CAT.oid}").status_code == 403
    assert _as(app, _CAT).get(f"/sessions/{session_id}/messages").status_code == 404
    assert ben.delete(f"/sessions/{session_id}/members/{_ANA.oid}").status_code == 403
    assert ben.delete(f"/sessions/{session_id}").status_code == 403
    assert ben.post(f"/sessions/{session_id}/fork").status_code == 403
    # The owner is not a member of their own session, and cannot be made one.
    assert _as(app, _ANA).put(f"/sessions/{session_id}/members/{_ANA.oid}").status_code == 409

    assert ben.delete(f"/sessions/{session_id}/members/{_BEN.oid}").status_code == 204
    assert ben.get(f"/sessions/{session_id}/messages").status_code == 404


def test_a_member_stops_only_their_own_turn_and_the_owner_any() -> None:
    """One member ending another's work in flight is not a standing a membership grants."""
    app = _app()
    session_id = _shared(app)
    turn = _Stoppable()
    app.state.running_turns.register(session_id, turn)
    app.state.active_turns[session_id] = TurnLease(
        token="t", deadline=math.inf, actor=_ANA.oid, claimed_at=0.0
    )

    assert _as(app, _BEN).post(f"/sessions/{session_id}/turn/stop").status_code == 403
    assert not turn.stopped, "a member stopped the owner's turn"
    assert _as(app, _ANA).post(f"/sessions/{session_id}/turn/stop").status_code == 200
    assert turn.stopped


# --- whose authority a turn carries -----------------------------------------------------------


def test_a_members_turn_runs_as_the_member_with_their_roles_and_memories() -> None:
    """The sender governs: identity, roles and `/memories/` and `recall_*` are the member's.

    Ana has a preference on file and Ben has none, so a turn that loaded the owner's memories would
    read Ana's solvent back inside Ben's turn — the isolation this asserts on the value, not on a
    namespace string alone.
    """
    asyncio.run(PREFERENCES.remember(_ANA.oid, "solvent", "2-MeTHF"))
    turn = _Recorder()
    app = _app(turn)
    session_id = _shared(app)

    _post(app, _BEN, session_id, "what should I run next?")
    _post(app, _ANA, session_id, "and for me?")

    ben, ana = turn.seen
    assert ben["actor"] == _BEN.oid, f"a member's turn ran as {ben['actor']}"
    assert ben["roles"] == _BEN.roles, "a member's turn carried somebody else's roles"
    assert ben["memories"] == memory_namespace(_BEN.oid)
    assert ben["preferences"] == {}, "the owner's memories loaded into a member's turn"
    assert ana["actor"] == _ANA.oid and ana["roles"] == _ANA.roles
    assert ana["preferences"] == {"solvent": "2-MeTHF"}


# --- who may decide a plan --------------------------------------------------------------------


def _decide(app: Any, principal: Principal, session_id: str) -> int:
    """Approve the session's current plan as `principal`; the status the route answered."""
    client = _as(app, principal)
    shown = client.get(f"/sessions/{session_id}/plan")
    if shown.status_code != 200:
        return shown.status_code
    return client.post(
        f"/sessions/{session_id}/plan/decision",
        json={"approved": True, "plan_hash": shown.json()["plan_hash"]},
    ).status_code


@pytest.fixture
def planned(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Any, str, str]]:
    """A shared session proposing `_STEPS`: `(app, session_id, plan_hash)`."""

    async def _plan(session_id: str, **_kwargs: Any) -> list[dict[str, Any]]:
        return _STEPS

    monkeypatch.setattr(plan_routes, "session_plan", _plan)
    app = _app()
    session_id = _shared(app)
    plan_hash = plan_identity(_STEPS)
    assert plan_hash is not None
    yield app, session_id, plan_hash


def test_a_member_cannot_approve_the_owners_plan_and_the_owner_can(
    planned: tuple[Any, str, str],
) -> None:
    """Only the plan's author decides it — the owner's plan is not a member's to approve."""
    app, session_id, plan_hash = planned
    asyncio.run(app.state.plan_approvals.record_author(session_id, plan_hash, _ANA.oid))

    assert _decide(app, _BEN, session_id) == 403
    assert _decide(app, _CAT, session_id) == 404
    assert asyncio.run(app.state.plan_approvals.decision(session_id, plan_hash)) is None
    assert _decide(app, _ANA, session_id) == 204
    shown = _as(app, _BEN).get(f"/sessions/{session_id}/plan").json()
    assert shown["author"] == _ANA.oid and shown["decided_by"] == _ANA.oid


def test_the_owner_cannot_approve_a_members_plan(planned: tuple[Any, str, str]) -> None:
    """Ownership is not authorship: a plan a member's turn wrote is the member's to decide."""
    app, session_id, plan_hash = planned
    asyncio.run(app.state.plan_approvals.record_author(session_id, plan_hash, _BEN.oid))

    assert _decide(app, _ANA, session_id) == 403
    assert _decide(app, _BEN, session_id) == 204


def test_with_no_recorded_author_the_owner_decides_and_a_member_does_not(
    planned: tuple[Any, str, str],
) -> None:
    """A plan from before authorship existed falls back to the owner rule it always had."""
    app, session_id, _plan_hash = planned
    assert _decide(app, _BEN, session_id) == 403
    assert _decide(app, _ANA, session_id) == 204


def test_the_owners_inbox_does_not_list_a_plan_a_members_turn_wrote(
    planned: tuple[Any, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An inbox entry the owner would then be refused on is an entry with nothing behind it."""
    app, session_id, plan_hash = planned
    monkeypatch.setattr(plan_routes, "_plan_gated", lambda _profile: True)
    owners = app.state.session_owners
    asyncio.run(owners.set_title_if_absent(session_id, "a shared question"))

    listed = _as(app, _ANA).get("/plans/pending").json()["plans"]
    assert [p["session_id"] for p in listed] == [session_id]
    asyncio.run(app.state.plan_approvals.record_author(session_id, plan_hash, _BEN.oid))
    assert _as(app, _ANA).get("/plans/pending").json()["plans"] == []


# --- what an approval authorizes --------------------------------------------------------------


async def _scope_as(actor: str, session_id: str, plan_hash: str) -> frozenset[str] | None:
    """The scope the gate would find standing for a turn `actor` is running."""
    tokens = set_current_identity(actor, frozenset())
    try:
        return await approved_scope(session_id, plan_hash)
    finally:
        reset_current_identity(tokens)


def test_an_approval_authorizes_only_its_approvers_turns() -> None:
    """Nobody acts under another person's yes — not a member under the owner's, nor the reverse."""
    store = store_module.plan_approval_store()
    assert isinstance(store, InMemoryPlanApprovalStore)
    plan_hash = plan_identity(_STEPS)
    assert plan_hash is not None
    asyncio.run(store.record("sess-bind", plan_hash, _ANA.oid, True, ["watch_for"]))

    assert asyncio.run(_scope_as(_ANA.oid, "sess-bind", plan_hash)) == frozenset({"watch_for"})
    assert asyncio.run(_scope_as(_BEN.oid, "sess-bind", plan_hash)) is None


def test_a_members_gated_call_is_refused_under_the_owners_approval() -> None:
    """The same property through the middleware a turn's tool call actually crosses."""
    store = store_module.plan_approval_store()
    plan_hash = plan_identity(_STEPS)
    assert plan_hash is not None
    asyncio.run(store.record("sess-gate", plan_hash, _ANA.oid, True, ["watch_for"]))

    async def _call(actor: str) -> bool:
        ran = False

        async def _handler(_request: Any) -> Any:
            nonlocal ran
            ran = True

        request = tool_request("watch_for", {"query": "x"})
        object.__setattr__(request, "state", {"todos": _STEPS})
        session_token = set_current_session_id("sess-gate")
        identity = set_current_identity(actor, frozenset())
        try:
            await run_middleware(enforce_plan_approval, request, _handler)
        except PlanNotApprovedError:
            return False
        finally:
            reset_current_identity(identity)
            reset_current_session_id(session_token)
        return ran

    assert asyncio.run(_call(_BEN.oid)) is False, "a member acted under the owner's approval"
    assert asyncio.run(_call(_ANA.oid)) is True


def test_writing_the_plan_stamps_the_turns_sender_as_its_author() -> None:
    """`write_todos` through the gate records who wrote it, last writer winning."""
    from langchain_core.messages import AIMessage

    store = store_module.plan_approval_store()
    plan_hash = plan_identity(_STEPS)
    assert plan_hash is not None

    async def _write(actor: str) -> None:
        request = tool_request("write_todos", {"todos": _STEPS}, call_id="w-1")
        message = AIMessage(
            content="",
            tool_calls=[{"id": "w-1", "name": "write_todos", "args": {"todos": _STEPS}}],
        )
        object.__setattr__(request, "state", {"messages": [message], "todos": []})

        async def _handler(_request: Any) -> Any:
            return None

        session_token = set_current_session_id("sess-author")
        identity = set_current_identity(actor, frozenset())
        try:
            await run_middleware(enforce_plan_approval, request, _handler)
        finally:
            reset_current_identity(identity)
            reset_current_session_id(session_token)

    asyncio.run(_write(_ANA.oid))
    assert asyncio.run(store.author("sess-author", plan_hash)) == _ANA.oid
    asyncio.run(_write(_BEN.oid))
    assert asyncio.run(store.author("sess-author", plan_hash)) == _BEN.oid


# --- the one rule, below the routes -----------------------------------------------------------


def test_participant_permits_admits_the_owner_and_members_and_nobody_else() -> None:
    """The rule the gate and the evidence tool share, including the owner-less session."""
    store = members_module.session_member_store()
    assert isinstance(store, InMemorySessionMemberStore)
    asyncio.run(store.add("sess-rule", _BEN.oid))
    permits = members_module.participant_permits

    assert asyncio.run(permits("sess-rule", _ANA.oid, _ANA.oid))
    assert asyncio.run(permits("sess-rule", _ANA.oid, _BEN.oid))
    assert not asyncio.run(permits("sess-rule", _ANA.oid, _CAT.oid))
    assert not asyncio.run(permits("sess-rule", _ANA.oid, None))
    # A session nobody owns admits no member: nobody held the standing to have let them in.
    assert not asyncio.run(permits("sess-rule", None, _BEN.oid))


# --- over a real database ---------------------------------------------------------------------


async def _pg_count(table: str, column: str, value: str) -> int:
    """How many rows of `table` carry `value` in `column`."""
    from chemclaw.core import db

    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(f"SELECT count(*) FROM {table} WHERE {column} = %s", (value,))
            row = await cur.fetchone()
    return int(row[0]) if row else 0


async def _say(session_id: str, actor: str, text: str) -> None:
    """Store one message in `session_id` as `actor` — through the real transcript writer."""
    from langchain_core.messages import HumanMessage

    from chemclaw.agent.session_store import PostgresHistoryProvider

    tokens = set_current_identity(actor, frozenset())
    try:
        await PostgresHistoryProvider().save_messages(session_id, [HumanMessage(content=text)])
    finally:
        reset_current_identity(tokens)


async def test_an_erasure_takes_a_members_words_and_standing_and_claims_the_shared_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leaver's rows in a session somebody else owns go; the owner's stay; the session is held.

    Three halves, each a way the sweep was blind to a shared session before membership existed: the
    member's messages (by author), their membership and their plan authorship (by person), and the
    turn claim — a member mid-turn in the owner's session is writing exactly the rows being erased,
    so the claim that stops that race on the leaver's own sessions has to span this one too. And
    the owner's session is *not* reported as residue: its ownership row stays, and so do the
    owner's own words.
    """
    import uuid
    from contextlib import asynccontextmanager

    from chemclaw.agent import leaver
    from chemclaw.agent.plan_approval_store import PlanApprovalStore
    from chemclaw.agent.session_members import SessionMemberStore
    from chemclaw.agent.session_store import SessionOwnerStore
    from tests.pg import migrated_db_or_skip

    await migrated_db_or_skip()
    session_id = f"sess-shared-{uuid.uuid4().hex[:8]}"
    ana, ben = f"ana-{session_id}", f"ben-{session_id}"
    await SessionOwnerStore().record(session_id, ana)
    await SessionMemberStore().add(session_id, ben)
    await PlanApprovalStore().record_author(session_id, "plan-by-ben", ben)
    await _say(session_id, ana, "the owner's question")
    await _say(session_id, ben, "the member's question")

    held: list[str] = []
    real_hold = leaver._sessions_held

    @asynccontextmanager
    async def _spy(sessions: list[str]) -> AsyncIterator[None]:
        held.extend(sessions)
        async with real_hold(sessions):
            yield

    monkeypatch.setattr(leaver, "_sessions_held", _spy)
    report = await leaver.erase_actor(ben, apply=True)

    assert session_id in held, "the erasure did not claim the shared session it was erasing from"
    assert report.erased["session_members"] == 1
    assert report.erased["plan_authors"] == 1
    assert report.erased["session_messages"] == 1
    assert not report.residue, f"the owner's surviving session was reported as residue: {report}"
    assert await _pg_count("session_messages", "actor", ben) == 0
    assert await _pg_count("session_messages", "actor", ana) == 1
    assert await _pg_count("session_owners", "session_id", session_id) == 1


async def test_a_sessions_members_and_plan_authors_go_with_its_ownership_row() -> None:
    """The cascade: delete, retention and an owner's erasure each need no second statement."""
    import uuid

    from chemclaw.agent.plan_approval_store import PlanApprovalStore
    from chemclaw.agent.session_members import SessionMemberStore
    from chemclaw.agent.session_store import SessionOwnerStore
    from chemclaw.core import db
    from tests.pg import migrated_db_or_skip

    await migrated_db_or_skip()
    session_id = f"sess-cascade-{uuid.uuid4().hex[:8]}"
    await SessionOwnerStore().record(session_id, "ana-cascade")
    members = SessionMemberStore()
    await members.add(session_id, "ben-cascade")
    await members.add(session_id, "ben-cascade")  # admitting twice is one membership
    await PlanApprovalStore().record_author(session_id, "plan-x", "ben-cascade")
    assert [m.actor for m in await members.members(session_id)] == ["ben-cascade"]
    assert [s.session_id for s in await members.shared_with("ben-cascade")] == [session_id]
    # A session with no ownership row records no author rather than failing the turn that wrote it.
    await PlanApprovalStore().record_author(f"{session_id}-orphan", "plan-x", "ben-cascade")
    assert await PlanApprovalStore().author(f"{session_id}-orphan", "plan-x") is None

    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM session_owners WHERE session_id = %s", (session_id,))
        await conn.commit()

    assert await _pg_count("session_members", "session_id", session_id) == 0
    assert await _pg_count("plan_authors", "session_id", session_id) == 0
