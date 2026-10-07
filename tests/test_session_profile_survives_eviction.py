"""A session's profile survives eviction, so it cannot silently regain what it gave up.

A profile only attenuates, so rehydrating on the default profile would switch the control off.
The live cache is an LRU with no TTL, so eviction happens mid-conversation on a busy pod; these
tests drive the real front door with a one-entry cache.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from chemclaw.api.app import create_app
from chemclaw.core.config import settings
from tests.test_service import _FakeOwnerStore, _no_connectors


def _client_with_one_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, Any, _FakeOwnerStore]:
    """The front door with a single live-session slot.

    One slot is the smallest faithful model of a full cache: the next session created evicts the
    previous one, which is exactly what `service_max_live_sessions` does on a busy pod.
    """
    monkeypatch.setattr(settings, "service_max_live_sessions", 1)
    owners = _FakeOwnerStore()
    app = create_app(
        owner_store=owners,
        connector_factory=_no_connectors,
    )
    return TestClient(app), app, owners


def _live_profile(app: Any, session_id: str) -> Any:
    """The profile the front door would run this session's next turn under.

    Read off the live entry, since `live.profile` is what the turn route passes to the graph and
    connector factories.
    """
    entry = app.state.live_sessions.get(session_id)
    assert entry is not None, "the session did not rehydrate at all"
    return entry.profile


def test_the_profile_is_recorded_beside_the_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """The durable row carries it, or there is nothing to rehydrate from."""
    client, _app, owners = _client_with_one_slot(monkeypatch)
    with client:
        session_id = client.post("/sessions", json={"profile": "property-lookup"}).json()[
            "session_id"
        ]
    assert owners.profiles[session_id] == "property-lookup"


def test_an_evicted_narrowed_session_comes_back_narrowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect itself: eviction must not hand back the tools the profile removed.

    `property-lookup` cuts the surface to four tools, drops every connector but `calc`, and removes
    the ability to start a durable job. Coming back on the default profile restores all of it.
    """
    client, app, _owners = _client_with_one_slot(monkeypatch)
    with client:
        narrowed = client.post("/sessions", json={"profile": "property-lookup"}).json()[
            "session_id"
        ]
        # One slot, so creating the second session evicts the first — no restart involved.
        client.post("/sessions")
        # Touching the evicted session rehydrates it.
        client.get(f"/sessions/{narrowed}/plan")
        restored = _live_profile(app, narrowed)

    assert restored == "property-lookup", (
        f"an evicted session rehydrated on profile {restored!r}, not 'property-lookup' — it "
        "silently regained every tool its profile had removed"
    )


def test_a_session_with_no_profile_still_rehydrates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A session with no profile still rehydrates on the default; `None` must round-trip as `None`.
    """
    client, app, _owners = _client_with_one_slot(monkeypatch)
    with client:
        plain = client.post("/sessions").json()["session_id"]
        client.post("/sessions")  # evicts it
        response = client.get(f"/sessions/{plain}/plan")
        assert response.status_code != 404, "an ordinary session stopped rehydrating"
        assert _live_profile(app, plain) is None
