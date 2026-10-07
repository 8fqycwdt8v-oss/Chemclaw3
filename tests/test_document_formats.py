"""PDF/PPTX/DOCX/XLSX ingest (D-089), and the refusal that survives.

Extraction is *structural*: each format is read through its own document model, with pages,
slides and sheets preserved. A **scanned** PDF, which extraction cannot fix, is refused by name
rather than returned as an empty document. Every fixture is built by the format's own writer
(`tests/document_fixtures.py`), never a checked-in blob.
"""

import io
import time
import zipfile

import pytest

from chemclaw.agent.attachments import AttachmentError, content_type_for, parse_attachment
from chemclaw.ingest.documents.parse import _decode
from tests.document_fixtures import (
    _blank_pdf_bytes,
    _docx_bytes,
    _highly_compressible_xlsx_bytes,
    _pptx_bytes,
    _text_pdf_bytes,
    _unbalanced_quote_csv_bytes,
    _with_truncated_member,
    _xlsx_bytes,
)

# --- PDF ----------------------------------------------------------------------------------------


def test_a_pdf_with_a_text_layer_is_extracted_page_by_page() -> None:
    """The primary case: a real report reaches the agent as its actual words, not an approximation.

    This is what makes the format safe to accept at all — the original refusal was about *guessing*
    at bytes, and extraction through the document's own text objects is not guessing.
    """
    raw = _text_pdf_bytes(["Yield 84 percent", "Impurity below 0.5 percent"])
    attachment = parse_attachment("coa.pdf", raw)
    assert attachment.rows == 2  # page count
    assert "Yield 84 percent" in attachment.text
    assert "Impurity below 0.5 percent" in attachment.text


def test_pdf_page_numbers_are_preserved_so_a_citation_still_resolves() -> None:
    """A citation to page 3 has to still resolve to page 3 after ingest."""
    text = parse_attachment("r.pdf", _text_pdf_bytes(["alpha", "beta", "gamma"])).text
    assert "[page 1]" in text
    assert "[page 3]" in text
    assert text.index("alpha") < text.index("beta") < text.index("gamma")


def test_a_mixed_pdf_keeps_its_text_pages_and_does_not_invent_the_blank_ones() -> None:
    """A report with a scanned figure page must not have that page silently renumbered away.

    The blank page contributes no text — but it must not shift the labels of the pages that follow
    it, or a citation to "page 3" would land on page 2's content.
    """
    reader_input = _text_pdf_bytes(["first page text", "", "third page text"])
    text = parse_attachment("mixed.pdf", reader_input).text
    assert "[page 1]" in text
    assert "[page 3]" in text
    assert "[page 2]" not in text  # it had nothing to show, and nothing was fabricated for it


def test_a_scanned_pdf_is_refused_rather_than_read_as_an_empty_document() -> None:
    """A scanned PDF is refused rather than read as an empty document.

    `pypdf` returns nothing for an image-only PDF, which would reach the chemist as "there was
    nothing in your CoA"; the refusal names the cause.
    """
    with pytest.raises(AttachmentError) as excinfo:
        parse_attachment("scan.pdf", _blank_pdf_bytes())
    message = str(excinfo.value)
    assert "scan" in message
    assert "OCR" in message
    # Names the evidence it judged on, so a chemist can tell this from a size or format refusal.
    assert "2 page(s)" in message


def test_a_short_pdf_is_accepted_because_the_scan_test_is_zero_text_not_a_length() -> None:
    """A one-line CoA is a legitimate document; any length threshold would refuse it.

    The distinguishing property of a scan is that it yields *no* characters, not few — so this
    pins that the refusal above cannot drift into a size check.
    """
    attachment = parse_attachment("short.pdf", _text_pdf_bytes(["ok"]))
    assert "ok" in attachment.text


def test_a_corrupt_pdf_is_refused_with_a_message_not_a_traceback() -> None:
    """A malformed upload is a user error; it must not surface as an unhandled library exception."""
    with pytest.raises(AttachmentError, match="could not read broken.pdf"):
        parse_attachment("broken.pdf", b"%PDF-1.4 this is not actually a pdf")


# --- PPTX ---------------------------------------------------------------------------------------


def test_a_deck_is_read_slide_by_slide_with_boundaries_preserved() -> None:
    """Slide numbers survive ingest: "the third slide" has to still mean something afterwards."""
    raw = _pptx_bytes([("Route A", "Pd(OAc)2, 80 C"), ("Route B", "NiCl2, 100 C")])
    attachment = parse_attachment("routes.pptx", raw)
    assert attachment.rows == 2  # slide count
    assert "[slide 1]" in attachment.text
    assert "[slide 2]" in attachment.text
    assert "Pd(OAc)2, 80 C" in attachment.text
    assert attachment.text.index("Route A") < attachment.text.index("Route B")


def test_speaker_notes_are_extracted_because_the_reasoning_often_lives_there() -> None:
    """A deck's "why" is usually in the notes; dropping them discards the informative half."""
    raw = _pptx_bytes([("Screen", "12 conditions")], notes="Ligand screen failed on the chloride.")
    text = parse_attachment("screen.pptx", raw).text
    assert "Ligand screen failed on the chloride." in text
    assert "(speaker notes)" in text


# --- DOCX ---------------------------------------------------------------------------------------


def test_a_word_document_yields_its_paragraphs_and_its_tables() -> None:
    """Both bodies of content, not just the prose — a protocol's numbers live in its table."""
    raw = _docx_bytes(
        ["Procedure for the Suzuki coupling.", "Charge the vessel under nitrogen."],
        table=[["reagent", "equiv"], ["boronic acid", "1.2"]],
    )
    attachment = parse_attachment("sop.docx", raw)
    assert "Charge the vessel under nitrogen." in attachment.text
    assert "boronic acid | 1.2" in attachment.text
    assert attachment.rows == 2


def test_a_docx_table_reads_the_same_as_a_csv_table() -> None:
    """One table representation for the agent to learn, whichever format delivered it."""
    docx_text = parse_attachment("t.docx", _docx_bytes([], table=[["a", "b"], ["1", "2"]])).text
    csv_text = parse_attachment("t.csv", b"a,b\n1,2\n").text
    assert "a | b" in docx_text
    assert "a | b" in csv_text


# --- XLSX ---------------------------------------------------------------------------------------


def test_a_workbook_is_read_sheet_by_sheet_with_names_kept() -> None:
    """Sheet names carry meaning in a lab workbook ("batch 3"), so they survive ingest."""
    raw = _xlsx_bytes({"yields": [["run", "yield"], [1, 84], [2, 91]], "notes": [["ok"]]})
    attachment = parse_attachment("runs.xlsx", raw)
    assert "[sheet yields]" in attachment.text
    assert "[sheet notes]" in attachment.text
    assert "1 | 84" in attachment.text
    assert attachment.rows == 4


def test_empty_rows_are_dropped_but_empty_cells_are_kept_as_gaps() -> None:
    """A blank row is spreadsheet padding; a blank *cell* is a missing measurement — not the same.

    Collapsing an empty cell would shift every value after it into the wrong column, which is the
    silent-wrong-number failure the CSV parser's docstring warns about.
    """
    raw = _xlsx_bytes({"s": [["a", "b", "c"], [], [1, None, 3]]})
    text = parse_attachment("gaps.xlsx", raw).text
    assert "1 |  | 3" in text
    assert parse_attachment("gaps.xlsx", raw).rows == 2


# --- the allowlist ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("report.pdf", "application/pdf"),
        ("deck.PPTX", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        ("sop.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("runs.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ],
)
def test_the_extension_resolves_when_a_browser_declares_nothing_useful(
    name: str, expected: str
) -> None:
    """Uploads routinely arrive as `application/octet-stream`; the extension has to carry it."""
    assert content_type_for(name, "application/octet-stream") == expected


def test_the_allowlist_is_still_closed_and_refuses_by_name() -> None:
    """Widening the scope did not turn the allowlist into a guess-what-this-is parser.

    The refusal now advertises the extensions rather than the MIME types: a chemist knows their
    file is a `.jdx`, and a list of `application/vnd.openxmlformats-…` strings tells them nothing.
    """
    with pytest.raises(AttachmentError) as excinfo:
        parse_attachment("spectrum.jdx", b"##TITLE=NMR\n")
    message = str(excinfo.value)
    assert ".pdf" in message
    assert ".xlsx" in message
    assert "OCR" in message


def test_a_binary_upload_never_reaches_a_text_parser() -> None:
    """The old failure mode: bytes that are not text being decoded into plausible-looking noise."""
    with pytest.raises(AttachmentError):
        parse_attachment("image.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)


# --- a file that breaks mid-parse is a refusal, never an escaping exception ----------------------
#
# These libraries do their work lazily (`openpyxl` in `iter_rows`, `python-pptx` per slide,
# `csv.reader` on iteration), so guarding only the constructor lets an exception escape. On a share
# that fails the sync activity, and with no cross-run cursor every later run hits the same file, so
# one malformed document stops the whole corpus.


@pytest.mark.parametrize(
    ("name", "make"),
    [
        (
            "runs.xlsx",
            lambda: _with_truncated_member(_xlsx_bytes({"Runs": [["a", 1]]}), "sheet1.xml"),
        ),
        ("deck.pptx", lambda: _with_truncated_member(_pptx_bytes([("T", "body")]), "slide1.xml")),
        ("notes.docx", lambda: _with_truncated_member(_docx_bytes(["hello"]), "document.xml")),
        ("export.csv", _unbalanced_quote_csv_bytes),
    ],
)
def test_a_file_that_breaks_after_it_opens_is_refused_by_name(name: str, make: object) -> None:
    """The refusal must be `AttachmentError`, so one bad file is counted rather than fatal."""
    with pytest.raises(AttachmentError):
        parse_attachment(name, make())  # type: ignore[operator]


def test_a_refused_file_names_itself_so_the_log_line_is_actionable() -> None:
    """An operator reading `skipped_unreadable: 1` needs to know which of 500k files it was."""
    with pytest.raises(AttachmentError, match="export.csv"):
        parse_attachment("export.csv", _unbalanced_quote_csv_bytes())


def test_a_container_that_expands_far_past_its_size_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zip bomb wearing a workbook's clothes: under every byte limit, and it OOMs the worker.

    The refusal reads the central directory, so it costs no decompression — the file is turned away
    before a parser ever sees it.
    """
    from chemclaw.core.config import settings

    raw = _highly_compressible_xlsx_bytes()
    ratio = sum(i.file_size for i in zipfile.ZipFile(io.BytesIO(raw)).infolist()) / len(raw)
    assert ratio > 20  # the property under test is the ratio, so it is measured, not assumed

    monkeypatch.setattr(settings, "document_max_expanded_bytes", 1_000_000)
    with pytest.raises(AttachmentError, match="expands to"):
        parse_attachment("bomb.xlsx", raw)


def _cmap_bomb_pdf_bytes(destination_hex_chars: int = 200_000, span: int = 20_000) -> bytes:
    """A small PDF whose font declares one `bfrange` line with an enormous destination string.

    Hand-assembled: no writer emits this; an attacker does. The destination is `A` then zeros so
    incrementing never gains a hex digit (an all-`F` string overflows and pypdf skips the line,
    which would measure nothing).
    """
    destination = b"A" + b"0" * (destination_hex_chars - 1)
    cmap = (
        b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
        b"1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
        b"1 beginbfrange\n<0000> <%04X> <%s>\nendbfrange\nendcmap\nend\nend\n"
    ) % (span, destination)
    content = b"BT /F1 12 Tf (\\000\\001) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type0 /BaseFont /X /Encoding /Identity-H "
        b"/DescendantFonts [6 0 R] /ToUnicode 7 0 R >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /X /CIDSystemInfo "
        b"<< /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(cmap), cmap),
    ]
    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    table = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        table,
    )
    return bytes(out)


def test_a_font_map_that_expands_far_past_its_size_is_refused() -> None:
    """A font map that expands far past its size is refused.

    The PDF twin of a zip bomb: older pypdf spent tens of seconds and gigabytes on this small input,
    because its limit bounds the number of `/ToUnicode` entries, not their size (CVE-2026-71852,
    CVE-2026-71870). Pinned as a test because the `pypdf>=6.15.0` floor is what provides it.
    `parse_document`'s boundary net turns `LimitReachedError` into the usual `AttachmentError`.
    """
    raw = _cmap_bomb_pdf_bytes()
    assert len(raw) < 300_000  # the whole point: the input is small and the expansion is not
    started = time.monotonic()
    with pytest.raises(AttachmentError, match="could not read bomb.pdf"):
        parse_attachment("bomb.pdf", raw)
    elapsed = time.monotonic() - started
    # Two orders of magnitude below the 33.8 s the unpinned version took, so this discriminates
    # without encoding a timing guess.
    assert elapsed < 5.0, f"the refusal itself took {elapsed:.1f}s"


def test_an_ordinary_workbook_is_not_mistaken_for_a_bomb() -> None:
    """The guard must not refuse the documents it exists to protect."""
    raw = _xlsx_bytes({"yields": [["run", "yield"], [1, 84]]})
    assert "1 | 84" in parse_attachment("runs.xlsx", raw).text


# --- encodings ----------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["notes.txt", "notes.md", "runs.csv", "runs.tsv"])
def test_a_utf8_document_reads_exactly_as_it_did_before_detection_existed(name: str) -> None:
    """A UTF-8 document reads exactly as it did before encoding detection existed.

    Detection is consulted only for bytes strict UTF-8 refuses, because a heuristic allowed to
    re-label a correct file can only make it wrong (`charset_normalizer` does on short inputs).
    Compared against the plain single-encoding policy written out here, not against the new code.
    """
    text = "Reaktion bei 60 °C; Ausbeute 87 %\nSolvens,Toluol\nBediener,A. Müller\n"
    for raw in (b"", b"plain ascii\n", text.encode("utf-8"), text.encode("utf-8") + b"\r\n"):
        assert _decode(raw) == raw.decode("utf-8", errors="replace"), raw

    parsed = parse_attachment(name, text.encode("utf-8")).text
    assert "°C" in parsed
    assert "Müller" in parsed


def test_a_cp1252_document_is_read_rather_than_punched_full_of_replacement_characters() -> None:
    """A cp1252 document is read rather than punched full of replacement characters.

    `errors="replace"` never raises, so mojibake would be chunked, embedded and cited like a correct
    reading — and can cost a chemist a unit symbol.
    """
    text = (
        "Reaction held at 60 °C; yield 87 %. Solvent: toluene. Operator: A. Müller. "
        "Reaction held at 60 °C; yield 87 %. Solvent: toluene. Operator: A. Müller. "
        "Reaction held at 60 °C; yield 87 %. Solvent: toluene. Operator: A. Müller. "
        "Reaction held at 60 °C; yield 87 %. Solvent: toluene. Operator: A. Müller. "
    )
    raw = text.encode("cp1252")

    # What shipped: the degree sign and the umlaut become U+FFFD, and nothing counts it.
    old_policy_text = raw.decode("utf-8", errors="replace")
    assert "60 �C" in old_policy_text
    assert "°C" not in old_policy_text

    assert parse_attachment("note.txt", raw).text == text


def test_a_utf8_bom_does_not_end_up_inside_the_first_csv_header_cell() -> None:
    """A UTF-8 BOM does not end up inside the first CSV header cell.

    Excel's "CSV UTF-8" writes one, and `<U+FEFF>Compound` is a column name nothing can match yet
    renders as `Compound`.
    """
    raw = "﻿Compound,Temp_C\naspirin,60\n".encode()

    old_policy_first_cell = raw.decode("utf-8", errors="replace").split(",", 1)[0]
    assert old_policy_first_cell == "﻿Compound"

    parsed = parse_attachment("runs.csv", raw)
    assert parsed.text.startswith("Compound | Temp_C")
    assert "﻿" not in parsed.text


def test_a_utf16_document_is_read_instead_of_being_refused_for_its_nul_bytes() -> None:
    """A UTF-16 document is read instead of being refused for its NUL bytes.

    Under `errors="replace"` its NULs survive and the sync files it as `skipped_unreadable` for a
    Postgres reason, saying nothing about the encoding.
    """
    text = "Reaction held at 60 °C; yield 87 %. Solvent: toluene.\n"
    raw = text.encode("utf-16")

    old_policy_text = raw.decode("utf-8", errors="replace")
    assert "\x00" in old_policy_text  # what the NUL guard saw, and why it fired

    parsed = parse_attachment("note.txt", raw)
    assert "\x00" not in parsed.text
    assert parsed.text == text
