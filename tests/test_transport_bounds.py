"""Every process that serves HTTP bounds a connection before a route can refuse it.

`D-2026-08-01-a-cheap-request-is-still-a-request` established three transport bounds — a
concurrency ceiling, an idle keep-alive timeout and a header-size ceiling — and
`deploy/entrypoint.sh` passes all three as uvicorn flags. `tests/test_deploy_chart.py::
test_the_front_door_is_launched_with_transport_bounds` pins them, **to that shell case**.

**Measured on 2026-09-11: three of the four processes that serve HTTP passed none of them.**
`api/mcp_face.py`, `connectors/server_entry.py` — which is *every* `connector-*` pod — and
`core/worker_http.py` call uvicorn themselves and ran at its defaults: unlimited concurrency, a
5 s keep-alive and a 16 KiB header ceiling. `deploy/README.md` gave the reason and the reason was
false: it said the settings are ones "the app can impose on itself" cannot, which is true of an
ASGI application and false of `uvicorn.run()` and `uvicorn.Config()`, both of which take all three
as keyword arguments at all three call sites.

**This guard is a partition, not a list, which is the only shape that catches the fifth process.**
The defect was never that someone chose to leave a server unbounded — it was that a server was
added and the question was not asked. So the assertion is over every `uvicorn.run`/`uvicorn.Config`
call site the tree contains: each either applies `transport_bounds` or is named below with a reason.
A sixth launcher added next year fails this the day it lands.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from chemclaw.core.asgi import transport_bounds
from chemclaw.core.config import settings

_SRC = Path(__file__).resolve().parents[1] / "src" / "chemclaw"

#: Launchers that deliberately do not bound, with the reason each is not a deployment surface.
#:
#: Both halves of a register, on the rule `D-2026-09-11` took from wave 15: an entry is checked for
#: staleness (the file still launches uvicorn) *and* for being spent (it is actually unbounded).
#: A row that outlives its launcher, or that sits on a server somebody has since bounded, is an
#: exemption nobody is using and goes.
_NOT_A_DEPLOYMENT_SURFACE = {
    "chemclaw/cli/mock_llm.py": (
        "the credential-free mock gateway for the live lane and the storm; it binds loopback, "
        "serves no chemist, and a bound here would shape the very load the storm exists to apply"
    ),
    "chemclaw/cli/connectors_dev.py": (
        "`make connectors`, one dev process on loopback carrying every enabled bundle at once — "
        "the concurrency ceiling is a per-pod number and this is deliberately not a pod"
    ),
}


def _launch_sites() -> dict[str, list[ast.Call]]:
    """Every `uvicorn.run(...)` / `uvicorn.Config(...)` call in `src/`, by module path.

    Read off the AST rather than by grep so that a call spread over several lines — which all of
    them are — is one site rather than none.
    """
    found: dict[str, list[ast.Call]] = {}
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"run", "Config"}:
                continue
            base = node.func.value
            if isinstance(base, ast.Name) and base.id == "uvicorn":
                found.setdefault(str(path.relative_to(_SRC.parent)), []).append(node)
    return found


def _applies_bounds(call: ast.Call) -> bool:
    """Whether this call spreads `transport_bounds(...)` into its keywords.

    A `**` keyword has `arg is None`. Matching the *call* rather than the resulting keys is the
    point: the keys are what `transport_bounds` decides, and a site that spelled them out by hand
    would be a second copy of the decision, which is the thing this module exists to prevent.
    """
    return any(
        kw.arg is None
        and isinstance(kw.value, ast.Call)
        and isinstance(kw.value.func, ast.Name)
        and kw.value.func.id == "transport_bounds"
        for kw in call.keywords
    )


def test_the_scan_finds_every_launcher() -> None:
    """Guard the guard: an empty walk would make every assertion below vacuously true."""
    sites = _launch_sites()
    assert len(sites) >= 5, (
        f"found only {len(sites)} uvicorn launch site(s) in src/; this tree has at least five and "
        "the AST walk has stopped matching them"
    )


@pytest.mark.parametrize("module", sorted(_launch_sites()))
def test_every_http_surface_is_bounded_or_argued(module: str) -> None:
    """A process serving HTTP applies the three bounds, or says here why it is not a surface."""
    if module in _NOT_A_DEPLOYMENT_SURFACE:
        pytest.skip(f"declared not a deployment surface: {_NOT_A_DEPLOYMENT_SURFACE[module]}")
    unbounded = [c.lineno for c in _launch_sites()[module] if not _applies_bounds(c)]
    assert not unbounded, (
        f"{module} launches uvicorn at line(s) {unbounded} without `**transport_bounds(...)`. "
        "Unbounded means uvicorn's defaults: no concurrency ceiling, a 5s keep-alive and a 16 KiB "
        "header limit — the three D-2026-08-01 refused for the front door. Apply "
        "`chemclaw.core.asgi.transport_bounds()`, or add this module to "
        "`_NOT_A_DEPLOYMENT_SURFACE` with the reason it serves nobody."
    )


def test_no_exemption_outlives_its_launcher() -> None:
    """A row naming a module that no longer launches uvicorn is a permission nobody can spend."""
    stale = sorted(set(_NOT_A_DEPLOYMENT_SURFACE) - set(_launch_sites()))
    assert not stale, f"exemption(s) naming a module that launches no server: {stale}; delete them"


def test_no_exemption_sits_on_a_server_that_is_already_bounded() -> None:
    """The other half: an exemption is spent only while its launcher is actually unbounded.

    Without this the register is write-only. Somebody bounds one of these two — which would be a
    good change — and the row stays, silently exempting whatever that file launches next.
    """
    sites = _launch_sites()
    unspent = sorted(
        module
        for module in _NOT_A_DEPLOYMENT_SURFACE
        if module in sites and all(_applies_bounds(call) for call in sites[module])
    )
    assert not unspent, (
        f"these are exempted but already apply the bounds: {unspent}. The exemption is not doing "
        "anything — delete the row so the partition means what it says."
    )


def test_the_probe_surface_keeps_its_keep_alive_and_header_bounds() -> None:
    """`concurrency=False` narrows one bound and must not quietly drop the other two.

    `core/worker_http.py` answers the two kubelet probes, and a liveness probe *refused* because a
    concurrency limit is full restarts a pod that is merely busy — so that one bound is withheld
    there on purpose. The other two are not about capacity: an idle socket and an oversized header
    are hazards whatever the surface serves, and an argument for dropping the first reads exactly
    like an argument for dropping all three.
    """
    narrowed = transport_bounds(concurrency=False)
    assert "limit_concurrency" not in narrowed
    assert narrowed == {
        "timeout_keep_alive": settings.service_keepalive_seconds,
        "h11_max_incomplete_event_size": settings.service_max_header_bytes,
    }
    full = transport_bounds()
    assert full["limit_concurrency"] == settings.service_max_connections
    assert narrowed.items() <= full.items(), "narrowing changed a bound instead of removing one"
