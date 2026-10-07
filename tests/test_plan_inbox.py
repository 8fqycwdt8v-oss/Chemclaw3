"""`GET /plans/pending`: the cross-session inbox of plans nobody has decided yet.

Pinned properties:

- **The filter is "undecided", not "unapproved"**: an approval is spent at the end of its turn
  (D-167), so "no live approval" is the resting state of finished work.
- **A session that cannot be holding a decision is never read**: the prune uses the profile on the
  ownership row, and reads are counted, since checkpointer statements serialize against every
  concurrent turn on the pod.
- **An empty list says which emptiness it is**: `gated == 0` means no plan gate; `unread > 0`
  means the answer is partial.
"""

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from chemclaw.agent.plan_approval_store import InMemoryPlanApprovalStore
from chemclaw.agent.plan_gate import plan_identity
from chemclaw.agent.profiles import _REGISTRY, AgentProfile
from chemclaw.api.app import create_app
from chemclaw.api.auth import Principal, require_principal
from chemclaw.api.routes import plan as plan_routes
from chemclaw.core.config import settings
from tests.test_service import _FakeOwnerStore, _no_connectors

_ALICE = Principal(oid="alice", upn="alice@corp", roles=frozenset())
_BOB = Principal(oid="bob", upn="bob@corp", roles=frozenset())


# What every step in this file declares. Non-empty so a route reporting the scope is
# distinguishable from one reporting `[]`; listing does not depend on it, so no case varies it.
_DECLARES = ["record_knowledge_note"]


def _steps(lines: list[str]) -> list[dict[str, Any]]:
    """Plan lines in the shape `plan_state.session_plan` answers: steps, declaration included.

    Shared by the stubbed read, the recorded decision and the identity check, since the declaration
    is part of the plan's identity.
    """
    return [{"content": line, "status": "pending", "tools": list(_DECLARES)} for line in lines]


class _Inbox:
    """The front door with the two stores this route reads, both in memory and inspectable.

    A helper rather than a fixture so each test's arrangement is legible in its body. The plan read
    is stubbed at `routes.plan.session_plan`; the checkpointer decode is
    `tests/test_plan_state.py`'s.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Wire an app whose plan reads come from `self.todos` and are counted in `self.reads`."""
        self.owners = _FakeOwnerStore()
        self.approvals = InMemoryPlanApprovalStore()
        # `None` for a session whose plan is unreadable, matching `plan_state.session_plan`; the
        # route counts it as `unread`. Bare lines; `_steps` shapes them.
        self.todos: dict[str, list[str] | None] = {}
        self.reads: list[str] = []
        self.app = create_app(owner_store=self.owners, connector_factory=_no_connectors)
        self.app.state.plan_approvals = self.approvals
        self.app.dependency_overrides[require_principal] = lambda: _ALICE

        async def _plan(session_id: str, **_kwargs: Any) -> list[dict[str, Any]] | None:
            self.reads.append(session_id)
            lines = self.todos.get(session_id)
            return None if lines is None else _steps(lines)

        monkeypatch.setattr(plan_routes, "session_plan", _plan)
        self.client = TestClient(self.app)

    def add_session(
        self, session_id: str, *, owner: str | None, profile: str | None, plan: list[str] | None
    ) -> None:
        """One session in the registry, with a plan.

        Titling it is what makes it *listable*: the real query derives last activity from
        `session_messages` and drops a session nobody has spoken in, and the fake reproduces that.
        """
        asyncio.run(self.owners.record(session_id, owner, profile))
        asyncio.run(self.owners.set_title_if_absent(session_id, f"conversation {session_id}"))
        self.todos[session_id] = plan

    def decide(self, session_id: str, plan: list[str], *, approved: bool, spent: bool) -> None:
        """Record a human decision on `session_id`'s plan, optionally already spent by its turn."""
        asyncio.run(
            # The inbox lists what nobody has decided on, so *what* a decision authorizes is
            # irrelevant here and the scope is empty on purpose — an approval that permits no
            # tool is still a decision, and this route must not list it.
            self.approvals.record(
                session_id, plan_identity(_steps(plan)) or "", "alice", approved, ()
            )
        )
        if spent:
            asyncio.run(self.approvals.consume_all(session_id))

    def get(self) -> dict[str, Any]:
        """The inbox as the caller sees it."""
        response = self.client.get("/plans/pending")
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body


@pytest.fixture
def gated(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment whose default profile is plan-gated — the posture the inbox exists for."""
    monkeypatch.setattr(settings, "harness_enabled", True)
    monkeypatch.setattr(settings, "harness_autonomy", "plan_only")


@pytest.mark.usefixtures("gated")
def test_an_undecided_plan_is_listed_with_the_conversation_that_holds_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row carries what a chemist navigates by: the session, its name, the steps and the scope.

    The session id is what a chemist who closed the tab cannot reconstruct. `scope` shows what
    approving would authorize, which a decision from the inbox requires.
    """
    inbox = _Inbox(monkeypatch)
    plan = ["screen the hazards", "file the note"]
    inbox.add_session("sess-blocked", owner="alice", profile=None, plan=plan)
    inbox.add_session("sess-quiet", owner="alice", profile=None, plan=[])

    body = inbox.get()

    assert [row["session_id"] for row in body["plans"]] == ["sess-blocked"], (
        "a session proposing nothing has nothing to decide on and must not be listed"
    )
    row = body["plans"][0]
    assert row["plan"] == plan
    assert row["title"] == "conversation sess-blocked"
    assert row["scope"] == _DECLARES, (
        "the row does not say what approving this plan would authorize: "
        f"{row['scope']} against the {_DECLARES} its steps declare"
    )
    assert row["plan_hash"] == plan_identity(_steps(plan)), (
        "the row must name the plan the gate would ask about, not a second hashing of it"
    )
    # Whose conversation it is, on the row (Chemclaw3 #503) — here the caller's own.
    assert row["owner"] == "alice"
    assert (body["considered"], body["gated"], body["unread"]) == (2, 2, 0)


@pytest.mark.usefixtures("gated")
def test_a_decided_plan_is_not_waiting_on_anyone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Approved, spent and rejected are all answers; only an unanswered plan is an inbox row.

    The spent case decides the design: the in-turn card's "live approval" predicate would list every
    finished plan-gated conversation forever.
    """
    inbox = _Inbox(monkeypatch)
    plan = ["run the calculation"]
    for session_id in ("sess-approved", "sess-spent", "sess-rejected", "sess-undecided"):
        inbox.add_session(session_id, owner="alice", profile=None, plan=plan)
    inbox.decide("sess-approved", plan, approved=True, spent=False)
    inbox.decide("sess-spent", plan, approved=True, spent=True)
    inbox.decide("sess-rejected", plan, approved=False, spent=False)

    listed = [row["session_id"] for row in inbox.get()["plans"]]

    assert listed == ["sess-undecided"], (
        "only a plan nobody has decided is waiting on somebody; a spent approval and a rejection "
        f"are both answers, and this listed {listed}"
    )


@pytest.mark.usefixtures("gated")
def test_the_inbox_never_names_a_session_the_caller_does_not_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ownership comes from the registry `GET /sessions` reads, so the two cannot disagree.

    A route that listed another chemist's blocked plan would leak both the existence of their
    conversation and its contents — the plan text is the agent's reading of what they asked.
    """
    inbox = _Inbox(monkeypatch)
    inbox.add_session("sess-alice", owner="alice", profile=None, plan=["alice's step"])
    inbox.add_session("sess-bob", owner="bob", profile=None, plan=["bob's step"])

    assert [row["session_id"] for row in inbox.get()["plans"]] == ["sess-alice"]

    inbox.app.dependency_overrides[require_principal] = lambda: _BOB
    assert [row["session_id"] for row in inbox.get()["plans"]] == ["sess-bob"]


def test_a_session_that_cannot_hold_a_decision_is_never_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no plan gate there is nothing to wait on, and the checkpointer is not asked.

    `gated == 0` lets a surface say "no plan gate" rather than "nothing waiting", and the read count
    keeps the inbox from taxing the pod's checkpointer on ungated deployments.
    """
    monkeypatch.setattr(settings, "harness_enabled", False)
    inbox = _Inbox(monkeypatch)
    inbox.add_session("sess-one", owner="alice", profile=None, plan=["a step"])

    body = inbox.get()

    assert body["plans"] == []
    assert (body["considered"], body["gated"], body["unread"]) == (1, 0, 0)
    assert inbox.reads == [], f"a plan was read for an ungated session: {inbox.reads}"


@pytest.mark.usefixtures("gated")
def test_a_profile_that_executes_without_asking_is_not_waiting_either(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`harness_autonomy="execute"` has a plan and no gate, so nobody is being asked anything.

    The prune is `gate_applies`, not `harness_enabled_for`, which only says a todo list exists.
    """
    monkeypatch.setitem(
        _REGISTRY, "autonomous", AgentProfile(name="autonomous", harness_autonomy="execute")
    )
    inbox = _Inbox(monkeypatch)
    inbox.add_session("sess-gated", owner="alice", profile=None, plan=["ask first"])
    inbox.add_session("sess-free", owner="alice", profile="autonomous", plan=["just do it"])

    body = inbox.get()

    assert [row["session_id"] for row in body["plans"]] == ["sess-gated"]
    assert (body["considered"], body["gated"]) == (2, 1)
    assert inbox.reads == ["sess-gated"]


@pytest.mark.usefixtures("gated")
def test_the_scan_is_bounded_and_reports_what_it_did_not_reach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past `service_max_plan_scans` the answer is partial and says so.

    The least recently active sessions go unread, since the listing is ordered by activity.
    """
    monkeypatch.setattr(settings, "service_max_plan_scans", 1)
    inbox = _Inbox(monkeypatch)
    for session_id in ("sess-old", "sess-new"):
        inbox.add_session(session_id, owner="alice", profile=None, plan=["a step"])

    body = inbox.get()

    assert [row["session_id"] for row in body["plans"]] == ["sess-new"]
    assert (body["gated"], body["unread"]) == (2, 1)
    assert inbox.reads == ["sess-new"], "the bound must cost reads, not merely hide rows"


@pytest.mark.usefixtures("gated")
def test_an_unreadable_plan_is_counted_unread_rather_than_reported_as_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable plan (`None`) is counted unread, not reported as nothing waiting.

    Otherwise an unreachable checkpointer would report every blocked session as clear.
    """
    inbox = _Inbox(monkeypatch)
    inbox.add_session("sess-unreadable", owner="alice", profile=None, plan=None)

    body = inbox.get()

    assert body["plans"] == []
    assert (body["gated"], body["unread"]) == (1, 1)


def test_without_a_durable_registry_the_inbox_is_empty_and_says_which_emptiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under `session_store="memory"` there is no registry to enumerate, as with `GET /sessions`.

    `gated == 0` is the honest report: nothing can be listed here, and a surface that renders it as
    an empty queue is making a claim the deployment cannot back.
    """

    async def _unreached(session_id: str, **_kwargs: Any) -> list[str] | None:
        raise AssertionError(f"no registry, so no session should be read: {session_id}")

    monkeypatch.setattr(plan_routes, "session_plan", _unreached)
    app = create_app(owner_store=None, connector_factory=_no_connectors)
    app.dependency_overrides[require_principal] = lambda: _ALICE
    with TestClient(app) as client:
        body = client.get("/plans/pending").json()

    # `truncated` is the fourth reading of an empty `plans` (see `PendingPlansOut`): there is no
    # listing to walk here at all, so the walk did not stop short of one.
    assert body == {
        "plans": [],
        "considered": 0,
        "gated": 0,
        "unread": 0,
        "truncated": False,
    }


@pytest.mark.usefixtures("gated")
def test_a_blocked_plan_below_the_listings_page_boundary_is_still_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocked plan below the listing's page boundary is still found.

    The inbox walks the whole listing, not its newest page: a conversation waiting on a plan gets no
    new turns, so its `updated_at` never moves and it would never rise above the boundary. Driven
    against the real `SessionOwnerStore`, since only it has a page boundary.
    """
    from langchain_core.messages import HumanMessage

    from chemclaw.agent.session_store import PostgresHistoryProvider, SessionOwnerStore
    from tests.pg import migrated_db_or_skip

    asyncio.run(migrated_db_or_skip())
    owners = SessionOwnerStore()
    sessions = [f"sess-inbox-page-{index}" for index in range(5)]

    async def _seed() -> None:
        for session_id in sessions:
            await owners.record(session_id, "alice", None)
            await owners.set_title_if_absent(session_id, f"conversation {session_id}")
            await PostgresHistoryProvider().save_messages(
                session_id, [HumanMessage(content="a turn")]
            )

    asyncio.run(_seed())
    # The oldest conversation is the blocked one, which is the shape that actually occurs: a plan
    # nobody answered is a conversation that has taken no turn since.
    blocked = sessions[0]
    monkeypatch.setattr(settings, "service_max_listed_sessions", 2)

    reads: list[str] = []

    async def _todos(session_id: str, **_kwargs: Any) -> list[dict[str, Any]]:
        reads.append(session_id)
        if session_id != blocked:
            return []
        return [{"content": "screen the hazards", "status": "pending", "tools": []}]

    monkeypatch.setattr(plan_routes, "session_plan", _todos)
    app = create_app(owner_store=owners, connector_factory=_no_connectors)
    app.state.plan_approvals = InMemoryPlanApprovalStore()
    app.dependency_overrides[require_principal] = lambda: _ALICE
    body = TestClient(app).get("/plans/pending").json()

    assert [row["session_id"] for row in body["plans"]] == [blocked], (
        f"the blocked conversation sits below the page boundary and was never looked at: {body}"
    )
    assert body["considered"] == 5, (
        f"`considered` reports {body['considered']}, which is a page rather than the caller's "
        "sessions"
    )
    assert body["unread"] == 0, "everything gated was read, so the queue is genuinely complete"


def test_the_listing_walk_is_bounded_when_nothing_the_caller_owns_is_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The listing walk is bounded when nothing the caller owns is gated.

    `gated` alone cannot bind the budget when nothing is gated, so the walk must also stop on its
    own bound rather than page through the caller's whole history on every request. The gate is
    turned off explicitly here. Driven against the real `SessionOwnerStore` with a small page and
    budget; the subclass only counts statements.
    """
    from langchain_core.messages import HumanMessage

    from chemclaw.agent.session_store import PostgresHistoryProvider, SessionOwnerStore
    from tests.pg import migrated_db_or_skip

    asyncio.run(migrated_db_or_skip())

    class _CountingOwners(SessionOwnerStore):
        """The real registry, with one counter around the keyset query the walk repeats."""

        def __init__(self) -> None:
            """Bind the real store and start the page count at zero."""
            super().__init__()
            self.pages = 0

        async def page_for_owner(
            self, owner: str | None, *, after: str | None = None
        ) -> list[tuple[str, Any, Any, str | None, str | None]]:
            """Count this page, then answer it with the store's own SQL."""
            self.pages += 1
            return await super().page_for_owner(owner, after=after)

    owners = _CountingOwners()
    sessions = [f"sess-inbox-walk-{index}" for index in range(20)]

    async def _seed() -> None:
        for session_id in sessions:
            await owners.record(session_id, "alice", None)
            await owners.set_title_if_absent(session_id, f"conversation {session_id}")
            await PostgresHistoryProvider().save_messages(
                session_id, [HumanMessage(content="a turn")]
            )

    asyncio.run(_seed())
    monkeypatch.setattr(settings, "service_max_listed_sessions", 2)
    monkeypatch.setattr(settings, "service_max_plan_scans", 3)
    # The posture this case is about, stated rather than inherited — see the docstring.
    monkeypatch.setattr(settings, "harness_enabled", False)

    async def _unreached(session_id: str, **_kwargs: Any) -> list[str] | None:
        raise AssertionError(f"no session is gated here, so {session_id} must not be read")

    monkeypatch.setattr(plan_routes, "session_plan", _unreached)
    app = create_app(owner_store=owners, connector_factory=_no_connectors)
    app.state.plan_approvals = InMemoryPlanApprovalStore()
    app.dependency_overrides[require_principal] = lambda: _ALICE

    body = TestClient(app).get("/plans/pending").json()

    assert owners.pages <= settings.service_max_plan_scans, (
        f"the inbox issued {owners.pages} keyset statements over {len(sessions)} sessions with "
        "nothing gated; the walk is bounded by a budget that cannot bind in this posture"
    )
    assert body["plans"] == []
    assert body["truncated"] is True, (
        "the walk stopped early and the response does not say so — an inbox that silently returns "
        "a partial answer is the confident emptiness this route's counts exist to prevent"
    )
