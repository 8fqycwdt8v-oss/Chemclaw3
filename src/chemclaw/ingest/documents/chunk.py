"""Cut a parsed document into retrievable pieces without losing where each piece came from.

A whole long document embeds to a vector close to everything and cites too broadly, so documents are
chunked. The parser's coordinates (`[page 3]`, `[slide 7]`, `[sheet Yields]`) are carried through
the cut, and a chunk never spans two coordinates, because a citation to the wrong page is worse than
none.
"""

import re
from dataclasses import dataclass

# A structural label as `chemclaw.ingest.documents.parse` writes it, alone on the first line of a
# block. Anchored to the three words the parsers emit, so document text like `[Figure 2: yield vs
# time]` is never adopted as a coordinate and stripped from the body.
_LABEL = re.compile(r"^\[(page|slide|sheet) ([^\]\n]{1,80})\]\n")


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
            coordinate = f"{match.group(1)} {match.group(2)}"
            buffer = [part[match.end() :]]
        else:
            buffer.append(part)
    if buffer:
        grouped.append((coordinate, "\n\n".join(buffer)))
    return [(coord, body) for coord, body in grouped if body.strip()]


def _hard_split(line: str, size: int) -> list[str]:
    """Cut one oversized line into `size`-character pieces.

    For CSV-like rows longer than any sensible chunk, which would otherwise be refused by the
    embedder.
    """
    return [line[start : start + size] for start in range(0, len(line), size)]


def _split_block(body: str, size: int, overlap: int) -> list[str]:
    """Pack a block's lines into pieces of at most roughly `size`, each repeating `overlap` chars.

    Line-aligned so a table row is never cut in half; the overlap keeps a sentence straddling a
    boundary findable.
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
