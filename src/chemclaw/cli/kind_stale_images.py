#!/usr/bin/env python3
"""Which Deployments run pods on an image the node no longer holds under that pod's tag.

Rebuilds reuse the `chemclaw/<x>:kind` tag, so compare by digest: each container's `imageID` against
what `crictl inspecti` lists for its tag.

    kubectl get pods -o json | python3 src/chemclaw/cli/kind_stale_images.py --node <node>

Prints one Deployment per line. Run as a file on the host's `python3`, so stdlib only and
3.9-readable.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any

_PREFIX = "chemclaw/"


def digest(reference: str) -> str:
    """The `sha256:<hex>` part of an image reference, `imageID` or image id ("" when there is none).

    Handles bare digests, `<repo>@sha256:…` and runtime prefixes such as `docker-pullable://`: the
    digest after the last `@` is the comparable part.
    """
    tail = reference.rsplit("@", 1)[-1]
    return tail if tail.startswith("sha256:") else ""


def node_digests(inspecti: dict[str, Any] | None) -> set[str]:
    """Every digest the node's current image for a tag answers to (`crictl inspecti -o json`)."""
    status = (inspecti or {}).get("status") or {}
    refs = [*(status.get("repoDigests") or []), status.get("id") or ""]
    return {d for d in map(digest, refs) if d}


def chemclaw_images(pods: dict[str, Any]) -> list[str]:
    """The distinct `chemclaw/*` images the pods' containers name, sorted."""
    return sorted(
        {
            c["image"]
            for p in pods.get("items", [])
            for c in p["spec"]["containers"]
            if c["image"].startswith(_PREFIX)
        }
    )


def _deployment(pod: dict[str, Any]) -> str | None:
    """The Deployment owning a pod, through its ReplicaSet's `<deployment>-<hash>` name."""
    for owner in pod["metadata"].get("ownerReferences") or []:
        name = owner.get("name", "")
        if owner.get("kind") == "ReplicaSet" and "-" in name:
            return str(name.rsplit("-", 1)[0])
    return None


def stale_deployments(pods: dict[str, Any], current: dict[str, set[str]]) -> list[str]:
    """The Deployments with a container whose `imageID` is not among its tag's current digests.

    Containers match statuses by name, since list order is not promised. A container with no status
    or `imageID` yet, or a tag the node could not report, is skipped rather than counted stale.
    """
    stale: set[str] = set()
    for pod in pods.get("items", []):
        deployment = _deployment(pod)
        if deployment is None:
            continue
        statuses = {
            s.get("name"): s for s in (pod.get("status") or {}).get("containerStatuses") or []
        }
        for container in pod["spec"]["containers"]:
            known = current.get(container["image"])
            running = digest((statuses.get(container["name"]) or {}).get("imageID") or "")
            if known and running and running not in known:
                stale.add(deployment)
    return sorted(stale)


def _inspect(node: str, image: str) -> dict[str, Any] | None:
    """`crictl inspecti` of an image on the kind node, or None when the node cannot say."""
    result = subprocess.run(
        ["docker", "exec", node, "crictl", "inspecti", "-o", "json", f"docker.io/{image}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        parsed: dict[str, Any] = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return parsed


def main() -> int:
    """Read `kubectl get pods -o json` on stdin, print the stale Deployments one per line."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--node", required=True, help="the kind node container to ask")
    args = parser.parse_args()
    pods = json.load(sys.stdin)
    current = {image: node_digests(_inspect(args.node, image)) for image in chemclaw_images(pods)}
    for deployment in stale_deployments(pods, current):
        print(deployment)
    return 0


if __name__ == "__main__":
    sys.exit(main())
