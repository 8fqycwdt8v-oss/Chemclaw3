"""Put a document's chunks back together, and be honest about what comes back.

A protocol is atomic, and the chunks are the only stored copy of a document's text. Reassembly
rather than re-reading the file because the retriever process must not import document parsers or
need the share mount, and above all because every citation points into the text as parsed at crawl
time; a re-read file may have changed since.

What comes back is the indexed text, not the original bytes (`DocumentText` says so): every chunk in
document order with the retrieval overlap removed once.
"""


def join_chunks(pieces: list[str], overlap_chars: int, max_chars: int | None = None) -> str:
    """Concatenate chunks in document order, removing the overlap each repeats from the last.

    Not arithmetic on `overlap_chars`: `_hard_split` pieces and the first piece of a new block share
    nothing with their predecessor, so slicing a fixed overlap would eat real text. The actual
    repeat is measured instead (see `_repeat_length`).

    Args:
        pieces: The chunks' `content`, in ascending `ordinal` order.
        overlap_chars: The cutting's `chunk_overlap_chars` — the largest repeat that can be real.
        max_chars: Stop assembling once this much text exists; a backstop to the bounded fetch.

    Returns:
        The document's indexed text. Empty for no pieces.
    """
    if not pieces:
        return ""
    assembled = pieces[0]
    for piece in pieces[1:]:
        if max_chars is not None and len(assembled) >= max_chars:
            break
        assembled += piece[_repeat_length(assembled, piece, overlap_chars) :]
    return assembled


def _repeat_length(assembled: str, piece: str, overlap_chars: int) -> int:
    """How many of `piece`'s leading characters `assembled` already ends with, at most `overlap`.

    Longest match first, so a full overlap is removed rather than a coincidental shorter prefix. A
    match is accepted only if it is positionally unique in the assembled tail: in repetitive text
    (`xxxx…`, periodic CSV) several alignments explain it, the boundary cannot be derived, and the
    repeat is kept. A duplicate is cosmetic; a dropped step is a wrong procedure.
    """
    limit = min(overlap_chars, len(assembled), len(piece))
    for length in range(limit, 0, -1):
        if not assembled.endswith(piece[:length]):
            continue
        # Two alignments' worth of tail: enough to see a second explanation for this match if one
        # exists, and bounded so a long document does not rescan itself at every boundary.
        window = assembled[-(2 * length) :]
        return 0 if window.count(piece[:length]) > 1 else length
    return 0
