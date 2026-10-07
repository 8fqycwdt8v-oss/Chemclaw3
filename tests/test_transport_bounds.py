"""Every process that serves HTTP bounds a connection before a route can refuse it.

The bounds are a concurrency ceiling, an idle keep-alive timeout and a header-size ceiling, all
of which `uvicorn.run()`/`uvicorn.Config()` accept. The guard is a partition over every such call
site in the tree: each applies `transport_bounds` or is named below with a reason, so a new
launcher fails the day it lands.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from chemclaw.core.asgi import transport_bounds
from chemclaw.core.config import settings

_SRC = Path(__file__).resolve().parents[1] / "src" / "chemclaw"

#: Launchers that deliberately do not bound, each with the reason it is not a deployment surface.
#: Each entry is checked for staleness (the file still launches uvicorn) and for being spent (it is
#: actually unbounded).
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

    Matches the call rather than the keys, so a site spelling the keys out by hand does not count.
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
    """`concurrency=False` drops only the concurrency bound on the probe surface.

    A liveness probe refused by a full concurrency limit restarts a merely busy pod, so that bound
    is withheld; idle sockets and oversized headers are hazards on any surface.
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
