"""Parse children that misbehave, and the driver that measures what the parent does about them.

Run as `python -m tests.parse_stalls` so it gets a forkserver of its own: the only channel into a
`forkserver` child is the preload list, fixed when the singleton server starts. Only the child's
behaviour is substituted; the parent is the shipped `parse_document_isolated`, called on a worker
thread as `agent/attachments.py` calls it. Each case prints one
`label|thread_alive|seconds|outcome` line.
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

    `Connection.send` frames a four-byte big-endian length then the payload. Writing the header and
    part of the payload makes `poll` return True while `recv` waits forever.
    """
    if name == "truncate.txt":
        os.write(connection.fileno(), (64).to_bytes(4, "big") + b"partial")
        time.sleep(_FOREVER)
        return
    _real_parse_into(connection, name, raw, declared)


def _parse_document(name: str, raw: bytes, declared: str | None = None) -> ParsedDocument:
    """The parser, with two names routed to children that outlive their own answer.

    `linger.txt` answers correctly and then does not exit (a non-daemon thread keeps it alive).
    `grandchild.txt` starts its own process and stalls, so a single-pid kill would leave CPU spent.
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

    Leads its own process group and leaves by `os._exit`: `multiprocessing`'s exit handler joins
    children without a timeout, so a regression would hang after printing. Killing the group takes
    the forkserver and every stalled child with it.
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

    The caller writes `Docs/quick.txt` and `Docs/stall.csv`; the stall exits only by the kill on
    `attachment_parse_timeout_seconds`. The pass runs on a worker thread joined far past that
    deadline, so "the thread ended" separates a killed parse from an unkilled one.

    Prints one `crawl|<ended>|<skipped_timeout>|<skipped_unreadable>|<indexed>|<kills>|<children>`
    line, then leaves the way `main` does.
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
