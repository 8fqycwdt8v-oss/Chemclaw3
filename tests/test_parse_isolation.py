"""What happens to a replica when a parse does not come back.

`agent/attachments.py` caps how many uploads may be parsed at once and sheds the rest, and that cap
was real. What it could not do was ever get a slot *back*: a slot stands for a running worker
thread and is released by that thread's completion callback, so a parse that never returns held its
slot for the life of the process. Driven before the fix, at the shipped cap of 2: both callers were
freed at their timeout, `in_flight` was still 2 five seconds later, and every later upload was shed.
The upload path of that replica was down permanently and nothing said so.

These tests hold the fix — `ingest/documents/isolate.py` — from both ends: that the work really
leaves this process, that a parser's own refusal still arrives as itself across that boundary, and
that a parse past its deadline costs a slot for the deadline rather than for the process. The last
one is the regression test for the wedge, and it is written as "the *next* upload is served"
because that is the property a chemist experiences; `in_flight` is the mechanism, not the promise.

The fourth is one layer down and was found by driving the third: `core/netguard.py` refused the
`AF_UNIX` socket the forkserver talks to itself over, while its own docstring said it exempted
local IPC. Nothing asserted it in either direction, and the C half of the same control
(`netguard_preload.c`) had it right — it reads `sa_family` and checks only the internet families.
"""

import asyncio
import socket
import time
from pathlib import Path

import pytest

from chemclaw.agent import attachments
from chemclaw.agent.attachments import (
    AttachmentError,
    AttachmentUnavailable,
    parse_attachment_off_loop,
)
from chemclaw.core import netguard
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from chemclaw.ingest.documents import isolate
from chemclaw.ingest.documents.isolate import ParseWorkerLost, parse_document_isolated
from chemclaw.ingest.documents.parse import ScannedDocumentError
from tests.test_document_formats import _blank_pdf_bytes  # type: ignore[attr-defined]

# A CSV big enough that parsing it is unmistakably longer than the deadline the wedge test sets,
# and small enough that building it costs nothing. Measured on this tree: 6 MB parses in 0.694 s,
# so 20 MB is ~2.3 s against a 0.2 s deadline — a factor of ten, not a race.
_SLOW_CSV = b"aaaa,bbbb,cccc,dddd\n" * 1_000_000


def _warm_the_forkserver() -> None:
    """Pay the forkserver's one-off start before a test measures anything.

    Measured: the first isolated parse in a process is ~0.93 s (the server is exec'd and preloads
    the parsers) and every one after it is ~0.04 s under pytest, ~0.01 s from a plain entry point.
    A test that timed a *first* parse would be timing that start-up, which is exactly the number
    `attachment_parse_reap_grace_seconds` exists to absorb and not the number under test.
    """
    parse_document_isolated("warm.csv", b"a,b\n1,2\n", None, 60.0)


def test_the_parse_does_not_run_in_this_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """The work crosses a process boundary, proven by breaking this process's copy of it.

    `isolate.parse_document` is the name `_parse_into` calls. Replacing it here would stop any
    in-process parse dead; the child imports its own copy from the forkserver, which never saw this
    assignment, so a parse that still succeeds could only have happened somewhere else.

    Asserted this way rather than by comparing pids because a pid would have to be smuggled back
    through the result — a test seam in production code to prove a property the code already has.
    """
    _warm_the_forkserver()

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the parse ran in the calling process")

    monkeypatch.setattr(isolate, "parse_document", refuse)
    parsed = parse_document_isolated("runs.csv", b"id,yield\nR-1,88\n", None, 60.0)
    assert parsed.rows == 1
    assert "R-1" in parsed.text


def test_a_parsers_refusal_crosses_the_process_boundary_as_itself() -> None:
    """A scanned PDF is still a `ScannedDocumentError`, not a generic "the reader died".

    This is what the tagged pair on the pipe buys. An exception raised in the child and left to
    kill it would reach the parent as an exit code, and the share sync — which counts scans apart
    from unsupported formats so an operator can see how much of a corpus needs OCR — would have
    been counting process deaths.
    """
    _warm_the_forkserver()
    with pytest.raises(ScannedDocumentError) as excinfo:
        parse_document_isolated("scan.pdf", _blank_pdf_bytes(), None, 60.0)
    assert "OCR" in str(excinfo.value)


def test_a_parse_past_its_deadline_is_killed_and_counted() -> None:
    """The direct form of the fix: the child is killed, and the kill is visible from a scrape."""
    _warm_the_forkserver()
    before = METRICS.value("chemclaw_document_parse_kills_total")
    started = time.monotonic()
    with pytest.raises(ParseWorkerLost):
        parse_document_isolated("slow.csv", _SLOW_CSV, None, 0.2)
    elapsed = time.monotonic() - started
    # The caller is freed on the deadline rather than on the parse: the whole point is that the
    # thread ends. Generous on the upper side because sending 20 MB to the child is real work.
    assert elapsed < 2.0, elapsed
    assert METRICS.value("chemclaw_document_parse_kills_total") - before == 1


def test_a_parse_past_its_deadline_frees_its_slot_for_the_next_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression test for the wedge, stated as what a chemist experiences.

    One slot, one upload that will not finish inside its deadline, and then an ordinary small file.
    Before `ingest/documents/isolate.py` the slot was held until the parse finished — so the second
    upload waited `attachment_parse_queue_seconds` and came back as a retryable 503 about a file
    that parses in milliseconds. Driven against that arrangement this test fails with
    `AttachmentUnavailable`, which is the shape of the production failure: uploads refused by a pod
    that has capacity on paper.

    Three of the four budgets are set against the ~2.3 s parse rather than against each other, so
    that the defect cannot hide behind any of them. The queue wait is shorter than the parse: if it
    were longer the second upload would simply outwait the wedge. The caller's backstop is shorter
    than the parse too, and the shipped 5 s is not — with that in place an in-process parse finishes
    *inside* the backstop and comes back as a success, so the mutation would be caught by the wrong
    assertion and this test would not be evidence about slots at all.
    """
    _warm_the_forkserver()
    monkeypatch.setattr(settings, "attachment_max_concurrent_parses", 1)
    monkeypatch.setattr(settings, "attachment_parse_timeout_seconds", 0.2)
    monkeypatch.setattr(settings, "attachment_parse_reap_grace_seconds", 0.5)
    monkeypatch.setattr(settings, "attachment_parse_queue_seconds", 0.5)
    monkeypatch.setattr(settings, "attachment_max_bytes", len(_SLOW_CSV) + 1)

    async def _drive() -> None:
        with pytest.raises(AttachmentError):
            await parse_attachment_off_loop("slow.csv", _SLOW_CSV)
        # The release rides `Future.add_done_callback`, which asyncio dispatches with `call_soon`,
        # so it lands on the next turn of the loop rather than before this coroutine resumes.
        await asyncio.sleep(0)
        assert attachments._PARSE_SLOTS.in_flight == 0

        started = time.monotonic()
        parsed = await parse_attachment_off_loop("small.csv", b"id,yield\nR-1,88\n")
        assert parsed.rows == 1
        # Served, not queued: the slot was free when it arrived. Bounded well under the queue wait
        # so "it eventually got in" cannot pass for "it was never shed".
        assert time.monotonic() - started < settings.attachment_parse_queue_seconds

    asyncio.run(_drive())


def test_the_cap_still_sheds_when_the_slots_are_genuinely_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other direction, so the test above cannot pass by the cap having quietly stopped working.

    A cap that returns every slot immediately would satisfy "the next upload is served" and protect
    nothing. Here the one slot is held by a parse that is still inside its deadline, and the second
    upload must still be shed with the retryable refusal.
    """
    _warm_the_forkserver()
    monkeypatch.setattr(settings, "attachment_max_concurrent_parses", 1)
    monkeypatch.setattr(settings, "attachment_parse_timeout_seconds", 30.0)
    monkeypatch.setattr(settings, "attachment_parse_queue_seconds", 0.1)
    monkeypatch.setattr(settings, "attachment_max_bytes", len(_SLOW_CSV) + 1)

    async def _drive() -> None:
        holder = asyncio.create_task(parse_attachment_off_loop("slow.csv", _SLOW_CSV))
        # Let the holder take the slot before the second upload asks for one.
        while attachments._PARSE_SLOTS.in_flight == 0:
            await asyncio.sleep(0.01)
        with pytest.raises(AttachmentUnavailable):
            await parse_attachment_off_loop("second.csv", b"a,b\n1,2\n")
        await holder

    asyncio.run(_drive())


def test_local_ipc_is_not_refused_as_egress(tmp_path: Path) -> None:
    """An `AF_UNIX` address names a path, and a path leaves this host by no route.

    `netguard._host_of` fell through to `host = address` for a non-tuple address, so a unix-socket
    connect arrived at `_check` as a hostname, failed `is_loopback_host` and was refused — while
    the same function's docstring said it returned None for exactly this case. Measured: every
    forkserver connect to `/tmp/pymp-*/listener-*` was logged as "outbound connection … is not on
    the allowlist" and raised `EgressForbidden`, which is why the isolated parse could not run at
    all until this was fixed.

    Asserted at both levels — the address reader, and a real `connect` through the armed guard —
    because the unit half alone would still pass if the guard were later re-plumbed to read the
    address somewhere else.
    """
    assert netguard._host_of("/tmp/pymp-abc/listener-def") is None
    assert netguard._host_of(b"\x00an-abstract-name") is None
    # An internet address is still read, in both spellings, so this is a narrowing and not a hole.
    assert netguard._host_of(("example.invalid", 443)) == "example.invalid"
    assert netguard._host_of((b"example.invalid", 443)) == "example.invalid"

    path = str(tmp_path / "probe.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(path)
        server.listen(1)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(path)  # refused with EgressForbidden before the fix
            accepted, _ = server.accept()
            accepted.close()
