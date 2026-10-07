"""Every port this repository's connectors claim is derived from the manifests, not transcribed.

* 8810–8819 is ours: `connectors_dev.DEV_PORT` and each locally served bundle's standalone
  loopback address sit inside it.
* A bundle whose server another repository runs (`chem`, `safety` in `Chemclaw3-mcp`) sits
  outside it, at the address the host's registry allocates.

Asserted over the shipped manifests, so a new bundle is covered the day its `connector.yaml`
lands.
"""

from pathlib import Path
from urllib.parse import urlparse

import yaml

from chemclaw.cli.connectors_dev import DEV_PORT

_BUNDLES = Path(__file__).resolve().parents[1] / "src" / "chemclaw" / "connectors"

# This repository's own block, and the only place it is written down. Ten numbers, chosen when the
# first bundle was addressed and never yet exhausted — six bundles ship, four of them served here.
_BLOCK = range(8810, 8820)


def _declared_ports() -> dict[str, int]:
    """The loopback port each shipped bundle's manifest declares, by bundle name.

    A bundle with no HTTP endpoint (a jobs-only bundle such as `results`) declares no address and
    is absent, which is why this returns a mapping rather than a list.
    """
    ports: dict[str, int] = {}
    for manifest_path in sorted(_BUNDLES.glob("*/connector.yaml")):
        endpoint = yaml.safe_load(manifest_path.read_text(encoding="utf-8")).get("endpoint") or {}
        url = endpoint.get("url")
        if endpoint.get("transport") == "http" and url:
            port = urlparse(url).port
            assert port is not None, f"{manifest_path}: endpoint url declares no port"
            ports[manifest_path.parent.name] = port
    return ports


def _served_here(bundle: str) -> bool:
    """Whether this repository builds the server behind `bundle` — i.e. the bundle has one."""
    return (_BUNDLES / bundle / "server" / "app.py").is_file()


def test_the_manifests_declare_a_port_at_all() -> None:
    """The parse is load-bearing for every assertion below, so its emptiness is a failure."""
    assert _declared_ports(), "no HTTP endpoint parsed out of any connector.yaml"


def test_every_locally_served_bundle_claims_a_port_in_this_repository_s_block() -> None:
    """A server we build listens on one of our numbers, or the two blocks are not disjoint."""
    outside = {
        name: port
        for name, port in _declared_ports().items()
        if _served_here(name) and port not in _BLOCK
    }
    assert not outside, (
        f"bundles served from this repository declaring a port outside "
        f"{_BLOCK.start}-{_BLOCK.stop - 1}: {outside}"
    )


def test_the_dev_composite_shares_the_block_it_fronts() -> None:
    """`make connectors` is one more address of ours, so it cannot live outside the reservation."""
    assert DEV_PORT in _BLOCK, (
        f"the dev runner listens on {DEV_PORT}, outside {_BLOCK.start}-{_BLOCK.stop - 1}"
    )


def test_no_two_bundles_claim_the_same_port() -> None:
    """Two manifests on one number is a collision that only appears when both pods schedule."""
    ports = _declared_ports()
    claimed = list(ports.values())
    duplicated = sorted(port for port in set(claimed) if claimed.count(port) > 1)
    assert not duplicated, f"more than one bundle declares {duplicated}: {ports}"


def test_a_bundle_hosted_elsewhere_does_not_squat_one_of_our_numbers() -> None:
    """A bundle hosted elsewhere does not squat one of our numbers.

    Otherwise a future local bundle could be given the same port.
    """
    inside = {
        name: port
        for name, port in _declared_ports().items()
        if not _served_here(name) and port in _BLOCK
    }
    assert not inside, (
        f"bundles whose server this repository does not build, declaring a port inside "
        f"{_BLOCK.start}-{_BLOCK.stop - 1}: {inside}"
    )
