"""Parse children that misbehave, and the driver that measures what the parent does about them.

**A separate process on purpose, and the reason is the only reason.** The single channel into a
`forkserver` child is the server's *preload list*: the server is `fork`+`exec`'d, so nothing the
test process patches after it starts is visible to any child, and the server itself is a
process-wide singleton that `multiprocessing` starts once and never restarts when the preload list
changes. So a test cannot install these wrappers into the forkserver a sibling test has already
warmed. Running the whole probe as `python -m tests.parse_stalls` gives it a forkserver of its own
and leaves the suite's alone.

What is substituted is only the *child's behaviour* — the axis under test. The parent side is the
shipped `parse_document_isolated`, called exactly as `agent/attachments.py` calls it, on a worker
thread, because the claim is about a thread ending and therefore about a parse slot coming back.

Each case prints one `label|thread_alive|seconds|outcome` line on stdout.
"""

import os
import signal
import subprocess
import sys
import threading
import time
from multiprocessing.connection import Connection

from chemclaw.ingest.documents import isolate
from chemclaw.ingest.documents.parse import ParsedDocument
from chemclaw.ingest.documents.parse import parse_document as _real_parse_document

#: Long enough that nothing here ends on its own inside the probe's own patience.
_FOREVER = 3600.0

#: A sleep duration nothing else on this machine is plausibly running, so the orphan check can
#: identify the grandchild by its command line alone.
_ORPHAN_SECONDS = 611

_real_parse_into = isolate._parse_into


def _parse_into(
    connection: "Connection[object]", name: str, raw: bytes, declared: str | None
) -> None:
    """The child entry point, with one name routed to a message that never finishes.

    `Connection.send` frames a message as a four-byte big-endian length followed by the payload.
    Writing the header and part of the payload is what makes `poll` return True — consuming the
    parent's only deadline before the fix — while `recv` waits for bytes that never come.
    """
    if name == "truncate.txt":
        os.write(connection.fileno(), (64).to_bytes(4, "big") + b"partial")
        time.sleep(_FOREVER)
        return
    _real_parse_into(connection, name, raw, declared)


def _parse_document(name: str, raw: bytes, declared: str | None = None) -> ParsedDocument:
    """The parser, with two names routed to children that outlive their own answer.

    `linger.txt` answers *correctly* and then does not exit, because a non-daemon thread keeps the
    interpreter alive past the child's last statement — the shape a parser that leaves a worker
    behind produces, and the one the unbounded `join()` waited on for ever.

    `grandchild.txt` starts a process of its own and then stalls, so a single-pid kill frees the
    slot while leaving the CPU spent.
    """
    if name == "linger.txt":
        threading.Thread(target=time.sleep, args=(_FOREVER,), daemon=False).start()
        return ParsedDocument(content_type="text/plain", text="answered", rows=0)
    if name == "grandchild.txt":
        subprocess.Popen(["sleep", str(_ORPHAN_SECONDS)])
        time.sleep(_FOREVER)
    if name.endswith("stall.csv"):
        # Never answers and allocates nothing, so the only bound that can end it is the parent's
        # deadline. A large real document ends in the child's memory ceiling instead, which is a
        # second bound racing the first — see `crawl`.
        time.sleep(_FOREVER)
    return _real_parse_document(name, raw, declared)


isolate._parse_into = _parse_into
setattr(isolate, "parse_document", _parse_document)  # noqa: B010


def _drive(name: str, timeout: float, patience: float) -> None:
    """Call the shipped `parse_document_isolated` on a worker thread and report whether it ended."""
    outcome: dict[str, object] = {}

    def _go() -> None:
        started = time.monotonic()
        try:
            outcome["result"] = type(
                isolate.parse_document_isolated(name, b"hello", "text/plain", timeout)
            ).__name__
        except BaseException as exc:
            outcome["result"] = type(exc).__name__
        outcome["seconds"] = round(time.monotonic() - started, 3)

    worker = threading.Thread(target=_go, daemon=True)
    worker.start()
    worker.join(patience)
    print(
        f"{name}|{'alive' if worker.is_alive() else 'ended'}|"
        f"{outcome.get('seconds', 'n/a')}|{outcome.get('result', 'n/a')}",
        flush=True,
    )


def main() -> None:
    """Drive every pathological child, then report whether the grandchild outlived the kill.

    **Leads its own process group and leaves by `os._exit`, and both halves are about the failing
    case.** `multiprocessing`'s exit handler joins every live child without a timeout, so with the
    regression present this process would hang at shutdown *after* printing — and the caller would
    see a timeout rather than the `alive` line the assertion is about, which is the failure shape
    that looks identical to every other. Killing the group takes the forkserver and every stalled
    child with it, so nothing outlives the probe either way.
    """
    os.setsid()
    isolate._PRELOAD = [*isolate._PRELOAD, "tests.parse_stalls"]
    _drive("truncate.txt", timeout=1.0, patience=15.0)
    _drive("linger.txt", timeout=1.0, patience=15.0)
    _drive("grandchild.txt", timeout=1.0, patience=15.0)
    found = subprocess.run(
        ["pgrep", "-f", f"sleep {_ORPHAN_SECONDS}"],
        capture_output=True,
        check=False,
    )
    orphans = [line for line in found.stdout.decode().split() if line]
    print(f"orphans|{len(orphans)}", flush=True)
    sys.stdout.flush()
    for pid in orphans:
        os.kill(int(pid), signal.SIGKILL)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.killpg(os.getpgid(0), signal.SIGTERM)
    os._exit(0)


def crawl(mount: str) -> None:
    """Run the shipped `sync_share` over `mount` with one stalling document, and report the pass.

    The share holds `Docs/quick.txt` and `Docs/stall.csv` (the caller writes them). The stall never
    answers, so its only way out is the kill on `attachment_parse_timeout_seconds`, and the deadline
    can be generous for the quick document without making the stalled one a race. The pass runs on
    a worker thread joined with patience far past that deadline, so "the thread ended" separates a
    killed parse from one nobody kills rather than a fast parse from a slow one.

    Prints one `crawl|<ended>|<skipped_timeout>|<skipped_unreadable>|<indexed>|<kills>|<children>`
    line, then leaves the way `main` does and for its reason.
    """
    import asyncio
    import multiprocessing

    from chemclaw.core.config import settings
    from chemclaw.core.metrics import METRICS
    from chemclaw.ingest.documents.binding import load_binding
    from chemclaw.ingest.documents.index import InMemoryDocumentIndex
    from chemclaw.ingest.documents.sync import SyncReport, sync_share

    os.setsid()
    isolate._PRELOAD = [*isolate._PRELOAD, "tests.parse_stalls"]
    # Start the forkserver, preload and all, before the pass: its start-up is a one-off interpreter
    # launch that a cold probe would otherwise charge to the quick document's deadline — measured
    # here, that alone filed `quick.txt` as timed out on a loaded box.
    isolate.parse_document_isolated("warm.txt", b"warm", "text/plain", 120.0)
    settings.attachment_parse_timeout_seconds = 10.0
    binding = load_binding(
        {
            "mount": mount,
            "roots": [{"path": "Docs"}],
            "public": True,
            "extensions": [".txt", ".csv"],
        }
    )
    before = METRICS.value("chemclaw_document_parse_kills_total")
    reports: list[SyncReport] = []

    def _go() -> None:
        reports.append(
            asyncio.run(sync_share("sharedrive", binding, InMemoryDocumentIndex(), limit=100))
        )

    worker = threading.Thread(target=_go, daemon=True)
    worker.start()
    worker.join(120.0)
    kills = METRICS.value("chemclaw_document_parse_kills_total") - before
    fields: tuple[object, ...] = ("n/a", "n/a", "n/a")
    if reports:
        fields = (reports[0].skipped_timeout, reports[0].skipped_unreadable, reports[0].indexed)
    print(
        f"crawl|{'alive' if worker.is_alive() else 'ended'}|{fields[0]}|{fields[1]}|{fields[2]}|"
        f"{kills:g}|{len(multiprocessing.active_children())}",
        flush=True,
    )
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.killpg(os.getpgid(0), signal.SIGTERM)
    os._exit(0)


if __name__ == "__main__":
    main()
