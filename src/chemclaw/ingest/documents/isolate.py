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

**What a child reads from `Settings` is the forkserver's, not the caller's.** The server is exec'd
once with the process's environment and imports `parse` at that moment, so a child's
`document_max_expanded_bytes` is whatever the environment said when the first upload arrived. That
is correct in a deployment, where the environment does not change under a running pod, and it is a
real difference in a test that monkeypatches a setting and then parses through this module — such a
test must drive `parse_document` directly, which is where that behaviour belongs anyway.
"""

import logging
import multiprocessing as mp
import threading
from multiprocessing.connection import Connection
from multiprocessing.context import ForkServerContext
from typing import cast

from chemclaw.core.metrics import METRICS
from chemclaw.ingest.documents.parse import DocumentParseError, ParsedDocument, parse_document

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
    try:
        connection.send(("parsed", parse_document(name, raw, declared)))
    except DocumentParseError as exc:
        connection.send(("refused", exc))
    # Broad on purpose: this is the child's last act, and an exception that escapes here dies with
    # it, leaving the parent an EOF it can only report as "stopped without answering".
    except BaseException as exc:
        connection.send(("failed", f"{type(exc).__name__}: {exc}"))
    finally:
        connection.close()


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

    The timeout covers time-to-first-byte rather than the whole transfer, because `Connection.poll`
    is what can be given a deadline. That is the right boundary: by the time a byte appears the
    parse is finished and what remains is a copy of already-computed text, bounded by
    `attachment_max_bytes` upstream. A child large enough to block on the pipe buffer unblocks as
    soon as the `recv` below starts reading.

    Args:
        name: The already-sanitized document name, used in refusal messages.
        raw: The document's bytes.
        declared_type: The client-declared content type, or None to infer from the name.
        timeout: Seconds to wait for the child's answer before killing it.

    Returns:
        The parsed document.

    Raises:
        DocumentParseError: The file is unsupported or unreadable — raised as the child's own
            exception, so a `ScannedDocumentError` still arrives as one.
        ParseWorkerLost: The child was killed on `timeout`, or died without answering.
    """
    context = parse_context()
    reader, writer = context.Pipe(duplex=False)
    child = context.Process(target=_parse_into, args=(writer, name, raw, declared_type))
    child.start()
    # Closed in the parent as soon as the child holds it, or `poll` never sees EOF when the child
    # dies: a pipe stays open while any process holds a write end, and this process is one.
    writer.close()
    try:
        if not reader.poll(timeout):
            child.kill()
            METRICS.increment("chemclaw_document_parse_kills_total")
            logger.warning(
                "killed the reader process for %s after %ss; the parse slot is released",
                name,
                timeout,
            )
            raise ParseWorkerLost(
                f"{name} was still being read after {timeout:g}s and was refused; a smaller or "
                "simpler file will work"
            )
        try:
            answer = cast("tuple[str, object]", reader.recv())
        except EOFError as exc:
            raise ParseWorkerLost(
                f"{name} could not be read: the reader process stopped without answering"
            ) from exc
    finally:
        reader.close()
        # Unconditional, and it is what reaps the child in every path — the kill above, a clean
        # answer, or the caller's own cancellation. `join` on an already-exited child is a no-op.
        child.join()
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
