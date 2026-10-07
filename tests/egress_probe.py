"""A parse-child probe that reports the no-egress posture it was *given*, importing nothing.

Its own module because `forkserver` imports the target's module in the child: a probe living in a
module that imports `chemclaw.core.config` would arm the guard itself and prove nothing. It reads
`chemclaw.core.netguard` from `sys.modules`; absent means the forkserver never armed it.
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
