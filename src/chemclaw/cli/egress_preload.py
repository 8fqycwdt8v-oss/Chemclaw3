"""Print the egress posture `deploy/entrypoint.sh` arms the compiled guard with.

    python -m chemclaw.cli.egress_preload
    enabled 127.0.0.1,localhost,postgres.example.svc

One line: the posture word and the comma-joined allowlist. On `enabled` the entrypoint exports
`CHEMCLAW_NETGUARD_PRELOAD_ALLOW` and `LD_PRELOAD` before `exec`ing the component.

A separate process because `LD_PRELOAD` must be in the environment before the guarded interpreter
starts. The allowlist is `netguard.derive_allowed(settings)`, the same function the in-process guard
uses, so the two layers cannot drift. A failure exits non-zero and, under `set -e`, stops the
container rather than starting it unguarded.
"""

from __future__ import annotations

import sys

from chemclaw.core.config import settings
from chemclaw.core.netguard import derive_allowed


def posture() -> str:
    """The one line the entrypoint reads: `enabled|disabled` and the derived allowlist.

    `disabled` when `CHEMCLAW_EGRESS_GUARD_ENABLED=false`, so both layers share one knob.
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
