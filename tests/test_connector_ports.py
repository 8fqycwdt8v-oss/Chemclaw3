"""Every port this repository's connectors claim is derived from the manifests, not transcribed.

The fleet in `Chemclaw3-mcp` reserves 8850–8899 for itself and documents that block as
"deliberately clear of Chemclaw3's own connectors at 8810–8815". That sentence was wrong the day
`bo` was added at **8816**, and nothing on either side of the seam could notice: a port lives in a
`connector.yaml` here, in a prose table there, and in a Markdown registry in a third repository.
Two bundles are addressed by the same number the first time somebody guesses, and the collision
surfaces when both pods are scheduled.

So this repository states its own block once, here, and derives everything else from the manifests
that actually ship:

* **8810–8819 is ours.** `make connectors` mounts every locally-served bundle under one uvicorn on
  `connectors_dev.DEV_PORT`, and each of those bundles also ships a standalone loopback address for
  running it alone. Both are addresses of servers *this* repository builds and runs, so both are
  inside the block.
* **A bundle whose server somebody else runs must sit outside it** (`chem` and `safety`, which moved
  to `Chemclaw3-mcp` wholesale). Its address belongs to the host's registry, and a bundle we do not
  run that squatted one of our numbers would make the two blocks overlap in exactly the way the
  fleet's sentence promises they do not.

Deliberately about the *shipped* manifests rather than about a list written here: a new bundle is
covered the day its `connector.yaml` lands, which is the property the transcribed range never had.
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
    """`chem` and `safety` are addressed in another repository's registry, not in this block.

    The other direction of the same disjointness: a bundle with no `server/` here is reached at an
    address the hosting fleet allocates, and if it also fell inside 8810–8819 then a future local
    bundle could be given the same number by a reader who trusted this block to be ours alone.
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
