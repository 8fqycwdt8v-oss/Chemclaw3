"""The shared HTTP primitive, existing to stop a second copy appearing.

`is_loopback_url` is the one answer the front door's bind rule and the connector manifest's
credential rule both ask for.

**`error_detail` was the other one, and it is gone.** It bounded an upstream error body (SEC-6) for
"several modules (the Nextflow launcher, the Entra token/OBO exchanges)" — its module docstring's
own words, in the present tense, about three call sites that had all been deleted. Nothing in
`src/` called it; only the two tests that used to stand here did, which is the shape
`D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution` names. The bound was never
wrong — it simply had nothing to bound, and a body cap is worth exactly as much as the caller that
applies it, so it comes back with one or not at all.
"""

import pytest

from chemclaw.core.http import is_loopback_url


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
