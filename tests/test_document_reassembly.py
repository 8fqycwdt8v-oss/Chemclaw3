"""Putting a document's chunks back together without losing any of it.

A protocol is atomic: half an SOP is not a shorter SOP, it is a misleading one. The share stores
documents cut into `chunk_chars` pieces because that is what makes retrieval work, and until now
nothing could put one back — `sync._read_and_parse` discards the parsed text after hashing it into
`doc_id`, so the chunks are the only copy.

These tests exist because the first two implementations of the de-overlapping rule **deleted real
text** and did it silently. Both are pinned below as regressions rather than described, because a
protocol missing a step reads exactly like a protocol that never had one.
"""

import random

from chemclaw.ingest.documents.chunk import chunk_document
from chemclaw.ingest.documents.reassemble import join_chunks

_WORDS = "charge reflux quench filter concentrate recrystallise degas cool add stir".split()


def _prose(blocks: int, lines: int, *, coordinates: bool, seed: int = 7) -> str:
    """A document shaped like a parsed one: blocks separated by blank lines, optionally labelled."""
    rng = random.Random(seed)
    out = []
    for block in range(blocks):
        body = "\n".join(
            " ".join(rng.choice(_WORDS) for _ in range(rng.randint(3, 14))) for _ in range(lines)
        )
        out.append(f"[page {block + 1}]\n{body}" if coordinates else body)
    return "\n\n".join(out)


def test_a_real_overlap_is_removed_exactly_once() -> None:
    """Prose cut with an overlap must come back the length it went in — no stutter, no loss."""
    text = _prose(2, 40, coordinates=False)
    chunks = chunk_document(text, chunk_chars=400, overlap_chars=200)
    assert len(chunks) > 1, "the fixture must actually be cut"
    assert sum(len(c.content) for c in chunks) > len(text), "and the cut must actually overlap"
    assert len(join_chunks([c.content for c in chunks], 200)) == len(text)


def test_repetitive_content_keeps_its_text_rather_than_guessing_a_boundary() -> None:
    """`_hard_split` pieces share nothing, but repetitive content makes them look as if they do.

    Measured against the naive longest-match rule: a 5,000-character line of `x` reassembled as
    **2,600** characters, and a comma-separated line of period 10 as **3,200 of 6,000**. Both are
    real text deleted at every boundary. A share whose allowlist includes `.csv`/`.tsv` makes this
    ordinary rather than exotic.
    """
    for text in ("x" * 5000, "1,2,3,4,5," * 600, "ab,cd,x" * 800):
        chunks = chunk_document(text, chunk_chars=400, overlap_chars=200)
        assert len(chunks) > 1
        rebuilt = join_chunks([c.content for c in chunks], 200)
        assert len(rebuilt) >= len(text), f"reassembly lost {len(text) - len(rebuilt)} characters"


def test_every_chunk_survives_reassembly_in_document_order() -> None:
    """The invariant that matters to a reader, over the cutting parameters the bindings allow."""
    checked = 0
    for coordinates in (False, True):
        for blocks, lines in ((1, 2), (3, 20), (3, 60)):
            for size, overlap in ((200, 0), (200, 50), (400, 200), (1800, 200)):
                text = _prose(blocks, lines, coordinates=coordinates)
                chunks = chunk_document(text, chunk_chars=size, overlap_chars=overlap)
                if not chunks:
                    continue
                checked += 1
                rebuilt = join_chunks([c.content for c in chunks], overlap)
                position = 0
                for chunk in chunks:
                    found = rebuilt.find(chunk.content, max(0, position - len(chunk.content)))
                    assert found >= 0, f"chunk {chunk.ordinal} vanished ({size}/{overlap})"
                    position = found
    assert checked >= 20, "the matrix must actually exercise something"


def test_reassembling_nothing_is_empty_rather_than_an_error() -> None:
    """A document with no chunks is a real state (nothing indexed yet), not a failure."""
    assert join_chunks([], 200) == ""
    assert join_chunks(["only"], 200) == "only"
