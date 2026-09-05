"""`GET /plans/pending` — the cross-session inbox of plans nobody has decided yet.

The plan gate is answered per session, and finding the session was the half nothing served: the
decision card lives inside a turn, a reload recovers it only for a conversation somebody opens, and
every other plan surface is addressed by a session id the chemist no longer has. These tests pin
the three properties that make the route worth having rather than merely present:

- **The filter is "undecided", not "unapproved".** An approval is spent at the end of the turn it
  authorized (D-167), so the in-turn card's predicate would leave every finished plan-gated
  conversation sitting in the inbox for good.
- **A session that cannot be holding a decision is never read.** The prune is on the profile the
  ownership row already carries, and the assertion is on the *reads*, because the cost this route
  has to stay clear of is a checkpointer statement — `AsyncPostgresSaver` serializes them against
  every concurrent turn on the pod.
- **An empty list says which emptiness it is.** `plans == []` under `gated == 0` means the
  deployment has no plan gate; under `unread > 0` it means the answer is partial. The companion
  UI's own `ISSUES.md` records what the third, unlabelled kind cost: a 404 swallowed into `[]` and
  rendered as a confident "nothing is waiting on you".
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


class _Inbox:
    """The front door with the two stores this route reads, both in memory and both inspectable.

    A helper rather than a fixture because every test *arranges* those stores — which sessions
    exist, on which profile, with which plan and which decision — and reading that arrangement in
    the test body is what makes each assertion legible.

    The plan read is stubbed at `routes.plan.session_todos`, the seam `tests/test_runner.py` uses
    for the same purpose: what is under test here is which sessions get read and what the route
    concludes, not the checkpointer decode `tests/test_plan_state.py` already drives against a real
    saver.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Wire an app whose plan reads come from `self.todos` and are counted in `self.reads`."""
        self.owners = _FakeOwnerStore()
        self.approvals = InMemoryPlanApprovalStore()
        # `None` for a session whose plan is unreadable, matching `plan_state.session_todos` — the
        # distinction the route turns into `unread` rather than into "nothing waiting".
        self.todos: dict[str, list[str] | None] = {}
        self.reads: list[str] = []
        self.app = create_app(owner_store=self.owners, connector_factory=_no_connectors)
        self.app.state.plan_approvals = self.approvals
        self.app.dependency_overrides[require_principal] = lambda: _ALICE

        async def _todos(session_id: str, **_kwargs: Any) -> list[str] | None:
            self.reads.append(session_id)
            return self.todos.get(session_id)

        monkeypatch.setattr(plan_routes, "session_todos", _todos)
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
        asyncio.run(self.approvals.record(session_id, plan_identity(plan) or "", "alice", approved))
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
    """The row carries what a chemist navigates by: the session, its name, and the steps.

    The session id is the load-bearing field — it is the one thing a chemist who closed the tab
    cannot reconstruct, and every other plan route needs it as a path segment.
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
    assert row["plan_hash"] == plan_identity(plan), (
        "the row must name the plan the gate would ask about, not a second hashing of it"
    )
    assert (body["considered"], body["gated"], body["unread"]) == (2, 2, 0)


@pytest.mark.usefixtures("gated")
def test_a_decided_plan_is_not_waiting_on_anyone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Approved, spent, and rejected are all answers — only an unanswered plan is an inbox row.

    The spent case is the one that decides the whole design. `runner._pending_plan_approval` asks
    whether a *live* approval stands, which is right for a card inside a turn and would put every
    finished plan-gated conversation in this list permanently: an approval is consumed at the end
    of the turn it authorized, so "no live approval" is the resting state of completed work.
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

    Two claims, and the second is the one worth a test. `gated == 0` is what lets a surface say
    "this deployment does not gate plans" instead of "nothing is waiting on you". And the read
    count is the assertion the route's cost argument rests on: every checkpointer statement is
    serialized against every concurrent turn on the pod, so an inbox that scanned the whole listing
    to discover the gate is off would be a permanent tax on the default deployment.
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

    The prune is `gate_applies`, the same predicate that decides whether the card is shown at all —
    not `harness_enabled_for`, which only says whether a todo list exists. Getting that wrong would
    fill the inbox with plans the agent is already free to execute.
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
    """Past `service_max_plan_scans` the answer is partial, and it says so rather than truncating.

    A silently short inbox is worse than a visibly short one: it is the shape that tells a chemist
    nothing is waiting when something is. The sessions that go unread are the least recently
    active, because the listing is ordered by last activity.
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
    """`session_todos` returning `None` means "unknown", and the inbox must not round it to "none".

    That distinction is `agent/plan_state`'s whole reason for not returning one list, and it fails
    open here in exactly the way it fails open there: a checkpointer nobody can reach would
    otherwise report every blocked session as clear.
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

    monkeypatch.setattr(plan_routes, "session_todos", _unreached)
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
    """The inbox reads the whole listing, not its newest page.

    `service_max_listed_sessions` became a *page* when `X-Next-Cursor` was added to
    `GET /sessions`; this reader was not moved with it, so `considered` was a page count presented
    as a population and `unread` counted only what the *scan* budget skipped inside that page.

    The failure that makes it a defect rather than an inaccuracy: an unanswered plan means no new
    turns, so the blocking conversation's `updated_at` never moves and it never rises back above
    the page boundary. Measured before this test existed, with the page standing at 2 and five
    owned sessions: `{"plans": [], "considered": 2, "gated": 2, "unread": 0}` — "nothing is
    waiting on you", indefinitely, with the field whose whole job is to say the answer is partial
    reading 0.

    Driven against the real `SessionOwnerStore` because a fake registry has no page boundary to
    fall off; the whole finding is about the cap in `_OWNER_LIST`.
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

    async def _todos(session_id: str, **_kwargs: Any) -> list[str] | None:
        reads.append(session_id)
        return ["screen the hazards"] if session_id == blocked else []

    monkeypatch.setattr(plan_routes, "session_todos", _todos)
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
    """The paged walk must terminate on the *shipped* posture, where nothing is ever gated.

    `_owned_sessions` exited only on a short page or on `len(gated) > budget`, and `gated` counts
    plan-gated sessions — so with `harness_enabled` off (the code's own default, and the case
    `_plan_gated`'s docstring names as the one this route is "free" in) the budget can never bind
    and the loop pages through the caller's entire history on every request. Measured against the
    real `_owned_sessions` at 5,000 sessions and the shipped page of 100: **51** keyset statements
    where the route before paging issued exactly one, returning `plans: []` every time, repeatable
    by the caller at will.

    Driven against the real `SessionOwnerStore` for the reason the page-boundary test above gives —
    a fake registry has no page boundary to fall off — with the page and the budget shrunk so the
    walk is many pages long without seeding thousands of rows. The subclass only *counts*; every
    statement is the store's own.
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

    async def _unreached(session_id: str, **_kwargs: Any) -> list[str] | None:
        raise AssertionError(f"no session is gated here, so {session_id} must not be read")

    monkeypatch.setattr(plan_routes, "session_todos", _unreached)
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
