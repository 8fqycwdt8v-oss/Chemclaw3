"""Reciprocal Rank Fusion for hybrid retrieval (plan F10-A3).

`gather_evidence` runs several source retrievers (graph substring, dense embedding, lexical FTS,
reaction fingerprints). In `graph` retrieval mode it unions their hits flatly; in `hybrid` mode it
fuses their *rankings* so a note that any one source ranks highly rises overall, without one
verbose source drowning the others. Reciprocal Rank Fusion is the standard, tuning-free way to do
that: a note's score is the sum over sources of `1 / (k + rank)`, where `rank` is its position in
that source's list — position matters, absolute scores (which are not comparable across a cosine
similarity, a `ts_rank`, and a substring hit) do not.

Fusion is over the source *note*, keyed by `source_note_id` (a note is the unit of evidence), so a
note surfaced by two sources outranks one surfaced by a single source. The representative chunk kept
for a note is the first one seen (stable input order), and graph expansion (`expand_note`) remains
the reasoning path over the fused entries — this only reorders the sweep, it does not replace
traversal (D-004).
"""

from report.evidence import EvidenceChunk


def reciprocal_rank_fusion(
    ranked_lists: list[list[EvidenceChunk]], *, k: int
) -> list[EvidenceChunk]:
    """Fuse per-source ranked chunk lists into one ranking by Reciprocal Rank Fusion.

    Args:
        ranked_lists: One ordered list of chunks per source (best first). Within a list, only a
            note's first (best) position counts, so repeating a note does not inflate it.
        k: The RRF constant (`settings.retrieval_fusion_k`); larger flattens the contribution of
            rank position. Must be positive.

    Returns:
        The chunks, one per source note, ordered by descending fused score. Ties break by
        `source_note_id` so the ordering is deterministic. The representative chunk for a note is
        the first one encountered across the lists (stable input order).
    """
    scores: dict[str, float] = {}
    representative: dict[str, EvidenceChunk] = {}
    for chunks in ranked_lists:
        seen_in_list: set[str] = set()
        for rank, chunk in enumerate(chunks):
            note_id = chunk.source_note_id
            representative.setdefault(note_id, chunk)
            if note_id in seen_in_list:
                continue  # a source's best position for a note is the only one that counts
            seen_in_list.add(note_id)
            scores[note_id] = scores.get(note_id, 0.0) + 1.0 / (k + rank)
    ordered = sorted(scores, key=lambda note_id: (-scores[note_id], note_id))
    return [representative[note_id] for note_id in ordered]
