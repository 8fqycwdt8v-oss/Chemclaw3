"""Cut a parsed document into retrievable pieces without losing where each piece came from.

A note is retrieved whole (`retrieval/vector_index.py` embeds one vector per note) because a note
is already one claim. A 60-page report is not: embedding it whole produces a vector that is close
to everything and useful for nothing, and citing it tells a chemist to go read 60 pages.

So documents are chunked — and the only thing that makes a chunk *checkable* is the coordinate the
parser already put in the text. `[page 3]`, `[slide 7]`, `[sheet Yields]` are the source document's
own numbering, and this module's job is to carry them through the cut rather than let them dissolve
into a stream of characters. A chunk therefore never spans two coordinates: it would have to claim
one of them, and a citation to the wrong page is worse than a citation to none.
"""

import re
from dataclasses import dataclass

# A structural label as `chemclaw.ingest.documents.parse` writes it: `[page 3]`, `[slide 7]`,
# `[sheet Yields]`, alone on the first line of a block. Bounded so a line of prose in square
# brackets cannot be mistaken for one.
_LABEL = re.compile(r"^\[([^\]\n]{1,80})\]\n")


@dataclass(frozen=True)
class Chunk:
    """One retrievable piece of a document, and the coordinate a reader can check it against."""

    ordinal: int
    content: str
    # "page 3" / "slide 7" / "sheet Yields", or "" for a format with no internal structure
    # (a Word document, a CSV, a Markdown file) — empty rather than invented.
    coordinate: str = ""


def _blocks(text: str) -> list[tuple[str, str]]:
    """Group the parsed text into `(coordinate, body)` runs, splitting at each structural label."""
    grouped: list[tuple[str, str]] = []
    coordinate = ""
    buffer: list[str] = []
    for part in text.split("\n\n"):
        match = _LABEL.match(part)
        if match:
            if buffer:
                grouped.append((coordinate, "\n\n".join(buffer)))
            coordinate = match.group(1)
            buffer = [part[match.end() :]]
        else:
            buffer.append(part)
    if buffer:
        grouped.append((coordinate, "\n\n".join(buffer)))
    return [(coord, body) for coord, body in grouped if body.strip()]


def _hard_split(line: str, size: int) -> list[str]:
    """Cut one oversized line into `size`-character pieces.

    A share holds CSV exports whose single row is longer than any sensible chunk. Splitting mid-line
    loses nothing a line break would have preserved, and the alternative — one chunk of 200 kB — is
    an embedding call that would be refused and a citation nobody can read.
    """
    return [line[start : start + size] for start in range(0, len(line), size)]


def _split_block(body: str, size: int, overlap: int) -> list[str]:
    """Pack a block's lines into pieces of at most roughly `size`, each repeating `overlap` chars.

    Line-aligned, because a table row or a bullet cut in half reads as corrupted data rather than
    as a truncation. The overlap is what stops a sentence that straddles a boundary from being
    findable in neither piece.
    """
    if len(body) <= size:
        return [body]
    pieces: list[str] = []
    current = ""
    for line in body.splitlines(keepends=True):
        if len(line) > size:
            if current.strip():
                pieces.append(current)
            pieces.extend(_hard_split(line, size))
            current = ""
            continue
        if current and len(current) + len(line) > size:
            pieces.append(current)
            current = current[-overlap:] if overlap else ""
        current += line
    if current.strip():
        pieces.append(current)
    return pieces


def chunk_document(text: str, *, chunk_chars: int, overlap_chars: int) -> list[Chunk]:
    """Split a parsed document into coordinate-tagged chunks, numbered from zero.

    Args:
        text: The document's extracted text, as `parse.parse_document` produced it.
        chunk_chars: The target size of one chunk.
        overlap_chars: How much of the previous chunk each following one repeats.

    Returns:
        The chunks in document order; empty when the document held no text at all.
    """
    chunks: list[Chunk] = []
    for coordinate, body in _blocks(text):
        for piece in _split_block(body.strip(), chunk_chars, overlap_chars):
            if piece.strip():
                chunks.append(
                    Chunk(ordinal=len(chunks), content=piece.strip(), coordinate=coordinate)
                )
    return chunks
