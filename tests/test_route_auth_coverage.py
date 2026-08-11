"""Prove the front door's one authorization gate is on every route, not merely convention (H1).

Before `chemclaw.api.deps.CurrentUser` existed, all twenty authenticated routes wrote
`principal: Principal = Depends(require_principal)` verbatim — a convention a 24th route (or a
21st) could silently forget. Forgetting it skips both authentication *and* the per-principal rate
budget in one stroke, because `_within_budget` lives inside `require_principal`
(`chemclaw.api.auth`).

This test makes that convention an assertion: it walks the built app's `APIRoute`s, resolves each
one's dependency tree, and requires `require_principal` to be present somewhere in it — unless the
route's `(path, method)` is in `_PROBE_ALLOWLIST`. The allowlist is what the health/metrics probes'
own docstrings already claim in prose; here it is the enforced declaration of what is
*intentionally* open, and adding a route to it is a change a reviewer will see in a diff.

**Why the dependency tree, not source text.** Grepping `app.py` for `Depends(require_principal)`
would pass a route that spells the same dependency a different way (a wrapped/renamed callable)
and would not survive `app.py` splitting into per-domain `APIRouter`s (R3.1) the way this does — a
route registered on a sub-router still resolves to the same `require_principal` object once it is
included into the app, so this test needs no changes when routes move modules. It would also be
fooled by a parameter that is merely *named* `principal` without depending on `require_principal`
at all; walking `route.dependant` cannot be, because it resolves the actual callable FastAPI will
invoke, not a name.

**Why not middleware.** `tests/test_request_limits.py` (`test_the_probes_are_never_limited`) already
establishes that `/healthz`, `/readyz` and `/metrics` must stay reachable with no dependency at all,
and that the rate limit inside `require_principal` must not become an app-level gate that would also
throttle them. This test enforces the same shape from the other side: everything *else* must go
through that one dependency, without turning it into middleware that would catch the probes too.
"""

from collections.abc import Iterable

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Route

from chemclaw.api.app import create_app
from chemclaw.api.auth import require_principal

# The declaration of what is intentionally reachable with no authenticated principal at all.
# `(path, method)` rather than path alone, so a future route that reuses a path for a new method
# (unlikely here, but cheap to be exact about) is not accidentally waved through.
#
# - GET /healthz: liveness — a kubelet cannot present a bearer token.
# - GET /readyz: readiness — same reason, and it must answer before an agent/tenant exists.
# - GET /metrics: a Prometheus scrape happens before and independently of user identity; the
#   exposition itself carries no session, user or turn content (D-152's label allowlist).
_PROBE_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        ("/healthz", "GET"),
        ("/readyz", "GET"),
        ("/metrics", "GET"),
    }
)


# The other surface: routes FastAPI serves that are *not* `APIRoute`s and therefore carry no
# `dependant` for `require_principal` to sit in. They cannot be gated, so the only safe statement
# about them is that there is exactly one and we know what it is.
#
# This declaration exists because excluding them silently is how `/openapi.json` stayed
# unauthenticated: `openapi_url` defaults to a plain `Route`, the sweep below skipped it for the
# perfectly true reason that it has no dependency tree, and the full route/parameter/model surface
# was readable by anyone who could reach the pod. `create_app` now passes `openapi_url=None`; this
# is what stops it — or any other ungatable route — from coming back unnoticed.
_UNGATABLE_SURFACE: frozenset[tuple[str, str]] = frozenset({("Mount", "")})


def _api_routes(app: FastAPI) -> Iterable[APIRoute]:
    """Every `APIRoute` the app declares — skips anything with no dependency tree to inspect.

    Non-`APIRoute` entries (`Route`, `Mount`) carry no `dependant` FastAPI could gate, so this
    filter excludes them for free rather than by name. What they are is pinned separately by
    `test_the_ungatable_surface_is_exactly_the_static_mount`; excluding them here without pinning
    them there is precisely the gap that left `/openapi.json` open.
    """
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

    Recursive, not a single-level scan: a dependency of a dependency (or, after R3.1, a
    router-level dependency threaded onto a sub-router) must count too. Identity (`is`), not name
    or signature — the whole point is to resolve the callable FastAPI will actually invoke, which
    a parameter merely *named* `principal` does not change and a differently-named wrapper around
    the same function would still satisfy.
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
    """The other half of the same guarantee: nothing *outside* the allowlist is actually open.

    Without this, `_PROBE_ALLOWLIST` could grow to cover a real gap and the test above would still
    pass — this pins the allowlist to exactly the routes that resolve with no `require_principal`
    in their tree, so widening it silently is itself a failure.
    """
    assert set(_unauthenticated_routes(_built_app())) == set(_PROBE_ALLOWLIST)


def test_the_ungatable_surface_is_exactly_the_static_mount() -> None:
    """Nothing but the static UI mount may be served outside the gateable route set.

    The sweep above can only speak for routes that *have* a dependency tree. This speaks for the
    rest: if a future change re-enables `openapi_url`, mounts a second sub-app, or adds a bare
    `Route`, that entry appears here and this fails with its name — rather than being skipped for
    the true-but-insufficient reason that it has nothing to gate.
    """
    assert _ungatable_surface(_built_app()) == set(_UNGATABLE_SURFACE)


def test_the_openapi_schema_is_not_served() -> None:
    """`/openapi.json` must 404: it is the concrete route this file failed to see (review finding).

    Asserted through the real ASGI stack rather than by reading `app.openapi_url`, because what
    matters is what a caller can fetch with no credential — the schema documents every route,
    parameter and model the service has.
    """
    with TestClient(_built_app()) as client:
        assert client.get("/openapi.json").status_code == 404


def test_mutation_proof_re_enabling_the_openapi_route_fails_the_surface_check() -> None:
    """Prove the surface check catches the exact regression it was written for.

    Re-registers the schema route the way FastAPI would if `openapi_url` were set again; the check
    must name it. Without this, the assertion above is a test that has never been seen to fail.
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
