"""A session's profile survives eviction, so it cannot silently regain what it gave up (REV-14).

`_LiveSessions` stores `(session, owner, profile)` because the three "can never drift"; the durable
`session_owners` row stored only the owner. So a rehydrated session came back on the **default**
profile, and the code said so plainly, calling it a case that "degrades gracefully — the
conversation resumes with the full tool surface rather than a narrowed one".

That has the direction backwards. A profile is *attenuation only* — `chemclaw.agent.chemclaw_agent`
states
it twice: "it can only attenuate, never widen". `property-lookup` cuts the surface to four tools and
drops every connector but `calc`, specifically removing the ability to start a durable job. Coming
back with the full surface is not graceful degradation; it is the control being switched off.

And it did not need a restart. The live cache is an LRU with a capacity and no TTL, so on a busy pod
session 1001 evicts session 1 while both are in use. A chemist mid-conversation, having done
nothing, regains every tool their profile removed — and nothing anywhere says so.

These tests drive the real front door with a one-entry cache, because the eviction is the point: a
test that only restarted the app would exercise the case the old comment described and miss the one
that actually happens.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from chemclaw.api.app import create_app
from chemclaw.core.config import settings
from tests.test_service import _FakeAgent, _FakeOwnerStore, _no_connectors


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
        agent_factory=lambda _profile: _FakeAgent(),
        owner_store=owners,
        connector_factory=_no_connectors,
    )
    return TestClient(app), app, owners


def _live_profile(app: Any, session_id: str) -> Any:
    """The profile the front door would run this session's next turn under.

    Read off the live entry rather than off the agent factory, because agents are cached one per
    profile — the factory is called once per profile per process and says nothing about which
    profile a *session* is on. `live.profile` is what `POST /messages` passes to `agent_pool.lease`
    and to `connector_factory`, so it is the value that actually decides the surface.
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
    """The common case must not be broken by the fix: no profile means the default, as before.

    Worth pinning because the natural way to write this fix — storing `""` for "no profile" — would
    turn every ordinary session into a request for a profile named empty-string, which
    `get_profile` rejects. `None` has to survive the round trip as `None`.
    """
    client, app, _owners = _client_with_one_slot(monkeypatch)
    with client:
        plain = client.post("/sessions").json()["session_id"]
        client.post("/sessions")  # evicts it
        response = client.get(f"/sessions/{plain}/plan")
        assert response.status_code != 404, "an ordinary session stopped rehydrating"
        assert _live_profile(app, plain) is None
