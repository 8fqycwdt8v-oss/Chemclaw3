"""Prove the front door's one authorization gate is on every route, not merely convention.

Forgetting `require_principal` skips both authentication and the per-principal rate budget. The
test walks each `APIRoute`'s resolved dependency tree, so a renamed wrapper or a sub-router
still counts and a parameter merely named `principal` does not. `_PROBE_ALLOWLIST` declares what
is intentionally open; the gate is a dependency rather than middleware so probes stay
unthrottled.
"""

from collections.abc import Iterable

import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Route

from chemclaw.api.app import create_app
from chemclaw.api.auth import require_principal
from chemclaw.core.config import settings

# What is intentionally reachable with no authenticated principal, keyed by `(path, method)`.
#
# - GET /healthz: liveness — a kubelet cannot present a bearer token.
# - GET /readyz: readiness — same reason, and it must answer before an agent/tenant exists.
# - GET /metrics: a Prometheus scrape has no identity, and the exposition carries no user content.
_PROBE_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        ("/healthz", "GET"),
        ("/readyz", "GET"),
        ("/metrics", "GET"),
    }
)


# Served routes that are not `APIRoute`s and so cannot carry `require_principal`; the only safe
# statement is that there is exactly one and we know what it is. This stops a plain `Route`, such
# as FastAPI's default `/openapi.json`, from appearing unnoticed.
_UNGATABLE_SURFACE: frozenset[tuple[str, str]] = frozenset({("Mount", "")})


def _api_routes(app: FastAPI) -> Iterable[APIRoute]:
    """Every `APIRoute` the app declares; non-`APIRoute` entries are pinned separately."""
    for route in app.routes:
        if isinstance(route, APIRoute):
            yield route


def _ungatable_surface(app: FastAPI) -> set[tuple[str, str]]:
    """Every non-`APIRoute` entry as `(class name, path)` — the surface no dependency can gate."""
    return {
        (type(route).__name__, getattr(route, "path", ""))
        for route in app.routes
        if not isinstance(route, APIRoute)
    }


def _requires_principal(dependant: Dependant) -> bool:
    """Whether `require_principal` appears anywhere in `dependant`'s dependency tree.

    Recursive, so nested and router-level dependencies count; compared by identity, so only the
    callable FastAPI invokes matters.
    """
    for sub in dependant.dependencies:
        if sub.call is require_principal or _requires_principal(sub):
            return True
    return False


def _unauthenticated_routes(app: FastAPI) -> list[tuple[str, str]]:
    """Every `(path, method)` reachable through `app` with no `require_principal` in its tree."""
    return [
        (route.path, method)
        for route in _api_routes(app)
        for method in route.methods or set()
        if not _requires_principal(route.dependant)
    ]


def _enforced_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """The same app in the posture the chart ships: identity enforced.

    Built through `monkeypatch` rather than by mutating `settings` directly, so the enforced
    posture cannot leak into another test in the same process.
    """
    monkeypatch.setattr(settings, "entra_required", True)
    monkeypatch.setattr(settings, "entra_audience", "api://chemclaw")
    monkeypatch.setattr(settings, "entra_tenant_id", "tenant-for-the-surface-check")
    return create_app()


def _built_app() -> FastAPI:
    """The real app, built the same way the service builds it, minus any live dependencies.

    `agent_factory` is a stub because building the app is what is under test, not running a turn
    through it — no route in this test is ever called.
    """
    return create_app()


def test_every_route_outside_the_probe_allowlist_requires_a_principal() -> None:
    """The gate, enforced: every route outside `_PROBE_ALLOWLIST` depends on `require_principal`.

    Fails with the exact `(path, method)` pairs that are missing the gate, so a regression names
    its own offender instead of a generic "some route somewhere" failure.
    """
    missing = sorted(
        pair for pair in _unauthenticated_routes(_built_app()) if pair not in _PROBE_ALLOWLIST
    )
    assert not missing, (
        "route(s) reachable with no authenticated principal and not in the probe allowlist: "
        f"{missing}"
    )


def test_the_probe_allowlist_names_exactly_the_open_routes() -> None:
    """The allowlist names exactly the routes that resolve with no `require_principal`.

    Otherwise the allowlist could silently grow to cover a real gap.
    """
    assert set(_unauthenticated_routes(_built_app())) == set(_PROBE_ALLOWLIST)


def test_the_ungatable_surface_is_exactly_the_static_mount() -> None:
    """Nothing but the static UI mount may be served outside the gateable route set."""
    assert _ungatable_surface(_built_app()) == set(_UNGATABLE_SURFACE)


def test_an_enforced_app_has_no_ungatable_surface_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under `entra_required` the static mount is gone, so every served route has a gate.

    The bundled UI sends no `Authorization` header, so `create_app` mounts it only when identity is
    off.
    """
    assert _ungatable_surface(_enforced_app(monkeypatch)) == set()


def test_the_bundled_ui_is_reachable_in_dev_and_absent_under_enforcement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asserted over the wire, because what matters is what a caller can fetch.

    Both directions: a dev deployment keeps the affordance it has always had, and an enforced one
    serves 404 where a broken login-less chat page used to be.
    """
    with TestClient(_built_app()) as dev:
        assert dev.get("/").status_code == 200
        assert dev.get("/app.js").status_code == 200
    with TestClient(_enforced_app(monkeypatch)) as enforced:
        assert enforced.get("/").status_code == 404
        assert enforced.get("/app.js").status_code == 404


def test_the_openapi_schema_is_served_through_the_one_gate() -> None:
    """`/openapi.json` is an `APIRoute` behind `require_principal` — served, and inside the sweep.

    The UI's contract check fetches it. Both the document being fetchable and its route carrying
    the gate are asserted.
    """
    app = _built_app()
    assert ("/openapi.json", "GET") not in _unauthenticated_routes(app), (
        "the schema route must resolve through `require_principal` like every other API route"
    )
    with TestClient(app) as client:
        response = client.get("/openapi.json")
        assert response.status_code == 200
        assert "/sessions" in response.json()["paths"]


def test_the_openapi_schema_is_refused_without_a_token_under_enforcement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In the enforced posture, an anonymous request for the OpenAPI schema gets 401."""
    with TestClient(_enforced_app(monkeypatch)) as client:
        assert client.get("/openapi.json").status_code == 401


def test_mutation_proof_re_enabling_the_openapi_route_fails_the_surface_check() -> None:
    """Re-registering a plain schema route the way FastAPI would fails the surface check.

    The property is "no plain `Route` serves it", since only the `APIRoute` form can be gated.
    """
    app = _built_app()

    async def _schema(_request: object) -> None:  # pragma: no cover - never called
        raise AssertionError("not invoked")

    app.router.routes.append(Route("/openapi.json", endpoint=_schema, include_in_schema=False))
    surface = _ungatable_surface(app)
    assert ("Route", "/openapi.json") in surface
    assert surface != set(_UNGATABLE_SURFACE)


def _add_unguarded_route(app: FastAPI) -> None:
    """Register a route that forgets `require_principal` — for the mutation-proof, not production.

    Not one of the app's real handlers: a route manufactured purely so the sweep has a known
    omission to catch.
    """

    @app.get("/mutation-proof/unguarded")
    async def _unguarded() -> dict[str, str]:
        return {"status": "ok"}


def test_mutation_proof_an_unguarded_route_fails_the_sweep() -> None:
    """Prove the sweep actually catches an omission, by manufacturing one.

    A coverage test that only ever passes is not known to catch anything: this asserts the sweep
    would name a route that slipped through review with no dependency at all.
    """
    app = _built_app()
    _add_unguarded_route(app)
    missing = _unauthenticated_routes(app)
    assert ("/mutation-proof/unguarded", "GET") in missing


def test_mutation_proof_allowlisting_the_new_route_makes_it_pass() -> None:
    """The allowlist is a real escape hatch: naming a route there is what makes it pass.

    Confirms the two proofs are consistent with each other rather than accidents of the fixture:
    the same route that fails the sweep above passes once (and only once) it is declared open.
    """
    app = _built_app()
    _add_unguarded_route(app)
    allowlist = _PROBE_ALLOWLIST | {("/mutation-proof/unguarded", "GET")}
    missing = [pair for pair in _unauthenticated_routes(app) if pair not in allowlist]
    assert not missing


def test_mutation_proof_removing_the_gate_from_a_real_route_fails() -> None:
    """The other direction: a route that *used to* have the gate and loses it must fail too.

    Rebuilds `/profiles` with the dependency stripped to simulate an edit that deleted the parameter
    — the exact regression this file exists to catch on a real route, not a manufactured one.
    """
    app = _built_app()
    for route in list(app.router.routes):
        if isinstance(route, APIRoute) and route.path == "/profiles":
            app.router.routes.remove(route)

    @app.get("/profiles")
    async def _profiles_without_the_gate() -> list[str]:
        return []

    missing = [pair for pair in _unauthenticated_routes(app) if pair not in _PROBE_ALLOWLIST]
    assert ("/profiles", "GET") in missing
