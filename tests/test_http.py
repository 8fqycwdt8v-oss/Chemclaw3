"""The shared HTTP primitive, existing to stop a second copy appearing.

`is_loopback_url` answers both the front door's bind rule and the connector manifest's credential
rule.
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
    """An unparseable address falls on the side that demands a credential.

    "Cannot tell" must mean "not loopback". Malformed IPv6 raises rather than returning None, so the
    implementation catches `ValueError`.
    """
    assert not is_loopback_url(url)
