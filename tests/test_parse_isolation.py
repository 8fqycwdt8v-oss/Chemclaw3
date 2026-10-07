"""What happens to a replica when a parse does not come back.

A parse slot stands for a running worker; if a parse never returned, its slot was never released
and the replica's upload path died silently. `ingest/documents/isolate.py` runs the parse in a
killable child. These tests hold that the work leaves this process, that a parser's refusal
arrives as itself across the boundary, that a parse past its deadline frees its slot for the next
upload, and that `core/netguard.py` does not refuse the forkserver's local `AF_UNIX` socket.
"""

import asyncio
import io
import socket
import subprocess
import sys
import textwrap
import time
import zipfile
from pathlib import Path

import pytest

from chemclaw.agent import attachments
from chemclaw.agent.attachments import (
    AttachmentError,
    AttachmentUnavailable,
    parse_attachment,
    parse_attachment_off_loop,
)
from chemclaw.core import netguard
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from chemclaw.ingest.documents import isolate
from chemclaw.ingest.documents.isolate import (
    ParseWorkerLost,
    parse_context,
    parse_document_isolated,
)
from chemclaw.ingest.documents.parse import (
    DocumentParseError,
    ScannedDocumentError,
    UnclassifiedParseError,
    parse_document,
)
from tests.egress_probe import egress_posture
from tests.test_document_formats import _blank_pdf_bytes  # type: ignore[attr-defined]

# A CSV whose parse clearly outlasts the wedge test's deadline and costs nothing to build. Wide rows
# rather than many narrow ones, so the rendered text stays under `document_parse_memory_bytes` (each
# `str` carries ~50 bytes of header) and the result is a slow parse, not a memory refusal.
_SLOW_CSV = (b",".join([b"a" * 24] * 8) + b"\n") * 100_000

# Above this, a fork round trip is the reason a derived deadline has no room, and no change to
# `_parse_csv` can be. 2.7x the 0.030 s the CI runner measures and a fifth of the 0.165 s median
# this remote sandbox does — see `_budgets_the_slow_fixture_overruns`, which is the only reader.
_FORK_IS_THE_PROBLEM = 0.08

# The marker the three fixture-timed tests skip under when this box cannot express the scenario.
# Spelled once so a run's epilogue and a reader are looking at the same string.
_SLOW_FIXTURE_SKIP = "process creation is too expensive on this box"


def test_a_parse_child_is_still_inside_the_no_egress_posture() -> None:
    """A parse child is still inside the no-egress posture.

    `forkserver` forks and execs, so the child's guard is whatever that fresh interpreter armed: the
    preload imports `chemclaw.ingest.documents.parse` → `chemclaw.core.config`, whose module body
    calls `arm_egress_guard(settings)`. The probe lives in `tests/egress_probe.py`, which imports no
    first-party module, so that chain is the only way the guard can be armed in the child.
    """
    context = parse_context()
    reader, writer = context.Pipe(duplex=False)
    child = context.Process(target=egress_posture, args=(writer,))
    child.start()
    writer.close()
    try:
        outcome, armed = reader.recv()
    finally:
        reader.close()
        child.join()

    assert armed is True, "the egress guard is not armed inside a parse child"
    assert outcome == "refused", (
        f"a parse child's outbound connect was {outcome!r} rather than refused by the egress "
        "guard, so untrusted bytes are parsed in a process outside the no-egress posture"
    )


def _in_process_parse_seconds(raw: bytes) -> float:
    """What `raw` costs to parse in-process, so a deadline can be derived from it."""
    started = time.perf_counter()
    parse_document("slow.csv", raw, None)
    return time.perf_counter() - started


def _fork_round_trip_seconds() -> float:
    """What one fork-and-answer costs, measured here.

    Measuring both the parse and the fork makes deadline comparisons scale-free: a faster runner
    shortens both together.
    """
    _warm_the_forkserver()
    started = time.perf_counter()
    parse_document_isolated("small.csv", b"id,yield\nR-1,88\n", None, 30.0)
    return time.perf_counter() - started


def _budgets_the_slow_fixture_overruns() -> tuple[float, float]:
    """`(cost, deadline)` for `_SLOW_CSV`, derived from what the parse costs on this box.

    The deadline is a quarter of the parse cost, with one fork round trip as the floor (below that
    the child dies before reading a byte). If the ratio collapses because the parse got faster, this
    fails: the downstream tests would no longer be evidence. If it collapses because process
    creation is unusually slow (`_FORK_IS_THE_PROBLEM`), it skips, since that says nothing about
    this code.
    """
    cost = _in_process_parse_seconds(_SLOW_CSV)
    fork = _fork_round_trip_seconds()
    deadline = cost / 4
    if deadline <= fork and fork > _FORK_IS_THE_PROBLEM:
        pytest.skip(
            f"{_SLOW_FIXTURE_SKIP}: a fork round trip costs {fork:.3f}s here against the 0.030s a "
            f"CI runner measures, so a deadline this {cost:.3f}s parse overruns four times over "
            f"({deadline:.3f}s) is under it. The parse is not the problem — process creation is, "
            "and this box cannot express a parse outrunning its deadline at all"
        )
    assert deadline > fork, (
        f"_SLOW_CSV parses in {cost:.3f}s here, so a deadline it overruns four times over is "
        f"{deadline:.3f}s — under the {fork:.3f}s a fork round trip costs, so the child would be "
        "killed before it read a byte and this would be about process creation rather than about a "
        "parse outrunning its deadline. A fork that cheap means the *parse* got faster, which is "
        "what this arm is for. Growing the fixture is not the way out: driven, 40 MB of the same "
        "CSV is refused by `document_parse_memory_bytes` before the deadline is reached."
    )
    return cost, deadline


def test_the_fixture_guard_tells_a_slow_box_from_a_fast_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both arms of the fixture guard's floor, driven with faked clocks.

    The machine-dependent branch must be exercised on purpose, and the failure arm must still fire.
    """
    monkeypatch.setattr("tests.test_parse_isolation._in_process_parse_seconds", lambda raw: 0.200)

    # A slow fork with a healthy parse is the box's fault, so the scenario is inexpressible here.
    monkeypatch.setattr("tests.test_parse_isolation._fork_round_trip_seconds", lambda: 0.165)
    with pytest.raises(pytest.skip.Exception, match=_SLOW_FIXTURE_SKIP):
        _budgets_the_slow_fixture_overruns()

    # A *cheap* fork with no room left means the parse got faster, which must still red the gate.
    monkeypatch.setattr("tests.test_parse_isolation._fork_round_trip_seconds", lambda: 0.060)
    with pytest.raises(AssertionError, match="the .parse. got faster"):
        _budgets_the_slow_fixture_overruns()

    # And a healthy ratio returns the budgets untouched.
    monkeypatch.setattr("tests.test_parse_isolation._fork_round_trip_seconds", lambda: 0.030)
    cost, deadline = _budgets_the_slow_fixture_overruns()
    assert (cost, deadline) == (0.200, 0.050)


def _warm_the_forkserver() -> None:
    """Pay the forkserver's one-off start before a test measures anything.

    The first isolated parse in a process includes starting the server and preloading parsers, which
    is what `attachment_parse_reap_grace_seconds` absorbs and not what tests measure.
    """
    parse_document_isolated("warm.csv", b"a,b\n1,2\n", None, 60.0)


def test_the_parse_does_not_run_in_this_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """The parse runs in another process, proven by breaking this process's copy of it.

    The child imports its own `isolate.parse_document` from the forkserver, so a parse that succeeds
    after this one is replaced happened elsewhere, without adding a pid seam to production code.
    """
    _warm_the_forkserver()

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the parse ran in the calling process")

    monkeypatch.setattr(isolate, "parse_document", refuse)
    parsed = parse_document_isolated("runs.csv", b"id,yield\nR-1,88\n", None, 60.0)
    assert parsed.rows == 1
    assert "R-1" in parsed.text


def test_a_parsers_refusal_crosses_the_process_boundary_as_itself() -> None:
    """A scanned PDF is still a `ScannedDocumentError` across the process boundary.

    The share sync counts scans apart from unsupported formats so an operator sees how much needs
    OCR; a dying child would turn that into process-death counts.
    """
    _warm_the_forkserver()
    with pytest.raises(ScannedDocumentError) as excinfo:
        parse_document_isolated("scan.pdf", _blank_pdf_bytes(), None, 60.0)
    assert "OCR" in str(excinfo.value)


def test_a_parse_past_its_deadline_is_killed_and_counted() -> None:
    """The direct form of the fix: the child is killed, and the kill is visible from a scrape."""
    _warm_the_forkserver()
    _, deadline = _budgets_the_slow_fixture_overruns()
    before = METRICS.value("chemclaw_document_parse_kills_total")
    started = time.monotonic()
    with pytest.raises(ParseWorkerLost):
        parse_document_isolated("slow.csv", _SLOW_CSV, None, deadline)
    elapsed = time.monotonic() - started
    # The caller is freed on the deadline rather than on the parse: the whole point is that the
    # thread ends. Generous on the upper side because sending 20 MB to the child is real work.
    assert elapsed < 2.0, elapsed
    assert METRICS.value("chemclaw_document_parse_kills_total") - before == 1


async def test_a_parse_past_its_deadline_frees_its_slot_for_the_next_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parse past its deadline frees its slot for the next upload.

    One slot, one upload that overruns its deadline, then a small file that must be served. The
    budgets are derived from the fixture's measured parse: the queue wait is shorter than the parse
    (else the second upload would outwait the wedge), and so is the caller's backstop (else an
    in-process parse would finish inside it and succeed for the wrong reason). The floor is a
    measured fork round trip, since the small upload must fit its deadline too. Growing the fixture
    is not an option: it would be refused by `document_parse_memory_bytes` instead.
    """
    _warm_the_forkserver()
    cost, deadline = _budgets_the_slow_fixture_overruns()
    fork = _fork_round_trip_seconds()
    monkeypatch.setattr(settings, "attachment_max_concurrent_parses", 1)
    monkeypatch.setattr(settings, "attachment_parse_timeout_seconds", deadline)
    monkeypatch.setattr(settings, "attachment_parse_reap_grace_seconds", deadline * 2.5)
    # Shorter than the parse, for the reason above: a queue wait past it would let the second
    # upload simply outwait the wedge instead of being shed by it.
    monkeypatch.setattr(settings, "attachment_parse_queue_seconds", cost / 2)
    monkeypatch.setattr(settings, "attachment_max_bytes", len(_SLOW_CSV) + 1)

    with pytest.raises(AttachmentError):
        await parse_attachment_off_loop("slow.csv", _SLOW_CSV)
    # Waited for, not asserted on the next loop turn: the shielded thread releases the slot only
    # after it kills the child, which takes a few tens of milliseconds. Five seconds still separates
    # "comes back" from "never comes back" by orders of magnitude.
    released = time.monotonic() + 5.0
    while attachments._PARSE_SLOTS.in_flight and time.monotonic() < released:
        await asyncio.sleep(0.01)
    assert attachments._PARSE_SLOTS.in_flight == 0, (
        "the slot was still held 5 s after the caller was freed, which is the wedge this test "
        "exists for: a replica with capacity on paper and none in fact"
    )

    # The second upload gets its own deadline: one value cannot both be overrun by the slow parse
    # and fit the small file on every runner. What is asserted is that the slot was free within the
    # queue wait, independent of the deadline.
    monkeypatch.setattr(settings, "attachment_parse_timeout_seconds", cost)
    assert settings.attachment_parse_queue_seconds > 2 * fork, (
        f"the queue wait is {settings.attachment_parse_queue_seconds:.3f}s and a fork round trip "
        f"is {fork:.3f}s, so the assertion below would be measuring process creation rather than "
        "whether the slot came back"
    )

    started = time.monotonic()
    parsed = await parse_attachment_off_loop("small.csv", b"id,yield\nR-1,88\n")
    assert parsed.rows == 1
    # Served, not queued: the slot was free when it arrived. Bounded well under the queue wait
    # so "it eventually got in" cannot pass for "it was never shed".
    assert time.monotonic() - started < settings.attachment_parse_queue_seconds


async def test_the_cap_still_sheds_when_the_slots_are_genuinely_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap still sheds when its slot is genuinely busy.

    Otherwise a cap that returned every slot immediately would pass the test above.
    """
    _warm_the_forkserver()
    monkeypatch.setattr(settings, "attachment_max_concurrent_parses", 1)
    monkeypatch.setattr(settings, "attachment_parse_timeout_seconds", 30.0)
    # Derived for the same reason the other two are: the holder has to still be parsing when this
    # wait elapses, and against a fixture that now costs 0.26-0.36 s the 0.1 s constant that stood
    # here was a 2x margin nobody had looked at since `_parse_csv` got faster.
    cost, _ = _budgets_the_slow_fixture_overruns()
    monkeypatch.setattr(settings, "attachment_parse_queue_seconds", cost / 4)
    monkeypatch.setattr(settings, "attachment_max_bytes", len(_SLOW_CSV) + 1)

    holder = asyncio.create_task(parse_attachment_off_loop("slow.csv", _SLOW_CSV))
    # Let the holder take the slot before the second upload asks for one.
    while attachments._PARSE_SLOTS.in_flight == 0:
        await asyncio.sleep(0.01)
    with pytest.raises(AttachmentUnavailable):
        await parse_attachment_off_loop("second.csv", b"a,b\n1,2\n")
    await holder


def test_local_ipc_is_not_refused_as_egress(tmp_path: Path) -> None:
    """An `AF_UNIX` address names a path and is not refused as egress.

    Asserted on the address reader and on a real `connect` through the armed guard, so re-plumbing
    the guard cannot bypass the unit half.
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


def test_a_child_that_stalls_after_its_first_byte_still_frees_the_worker_thread() -> None:
    """A child that stalls after its first byte still frees the worker thread.

    `poll(timeout)` returning True consumes the deadline; `recv` and `join()` must still be bounded
    and the child killed, or a truncated message or a child that never exits pins the slot forever.
    `agent/attachments.py`'s backstop cannot see this because its wait is shielded. Driven in a
    subprocess via `tests/parse_stalls.py`, because a forkserver child can only be changed through
    the server's preload list. The grandchild case checks the whole process group is killed.
    """
    probe = subprocess.run(
        [sys.executable, "-c", "from tests.parse_stalls import main; main()"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    reported = {
        line.split("|")[0]: line.split("|")[1:] for line in probe.stdout.splitlines() if "|" in line
    }
    assert set(reported) == {"truncate.txt", "linger.txt", "grandchild.txt", "orphans"}, reported
    for case in ("truncate.txt", "linger.txt", "grandchild.txt"):
        assert reported[case][0] == "ended", (
            f"the worker thread for a child that {case} was still alive after 15 s, so its parse "
            f"slot is held for the life of the process: {reported[case]}"
        )
    # The one that answered correctly must still have been answered — a fix that turned every
    # lingering child into a refusal would pass the liveness assertion above and lose a parse.
    assert reported["linger.txt"][2] == "ParsedDocument", reported["linger.txt"]
    assert reported["orphans"][0] == "0", (
        f"{reported['orphans'][0]} grandchild process(es) outlived the kill that freed the slot"
    )


def _workbook_of_shared_strings(references: int, wide: bool) -> bytes:
    r"""A legal `.xlsx` whose text is many times its expanded size, optionally one code point wide.

    Raw OOXML, because a shared string stored once and referenced from many cells is what makes the
    extracted text far exceed the archive; every central-directory `file_size` is true. `wide` adds
    one astral code point, which makes CPython store the whole joined document four bytes per char.

    Args:
        references: How many cells point at the long shared string.
        wide: Whether one further cell holds a single astral code point.

    Returns:
        The workbook's bytes.
    """
    run = ("tetrahydrofuran-4-methoxybenzaldehyde-isolated-yield-" * 19)[:1000]
    strings = [run, "\U0001f9ea"] if wide else [run]
    per_row = 20
    rows = []
    for row in range(1, references // per_row + 1):
        cells = "".join(
            f'<c r="{chr(65 + column)}{row}" t="s"><v>0</v></c>' for column in range(per_row)
        )
        rows.append(f'<row r="{row}">{cells}</row>')
    if wide:
        rows.append(f'<row r="{len(rows) + 1}"><c r="A{len(rows) + 1}" t="s"><v>1</v></c></row>')
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    package = "http://schemas.openxmlformats.org/package/2006/relationships"
    document = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    parts = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
            'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
            'officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/></Types>'
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{package}"><Relationship Id="rId1" '
            f'Type="{document}/officeDocument" Target="xl/workbook.xml"/></Relationships>'
        ),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<workbook xmlns="{main}" xmlns:r="{document}"><sheets>'
            '<sheet name="runs" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{package}">'
            f'<Relationship Id="rId1" Type="{document}/worksheet" Target="worksheets/sheet1.xml"/>'
            f'<Relationship Id="rId2" Type="{document}/sharedStrings" Target="sharedStrings.xml"/>'
            "</Relationships>"
        ),
        "xl/worksheets/sheet1.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<worksheet xmlns="{main}"><sheetData>{"".join(rows)}</sheetData></worksheet>'
        ),
        "xl/sharedStrings.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<sst xmlns="{main}" count="{references + int(wide)}" uniqueCount="{len(strings)}">'
            + "".join(f"<si><t>{one}</t></si>" for one in strings)
            + "</sst>"
        ),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, body in parts.items():
            archive.writestr(path, body)
    return buffer.getvalue()


def _declared_expansion(raw: bytes) -> int:
    """What `_refuse_a_bomb` reads out of the archive's central directory."""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        return sum(item.file_size for item in archive.infolist())


def test_a_legal_upload_cannot_spend_more_than_the_parse_budget_declares() -> None:
    """A legal upload cannot spend more than the parse budget declares.

    This workbook passes every archive bound yet asks for ~200 M characters, enough to OOM-kill the
    pod under concurrency. `document_parse_memory_bytes` is enforced by the kernel on the parsing
    child. Asserted as a refusal because the forkserver child is not ours to `waitpid`, so
    `RUSAGE_CHILDREN` never sees it; the pod-level budget is in
    `tests/test_deploy_chart.py::PARSE_MIB_PER_PARSE_BUDGET_MIB`.
    """
    raw = _workbook_of_shared_strings(200_000, wide=False)
    assert len(raw) < settings.attachment_max_bytes, "the fixture stopped being a legal upload"
    assert _declared_expansion(raw) < settings.document_max_expanded_bytes, (
        "the fixture stopped being legal by the expansion ceiling, which is the whole point of it"
    )
    with pytest.raises(DocumentParseError) as refusal:
        parse_document_isolated("runs.xlsx", raw, None, 120.0)
    assert "memory" in str(refusal.value), (
        "a document refused for its size must say so; the caller cannot act on 'could not be read'"
    )


def test_one_wide_code_point_does_not_multiply_what_a_parse_may_spend() -> None:
    r"""One astral code point does not multiply what a parse may spend.

    `_parse_xlsx` joins the document into one `str`, stored at the width of its widest code point.
    The ASCII twin must still parse, so the bound is not bought by refusing everything.
    """
    narrow = _workbook_of_shared_strings(30_000, wide=False)
    wide = _workbook_of_shared_strings(30_000, wide=True)
    assert len(wide) - len(narrow) < 200, "the two fixtures must differ by one character, not more"

    parsed = parse_document_isolated("narrow.xlsx", narrow, None, 120.0)
    assert parsed.text.isascii() and len(parsed.text) > 30_000_000

    with pytest.raises(DocumentParseError) as refusal:
        parse_document_isolated("wide.xlsx", wide, None, 120.0)
    assert "memory" in str(refusal.value)


def _markup_heavy_docx(paragraphs: int, runs: int) -> bytes:
    """A legal Word report whose cost is its markup rather than its text.

    Every word its own styled run, as Word produces after tracked changes or mixed fonts. The lxml
    DOM cost is in elements, which no archive ceiling predicts.
    """
    body = "".join(
        "<w:p><w:pPr><w:jc w:val='both'/></w:pPr>"
        + "".join(
            '<w:r><w:rPr><w:b/><w:color w:val="1F4E79"/><w:sz w:val="22"/></w:rPr>'
            f'<w:t xml:space="preserve">word{run} </w:t></w:r>'
            for run in range(runs)
        )
        + "</w:p>"
        for _ in range(paragraphs)
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}<w:sectPr/></w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
            'content-types"><Default Extension="xml" ContentType="application/xml"/>'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
            'relationships+xml"/><Override PartName="/word/document.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/'
            '2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            "</Relationships>",
        )
        archive.writestr(
            "word/_rels/document.xml.rels",
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/'
            '2006/relationships"/>',
        )
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def test_a_document_stopped_by_the_budget_says_so_even_when_a_c_parser_reported_it() -> None:
    """A `.docx` stopped by the budget gets the memory refusal even when lxml reported it.

    lxml reports its own allocation failure rather than raising `MemoryError`, so `_at_ceiling`
    classifies it; otherwise a legal report would be called malformed at line 0. Both arms: a truly
    unreadable document keeps its own message (it fails with its allowance nearly unspent).
    """
    raw = _markup_heavy_docx(2_000, 200)
    assert len(raw) < settings.attachment_max_bytes, "the fixture stopped being a legal upload"
    assert _declared_expansion(raw) < settings.document_max_expanded_bytes, (
        "the fixture stopped being legal by the expansion ceiling, which is the point of it"
    )

    with pytest.raises(DocumentParseError) as refusal:
        parse_document_isolated("report.docx", raw, None, 120.0)
    assert "memory" in str(refusal.value), (
        f"a document the budget stopped was refused as {str(refusal.value)!r}, which reads as "
        "'your file is malformed' and names neither the ceiling nor the knob that moves it"
    )

    broken = io.BytesIO()
    with zipfile.ZipFile(broken, "w") as archive:
        archive.writestr("word/document.xml", "<w:document><not closed")
    with pytest.raises(DocumentParseError) as unreadable:
        parse_document_isolated("broken.docx", broken.getvalue(), None, 120.0)
    assert "memory" not in str(unreadable.value), (
        "a genuinely unreadable document was blamed on the memory budget, so the assertion above "
        "proves nothing — every refusal would pass it"
    )


def test_an_ambient_hard_limit_below_the_budget_is_a_smaller_budget_not_a_dead_parser() -> None:
    """An ambient hard `RLIMIT_DATA` below the budget becomes a smaller budget, not a dead parser.

    `setrlimit` cannot raise a maximum, and the resulting `ValueError` would make every document of
    every format unreadable. Driven in a subprocess because a process cannot restore its lowered
    hard limit.
    """
    probe = textwrap.dedent(
        """
        import resource, sys
        from chemclaw.ingest.documents.isolate import _anonymous_bytes, _bound_allocations

        base = _anonymous_bytes()
        tight = base + 8 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_DATA, (tight, tight))
        ceiling, hard = _bound_allocations(160 * 1024 * 1024)
        print("CLAMPED" if ceiling == tight and hard == tight else f"UNEXPECTED:{ceiling},{hard}")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, (
        "a hard RLIMIT_DATA below the parse budget took the bound out with an exception, so every "
        f"document of every format is refused as unreadable: {result.stderr[-400:]}"
    )
    assert "CLAMPED" in result.stdout, (
        "the ambient ceiling did not become the budget; a lower platform limit is a smaller "
        f"budget, which is what this knob is for: {result.stdout!r}"
    )


def _markup_heavy_pptx(slides: int, runs: int) -> bytes:
    """A legal deck whose cost is its markup, the `.pptx` twin of `_markup_heavy_docx`."""
    from pptx import Presentation
    from pptx.util import Inches, Pt

    deck = Presentation()
    blank = deck.slide_layouts[6]
    for _ in range(slides):
        slide = deck.slides.add_slide(blank)
        frame = slide.shapes.add_textbox(Inches(0.2), Inches(0.2), Inches(9), Inches(6)).text_frame
        paragraph = frame.paragraphs[0]
        for index in range(runs):
            run = paragraph.add_run()
            run.text = f"word{index} "
            run.font.bold = True
            run.font.size = Pt(11)
    buffer = io.BytesIO()
    deck.save(buffer)
    return buffer.getvalue()


def test_a_deck_stopped_by_the_budget_earns_the_same_named_refusal_a_document_does() -> None:
    """A `.pptx` stopped by the budget earns the same named refusal a `.docx` does.

    `python-pptx` uses the same lxml layer. Both arms: a truly unreadable deck keeps its own
    message.
    """
    raw = _markup_heavy_pptx(600, 600)
    assert len(raw) < settings.attachment_max_bytes, "the fixture stopped being a legal upload"

    with pytest.raises(DocumentParseError) as refusal:
        parse_document_isolated("deck.pptx", raw, None, 120.0)
    assert "memory" in str(refusal.value), (
        f"a deck the budget stopped was refused as {str(refusal.value)!r}, which names neither the "
        "ceiling nor the knob that moves it"
    )

    broken = io.BytesIO()
    with zipfile.ZipFile(broken, "w") as archive:
        archive.writestr("ppt/presentation.xml", "<p:presentation><not closed")
    with pytest.raises(DocumentParseError) as unreadable:
        parse_document_isolated("broken.pptx", broken.getvalue(), None, 120.0)
    assert "memory" not in str(unreadable.value), (
        "a genuinely unreadable deck was blamed on the memory budget, so the assertion above "
        "proves nothing"
    )


def test_a_refusal_is_not_bounded_by_the_ceiling_that_caused_it() -> None:
    """The reply is released from the ceiling before it is sent.

    A `MemoryError` raised inside an `except` clause reaches no later arm, so failing to pickle the
    refusal would arrive as a nameless EOF. `_release_allocations` lifts the ceiling once the parse
    is over; asserted on that mechanism, since a real allocation failure in a handler cannot be
    staged honestly.
    """
    probe = textwrap.dedent(
        """
        import resource
        from chemclaw.ingest.documents.isolate import _bound_allocations, _release_allocations

        before = resource.getrlimit(resource.RLIMIT_DATA)
        ceiling, hard = _bound_allocations(64 * 1024 * 1024)
        bounded = resource.getrlimit(resource.RLIMIT_DATA)[0]
        _release_allocations(hard)
        after = resource.getrlimit(resource.RLIMIT_DATA)[0]
        print("BOUNDED" if bounded == ceiling else f"NOT-BOUNDED:{bounded}")
        print("RELEASED" if after == hard and after != bounded else f"STILL-BOUND:{after}")
        same = resource.getrlimit(resource.RLIMIT_DATA)[1] == before[1]
        print("UNCHANGED-HARD" if same else "HARD-MOVED")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, f"the probe did not run: {result.stderr[-400:]}"
    assert "BOUNDED" in result.stdout, f"the parse was never bounded: {result.stdout!r}"
    assert "RELEASED" in result.stdout, (
        "the ceiling still stood after the failure arm released it, so a refusal is pickled under "
        f"the budget that stopped the parse: {result.stdout!r}"
    )
    assert "UNCHANGED-HARD" in result.stdout, (
        "releasing moved the hard limit, which is irreversible for the process and is not what "
        f"this is for: {result.stdout!r}"
    )


def _zip_with_truncated_markup() -> bytes:
    """A structurally valid `.docx` container whose `word/document.xml` stops mid-element.

    The archive opens and `_refuse_a_bomb` has nothing to say; lxml fails for a reason no caller can
    name, as with an interrupted copy on a share.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as container:
        container.writestr("[Content_Types].xml", "<?xml version='1.0'?><Types/>")
        container.writestr("word/document.xml", "<?xml version='1.0'?><w:document><w:body>")
    return buffer.getvalue()


def _zip_that_declares_too_much() -> bytes:
    """A `.docx` whose central directory declares more expansion than the ceiling allows.

    Deflated zeroes, so it is ~64 KiB on disk for 64 MiB declared — `_refuse_a_bomb` reads the
    declared `file_size` and never decompresses, which is the whole point of refusing there.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as container:
        container.writestr("[Content_Types].xml", "<?xml version='1.0'?><Types/>")
        container.writestr("word/document.xml", b"\0" * (settings.document_max_expanded_bytes + 1))
    return buffer.getvalue()


def test_an_unbounded_parse_does_not_blame_the_document_for_what_it_cannot_know() -> None:
    """An unbounded parse does not blame the document for what it cannot know.

    `parse_attachment` (used by `cli/backfill_corpus.py` and format tests) sets no ceiling, so an
    unknown C-parser failure may be memory or malformation; its refusal says so and keeps the
    parser's words for operators (`D-2026-09-22-an-unbounded-parse-may-not-blame-the-document`). The
    input must be a valid zip with truncated markup: a non-zip is refused by `_refuse_a_bomb`, a
    classified fact that never reaches this arm.
    """
    with pytest.raises(UnclassifiedParseError) as refused:
        parse_attachment("report.docx", _zip_with_truncated_markup())

    message = str(refused.value)
    assert "could not read report.docx" in message, (
        f"the parser's own words must survive — an operator needs them: {message}"
    )
    assert "not established here" in message, (
        "the refusal reads as a verdict on the document, which is the one thing an unbounded parse "
        f"cannot establish: {message}"
    )
    assert "no memory ceiling" in message, message
    assert ".." not in message, (
        f"the caveat was appended to a cause that already ended in a period: {message}"
    )
    assert message.count("report.docx") == 1, (
        f"the document is named twice in one refusal: {message}"
    )


def test_a_classified_refusal_is_not_buried_under_a_caveat_about_memory() -> None:
    """A classified refusal is not buried under a caveat about memory.

    Unsupported format, not-a-zip and scanned PDF are facts about the document. Every named
    population is driven, not one representative.
    """
    populations = {
        "unsupported format": (("notes.zzz", b"hello"), "not a supported format"),
        "not a zip container": (("report.docx", b"not a zip at all"), "File is not a zip file"),
        "over-expanding archive": (("bomb.docx", _zip_that_declares_too_much()), "past the"),
        "scanned PDF": (("scan.pdf", _blank_pdf_bytes()), "so it is a scan"),
    }
    for population, ((name, raw), expected) in populations.items():
        with pytest.raises(DocumentParseError) as refused:
            parse_attachment(name, raw)
        message = str(refused.value)
        assert expected in message, f"{population}: {message}"
        assert not isinstance(refused.value, UnclassifiedParseError), (
            f"{population} is a classified fact and must not carry the unclassified type: {message}"
        )
        assert "not established here" not in message, (
            f"{population} grew the unbounded caveat: {message}"
        )


def test_only_the_unknown_failures_carry_the_type_the_caveat_keys_on() -> None:
    """Only the unknown failures carry `UnclassifiedParseError`, the type the caveat keys on.

    Callers branch on the type, never on message text. Derived over `except` handlers: every broad
    handler (bare, `Exception`, `BaseException`) must raise this type and no narrow handler may, so
    a new broad arm or a misclassified narrow one is red.
    """
    import ast

    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "chemclaw"
        / "ingest"
        / "documents"
        / "parse.py"
    )
    tree = ast.parse(source.read_text(encoding="utf-8"))

    def raises_unclassified(handler: ast.ExceptHandler) -> bool:
        return any(
            isinstance(node, ast.Raise)
            and isinstance(node.exc, ast.Call)
            and isinstance(node.exc.func, ast.Name)
            and node.exc.func.id == "UnclassifiedParseError"
            for node in ast.walk(handler)
        )

    def is_broad(handler: ast.ExceptHandler) -> bool:
        if handler.type is None:
            return True
        return isinstance(handler.type, ast.Name) and handler.type.id in {
            "Exception",
            "BaseException",
        }

    handlers = [node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)]
    assert handlers, "parse.py has no `except` handlers at all, so this derivation is measuring air"

    uncovered = [h.lineno for h in handlers if is_broad(h) and not raises_unclassified(h)]
    assert not uncovered, (
        f"broad `except` arms at lines {uncovered} do not raise UnclassifiedParseError, so a "
        "failure this system cannot explain is reported as a verdict on the document"
    )
    misplaced = [h.lineno for h in handlers if not is_broad(h) and raises_unclassified(h)]
    assert not misplaced, (
        f"narrow `except` arms at lines {misplaced} raise UnclassifiedParseError, so a classified "
        "refusal is about to be buried under a caveat about memory it does not need"
    )
    assert issubclass(UnclassifiedParseError, DocumentParseError), (
        "it must stay a DocumentParseError or every existing handler — the share sync's "
        "reject-and-continue net, the upload route, the isolate child — stops catching it"
    )
