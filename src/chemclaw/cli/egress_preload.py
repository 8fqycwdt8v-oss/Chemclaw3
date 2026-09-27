"""Print the egress posture `deploy/entrypoint.sh` arms the compiled guard with.

    python -m chemclaw.cli.egress_preload
    enabled 127.0.0.1,localhost,postgres.example.svc

One line: the posture word, a space, and the comma-joined allowlist. The entrypoint reads it, and on
`enabled` exports `CHEMCLAW_NETGUARD_PRELOAD_ALLOW` and `LD_PRELOAD` before `exec`ing the component.

**Why a separate process at all.** `LD_PRELOAD` is consumed by the dynamic loader before the
interpreter exists, so the allowlist has to be in the environment *before* the process that will be
guarded starts. Nothing in Python can arm it for itself after the fact. One interpreter start at
container boot buys the whole layer.

**Why this and not a list in the shell.** The allowlist is `netguard.derive_allowed(settings)` and
nothing else — the same function, on the same settings object, that the in-process guard arms with.
A hand-written copy in `entrypoint.sh` or in C would be a second declaration of the deployment's own
destinations, which is the defect this family keeps finding; it would also drift silently, because
the two layers would refuse different hosts and only one of them logs in a format anybody greps.

**A failure here must stop the container, not skip the layer.** This command writes nothing and
exits non-zero if the settings cannot be loaded — which is the same failure the component would hit
a moment later — and `entrypoint.sh` runs under `set -e`, so the pod crashloops with the reason
instead of starting unguarded. That direction is the whole point: a deployment that omits the
library or the variable runs with the compiled path open, which is the `MCP_EGRESS_GUARD=off` shape.
"""

from __future__ import annotations

import sys

from chemclaw.core.config import settings
from chemclaw.core.netguard import derive_allowed


def posture() -> str:
    """The one line the entrypoint reads: `enabled|disabled` and the derived allowlist.

    `disabled` when `CHEMCLAW_EGRESS_GUARD_ENABLED=false`, so the two layers have **one** knob
    rather than two — a deployment that takes the stated opt-out gets neither guard, and cannot end
    up with a compiled layer refusing what the Python layer was told to allow.
    """
    if not settings.egress_guard_enabled:
        return "disabled "
    return "enabled " + ",".join(sorted(derive_allowed(settings)))


def main() -> int:
    """Write the posture line. No arguments: there is one question and one answer."""
    sys.stdout.write(posture() + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
