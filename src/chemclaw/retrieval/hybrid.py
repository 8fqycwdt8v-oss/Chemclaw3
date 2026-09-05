"""Reciprocal Rank Fusion for hybrid retrieval (plan F10-A3).

`gather_evidence` runs several source retrievers (graph substring, dense embedding, lexical FTS,
reaction fingerprints). In `graph` retrieval mode it unions their hits flatly; in `hybrid` mode it
fuses their *rankings* so a note that any one source ranks highly rises overall, without one
verbose source drowning the others. Reciprocal Rank Fusion is the standard, tuning-free way to do
that: a note's score is the sum over sources of `1 / (k + rank)`, where `rank` is its 1-based
position in that source's list — position matters, absolute scores (which are not comparable across
a cosine similarity, a `ts_rank`, and a substring hit) do not.

Fusion is over the source *note*, keyed by `source_note_id` (a note is the unit of evidence), so a
note surfaced by two sources outranks one surfaced by a single source. The representative chunk kept
for a note is the first one seen (stable input order), and graph expansion (`expand_note`) remains
the reasoning path over the fused entries — this only reorders the sweep, it does not replace
traversal (D-004).
"""

from chemclaw.retrieval.evidence import EvidenceChunk


def reciprocal_rank_fusion(
    ranked_lists: list[list[EvidenceChunk]],
    *,
    k: int,
    weights: dict[str, float] | None = None,
) -> list[EvidenceChunk]:
    """Fuse per-source ranked chunk lists into one ranking by Reciprocal Rank Fusion.

    Args:
        ranked_lists: One ordered list of chunks per source (best first). Within a list, only a
            note's first (best) position counts, so repeating a note does not inflate it.
        k: The RRF constant (`settings.retrieval_fusion_k`); larger flattens the contribution of
            rank position. Must be positive.
        weights: Optional per-retriever tier factors (gap IDEA-5). RRF is deliberately
            score-agnostic, which is right for combining heterogeneous *rankers* and wrong for
            combining heterogeneous *evidence classes*: a validated internal ELN entry, an
            agent-distilled playbook, and a literature analogy otherwise fuse identically. This is
            the mechanical expression of the architecture's own "keep evidenced history separate
            from transferred analogy" rule, which is otherwise enforced only by asking the model
            nicely. Absent or empty = uniform weighting (today's behavior exactly). Every weight
            must be positive; `retrieval_source_weights` refuses anything else.

            **Applied in rank space (`k + rank / weight`), not as a multiplier on the score.** A
            multiplier could not express a tier, because RRF's rank term is almost flat at the
            default `k = 60`: rank 1 scores 0.01639 and rank 30 scores 0.01111, a ratio of 1.48
            across thirty positions, so any weight above ~1.02 outranks a whole competing list. At
            the value `core/config/retrieval.py`'s own ENV comment gives as its example
            (`{"graph": 1.5, "vector": 0.8}`) a graph hit beat every other source's best hit for
            all its own ranks below 31, and a measured 40-chunk sweep of four sources went from
            15 graph / 8 lexical / 10 share / 7 vector to 34 / 3 / 3 / **0** — one leg contributing
            nothing at all, which is the defect `D-2026-08-01-a-cap-that-starves-a-source` names as
            this merge design's reason to exist, reintroduced by the knob meant to tune it.

            Dividing the rank instead makes a weight mean what its documentation says: `1.5`
            promotes a hit by a third of its own rank — graph rank 3 fuses like rank 2 — and no
            weight can push a source's rank-1 hit below another source's *tail*, because every
            source's best hit still scores within one rank position of every other's.

    Returns:
        The chunks, one per source note, ordered by descending fused score. Ties break by
        `source_note_id` so the ordering is deterministic. The representative chunk for a note is
        the first one encountered across the lists (stable input order).
    """
    scores: dict[str, float] = {}
    representative: dict[str, EvidenceChunk] = {}
    for chunks in ranked_lists:
        seen_in_list: set[str] = set()
        for rank, chunk in enumerate(chunks, start=1):  # 1-based: canonical RRF, top rank = 1/(k+1)
            note_id = chunk.source_note_id
            representative.setdefault(note_id, chunk)
            if note_id in seen_in_list:
                continue  # a source's best position for a note is the only one that counts
            seen_in_list.add(note_id)
            weight = (weights or {}).get(chunk.retriever, 1.0)
            scores[note_id] = scores.get(note_id, 0.0) + 1.0 / (k + rank / weight)
    ordered = sorted(scores, key=lambda note_id: (-scores[note_id], note_id))
    return [representative[note_id] for note_id in ordered]


def restated_as_position(chunks: list[EvidenceChunk]) -> list[EvidenceChunk]:
    """Re-state each fused chunk's `score` as its position in the fused ranking.

    A chunk arrives here carrying the score its *finder* gave it — a note's `confidence` from the
    graph leg, a `ts_rank` from the lexical one, a cosine from the dense one — and after fusion
    that number no longer explains anything a reader can see: the list is ordered by summed
    reciprocal rank, and a chunk's own score is a different quantity on a different scale, so the
    model is handed an order and a number that contradict each other. Measured over the shipped
    `knowledge/` corpus with all three note legs enabled, the reported score was monotone with the
    fused order on **0 of 7** ordinary queries; on `reaction temperature optimization` the column
    read 0.85 at position 1, 0.072 at position 8 and 0.90 at position 10, because it mixes a note's
    `confidence`, a `ts_rank` and a cosine in one list.

    There is no similarity left to report after fusing a cosine with a `ts_rank` (`EvidenceChunk`'s
    own field comment says the score orders one source's list and nothing wider), so what is
    reported is the only quantity the fusion actually produced: rank. `1 / (1 + position)` keeps it
    inside the field's `[0, 1]` domain, descending, and monotone with the order it explains.

    Not applied to the `graph` merge mode, and that is the distinction rather than an omission:
    round-robin preserves each source's own ordering, so a chunk's own score still explains its
    position within the list it came from. Only the fused order is a quantity no source holds.
    """
    return [
        chunk.model_copy(update={"score": round(1.0 / (1 + position), 4)})
        for position, chunk in enumerate(chunks)
    ]
