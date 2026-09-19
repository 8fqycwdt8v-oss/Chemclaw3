"""Run one parse in a process that can be killed, so a runaway parse cannot wedge its caller.

**The defect this closes is a liveness failure, not a resource leak.** `agent/attachments.py` bounds
how many uploads may be parsed at once and sheds the rest, and that cap is real — driven, a third
upload was refused in 2.00 s with a retryable 503 while two slots were held. What it could not do is
ever get those slots *back*: a slot stands for a running worker thread and is released by that
thread's completion callback, so a parse that does not terminate holds its slot for the life of the
process. Driven against a deliberately non-terminating parser at the shipped cap of 2: both callers
were freed at their 1.0 s timeout, `in_flight` stayed at **2** five seconds later, and every
subsequent upload — measured at +2 s and again at +7 s — was shed. The replica's upload path was
down permanently, and nothing said so.

A worker thread is the wrong container for that work because CPython cannot stop one. A child
process is the right one, and the only question was what it costs. **Measured, on this tree:**

- A fresh interpreter that imports the parsers costs **0.97 s** — which is why a subprocess *per
  upload*, started from scratch, was never a serious option.
- A `forkserver` child with the parsers preloaded costs **10 ms** after the first, and the first
  pays the 0.86 s that starts the server. Four consecutive parses: 0.857 s, 0.011 s, 0.010 s,
  0.010 s.
- A non-terminating child is killed and reaped, `exitcode=-9`, `is_alive()` False.

So the fix costs ten milliseconds an upload and returns the slot in bounded time whatever the file
does. That is the whole argument, and
`D-2026-09-12-a-parse-that-cannot-be-killed-wedges-its-replica` carries it.

**`forkserver` rather than `fork`, and that is a correctness choice rather than a preference.** The
front door is a threaded process — uvicorn's default executor runs the parse threads and
`api/auth.py` validates every bearer token on it — and `os.fork()` from a multithreaded parent gives
the child a memory image in which any lock another thread held at that instant is held forever by a
thread that does not exist. CPython deprecates it for exactly this reason. `forkserver` starts its
server by fork **and exec** (`multiprocessing.util.spawnv_passfds`), so the server is a clean
single-threaded interpreter, and every parse is then forked from *it* rather than from the front
door. The preload list is what makes those forks cost 10 ms instead of 970.

**A parse child is still inside the no-egress posture, and that is observed rather than inherited.**
Moving untrusted bytes into a process this deployment did not previously have is exactly the move
that could carry them outside a guard armed in the parent — and `forkserver` starts its server by
fork *and exec*, so the child's guard is whatever that fresh interpreter armed, not a copy of the
parent's memory. It is armed, by a chain worth naming because it is not obvious: `_PRELOAD` imports
`parse`, which imports `chemclaw.core.config`, whose module body ends in
`arm_egress_guard(settings)`. So the guard is armed in the forkserver before it forks anything.
`tests/test_parse_isolation.py::test_a_parse_child_is_still_inside_the_no_egress_posture` drives a
non-loopback connect from inside a child and reads both the refusal and `netguard._armed` back.

**A child re-executes the serving process's `__main__` on every parse, and that is a constraint on
what may go in one.** `multiprocessing.spawn.prepare` calls `_fixup_main_from_path` in each child,
which `runpy`-executes the parent's `__main__` file under the name `__mp_main__`. The shipped front
door is `exec uvicorn … --factory`, so that file is `.venv/bin/uvicorn`, whose body is
`from uvicorn.main import main` *outside* any `__name__` guard — benign, and 14 ms: measured, a warm
parse costs 0.017 s with a cheap `__main__` and 0.031 s with uvicorn's, while `runpy.run_path` on
that file alone is 0.090 s cold. **Latency is the small half.** The hazard is that any side effect
in the serving process's `__main__` body now runs once per upload, in a child — and it is not
hypothetical: a probe script written while measuring this module had no `if __name__ ==
"__main__"` guard, so every child re-ran the probe and the parse came back `ParseWorkerLost`. An
entry point that starts a server, opens a connection or writes a file at module scope would do the
same thing invisibly. Anything this process may be started as must keep its work behind a guard.

**A warm forkserver is a second resident copy of the parsers, not a shared one.** It is started by
fork **and exec**, so none of its pages are copy-on-write with the front door's: the parsers are
resident in the parent too (`agent/attachments.py` imports this module, which imports `parse`), and
the forkserver imports them again. **What that costs the pod is smaller than its `VmRSS` and is not
a number this docstring may hold** — a cgroup is charged once for a unique physical page, so the
109 MiB this paragraph used to quote double-counted the shared objects the parent already had
mapped, and `resources.service` was sized against neither figure.
`tests/test_deploy_chart.py::test_a_warm_parse_forkserver_still_costs_what_this_budget_was_derived_against`
measures it off a running forkserver and is what the chart's memory request is derived from;
`D-2026-09-18-a-second-process-in-the-pod-is-memory-the-chart-never-declared` has the tables. Adding
a module to `_PRELOAD` moves what every front door and every background worker costs its node, and
reds there.

**What a child reads from `Settings` is the forkserver's, not the caller's.** The server is exec'd
once with the process's environment and imports `parse` at that moment, so a child's
`document_max_expanded_bytes` is whatever the environment said when the first upload arrived. That
is correct in a deployment, where the environment does not change under a running pod, and it is a
real difference in a test that monkeypatches a setting and then parses through this module — such a
test must drive `parse_document` directly, which is where that behaviour belongs anyway.
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

# What the forkserver imports before it forks anything, and the reason a child costs 10 ms rather
# than 970. One name: importing `parse` pulls pypdf, python-docx, openpyxl and python-pptx with it,
# which is the whole cost being amortised.
_PRELOAD = ["chemclaw.ingest.documents.parse"]

_context: ForkServerContext | None = None
# A `threading.Lock`, not an `asyncio` one: every caller of this module is already on a worker
# thread (`attachments._ParseSlots.submit` hands it to the default executor), so there is no loop
# here to await on and two threads can genuinely arrive together.
_context_lock = threading.Lock()


def parse_context() -> ForkServerContext:
    """The process's forkserver context, started and preloaded on first use.

    Built lazily rather than at import, because starting a forkserver is 0.86 s of interpreter
    start-up that a process which never parses a document must not pay — the Temporal workers and
    the CLI import this package's siblings and never reach here.

    Exposed rather than private so a deployment that *does* want the first upload to cost 10 ms can
    warm it at startup, and so a test can assert the context is a forkserver rather than inferring
    it from a timing.

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

    A `DocumentParseError` subclass so every existing caller's `except` still covers it: from the
    outside this is the same event as an unreadable file, in that re-sending the same bytes will do
    the same thing. Its own name so the *log* can tell a timeout from a malformed PDF, which is the
    distinction an operator needs and a chemist does not.
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


#: How close to its ceiling a failed parse has to have been for the ceiling to be the explanation.
#: Not a `Settings` field, for the reason `_REAP_SECONDS` below is not one: it is the resolution of
#: a measurement, not a posture a deployment states. Measured on this parser set — a document that
#: exhausts the budget fails with **0.1 MiB** of headroom left, and every genuinely unreadable one
#: (truncated XML, not a zip at all, empty, wrong extension) fails with the **whole 160 MiB**
#: unspent. Anything in between is a parser this repository has not seen, and the conservative
#: reading of it is "not the budget", which is why this is small rather than generous.
_CEILING_HEADROOM_BYTES = 8 * 1024 * 1024


def _at_ceiling(ceiling: int | None) -> bool:
    """Had this parse spent its whole allowance at the moment it failed?

    **The one thing that distinguishes an oversized document from a broken one**, and without it
    they are the same sentence. A C parser that reports its own allocation failure never lets
    CPython raise `MemoryError`, so lxml's arrives as an ordinary parse error — and the message a
    chemist gets is that their perfectly good report is malformed at line 0, which is worse than
    the generic wording `too_large_to_read` exists to replace rather than equal to it.

    `VmData` rather than a flag set by the failing allocation, because there is no such flag to
    set: the failure happens inside libxml2 and surfaces as a value, not as a signal. What is
    checkable afterwards is how much of the budget the process was holding, and the two populations
    do not overlap — see `_CEILING_HEADROOM_BYTES`.

    Args:
        ceiling: The `RLIMIT_DATA` value `_bound_allocations` actually set, or None if it set none.

    Returns:
        True when the budget is the likeliest explanation for the failure.
    """
    if ceiling is None:
        return False
    now = _anonymous_bytes()
    return now is not None and now >= ceiling - _CEILING_HEADROOM_BYTES


def _bound_allocations(budget: int) -> int | None:
    """Cap what this parse may allocate, so an unbounded document is a refusal not an OOMKill.

    **This is the only bound in the unit that kills the pod.** Every ceiling upstream of here is a
    number read out of the archive — the bytes on the wire, a binding's `max_file_bytes`, the
    declared expansion `_refuse_a_bomb` sums. Measured over a real memory cgroup, three independent
    reasons none of them predicts the cost: one wide code point anywhere widens a whole
    document-wide join by 2× or 4×, a shared string is stored once and read N times, and
    `python-docx` builds an lxml DOM out of the markup rather than out of the text. A kernel ceiling
    on the process that does the allocating needs a model of none of that, and it covers a parser
    added after this was written —
    `D-2026-09-19-a-ceiling-on-the-archive-is-not-a-ceiling-on-the-parse` has the tables.

    `RLIMIT_DATA` rather than `RLIMIT_AS`: since Linux 4.7 it covers the heap *and* private
    anonymous mappings, which is what a parse spends, and it leaves the file-backed mappings this
    child inherited — libpython, libxml2, the parsers — out of the sum. The baseline is read rather
    than assumed, because a `forkserver` child starts with the whole preload list already resident
    and a budget charged against zero would refuse the first document it saw.

    What an exhausted budget looks like from outside is a refusal, by one of two routes: CPython
    raises `MemoryError`, which `parse_document` names; or a C parser reports its own allocation
    failure, which arrives as the same broad `DocumentParseError` an unreadable file does. Either
    way the caller is told the document could not be read, which is more than an OOM-killed pod
    tells anybody, and every other turn on the replica is still being served.

    Args:
        budget: Bytes this process may allocate beyond what it already holds.
    """
    base = _anonymous_bytes()
    if base is None:
        # Not Linux, so there is no `/proc/self/status` to read a baseline from and no shipped
        # deployment either. Said out loud rather than passed over: the parse runs unbounded here.
        logger.warning(
            "no /proc/self/status to size a parse budget against; this parse is not memory-bounded"
        )
        return None
    ceiling = base + budget
    # **The inherited hard limit wins, and it used to be an exception.** `setrlimit` refuses to
    # raise a maximum, so a process tree carrying any hard `RLIMIT_DATA` below `base + budget` — a
    # systemd `LimitDATA=`, a container security profile — raised `ValueError` here, landed in the
    # broad arm below and made **every document of every format** unreadable, with the cause only
    # in a log line. Driven at `base + 8 MiB`. A lower ambient ceiling is a smaller budget, which is
    # the outcome this knob is for; it is not a reason to refuse everything.
    hard = resource.getrlimit(resource.RLIMIT_DATA)[1]
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
        return None
    return ceiling


def _parse_into(
    connection: "Connection[object]", name: str, raw: bytes, declared: str | None
) -> None:
    """Child entry point: parse, put the outcome on the pipe, and exit.

    A module-level function because `forkserver` pickles the target by reference — a closure or a
    lambda cannot cross.

    The outcome is a tagged pair rather than a raised exception, because an exception raised here
    dies with the child and reaches the parent only as a non-zero exit code. `DocumentParseError` is
    sent as itself (it is a `ValueError` carrying one string, so it pickles), and anything else is
    sent as its `repr`: an unexpected exception from a third-party parser must not be reconstructed
    in the front door, where unpickling it would import whatever class it belongs to.

    Args:
        connection: The write end of the pipe the parent is reading.
        name: The already-sanitized document name.
        raw: The document's bytes.
        declared: The client-declared content type, or None.
    """
    ceiling: int | None = None
    try:
        # **Lead a new process group, so a kill reaches whatever this parse starts.**
        # `Process.kill()` signals one pid: a parser that shells out leaves the grandchild running
        # when its parent is killed, and the slot comes back while the CPU does not. Measured on a
        # child that opened `sleep 600` and then stalled — killed, slot released, one orphan left.
        # The sibling repository reached the same answer for `xtb` (`run_isolated`:
        # `start_new_session=True` plus a group kill), and this is that shape for a `forkserver`
        # child, which has no `start_new_session` to pass.
        #
        # Failure is survivable and must not be fatal: the parent checks the group it is about to
        # signal is not its own before using `killpg`, so a child that could not detach is killed
        # singly, exactly as before.
        try:
            os.setsid()
        except OSError:  # pragma: no cover - only reachable if the child already leads a group
            logger.debug("parse child could not lead its own process group; kills stay per-pid")
        # Before a byte is read, and in this process rather than in the parent: a limit set on the
        # front door would bound the front door, which is the thing being protected.
        ceiling = _bound_allocations(settings.document_parse_memory_bytes)
        connection.send(("parsed", parse_document(name, raw, declared)))
    # **A parse that stopped at its ceiling is renamed here, whatever it called itself.** lxml
    # reports its own allocation failure rather than letting CPython raise, so a markup-heavy but
    # entirely legal `.docx` arrived as `unknown error (<string>, line 0)` — a sentence that tells
    # a chemist their report is malformed at line 0 and an operator nothing at all. Measured: a
    # 485 kB Word report of 2,000 paragraphs x 200 styled runs holds 2,979,999 characters, parses
    # unbounded in 19.4 s, and is refused here in 2.2 s. `_at_ceiling` is what separates it from a
    # document that really is broken, and the two populations do not overlap.
    except DocumentParseError as exc:
        connection.send(("refused", too_large_to_read(name) if _at_ceiling(ceiling) else exc))
    # Outside `parse_document`'s own arm on purpose: this one is the *pickling* of a document that
    # parsed, which is a second full copy of the text and is deliberately inside the same budget.
    # Driven, a 7.2 M-character `.docx` extracted fine and then exhausted the ceiling on the pipe,
    # and before this arm existed the caller was told only that the reader "stopped without
    # answering" — the one refusal in this module whose cause is knowable, arriving nameless.
    except MemoryError:
        connection.send(("refused", too_large_to_read(name)))
    # Broad on purpose: this is the child's last act, and an exception that escapes here dies with
    # it, leaving the parent an EOF it can only report as "stopped without answering".
    except BaseException as exc:
        if _at_ceiling(ceiling):
            connection.send(("refused", too_large_to_read(name)))
        else:
            connection.send(("failed", f"{type(exc).__name__}: {exc}"))
    finally:
        connection.close()


#: How long a reap waits on a child that has just been SIGKILLed. Not a `Settings` field, for the
#: reason `_EVICTION_PAGE` in `agent/scratchpad.py` is not one: it is the latency of a signal
#: being delivered, not a posture a deployment states. A `join` that outlives it logs and returns,
#: because holding the worker thread is the one outcome this module exists to prevent.
_REAP_SECONDS = 2.0


def _left(deadline: float) -> float:
    """Seconds remaining until `deadline`, never negative.

    A negative timeout means "block forever" to `Connection.poll` and "do not wait" to
    `Process.join`, so the two stages of one exchange would disagree about an exhausted budget in
    opposite directions. Clamping here is what makes one deadline mean one thing.

    Args:
        deadline: A `time.monotonic()` value.

    Returns:
        The non-negative remainder.
    """
    return max(deadline - time.monotonic(), 0.0)


def _kill(child: BaseProcess, name: str, timeout: float, why: str) -> None:
    """SIGKILL `child` and everything it started, and record that it happened.

    `SIGKILL` rather than `terminate()`: a parse that ignores a signal is exactly the parse this
    exists for, and a terminate would leave the same runaway holding the same slot one indirection
    further out.

    **The group, not the pid, and the guard on that is load-bearing.** `_parse_into` calls
    `setsid()` so the child leads its own group and a grandchild dies with it. If that call failed
    the child is still in *this* process's group, and `killpg` would take the front door down with
    it — so the group is signalled only when it is demonstrably not our own, and otherwise the kill
    is the single-pid one this function replaced.

    Called from the watchdog thread as well as from the calling thread, and is safe there: every
    step is a syscall against a pid, and a double kill is a `ProcessLookupError` that is swallowed.

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

    **Blocking on purpose.** The caller is a worker thread that already exists to hold this work off
    the event loop; making this `async` would put the wait back on the loop and buy nothing, since
    the point is that the *thread* now ends in bounded time.

    The child is killed with `SIGKILL` rather than terminated, and the difference matters here: a
    parse that ignores a signal is exactly the parse this exists for, and `terminate()` would leave
    the same runaway holding the same slot one indirection further out. `join()` after the kill is
    what reaps it, so the pod does not accumulate zombies at one per timed-out upload.

    **`timeout` is one deadline over the whole exchange, and it used to be one deadline over the
    first stage of it.** The only bound was `reader.poll`, and `poll` returning True *consumes* it:
    `recv` then blocks until the whole pickled message arrives or the pipe reaches EOF, and
    `join()` had no timeout at all. Neither path killed anything, so the two failures a hostile or
    merely broken child can produce — writing a truncated message and stopping, or answering
    correctly and then never exiting — held the worker thread, and therefore its parse slot,
    **forever**. That is precisely the wedge this module exists to close, reappearing one stage
    later, and `attachments.py`'s `wait_for` backstop cannot see it: that wait is `shield`ed, so it
    frees the *caller* while the thread it stands for runs on.

    So every stage measures against the same monotonic deadline. `recv` gets it through a watchdog
    that kills the child rather than through a parameter it does not have — a dead child closes the
    write end, which is what turns a stalled `recv` into the `EOFError` this function already
    handles. The reap that follows is bounded the same way and escalates to a kill.

    The transfer after the first byte is a copy of already-computed text, bounded by
    `document_max_expanded_bytes` — 64 MB, not the 2 MB `attachment_max_bytes` this paragraph used
    to name, which is 32x smaller and the wrong setting entirely. A child large enough to block on
    the pipe buffer unblocks as soon as the `recv` below starts reading.

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
    # Set by whichever of the three stages kills, and read by the reap so one pathological parse
    # books one kill rather than three. It is an `Event` because the watchdog sets it from its own
    # thread while this one reads it.
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
        # The watchdog is what gives `recv` a deadline it has no argument for. It is armed on every
        # parse rather than only on a suspect one, because "suspect" is not knowable from here, and
        # it costs a timer thread that is cancelled microseconds later on every healthy parse.
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
        # Unconditional, and it is what reaps the child in every path — a kill above, a clean
        # answer, or the caller's own cancellation. A child that has been killed only has to be
        # reaped; one that answered and then declined to exit is given what is left of the deadline
        # and then killed, because waiting on it is what held the worker thread for ever.
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
    # Either the child said "failed", or it sent something this function does not recognise —
    # which can only be a version skew between the two halves of one process tree, and is a lost
    # parse either way.
    logger.error("parsing %s failed inside the reader process: %s", name, payload)
    raise ParseWorkerLost(f"{name} could not be read")
