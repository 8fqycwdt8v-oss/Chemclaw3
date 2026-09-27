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

        corpora: The corpus each list reads, positionally. Given, the fusion runs in **two
            stages** — within a corpus first, then across corpora — so a corpus read by three legs
            votes once rather than three times. Omitted (or all-distinct), every list is its own
            corpus and this is exactly the single-stage fusion it has always been.

            **RRF's premise is independent rankers, and three legs over one note corpus are not
            independent.** Measured on the shipped `knowledge/` corpus: `graph ∩ lexical` = 47/55,
            `graph ∩ vector` = 44/55, `lexical ∩ vector` = 41/53, because the shipped
            `embedding_provider` is `hash` — token-count hashing — so all three are term-overlap
            rankers. The agreement term then swamps the rank term: it contains no `k`, so a note
            found at rank 1 by two legs scores `2/(k + 1/w)` against a single leg's `1/(k+1)`, and
            the first wins for *every* positive `k` and `w`. That is arithmetic rather than tuning,
            which is why neither `retrieval_fusion_k` nor `retrieval_source_weights` closes it —
            both were driven and both changed the order on 0 of 7 queries.

            The corpus's own weight, for the cross-corpus stage, is the **mean** of its sources'
            weights. A corpus read by one source therefore keeps that source's weight exactly, so
            every existing configuration fuses as it did; the mean is the generalisation that
            leaves that case alone.

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

    Separate from the single-stage body rather than folded into it, because the recursion would
    otherwise be a branch inside a loop that is already the hot path of every sweep — and because
    the two stages answer different questions. Within a corpus the legs *are* the heterogeneous
    rankers RRF assumes, and the agreement between them is real information about that corpus.
    Across corpora the agreement term is what it was designed to be again: a note two different
    bodies of evidence both surface.

    `ingest/documents/retriever.py` has always done this for the mounted share — it fuses its dense
    and lexical legs internally, so the share votes once — and that is the second caller this rule
    needed before it was worth naming.
    """
    grouped: dict[str, list[list[EvidenceChunk]]] = {}
    tiers: dict[str, list[float]] = {}
    for corpus, chunks in zip(corpora, ranked_lists, strict=True):
        grouped.setdefault(corpus, []).append(chunks)
        # The tier of each *list*, taken from the retriever that produced it. Read off the chunks
        # rather than from a source name the fusion is not given, and an empty list is **skipped**,
        # because it contributes nothing to the ranking and must not skew its corpus's mean either.
        #
        # That second half is what the code did not do: `extend(tier or {1.0})` defaulted an empty
        # leg to the *neutral* tier and then averaged it in. Measured with
        # `{graph: 1.5, lexical: 1.5, eln: 1.0}`: two legs over one note corpus give 1.5, and the
        # lexical leg returning zero chunks gives **1.25** — a corpus pulled a quarter of the way to
        # neutral by a leg that found nothing. No order flip was observed (RRF is near-flat at
        # `k=60`), which is why this was a comment claiming a control rather than a visible defect.
        tiers.setdefault(corpus, [])
        tiers[corpus].extend({(weights or {}).get(chunk.retriever, 1.0) for chunk in chunks})
    fused_per_corpus = {
        corpus: reciprocal_rank_fusion(lists, k=k, weights=weights)
        for corpus, lists in grouped.items()
    }
    # The cross-corpus stage reuses the same body by keying its weights on the corpus name, which
    # is what the representative chunk's `retriever` is rewritten to for the duration. Rewritten on
    # a copy: the chunk a caller receives must still name the leg that found it, because that is
    # what a citation and `sources_truncated` are read against.
    relabelled: list[list[EvidenceChunk]] = []
    corpus_weights: dict[str, float] = {}
    for corpus, chunks in fused_per_corpus.items():
        # `or [1.0]` for the corpus every one of whose legs came back empty: it has no chunk in the
        # fusion, so the weight is never applied to anything, and the alternative is a
        # `ZeroDivisionError` on a total retrieval miss for that corpus. It is the one place a
        # neutral default is right — there is no ranking here for it to skew.
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

    Applied to **both** merge modes. It was `hybrid` only, on the argument that round-robin
    preserves each source's own ordering so a chunk's score still explains its position within the
    list it came from — an argument that was thin (the delivered list is interleaved, so the reader
    sees one column, not four) and became false when `GraphRetriever` started ranking by BM25-lite
    relevance rather than by the confidence it writes into this field. Measured in `graph` mode over
    the shipped corpus, the score column was monotone with the delivered order on **2 of 7**
    queries, and those two returned one and two chunks.
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

    **This is the RRF-side half of `D-2026-08-01-a-cap-that-starves-a-source`.** That decision made
    truncation round-robin across sources so a flat cut could not take a whole leg to zero — and it
    left the fused path alone, on the argument that RRF ranks by position and so cannot be
    dominated the way a score-sorted union was. That argument stopped holding when
    `retrieval_source_weights` arrived: a weight divides the rank, so a large enough one puts one
    leg's whole list above every other leg's best hit, and the cut then takes only that leg. Driven
    at the shipped `retrieval_fusion_k=60` over five legs, counting the retriever of each kept
    chunk: uniform weighting keeps `graph 2 / lexical 2 / share 2 / vector 1 / warehouse 1` out of
    eight, and `{"graph": 10}` keeps **`graph 8` and nothing else**.

    **The floor is one chunk per leg, and it is a floor rather than a share on purpose.** One is
    exactly what round-robin's first pass gives, so this says the same thing the ADR said, at the
    one cut it did not reach. Anything larger is an allocation, and the shape of allocation the ADR
    considered — proportional to what a leg returned — it rejected outright, for rewarding a source
    that returns many weak hits. Measured on the same five legs, a floor of `limit // (2 x legs)`
    moves cuts that were never starved (at 30: `graph 22` becomes 18; at 40: 30 becomes 24), which
    is a second policy rather than a guard against the first one failing.

    **Inert unless a leg is actually at zero**, by construction and measured: if every leg already
    has a representative inside the first `limit`, every reserved position is already below it and
    the reordering is the identity. Uniform weighting is byte-identical at cuts of 8, 30 and 40,
    and so is `{"graph": 10}` at 30 and 40 — where nothing was starved. Only the cut of 8 moves,
    to `graph 4 / lexical 1 / share 1 / vector 1 / warehouse 1`: the weight still buys graph half
    the window, which is what a deployment that wrote `10` asked for.

    **Leg membership comes from `ranked_lists`, never from `chunk.retriever`.** After the fusion's
    `representative.setdefault`, a note found by three legs carries the name of whichever found it
    *first*, so counting a leg's survivors by that field credits earlier legs and pins later ones
    at zero — the error `fanout.record_kept_chunks` documents having measured as `graph 16,
    lexical 0, vector 0`.

    **The corpus path is not a second reason for that, though an earlier draft said it was.**
    `_fuse_by_corpus` does relabel `retriever` to the corpus name — on a `model_copy`, returning
    the originals by note id, which its own comment says is the point ("the chunk a caller receives
    must still name the leg that found it"). So the relabelling never escapes that function and a
    `retriever`-reading floor would not see a corpus name here. The reason above stands on its own;
    this one was invented to reinforce it and contradicted the code eighty lines up.

    **What it does not reach is the character budget.** `gather_evidence_max_chars` is a second cut
    that spends down this same order, so a promoted chunk can still be cut by it. A promoted leg is
    nonetheless strictly better off than before: it was *certainly* cut by the count cap and now
    merely might be cut by the character one. That is the whole claim. An earlier draft argued it
    from the promoted chunks landing "at their own fused position", which is not what happens —
    measured on the case above they land at the *end* of the window, `lexical-0` moving from fused
    index 10 to output index 4 and `warehouse-0` from 13 to 7. What is bounded here is the count
    cap, which is the cut the row is about.
    """
    place_of = {chunk.source_note_id: index for index, chunk in enumerate(fused)}
    reserved: set[int] = set()
    for offered in ranked_lists:
        places = sorted(
            place_of[chunk.source_note_id] for chunk in offered if chunk.source_note_id in place_of
        )
        if places:
            reserved.add(places[0])
    # No `[:limit]` on `reserved`: with more legs than slots the fill loop below stops at `limit`
    # and the output's first `limit` entries are the lowest reserved indices either way, so a slice
    # here changed only the discarded tail. It read as if it decided which legs go without, and it
    # never did — the fused ranking decides that, which is the right answer and was already the
    # behaviour. Fuzzed both ways: 0 windows differ over 5,000 sweeps at every limit.
    keep = set(reserved)
    for index in range(len(fused)):
        if len(keep) >= limit:
            break
        keep.add(index)
    return [chunk for index, chunk in enumerate(fused) if index in keep] + [
        chunk for index, chunk in enumerate(fused) if index not in keep
    ]
