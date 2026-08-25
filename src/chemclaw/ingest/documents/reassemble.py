"""Put a document's chunks back together, and be honest about what comes back.

A protocol is atomic: an SOP is one procedure, and half of one is not a smaller procedure but a
misleading one. The share stores protocols cut into `chunk_chars` pieces, because a 60-page report
embedded whole produces a vector close to everything and useful for nothing (`chunk.py` argues
that, and it is right *for retrieval*). What was missing is the way back: `sync._read_and_parse`
hashes the parsed text into `doc_id` and then discards the text, so nothing in the tree could
answer "show me this whole document" — the chunks were the only copy and no reader assembled them.

**Why reassembly rather than re-reading the file**, in ascending order of force:

- `chemclaw.ingest.documents.retriever` deliberately imports nothing that can open a document, and
  `tests/test_datasource_isolation.py` holds it in a subprocess. Re-reading means `pypdf`,
  `python-docx`, `python-pptx` and `openpyxl` in the process that serves conversations.
- The chat pod need not have the CIFS mount at all. The crawler does; the front door does not.
- **Correctness, which is the decisive one.** `doc_id` is the hash of the text *as parsed at crawl
  time*, and every citation a turn is holding points into chunks of that text. Re-reading can
  return different bytes — the file moved on and the crawl has not run — so the whole document
  would not be the document the citation came from. That is `verifier.turn_evidence`'s distinction
  ("does this exist" versus "did this turn see it") one level up, and getting it wrong would let a
  chemist check a quotation against a file that no longer contains it.

**What comes back is the indexed text, not the original bytes, and `DocumentText` says so.**
`chunk_document` strips each piece, drops empty ones, joins blocks with a blank line and hoists the
`[page 3]` coordinate out of the body. None of that is recoverable from the chunks, so a promise of
byte-fidelity would be false. The promise made instead is the one that matters to a reader: every
chunk of the document, in document order, with the overlap that exists only to make retrieval work
removed exactly once.
"""


def join_chunks(pieces: list[str], overlap_chars: int) -> str:
    """Concatenate chunks in document order, removing the overlap each repeats from the last.

    **Not arithmetic on `overlap_chars`, and that is the whole subtlety.** `chunk._split_block`
    carries the previous piece's last `overlap_chars` characters into the next one, but only on the
    line-packing path: `_hard_split` cuts an oversized line into adjacent pieces that share nothing,
    and a new block (a new `[page N]` coordinate) starts a piece with no predecessor to overlap.
    So slicing `overlap_chars` off every piece would eat real text at exactly those boundaries —
    silently, in the middle of a procedure.

    What is measured instead is the actual repeat: the longest suffix of what has been assembled so
    far that the next piece begins with, bounded by `overlap_chars` so a coincidental repetition
    longer than any real overlap cannot swallow a line. A zero-length match is the `_hard_split`
    and new-block case and yields a plain concatenation, which is correct there.

    Args:
        pieces: The chunks' `content`, in ascending `ordinal` order.
        overlap_chars: The cutting's `chunk_overlap_chars` — the largest repeat that can be real.

    Returns:
        The document's indexed text. Empty for no pieces.
    """
    if not pieces:
        return ""
    assembled = pieces[0]
    for piece in pieces[1:]:
        assembled += piece[_repeat_length(assembled, piece, overlap_chars) :]
    return assembled


def _repeat_length(assembled: str, piece: str, overlap_chars: int) -> int:
    """How many of `piece`'s leading characters `assembled` already ends with, at most `overlap`.

    Longest first, so a piece whose whole overlap is present loses all of it rather than a shorter
    coincidental prefix — taking the shortest match would leave a partial repeat mid-sentence.

    **An ambiguous match is refused, and measuring before building on this is how that was found.**
    Content alone cannot always distinguish a real overlap from genuinely repetitive text.
    `_hard_split` cuts a 5,000-character line into pieces that share *nothing*, yet if that line is
    `xxxx…` the assembled tail ends with 200 `x` and the next piece begins with 200 `x`, exactly as
    a real overlap would. Measured, the naive longest-match rule reassembled that document as
    **2,600 of 5,000 characters**, and a comma-separated line of period 10 as **3,200 of 6,000** —
    it deleted real text at every boundary, silently, which in a protocol means deleting steps.
    A share whose allowlist includes `.csv` and `.tsv` makes that the ordinary case, not the exotic
    one.

    The discriminator is whether the repeat is *positionally unique*: if the candidate prefix occurs
    more than once in the tail of what has been assembled, more than one alignment explains the
    match, so the boundary is not derivable from the content and the repeat is left in place. A
    bounded duplicate is a cosmetic wart; a dropped step is a wrong procedure, so ambiguity always
    resolves toward keeping text.
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
