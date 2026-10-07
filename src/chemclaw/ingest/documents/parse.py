"""Turn a document's bytes into text, structurally and offline. The one parsing implementation.

Shared by the share crawler and chat uploads; it lives in `ingest` because `ingest` may not import
`chemclaw.agent`.

Extraction is structural, never heuristic: page, slide, sheet and cell boundaries come from each
format's own document model, and a file the library cannot open is refused rather than salvaged. A
scanned PDF is refused by name (`ScannedDocumentError`) rather than returned empty, so silence never
reads as "the file was blank" and the sync can count scans separately. No format needs a network
service (D-089).
"""

import csv
import io
import logging
import zipfile
from collections.abc import Callable

from charset_normalizer import from_bytes
from docx import Document
from openpyxl import load_workbook
from pptx import Presentation
from pydantic import BaseModel
from pypdf import PdfReader

from chemclaw.core.config import settings
from chemclaw.ingest.documents.formats import EXTENSIONS, content_type_for

logger = logging.getLogger(__name__)


class DocumentParseError(ValueError):
    """A document that cannot be read, with a message naming what is supported."""


class UnclassifiedParseError(DocumentParseError):
    """A third-party parser failed and **this system does not know why**.

    A C parser (lxml) can report its own allocation failure as an ordinary parse error, so "could
    not read" may really be "out of memory". The distinction lives in the type rather than the
    message: `isolate._at_ceiling` can resolve it where a ceiling was set, and an unbounded caller
    (`agent/attachments.parse_attachment`) uses `read_without_a_ceiling`. A `DocumentParseError`
    subclass, so existing handlers are unchanged.
    """


class ScannedDocumentError(DocumentParseError):
    """A PDF with no text layer at all — a scan or an image-only export.

    Its own type because it is about the document rather than format support; the share sync counts
    these separately so an operator can see how much of the corpus would need OCR.
    """


class ParsedDocument(BaseModel):
    """One document's extracted text, with the content type it was read as."""

    content_type: str
    text: str
    # Row count for a tabular document, so a caller can say "42 runs" without re-parsing.
    rows: int = 0


def _refuse_a_bomb(name: str, raw: bytes) -> None:
    """Refuse an OOXML container whose parts expand past the configured ceiling.

    `.docx`/`.xlsx`/`.pptx` are zip archives, so upstream size limits bound only the compressed
    bytes. Read from the central directory, so it costs no decompression. A crafted archive can
    understate `file_size`; the real memory bound is the kernel ceiling
    `document_parse_memory_bytes` set in `isolate.py`, and this is a cheap, early, well-worded
    refusal for the realistic case.

    Raises:
        DocumentParseError: The declared expansion exceeds `document_max_expanded_bytes`.
    """
    ceiling = settings.document_max_expanded_bytes
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as container:
            expanded = sum(item.file_size for item in container.infolist())
    except zipfile.BadZipFile as exc:
        # `DocumentParseError`, not `UnclassifiedParseError`: a central directory that is not a
        # zip's is a classified fact about the document, not a disguised allocation failure.
        raise DocumentParseError(f"could not read {name}: {exc}") from exc
    if expanded > ceiling:
        raise DocumentParseError(
            f"{name} expands to {expanded} bytes from {len(raw)} on disk, past the "
            f"{ceiling}-byte limit. A document this large compressed this well is a data export or "
            "a malformed file rather than a document; extracting the relevant sheet will work."
        )


def too_large_to_read(name: str) -> DocumentParseError:
    """The refusal a document earns by exhausting `document_parse_memory_bytes`.

    One function because the ceiling is hit in three places — extraction in `parse_document`,
    pickling the answer in `isolate._parse_into`, and a C parser's own failure renamed by
    `isolate._at_ceiling` — and all three must read identically.

    Args:
        name: The document name, for the message.

    Returns:
        The refusal to raise, or to send back across the parse boundary.
    """
    return DocumentParseError(
        f"{name} needs more memory to read than one parse is allowed to use "
        f"({settings.document_parse_memory_bytes} bytes). Reading it whole would take the pod's "
        "memory from every other request in flight; the relevant sheet, or the file split into "
        "parts, will work."
    )


def read_without_a_ceiling(cause: UnclassifiedParseError) -> UnclassifiedParseError:
    """The refusal an unclassified failure earns on a path that set **no** memory ceiling.

    Without a ceiling, an allocation failure and a malformed file are the same observation, so this
    keeps the parser's message for the operator and states that it cannot establish the cause,
    rather than blaming the document. No string in `cause` is inspected. Takes no `name` because
    `cause`'s message already carries it
    (D-2026-09-22-an-unbounded-parse-may-not-blame-the-document).

    Args:
        cause: The unclassified failure, whose own words are kept verbatim.

    Returns:
        The refusal to raise — still an `UnclassifiedParseError`, so the distinction the type
        carries survives.
    """
    # The trailing period goes because `cause` may or may not end in one — a parser's own wording is
    # not ours to predict — and "zip file.. This parse" is how that reads when it does.
    return UnclassifiedParseError(
        f"{str(cause).rstrip('.')}. This parse ran with no memory ceiling, so whether the document "
        "is malformed or simply needs more memory than this machine had is not established here — "
        "the parser's own words above are all there is. An upload through the API is bounded and "
        "would say which."
    )


def _decode(raw: bytes) -> str:
    """Turn a text document's bytes into characters: strict UTF-8 first, detection only after it.

    `utf-8-sig` first accepts exactly what strict UTF-8 accepts (stripping a BOM that would
    otherwise land in the first CSV header cell), so every file that decodes cleanly is never
    re-labelled. Only bytes UTF-8 refuses go to `charset-normalizer`, which handles legacy
    cp1252/UTF-16 files instead of indexing replacement characters. Detection is unreliable on
    short, low-variety text, but such files were already refused by UTF-8, so at worst one wrong
    reading replaces another.

    Args:
        raw: The document's bytes, as read off the share or off an upload.

    Returns:
        The decoded text. `errors="replace"` is the last resort for bytes no encoding claims, since
        refusing would hide a mostly readable file.
    """
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    detected = from_bytes(raw).best()
    if detected is not None:
        return str(detected)
    return raw.decode("utf-8", errors="replace")


def _parse_text(raw: bytes) -> tuple[str, int]:
    """Decode a text document verbatim — nothing is summarized or dropped at ingest."""
    return _decode(raw), 0


def _parse_csv(raw: bytes) -> tuple[str, int]:
    """Render a delimited table as aligned text, preserving every cell.

    Rendered rather than passed as raw CSV because the agent misreads quoting rules, and a mangled
    quote can silently shift a column. Decoded by `_decode`, so a BOM or legacy byte does not
    corrupt a header cell.
    """
    text = _decode(raw)
    dialect_sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(dialect_sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel  # a single-column or unusual file is still readable as plain rows
    reader = csv.reader(io.StringIO(text), dialect)
    header = next(reader, None)
    if header is None:
        return "", 0
    # Rendered row by row rather than materialising the reader: holding every cell object at once
    # multiplies memory several times on a large export.
    lines = [" | ".join(header), "-" * 40]
    rows = 0
    for row in reader:
        lines.append(" | ".join(row))
        rows += 1
    return "\n".join(lines), rows


def _parse_pdf(raw: bytes) -> tuple[str, int]:
    """Extract a PDF's text layer page by page; refuse a scan rather than return nothing.

    Pages are labelled and kept in order so a citation can name the page. A PDF where no page yields
    text is refused as a scan; the test is zero characters, not a minimum length, so a one-line CoA
    is still read.
    """
    reader = PdfReader(io.BytesIO(raw))
    pages = [(page.extract_text() or "").strip() for page in reader.pages]
    if not any(pages):
        raise ScannedDocumentError(
            f"no text could be extracted from any of this PDF's {len(pages)} page(s), so it is a "
            "scan or an image-only export. Reading it needs OCR, which is not built — a text-based "
            "PDF, or the relevant text pasted directly, will work."
        )
    # Page labels come from the original numbering, so a page that is itself a scan drops out
    # without renumbering the ones after it — a citation to "page 3" must still land on page 3.
    return "\n\n".join(
        f"[page {number}]\n{text}" for number, text in enumerate(pages, 1) if text
    ), len(pages)


def _parse_pptx(raw: bytes) -> tuple[str, int]:
    """Extract a deck's text slide by slide, including tables and speaker notes.

    Notes are included because a deck's reasoning often lives there.
    """
    deck = Presentation(io.BytesIO(raw))
    blocks: list[str] = []
    slides = list(deck.slides)
    for number, slide in enumerate(slides, 1):
        parts: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                parts.append(shape.text_frame.text.strip())
            if shape.has_table:
                parts += [" | ".join(cell.text for cell in row.cells) for row in shape.table.rows]
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                parts.append(f"(speaker notes) {notes}")
        if parts:
            blocks.append(f"[slide {number}]\n" + "\n".join(parts))
    return "\n\n".join(blocks), len(slides)


def _parse_docx(raw: bytes) -> tuple[str, int]:
    """Extract a Word document's paragraphs and tables in document order.

    Tables use the same `|` separator as `_parse_csv`, so a table reads identically whatever its
    source.
    """
    document = Document(io.BytesIO(raw))
    parts = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    rows = 0
    for table in document.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text.strip() for cell in row.cells))
            rows += 1
    return "\n".join(parts), rows


def _parse_xlsx(raw: bytes) -> tuple[str, int]:
    """Extract a workbook sheet by sheet as delimited rows.

    `data_only=True` reads cached values of formula cells rather than the formulas; a workbook saved
    without cached values yields visibly empty cells there.
    """
    book = load_workbook(io.BytesIO(raw), data_only=True, read_only=True)
    try:
        blocks: list[str] = []
        rows = 0
        for sheet in book.worksheets:
            lines = []
            for row in sheet.iter_rows(values_only=True):
                if any(cell is not None for cell in row):
                    lines.append(" | ".join("" if c is None else str(c) for c in row))
                    rows += 1
            if lines:
                blocks.append(f"[sheet {sheet.title}]\n" + "\n".join(lines))
        return "\n\n".join(blocks), rows
    finally:
        # read_only workbooks hold an open zip handle; leaking it would exhaust file descriptors
        # over a long-lived pod's worth of uploads.
        book.close()


# The closed allowlist. A content type absent here is refused with a message, never guessed at.
_PARSERS: dict[str, Callable[[bytes], tuple[str, int]]] = {
    "text/markdown": _parse_text,
    "text/plain": _parse_text,
    "text/csv": _parse_csv,
    "text/tab-separated-values": _parse_csv,
    "application/pdf": _parse_pdf,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": _parse_pptx,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": _parse_docx,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": _parse_xlsx,
}

# The zip-container formats. Their size limits upstream bound compressed bytes only, so these are
# the three that need the expansion check before a parser is handed the archive.
_ZIP_CONTAINERS = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
)

# `formats.EXTENSIONS` decides what a crawl opens and `_PARSERS` what can be read; a format in one
# but not the other is caught here at import.
_UNPARSEABLE = set(EXTENSIONS.values()) - set(_PARSERS)
_UNREACHABLE = set(_PARSERS) - set(EXTENSIONS.values())
if _UNPARSEABLE or _UNREACHABLE:  # pragma: no cover - a wiring error, not a runtime state
    raise ImportError(
        "the format allowlist and the parser table disagree; "
        f"declared but unparseable: {sorted(_UNPARSEABLE)}; "
        f"parseable but undeclared: {sorted(_UNREACHABLE)}"
    )


def parse_document(name: str, raw: bytes, declared_type: str | None = None) -> ParsedDocument:
    """Extract one document's text, or refuse it with a message naming the supported formats.

    Args:
        name: The file name; its extension resolves the format when no type is declared.
        raw: The document's bytes.
        declared_type: A content type from the transport, if the caller has one.

    Returns:
        The extracted text with the content type it was read as.

    Raises:
        ScannedDocumentError: A PDF with no text layer at all.
        DocumentParseError: An unsupported format, or a file that could not be read.
    """
    content_type = content_type_for(name, declared_type)
    parser = _PARSERS.get(content_type)
    if parser is None:
        raise DocumentParseError(
            f"{name} ({content_type}) is not a supported format. Supported: "
            f"{', '.join(sorted(EXTENSIONS))}. Spectra and image formats need OCR/vision "
            "ingestion, which is not built — exporting the relevant text or table is the "
            "reliable path today."
        )
    if content_type in _ZIP_CONTAINERS:
        _refuse_a_bomb(name, raw)
    try:
        text, rows = parser(raw)
    except DocumentParseError:
        # Already precise — a refusal the parser named itself, `ScannedDocumentError` included.
        raise
    except MemoryError as exc:
        # The parse ran out of the budget `isolate.py` set on this process: the document is too
        # large, not malformed, so it is named as such.
        raise too_large_to_read(name) from exc
    except Exception as exc:
        # One broad net around the whole parse: these libraries do their work lazily (`iter_rows`,
        # lazy slide parts, the CSV reader), so a guard on constructors misses truncated or
        # malformed input. `raw` is untrusted bytes and every library here is third-party, so any
        # failure is "could not be read" — one counted refusal rather than a failed sync that
        # restarts on the same file.
        raise UnclassifiedParseError(f"could not read {name}: {exc}") from exc
    return ParsedDocument(content_type=content_type, text=text, rows=rows)
