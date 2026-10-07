"""Reciprocal Rank Fusion for hybrid retrieval.

In `hybrid` mode `gather_evidence` fuses its sources' *rankings*: a note's score is the sum over
sources of `1 / (k + rank)`, so position matters and incomparable absolute scores do not. Fusion
is keyed by `source_note_id`, so a note surfaced by two sources outranks one surfaced by one; the
representative chunk is the first seen in stable input order. This only reorders the sweep;
`expand_note` remains the traversal path (D-004).
"""

from collections.abc import Sequence

from chemclaw.retrieval.evidence import EvidenceChunk


def reciprocal_rank_fusion(
    ranked_lists: list[list[EvidenceChunk]],
    *,
    k: int,
    weights: dict[str, float] | None = None,
    corpora: Sequence[str] | None = None,
) -> list[EvidenceChunk]:
    """Fuse per-source ranked chunk lists into one ranking by Reciprocal Rank Fusion.

    Args:
        ranked_lists: One ordered list of chunks per source (best first). Within a list, only a
            note's first (best) position counts, so repeating a note does not inflate it.
        k: The RRF constant (`settings.retrieval_fusion_k`); larger flattens the contribution of
            rank position. Must be positive.
        weights: Optional per-retriever tier factors, so evidence classes (validated ELN, distilled
            playbook, literature analogy) need not fuse identically. Absent or empty means uniform
            weighting; every weight must be positive. Applied in rank space (`k + rank / weight`),
            not as a score multiplier: RRF's rank term is nearly flat, so a multiplier above ~1.02
            would let one source's whole list outrank every other source's best hit. As a rank
            divisor, `1.5` promotes a hit by a third of its rank and no source's best hit falls
            below another's tail.
        corpora: The corpus each list reads, positionally. When given, fusion runs in two stages
            (within a corpus, then across corpora) so several legs over one corpus vote once; RRF
            assumes independent rankers, and term-overlap legs over one corpus are not. A corpus's
            weight is the mean of its non-empty legs' weights, so a single-leg corpus keeps its
            source's weight. Omitted, every list is its own corpus.

    Returns:
        The chunks, one per source note, ordered by descending fused score. Ties break by
        `source_note_id` so the ordering is deterministic. The representative chunk for a note is
        the first one encountered across the lists (stable input order).
    """
    if corpora is not None and len(corpora) != len(ranked_lists):
        raise ValueError(
            f"corpora names {len(corpora)} corpus/corpora for {len(ranked_lists)} ranked list(s); "
            "the mapping is positional and a mismatch would fuse a list under another's corpus"
        )
    if corpora is not None and len(set(corpora)) != len(corpora):
        return _fuse_by_corpus(ranked_lists, corpora, k=k, weights=weights)
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


def _fuse_by_corpus(
    ranked_lists: list[list[EvidenceChunk]],
    corpora: Sequence[str],
    *,
    k: int,
    weights: dict[str, float] | None,
) -> list[EvidenceChunk]:
    """One corpus, one vote: fuse within each corpus, then fuse the corpora.

    Within a corpus the legs are the heterogeneous rankers RRF assumes; across corpora the agreement
    term again means two bodies of evidence surface the note. `ingest/documents/retriever.py` does
    the same for the mounted share.
    """
    grouped: dict[str, list[list[EvidenceChunk]]] = {}
    tiers: dict[str, list[float]] = {}
    for corpus, chunks in zip(corpora, ranked_lists, strict=True):
        grouped.setdefault(corpus, []).append(chunks)
        # Each list's tier, read off its chunks. An empty list is skipped so it cannot pull its
        # corpus's
        # mean weight toward neutral.
        tiers.setdefault(corpus, [])
        tiers[corpus].extend({(weights or {}).get(chunk.retriever, 1.0) for chunk in chunks})
    fused_per_corpus = {
        corpus: reciprocal_rank_fusion(lists, k=k, weights=weights)
        for corpus, lists in grouped.items()
    }
    # The cross-corpus stage reuses the same body keyed by corpus name, written into `retriever` on
    # a
    # copy: the chunk a caller receives must still name the leg that found it.
    relabelled: list[list[EvidenceChunk]] = []
    corpus_weights: dict[str, float] = {}
    for corpus, chunks in fused_per_corpus.items():
        # `or [1.0]` only for a corpus whose legs all came back empty: it has no chunk to weight,
        # and this
        # avoids a `ZeroDivisionError`.
        weighted = tiers[corpus] or [1.0]
        corpus_weights[corpus] = sum(weighted) / len(weighted)
        relabelled.append([chunk.model_copy(update={"retriever": corpus}) for chunk in chunks])
    order = reciprocal_rank_fusion(relabelled, k=k, weights=corpus_weights)
    # Back to the originals, by note id: the relabelled copies were a vehicle for the weight key.
    original = {
        chunk.source_note_id: chunk for chunks in fused_per_corpus.values() for chunk in chunks
    }
    return [original[chunk.source_note_id] for chunk in order]


def restated_as_position(chunks: list[EvidenceChunk]) -> list[EvidenceChunk]:
    """Re-state each fused chunk's `score` as its position in the fused ranking.

    A finder's score (confidence, `ts_rank`, cosine) is on its own scale and contradicts the merged
    order, so after merging the only meaningful quantity is rank. `1 / (1 + position)` stays in
    `[0, 1]`, descends, and matches the order. Applied in both merge modes, since the reader sees
    one interleaved column either way.
    """
    return [
        chunk.model_copy(update={"score": round(1.0 / (1 + position), 4)})
        for position, chunk in enumerate(chunks)
    ]


def with_no_leg_cut_out(
    fused: list[EvidenceChunk],
    ranked_lists: list[list[EvidenceChunk]],
    *,
    limit: int,
) -> list[EvidenceChunk]:
    """Reorder a fused ranking so the first `limit` entries leave no contributing leg at zero.

    The RRF-side counterpart of round-robin truncation: a large `retrieval_source_weights` entry can
    put one leg's whole list above every other leg's best hit, and a flat cut would then starve the
    rest. The floor is one chunk per leg (what round-robin's first pass gives), a floor rather than
    a share, so a heavily weighted leg still gets most of the window. When every leg already has a
    representative inside `limit` the result is the identity.

    Leg membership comes from `ranked_lists`, never `chunk.retriever`, which names only a note's
    first finder. This bounds the count cap only: `gather_evidence_max_chars` spends down the same
    order and may still cut a promoted chunk (which lands at the end of the window).
    """
    place_of = {chunk.source_note_id: index for index, chunk in enumerate(fused)}
    reserved: set[int] = set()
    for offered in ranked_lists:
        places = sorted(
            place_of[chunk.source_note_id] for chunk in offered if chunk.source_note_id in place_of
        )
        if places:
            reserved.add(places[0])
    # No `[:limit]` on `reserved`: the fill loop stops at `limit`, and the fused ranking already
    # decides which legs go without when there are more legs than slots.
    keep = set(reserved)
    for index in range(len(fused)):
        if len(keep) >= limit:
            break
        keep.add(index)
    return [chunk for index, chunk in enumerate(fused) if index in keep] + [
        chunk for index, chunk in enumerate(fused) if index not in keep
    ]
