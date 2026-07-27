"""The human decision surface for the durable approval hold (gap RCH-3).

Before these routes existed, `InteractionApprovalWorkflow` (D-032) could be *started* but never
answered: the seam functions were not agent tools, no HTTP route reached them, and the chat UI
rendered the request as an inert trace line. A hold nobody can find or answer can only expire —
silently discarding the knowledge it was holding.

These tests drive the real FastAPI routes with the Temporal seam faked, so the surface, its
owner-scoping, and its 404 behavior are proven without a Temporal server (unavailable offline).
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from agents.interaction_tools import PendingApproval
from service.app import create_app


class _FakeAgent:
    """Minimal agent stand-in — the approval routes never touch it."""

    mcp_tools: list[Any] = []

    def create_session(self, *, session_id: str) -> Any:
        from agent_framework import AgentSession

        return AgentSession(session_id=session_id)


@pytest.fixture
def seam(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake the Temporal-backed approval seam; record what the routes asked it to do."""
    state: dict[str, Any] = {
        "owner": "",
        "status": "pending",
        "decisions": [],
        "pending": [PendingApproval(approval_id="approval-1", question="Save?", requested_by="")],
    }

    async def fake_owner(approval_id: str) -> str:
        if approval_id == "missing":
            raise ValueError("no approval hold with id 'missing'")
        return str(state["owner"])

    async def fake_status(approval_id: str) -> str:
        return str(state["status"])

    async def fake_decide(approval_id: str, approved: bool) -> None:
        state["decisions"].append((approval_id, approved))

    async def fake_list(owner: str | None = None) -> list[PendingApproval]:
        holds: list[PendingApproval] = state["pending"]
        return [h for h in holds if owner is None or h.requested_by in ("", owner)]

    monkeypatch.setattr("service.app.approval_owner", fake_owner)
    monkeypatch.setattr("service.app.approval_status", fake_status)
    monkeypatch.setattr("service.app.decide_approval", fake_decide)
    monkeypatch.setattr("service.app.list_pending_approvals", fake_list)
    return state


@pytest.fixture
def client() -> TestClient:
    """The real app with a fake agent factory."""
    return TestClient(create_app(agent_factory=lambda _profile: _FakeAgent()))


def test_a_pending_hold_can_be_listed(client: TestClient, seam: dict[str, Any]) -> None:
    """The review queue exists at all — a hold's id is no longer lost with the turn that made it."""
    response = client.get("/approvals")
    assert response.status_code == 200
    assert [item["approval_id"] for item in response.json()] == ["approval-1"]


def test_status_is_readable(client: TestClient, seam: dict[str, Any]) -> None:
    """A surface can poll one hold's state to render the right control."""
    seam["status"] = "approved"
    response = client.get("/approvals/approval-1")
    assert response.status_code == 200
    assert response.json() == {"approval_id": "approval-1", "status": "approved"}


def test_yes_reaches_the_workflow(client: TestClient, seam: dict[str, Any]) -> None:
    """The button click is delivered as the `decide` signal — the whole point of the gap."""
    response = client.post("/approvals/approval-1/decision", json={"approved": True})
    assert response.status_code == 204
    assert seam["decisions"] == [("approval-1", True)]


def test_no_is_delivered_too(client: TestClient, seam: dict[str, Any]) -> None:
    """A rejection must reach the hold, so it ends deliberately instead of expiring."""
    client.post("/approvals/approval-1/decision", json={"approved": False})
    assert seam["decisions"] == [("approval-1", False)]


def test_unknown_hold_is_404(client: TestClient, seam: dict[str, Any]) -> None:
    """An unknown id is a clean 404, not a 500 from the Temporal client."""
    assert client.get("/approvals/missing").status_code == 404
    assert client.post("/approvals/missing/decision", json={"approved": True}).status_code == 404


def test_another_users_hold_is_indistinguishable_from_absent(
    client: TestClient, seam: dict[str, Any]
) -> None:
    """A hold authorizes a knowledge write, so only its owner may answer it.

    The wrong owner gets the same 404 as a non-existent hold — no existence leak, mirroring how
    `_resolve_session` scopes sessions.
    """
    seam["owner"] = "somebody-else"
    assert client.get("/approvals/approval-1").status_code == 404
    response = client.post("/approvals/approval-1/decision", json={"approved": True})
    assert response.status_code == 404
    assert seam["decisions"] == []  # and the signal was never sent


def test_listing_is_scoped_to_the_caller(client: TestClient, seam: dict[str, Any]) -> None:
    """The queue shows the caller's holds, not everyone's."""
    seam["pending"] = [
        PendingApproval(approval_id="mine", question="q", requested_by=""),
        PendingApproval(approval_id="theirs", question="q", requested_by="somebody-else"),
    ]
    ids = [item["approval_id"] for item in client.get("/approvals").json()]
    assert ids == ["mine"]


def test_decision_is_not_an_agent_tool() -> None:
    """The agent must never be able to approve its own candidate (D-005: human signs off).

    This is the load-bearing constraint of the whole fix: exposing `decide_approval` as a tool
    would have been the shortest path to "the hold can be answered" and would have collapsed the
    GxP line the PR-gate exists to draw.
    """
    from agents.chemclaw_agent import _capability_tools

    names = {getattr(tool, "__name__", "") for tool in _capability_tools()}
    assert "decide_approval" not in names
    assert "start_approval" not in names
