"""A parse-child probe that reports the no-egress posture it was *given*, importing nothing.

**Its own module, and that is the entire reason it exists.** `forkserver` pickles a `Process`
target by reference, so the child imports the module the target lives in — and while this probe
lived in `tests/test_parse_isolation.py`, that import pulled `chemclaw.core.config` and armed the
egress guard **in the child, for this probe only**. The test then passed with `isolate._PRELOAD`
emptied: it proved that importing a module which arms the guard arms the guard, and said nothing
about the chain it claimed to be testing.

This module imports `socket` and `sys` and nothing else, and reads `chemclaw.core.netguard` out of
`sys.modules` rather than importing it. Absent means the forkserver never imported it, which is
precisely the regression the probe exists to catch — and the connect below then goes out unguarded
and says so.
"""

import socket
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from multiprocessing.connection import Connection


def egress_posture(connection: "Connection[object]", *_ignored: object) -> None:
    """Child entry point: report what the egress guard does to a non-loopback connect.

    Args:
        connection: The write end of the pipe the parent reads.
    """
    netguard = sys.modules.get("chemclaw.core.netguard")
    armed = getattr(netguard, "_armed", None)
    forbidden: type[BaseException] | tuple[()] = getattr(netguard, "EgressForbidden", None) or ()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(1.0)
            probe.connect(("198.51.100.1", 80))  # TEST-NET-2, routed nowhere
        connection.send(("reached", armed))
    except forbidden:
        connection.send(("refused", armed))
    except OSError as exc:
        connection.send((f"other:{type(exc).__name__}", armed))
    finally:
        connection.close()
