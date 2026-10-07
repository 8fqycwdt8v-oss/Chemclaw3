"""The Python side of the compiled egress guard: is the interposer loaded, and what has it refused?

`netguard.py` patches Python's socket surface; `netguard_preload.c`, loaded via `LD_PRELOAD`, covers
what that cannot (grpc's C-core, Temporal's Rust core). This module holds the names the C file and
`deploy/entrypoint.sh` agree on and publishes the layer's own state.

Its gauges are separate from `chemclaw_egress_guard_armed` so the Python guard's health can never
stand in for the compiled layer's. Armed is measured by asking the dynamic linker whether the
library's symbol resolves, not by reading `LD_PRELOAD` (a missing path is silently ignored). The
refusal counts live in C atomics and are bound as gauges, one per kind (lookup vs dial).
"""

from __future__ import annotations

import ctypes
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# The interposer's source, beside the Python half it extends; the image picks it up with `src/`.
SOURCE = Path(__file__).with_name("netguard_preload.c")

# The shared object the build produces and `LD_PRELOAD` names. The build script and entrypoint hold
# their own literals (sh and a Containerfile argument);
# `tests/test_netguard_preload.py::test_the_entrypoint_preloads_the_path_the_image_installs` pins
# them against this one.
LIBRARY_NAME = "libchemclaw_netguard.so"

#: Where `deploy/Containerfile` installs it. The entrypoint preloads this path; a test pins that the
#: two agree, because an `LD_PRELOAD` pointing at nothing is ignored in silence.
LIBRARY_PATH = f"/app/lib/{LIBRARY_NAME}"

# The comma-separated allowlist the interposer reads, written by `deploy/entrypoint.sh` from
# `chemclaw.cli.egress_preload` (which calls `netguard.derive_allowed`, the one derivation). Absent
# or empty means loopback only, never disabled.
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

    Cached: `LD_PRELOAD` is consumed before the interpreter starts, so the answer cannot change.
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

    0 rather than an exception because this runs on a scrape; `chemclaw_egress_preload_armed` says
    which zero it is.
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

    Called unconditionally from `chemclaw.core.config` beside `arm_from_settings`: whether the
    compiled layer is loaded matters even when the Python guard is off. Best-effort because the
    registry may not exist yet at config import.
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
