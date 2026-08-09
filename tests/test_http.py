"""The shared HTTP primitives, each existing to stop a second copy appearing.

`error_detail` bounds an upstream body (SEC-6); `is_loopback_url` is the one answer the front
door's bind rule and the connector manifest's credential rule both ask for.
"""

import httpx
import pytest

from chemclaw.core.http import _ERROR_BODY_MAX_CHARS, error_detail, is_loopback_url


def _response(status: int, text: str) -> httpx.Response:
    """Build an httpx.Response with a text body for formatting."""
    return httpx.Response(status_code=status, text=text)


def test_error_detail_includes_status_and_body() -> None:
    """A short body is reported verbatim alongside the status code."""
    detail = error_detail(_response(500, "boom"))
    assert "500" in detail
    assert "boom" in detail


def test_error_detail_truncates_a_large_body() -> None:
    """A body longer than the cap is truncated with an ellipsis, never streamed whole."""
    detail = error_detail(_response(502, "x" * (_ERROR_BODY_MAX_CHARS + 100)))
    assert "…" in detail
    # The kept body is bounded to the cap (plus the status/reason prefix and ellipsis).
    assert len(detail) < _ERROR_BODY_MAX_CHARS + 100


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8811/mcp",
        "http://localhost:8811/mcp",
        "http://[::1]:8811/mcp",
        "https://localhost/healthz",
        # No port, no path, and a trailing slash — all still the same host.
        "http://127.0.0.1",
    ],
)
def test_a_loopback_address_is_recognised_however_it_is_spelt(url: str) -> None:
    """The three loopback spellings, with and without a port, scheme or path."""
    assert is_loopback_url(url)


@pytest.mark.parametrize(
    "url",
    [
        # An in-cluster Service: reachable from every pod in the namespace, so not loopback.
        "http://chemclaw-connector-molfp:8080/mcp",
        "https://model.vendor.example/mcp",
        "http://10.0.0.5:8080/mcp",
        # A host that merely *contains* a loopback name must not pass — this is the substring
        # mistake the frozenset membership test exists to avoid.
        "https://localhost.vendor.example/mcp",
        "https://127.0.0.1.vendor.example/mcp",
    ],
)
def test_a_networked_address_is_not_loopback(url: str) -> None:
    """Anything reachable from another machine, including near-misses on the host name."""
    assert not is_loopback_url(url)


@pytest.mark.parametrize("url", ["", "not a url", "/mcp", "http://[oops/mcp"])
def test_an_unparseable_address_falls_on_the_side_that_demands_a_credential(url: str) -> None:
    """Every caller asks this to decide whether a credential is required.

    So the answer for "I cannot tell" has to be "not loopback" — the side that demands one. The
    malformed-IPv6 case is the one that raises rather than returning None, which is why the
    implementation catches `ValueError` instead of trusting `urlsplit` to be total.
    """
    assert not is_loopback_url(url)
