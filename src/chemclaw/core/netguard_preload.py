"""The Python side of the compiled egress guard: is the interposer loaded, and what has it refused?

`netguard.py` patches `socket.socket` and the `socket` module's resolvers, which is the whole of the
pure-Python surface. `netguard_preload.c` is the second layer, loaded through `LD_PRELOAD`, and it
reaches what the first cannot: grpc's C-core and Temporal's Rust sdk-core, the two destinations the
guard most wants to bound. This module is the *only* thing in `src/` that knows the two halves are
connected — it holds the names the C file and `deploy/entrypoint.sh` agree on, and it publishes the
layer's own state so an operator can see it.

**Its own signal, deliberately not the existing one.** `chemclaw_egress_guard_armed` says the
*Python* guard is installed, and the finding that licensed this work is precisely that it reported
health while the compiled path was wide open — measured, seven gRPC/Temporal/OTLP connections to an
off-allowlist listener with `chemclaw_egress_refused_total` flat. Folding the two layers into one
gauge would rebuild that blindness, so there are three more series and no change to the first:
`chemclaw_egress_preload_armed`, and one refusal count per *kind* of refusal, because a blocked
lookup and a blocked dial have different causes and an operator has to be able to tell them apart.

**Armed is measured, not believed.** `is_armed()` asks the dynamic linker whether the library's
symbol resolves in this process. Reading `LD_PRELOAD`, or the allowlist variable, would report what
a launcher *intended*: an `LD_PRELOAD` naming a path that does not exist is silently ignored by the
loader, which is exactly the shape where a gauge lies.

The counts are bound as gauges rather than mirrored into counters. A counter in this registry is
incremented by Python, and nothing in Python performs these refusals — the C library does, in its
own atomics. Binding the reading means a scrape reflects what the interposer has actually counted,
with no polling loop to drift or die.
"""

from __future__ import annotations

import ctypes
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: The interposer's source, beside this module. `src/` is all the code, and this is code: it lives
#: next to the Python half it extends rather than in a directory of its own, which also means the
#: image picks it up through the `COPY src ./src` that is already there.
SOURCE = Path(__file__).with_name("netguard_preload.c")

#: The shared object the build produces and `LD_PRELOAD` names. **Read by the tests, not by the
#: build script or the entrypoint** — both of those hold their own literal, because one is `sh` and
#: the other is a `Containerfile` argument, and neither can import Python. This comment claimed all
#: three, which would have made a rename here look safe; what actually keeps the spellings together
#: is `tests/test_netguard_preload.py::test_the_entrypoint_preloads_the_path_the_image_installs`,
#: which pins the other two against this one.
LIBRARY_NAME = "libchemclaw_netguard.so"

#: Where `deploy/Containerfile` installs it. The entrypoint preloads this path; a test pins that the
#: two agree, because an `LD_PRELOAD` pointing at nothing is ignored in silence.
LIBRARY_PATH = f"/app/lib/{LIBRARY_NAME}"

#: The allowlist the interposer reads, comma-separated, written by `deploy/entrypoint.sh` from
#: `chemclaw.cli.egress_preload` — which calls `netguard.derive_allowed`, so there is one derivation
#: rather than a second list in C. Absent or empty means *loopback only*, never "disabled".
ALLOWLIST_VARIABLE = "CHEMCLAW_NETGUARD_PRELOAD_ALLOW"

#: The refusal kinds `chemclaw_netguard_preload_refused` takes, mirroring the C constants.
REFUSAL_CONNECT = 0
REFUSAL_RESOLVE = 1

_ARMED_SYMBOL = "chemclaw_netguard_preload_armed"
_REFUSED_SYMBOL = "chemclaw_netguard_preload_refused"

_process: ctypes.CDLL | None = None
_looked_up = False


def _global_symbols() -> ctypes.CDLL | None:
    """This process's global symbol table, or None where `ctypes` cannot open it.

    Cached because the answer cannot change: `LD_PRELOAD` is consumed by the loader before the
    interpreter starts, and nothing can preload a library into a running process afterwards.
    """
    global _process, _looked_up
    if not _looked_up:
        _looked_up = True
        try:
            _process = ctypes.CDLL(None)
        except OSError:  # pragma: no cover - not Linux
            _process = None
    return _process


def is_armed() -> bool:
    """True when the interposer is loaded into *this* process, asked of the dynamic linker."""
    library = _global_symbols()
    if library is None:
        return False
    return hasattr(library, _ARMED_SYMBOL)


def refusals(kind: int) -> int:
    """How many refusals of `kind` the interposer has counted, or 0 when it is not loaded.

    0 rather than an exception, because this is read on a metrics scrape: a process without the
    library has refused nothing, and `chemclaw_egress_preload_armed` is the series that says which
    of the two zeroes this is.
    """
    library = _global_symbols()
    if library is None or not hasattr(library, _REFUSED_SYMBOL):
        return 0
    function = getattr(library, _REFUSED_SYMBOL)
    function.restype = ctypes.c_ulong
    function.argtypes = [ctypes.c_int]
    return int(function(kind))


def publish_state() -> None:
    """Bind the three preload gauges to live sources, best-effort.

    Called from `chemclaw.core.config` beside `arm_from_settings`, and **unconditionally** — whether
    the compiled layer is loaded is a fact about the process rather than a consequence of
    `egress_guard_enabled`, and a deployment that turned the Python guard off still needs to know
    which layers it has. Best-effort for the same reason as `netguard._publish_armed`: this runs at
    config import, possibly before the registry exists.
    """
    try:
        from chemclaw.core.metrics import METRICS

        METRICS.bind_gauge("chemclaw_egress_preload_armed", lambda: 1.0 if is_armed() else 0.0)
        METRICS.bind_gauge(
            "chemclaw_egress_preload_refused_connect", lambda: float(refusals(REFUSAL_CONNECT))
        )
        METRICS.bind_gauge(
            "chemclaw_egress_preload_refused_resolve", lambda: float(refusals(REFUSAL_RESOLVE))
        )
    except Exception:  # pragma: no cover - registry not built yet
        pass
