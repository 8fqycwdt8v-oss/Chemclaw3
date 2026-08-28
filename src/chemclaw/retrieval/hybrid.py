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
            promotes a hit by a third of its own rank — graph rank 3 fuses like rank 2.

            **It does not make starvation impossible on its own, and this docstring claimed it
            did.** The sentence here read "no weight can push a source's rank-1 hit below another
            source's *tail*, because every source's best hit still scores within one rank position
            of every other's" — both halves false, and computed rather than argued. A source at
            weight `w` fuses its rank-1 hit at effective rank `1/w`, so at `w = 0.1` its best hit
            fuses at rank 10, nine positions from rank 1, not one. Measured over six sources of
            eight hits each with every other weight at 1.0: at `0.5` the weighted leg's best hit
            is at fused index 6 of 48 and it keeps 4 chunks under the 40-chunk cap; at `0.2`,
            index 21 and 1 chunk; at **`0.1`, index 40 — behind all five other sources'
            complete tails — and it keeps 0**. In the other direction a weight of `8.0` puts every
            *other* source's rank-1 hit behind the weighted source's whole eight-hit list.

            What holds the property is the band `retrieval_source_weights` is now validated
            against, `[1/W, W]` with `W = 2`: a source's best hit fuses at effective rank at most
            `W`, another source's rank `r` at effective rank at least `r / W`, so at most `W²`
            chunks of any other source can precede it. `core/config/retrieval.py` carries the
            derivation and refuses anything outside it; the fusion cannot, because it does not
            know the cap.

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
