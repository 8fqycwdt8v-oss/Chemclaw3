"""Run one parse in a process that can be killed, so a runaway parse cannot wedge its caller.

A parse slot is released when its worker thread finishes, and CPython cannot stop a thread, so a
non-terminating parse would hold its slot for the life of the process
(D-2026-09-12-a-parse-that-cannot-be-killed-wedges-its-replica). Each parse therefore runs in a
child forked from a `forkserver` with the parsers preloaded, which keeps the per-parse cost to
milliseconds, and a stuck child is SIGKILLed and reaped.

Constraints a maintainer must know:

- **`forkserver`, not `fork`.** The front door is multithreaded, and `fork()` from it can leave
  locks held by threads that do not exist in the child. The forkserver is started by fork-and-exec,
  so it is a clean single-threaded interpreter.
- **The child is inside the no-egress posture** because importing `parse` imports
  `chemclaw.core.config`, which arms the egress guard in the forkserver before it forks.
- **The parent's `__main__` is re-executed in every child** (`multiprocessing.spawn.prepare`), so
  any entry point this process may be started as must keep side effects behind an `if __name__ ==
  "__main__"` guard.
- **The forkserver is a second resident copy of the parsers.** The chart's memory request is derived
  from a measurement in `tests/test_deploy_chart.py`; adding to `_PRELOAD` changes that cost.
- **A child reads `Settings` as the forkserver saw the environment at first use.** Tests that
  monkeypatch a setting must call `parse_document` directly.
"""

import logging
import multiprocessing as mp
import os
import resource
import signal
import threading
import time
from multiprocessing.connection import Connection
from multiprocessing.context import ForkServerContext
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import cast

from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from chemclaw.ingest.documents.parse import (
    DocumentParseError,
    ParsedDocument,
    parse_document,
    too_large_to_read,
)

logger = logging.getLogger(__name__)

# What the forkserver imports before it forks anything; importing `parse` pulls in every document
# parser, which is the cost being amortised.
_PRELOAD = ["chemclaw.ingest.documents.parse"]

_context: ForkServerContext | None = None
# A `threading.Lock`: every caller is already on a worker thread, so two threads can genuinely
# arrive together.
_context_lock = threading.Lock()


def parse_context() -> ForkServerContext:
    """The process's forkserver context, started and preloaded on first use.

    Lazy so a process that never parses (workers, CLI) does not pay the forkserver start-up. Public
    so a deployment can warm it at startup and a test can assert the context type.

    Returns:
        The shared context every isolated parse forks from.
    """
    global _context
    if _context is not None:
        return _context
    with _context_lock:
        if _context is None:
            # Typeshed overloads `get_context` on the literal, so this name selects the concrete
            # context type rather than the base — which is why `_context` can be annotated with it.
            context = mp.get_context("forkserver")
            context.set_forkserver_preload(_PRELOAD)
            _context = context
    return _context


class ParseWorkerLost(DocumentParseError):
    """The child died without answering — killed on timeout, OOM-killed, or crashed.

    A `DocumentParseError` subclass so existing handlers cover it (re-sending the same bytes would
    do the same thing); its own name so the log distinguishes a timeout from a malformed file.
    """


def _anonymous_bytes() -> int | None:
    """This process's private anonymous memory, which is what `RLIMIT_DATA` counts.

    Returns:
        `VmData` in bytes, or None where there is no `/proc` to read it from.
    """
    try:
        status = Path("/proc/self/status").read_text()
    except OSError:
        return None
    for line in status.splitlines():
        if line.startswith("VmData:"):
            return int(line.split()[1]) * 1024
    return None


#: How close to its ceiling a failed parse must have been for the ceiling to be the explanation. A
#: constant, not a setting: it is the resolution of a measurement. Budget-exhausted parses fail
#: with almost no headroom and genuinely unreadable files with the whole budget unspent, so a
#: small value is the conservative reading.
_CEILING_HEADROOM_BYTES = 8 * 1024 * 1024


def _at_ceiling(ceiling: int | None) -> bool:
    """Had this parse spent its whole allowance at the moment it failed?

    Distinguishes an oversized document from a broken one: a C parser (lxml) reports its own
    allocation failure as an ordinary parse error rather than `MemoryError`. The process's `VmData`
    against the ceiling is the only signal left afterwards; see `_CEILING_HEADROOM_BYTES`.

    Args:
        ceiling: The `RLIMIT_DATA` value `_bound_allocations` actually set, or None if it set none.

    Returns:
        True when the budget is the likeliest explanation for the failure.
    """
    if ceiling is None:
        return False
    now = _anonymous_bytes()
    return now is not None and now >= ceiling - _CEILING_HEADROOM_BYTES


def _bound_allocations(budget: int) -> tuple[int | None, int | None]:
    """Cap what this parse may allocate, so an unbounded document is a refusal not an OOMKill.

    Upstream size limits are read from the archive and cannot predict a parse's memory (string
    widening, shared strings, DOM construction), so the bound is a kernel ceiling on the allocating
    process (D-2026-09-19-a-ceiling-on-the-archive-is-not-a-ceiling-on-the-parse). `RLIMIT_DATA`
    rather than `RLIMIT_AS`: it covers heap and private anonymous mappings and excludes inherited
    file-backed ones. The baseline is read, not assumed, because a forkserver child starts with the
    preload resident.

    The baseline is read after `raw` is unpickled, so the budget is what a parse may allocate beyond
    the document; `binding.PARSE_COEFFICIENT_BASIS_BYTES` caps the document term. An exhausted
    budget surfaces as `MemoryError` or as a C parser's own failure, both reported as a refusal.

    Args:
        budget: Bytes this process may allocate beyond what it already holds.

    Returns:
        The ceiling actually set and the hard limit it was set under, or `(None, None)` when no
        bound could be applied. The hard limit is what `_release_allocations` restores to.
    """
    base = _anonymous_bytes()
    if base is None:
        # Not Linux, so there is no `/proc/self/status` to read a baseline from and no shipped
        # deployment either. Said out loud rather than passed over: the parse runs unbounded here.
        logger.warning(
            "no /proc/self/status to size a parse budget against; this parse is not memory-bounded"
        )
        return None, None
    ceiling = base + budget
    # The inherited hard limit wins: `setrlimit` cannot raise a maximum, and a lower ambient ceiling
    # is simply a smaller budget rather than a reason to refuse every document.
    hard: int = resource.getrlimit(resource.RLIMIT_DATA)[1]
    if hard != resource.RLIM_INFINITY and hard < ceiling:
        logger.warning(
            "an ambient hard RLIMIT_DATA of %d bytes is below this parse's budget of %d; parsing "
            "against the ambient ceiling instead",
            hard,
            ceiling - base,
        )
        ceiling = hard
    try:
        # The hard limit is passed through unchanged rather than lowered to the soft one: lowering
        # it is irreversible for the process, and this child has no reason to take that from itself.
        resource.setrlimit(resource.RLIMIT_DATA, (ceiling, hard))
    except (OSError, ValueError):
        logger.exception("could not bound this parse's allocations; it runs unbounded")
        return None, None
    return ceiling, hard


def _release_allocations(hard: int | None) -> None:
    """Give the ceiling back, once the parse it was bounding has finished failing.

    Pickling the refusal onto the pipe still allocates, and a `MemoryError` raised inside an
    `except` clause would reach the parent as a nameless EOF. Restoring the soft limit gives the
    reply room; nothing else runs in this process afterwards.

    Args:
        hard: The hard limit `_bound_allocations` set the soft one under, or None if it set none.
    """
    if hard is None:
        return
    try:
        resource.setrlimit(resource.RLIMIT_DATA, (hard, hard))
    except (OSError, ValueError):  # pragma: no cover - the ceiling was set under this same limit
        logger.exception("could not release this parse's allocation ceiling before replying")


def _parse_into(
    connection: "Connection[object]", name: str, raw: bytes, declared: str | None
) -> None:
    """Child entry point: parse, put the outcome on the pipe, and exit.

    Module-level because `forkserver` pickles the target by reference. The outcome is a tagged pair,
    since a raised exception reaches the parent only as an exit code. `DocumentParseError` is sent
    as itself; anything else as its `repr`, so the front door never unpickles an arbitrary
    third-party class.

    Args:
        connection: The write end of the pipe the parent is reading.
        name: The already-sanitized document name.
        raw: The document's bytes.
        declared: The client-declared content type, or None.
    """
    ceiling: int | None = None
    hard: int | None = None
    try:
        # Lead a new process group so a kill reaches anything this parse starts; `Process.kill()`
        # signals one pid and would leave a grandchild running. If `setsid` fails, the parent sees
        # the group is its own and kills the child singly.
        try:
            os.setsid()
        except OSError:  # pragma: no cover - only reachable if the child already leads a group
            logger.debug("parse child could not lead its own process group; kills stay per-pid")
        # Before a byte is read, and in this process rather than in the parent: a limit set on the
        # front door would bound the front door, which is the thing being protected.
        ceiling, hard = _bound_allocations(settings.document_parse_memory_bytes)
        connection.send(("parsed", parse_document(name, raw, declared)))
    # A parse that stopped at its ceiling is renamed here, whatever it called itself: lxml reports
    # its own allocation failure as a generic error that would tell a chemist a legal document is
    # malformed. `_at_ceiling` separates it from a genuinely broken file.
    except DocumentParseError as exc:
        # Read before the release, and release before the send, so the reply is not bounded by the
        # ceiling that stopped the parse.
        stopped = _at_ceiling(ceiling)
        _release_allocations(hard)
        connection.send(("refused", too_large_to_read(name) if stopped else exc))
    # Outside `parse_document`'s own arm: this is the pickling of a document that parsed, a second
    # full copy of the text, deliberately inside the same budget and reported by name.
    except MemoryError:
        _release_allocations(hard)
        connection.send(("refused", too_large_to_read(name)))
    # Broad on purpose: this is the child's last act, and an exception that escapes here dies with
    # it, leaving the parent an EOF it can only report as "stopped without answering".
    except BaseException as exc:
        stopped = _at_ceiling(ceiling)
        _release_allocations(hard)
        if stopped:
            connection.send(("refused", too_large_to_read(name)))
        else:
            connection.send(("failed", f"{type(exc).__name__}: {exc}"))
    finally:
        connection.close()


#: How long a reap waits on a child that has just been SIGKILLed — signal-delivery latency, not a
#: deployment setting. A `join` that outlives it logs and returns rather than holding the worker
#: thread.
_REAP_SECONDS = 2.0


def _left(deadline: float) -> float:
    """Seconds remaining until `deadline`, never negative.

    A negative timeout means "block forever" to `Connection.poll` and "do not wait" to
    `Process.join`; clamping makes one deadline mean one thing.

    Args:
        deadline: A `time.monotonic()` value.

    Returns:
        The non-negative remainder.
    """
    return max(deadline - time.monotonic(), 0.0)


def _kill(child: BaseProcess, name: str, timeout: float, why: str) -> None:
    """SIGKILL `child` and everything it started, and record that it happened.

    `SIGKILL` because a parse that ignores signals is the case this exists for. The group is
    signalled only when it is demonstrably not this process's own (if the child's `setsid` failed,
    `killpg` would kill the front door); otherwise the single pid. Safe from the watchdog thread: a
    double kill raises `ProcessLookupError`, which is swallowed.

    Args:
        child: The parse child.
        name: The document name, for the log.
        timeout: The deadline that was exceeded, for the log.
        why: What the child did, for the log.
    """
    pid = child.pid
    if pid is None:  # pragma: no cover - a child that never started has no slot to free
        return
    METRICS.increment("chemclaw_document_parse_kills_total")
    logger.warning(
        "killed the reader process for %s after %ss (%s); the parse slot is released",
        name,
        timeout,
        why,
    )
    try:
        group = os.getpgid(pid)
    except OSError:  # pragma: no cover - the child is already gone, which is the outcome wanted
        return
    if group != os.getpgid(0):
        try:
            os.killpg(group, signal.SIGKILL)
            return
        except OSError:  # pragma: no cover - already reaped between the two syscalls
            return
    child.kill()


def parse_document_isolated(
    name: str, raw: bytes, declared_type: str | None, timeout: float
) -> ParsedDocument:
    """Parse `raw` in a killable child, refusing it if the child outruns `timeout`.

    Blocking on purpose: the caller is a worker thread that exists to hold this off the event loop,
    and the point is that the thread now ends in bounded time.

    `timeout` is one monotonic deadline over the whole exchange — `poll`, `recv` and the reap.
    `recv` has no timeout argument, so a watchdog kills the child at the deadline, which turns a
    stalled `recv` into `EOFError`; a child that answered but will not exit is killed at the reap.
    Without this a truncated message or a lingering child would hold the slot forever. The transfer
    itself is bounded by `document_max_expanded_bytes`.

    Args:
        name: The already-sanitized document name, used in refusal messages.
        raw: The document's bytes.
        declared_type: The client-declared content type, or None to infer from the name.
        timeout: Seconds this whole exchange may take before the child is killed.

    Returns:
        The parsed document.

    Raises:
        DocumentParseError: The file is unsupported or unreadable — raised as the child's own
        exception, so a `ScannedDocumentError` still arrives as one.
        ParseWorkerLost: The child was killed on `timeout`, or died without answering.
    """
    context = parse_context()
    reader, writer = context.Pipe(duplex=False)
    try:
        child = context.Process(target=_parse_into, args=(writer, name, raw, declared_type))
        child.start()
    except BaseException:
        # Both ends, because neither is owned by anything else yet: a failed `start()` used to
        # leave the pipe's two descriptors open for the life of the process.
        reader.close()
        writer.close()
        raise
    # Closed in the parent as soon as the child holds it, or `poll` never sees EOF when the child
    # dies: a pipe stays open while any process holds a write end, and this process is one.
    writer.close()
    deadline = time.monotonic() + timeout
    # Set by whichever stage kills, so one pathological parse books one kill; an `Event` because the
    # watchdog sets it from its own thread.
    killed = threading.Event()

    def _kill_once(why: str) -> None:
        """Kill the child and record it, unless something already has."""
        if not killed.is_set():
            killed.set()
            _kill(child, name, timeout, why)

    try:
        if not reader.poll(_left(deadline)):
            _kill_once("never answered")
            raise ParseWorkerLost(
                f"{name} was still being read after {timeout:g}s and was refused; a smaller or "
                "simpler file will work"
            )
        # Gives `recv` the deadline it has no argument for. Armed on every parse and cancelled
        # immediately on a healthy one.
        watchdog = threading.Timer(_left(deadline), _kill_once, args=("stopped mid-answer",))
        watchdog.start()
        try:
            answer = cast("tuple[str, object]", reader.recv())
        # `EOFError` when nothing of the message arrived, `OSError` when part of it did — the
        # second is what a truncated write, or the watchdog's kill partway through one, produces.
        except (EOFError, OSError) as exc:
            raise ParseWorkerLost(
                f"{name} could not be read: the reader process stopped without answering"
            ) from exc
        finally:
            watchdog.cancel()
    finally:
        reader.close()
        # Unconditional: reaps the child on every path. A killed child only needs reaping; one that
        # answered but will not exit gets the rest of the deadline and is then killed.
        if killed.is_set():
            child.join(_REAP_SECONDS)
        else:
            child.join(_left(deadline))
            if child.is_alive():
                _kill_once("answered and did not exit")
                child.join(_REAP_SECONDS)
        if child.is_alive():  # pragma: no cover - a SIGKILL this process may send cannot be refused
            logger.error("the reader process for %s outlived its kill; the pod is leaking", name)
    outcome, payload = answer
    if outcome == "parsed" and isinstance(payload, ParsedDocument):
        return payload
    if outcome == "refused" and isinstance(payload, DocumentParseError):
        raise payload
    # Either the child said "failed" or sent something unrecognised (version skew); a lost parse
    # either way.
    logger.error("parsing %s failed inside the reader process: %s", name, payload)
    raise ParseWorkerLost(f"{name} could not be read")
