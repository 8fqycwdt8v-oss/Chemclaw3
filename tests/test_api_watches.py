"""A chemist sees and stops their own standing queries over HTTP, and nobody else's.

`watch_for` is the only way a watch is saved; these routes are how a person finds and cancels one
without a turn. The claims: the listing is the caller's own, says whether digests run, and a stop
removes exactly the named watch of the caller — another person's id is a 404 that deletes nothing.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chemclaw.agent import subscriptions
from chemclaw.api.app import create_app
from chemclaw.api.auth import Principal, require_principal
from chemclaw.core.config import settings
from tests.pg import migrated_db_or_skip

_ALICE = Principal(oid="watch-route-alice", upn="alice@example.com", roles=frozenset())
_BOB = Principal(oid="watch-route-bob", upn="bob@example.com", roles=frozenset())


def _no_connectors(profile: str | None = None) -> list[object]:
    """No connector session: these routes reach Postgres only."""
    return []


def _app() -> FastAPI:
    """The front door with no connector or graph."""
    return create_app(connector_factory=_no_connectors, graph_factory=lambda *a, **k: None)


def _client(app: FastAPI, principal: Principal) -> TestClient:
    """A client whose every request arrives as `principal`."""
    app.dependency_overrides[require_principal] = lambda: principal
    return TestClient(app)


async def _clear() -> None:
    """Remove every watch the two test principals hold."""
    for principal in (_ALICE, _BOB):
        for watch in await subscriptions.for_owner(principal.oid):
            await subscriptions.remove(principal.oid, watch.query)


@pytest.mark.parametrize("enabled", [True, False])
async def test_the_listing_is_the_callers_own_and_says_whether_anything_evaluates_it(
    enabled: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    await migrated_db_or_skip()
    await _clear()
    monkeypatch.setattr(settings, "digest_enabled", enabled)
    try:
        await subscriptions.add(_ALICE.oid, "biaryl couplings", "playbook")
        await subscriptions.add(_BOB.oid, "Bob's secret campaign", None)
        body = _client(_app(), _ALICE).get("/watches").json()
        assert body["digests_enabled"] is enabled
        assert [(w["query"], w["note_type"]) for w in body["watches"]] == [
            ("biaryl couplings", "playbook")
        ]
    finally:
        await _clear()


async def test_a_stop_removes_the_callers_watch_and_never_anothers() -> None:
    await migrated_db_or_skip()
    await _clear()
    try:
        await subscriptions.add(_ALICE.oid, "suzuki", None)
        await subscriptions.add(_BOB.oid, "suzuki", None)
        (bobs,) = await subscriptions.for_owner(_BOB.oid)
        (alices,) = await subscriptions.for_owner(_ALICE.oid)
        app = _app()

        assert _client(app, _ALICE).delete(f"/watches/{bobs.id}").status_code == 404
        assert [w.id for w in await subscriptions.for_owner(_BOB.oid)] == [bobs.id]

        assert _client(app, _ALICE).delete(f"/watches/{alices.id}").status_code == 204
        assert await subscriptions.for_owner(_ALICE.oid) == []
        assert _client(app, _ALICE).delete(f"/watches/{alices.id}").status_code == 404
    finally:
        await _clear()
