"""One boolean semantics for the lexical leg, so rank fusion is not fed an empty list.

`PostgresNoteIndex` and the in-memory reference must agree: an AND-only backend makes an ordinary
multi-word question match nothing, so fusion runs one-legged. The widened form ORs the parsed
query's clauses, so `websearch_to_tsquery`'s `-term` exclusion and quoted phrases survive; both are
asserted on both backends. RRF cannot fix an empty leg: a list with no rows contributes nothing to
any fusion rule. The server-backed half needs Postgres and skips offline.
"""

import asyncio
from typing import Any

import pytest

from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.core.embeddings import embed_texts
from chemclaw.retrieval.evidence import EvidenceChunk
from chemclaw.retrieval.hybrid import reciprocal_rank_fusion
from chemclaw.retrieval.vector_index import (
    InMemoryNoteIndex,
    NoteIndex,
    NoteRecord,
    PostgresNoteIndex,
    note_embedding_key,
)
from tests.pg import migrated_db_or_skip

# One corpus, used by both backends, built so the query's terms are split across notes:
# `complete` holds all four, `partial-*` hold two each, `unrelated` none. Under the old AND-only
# durable statement this query matched nothing at all.
_CORPUS = {
    "complete": "amide coupling solvent screen",
    "partial-solvent": "solvent screen for the Suzuki step",
    "partial-amide": "amide coupling with HATU",
    "unrelated": "distillation reflux ratio study",
}
_QUERY = "amide coupling solvent screen"
# The same question with the solvent notes excluded: exactly one note (`partial-amide`) qualifies.
_EXCLUDING_QUERY = "amide coupling -solvent"


async def _load(index: NoteIndex, corpus: dict[str, str] | None = None) -> None:
    """Fill `index` with `_CORPUS` (or a subset), embedding each note with the config's embedder."""
    notes = _CORPUS if corpus is None else corpus
    texts = list(notes.values())
    embeddings = await asyncio.to_thread(embed_texts, texts)
    await index.upsert(
        [
            NoteRecord(note_id=note_id, text=text, embedding=embedding)
            for note_id, text, embedding in zip(notes, texts, embeddings, strict=True)
        ],
        note_embedding_key(),
    )


async def test_inmemory_lexical_ranks_a_complete_match_above_a_partial_one() -> None:
    """Every term first, then partial matches — a partial match is still a hit, not nothing."""
    index = InMemoryNoteIndex()
    await _load(index)
    hits = [h.note_id for h in await index.search_lexical(_QUERY, top_k=5)]
    assert hits[0] == "complete"
    assert set(hits) == {"complete", "partial-solvent", "partial-amide"}
    assert "unrelated" not in hits


async def test_inmemory_lexical_returns_partial_matches_when_nothing_matches_every_term() -> None:
    """A question no single note fully answers still returns the notes that answer part of it.

    This is the failure the durable backend had and the in-memory one hid: answering "nothing known"
    to a four-word question about a corpus that holds three relevant notes.
    """
    index = InMemoryNoteIndex()
    # Everything but the note that holds all four terms, so nothing matches them all.
    await _load(index, {k: v for k, v in _CORPUS.items() if k != "complete"})
    hits = [h.note_id for h in await index.search_lexical(_QUERY, top_k=5)]
    assert set(hits) == {"partial-solvent", "partial-amide"}


async def test_postgres_lexical_states_the_same_boolean_rule_as_the_reference() -> None:
    """The durable backend and the in-memory reference return the same notes, complete first.

    Scores still differ — `ts_rank` against a token count — so this asserts on the *set* and on the
    top position, which is exactly what the two backends are required to agree about.
    """
    await migrated_db_or_skip()
    async with await connect(settings.postgres_dsn) as conn:
        await conn.execute("TRUNCATE note_index")
        await conn.commit()

    durable = PostgresNoteIndex()
    reference = InMemoryNoteIndex()
    await _load(durable)
    await _load(reference)

    durable_hits = [h.note_id for h in await durable.search_lexical(_QUERY, top_k=5)]
    reference_hits = [h.note_id for h in await reference.search_lexical(_QUERY, top_k=5)]
    assert durable_hits[0] == "complete"
    assert set(durable_hits) == set(reference_hits)
    assert "unrelated" not in durable_hits


async def test_a_negated_term_is_excluded_by_the_reference() -> None:
    """`-solvent` removes the solvent notes in the in-memory reference instead of asking for them.

    A reference that reads an exclusion backwards cannot witness the durable backend doing so.
    """
    index = InMemoryNoteIndex()
    await _load(index)
    hits = [h.note_id for h in await index.search_lexical(_EXCLUDING_QUERY, top_k=5)]
    assert hits == ["partial-amide"]


async def test_a_negated_term_is_excluded_by_the_durable_backend() -> None:
    """`-solvent` removes the solvent notes in the durable backend.

    The widened query must be `( 'amid' | 'coupl' ) & !'solvent'`, not `'amid' | 'coupl' |
    'solvent'`.
    """
    await migrated_db_or_skip()
    async with await connect(settings.postgres_dsn) as conn:
        await conn.execute("TRUNCATE note_index")
        await conn.commit()

    durable = PostgresNoteIndex()
    reference = InMemoryNoteIndex()
    await _load(durable)
    await _load(reference)
    hits = [h.note_id for h in await durable.search_lexical(_EXCLUDING_QUERY, top_k=5)]
    assert hits == ["partial-amide"]
    assert hits == [h.note_id for h in await reference.search_lexical(_EXCLUDING_QUERY, 5)]
    # Widening is what the exclusion must not undo: the same question without the `-` still
    # returns every note sharing any term, which is the property PR #173 was written for.
    widened = await durable.search_lexical("amide coupling solvent", top_k=5)
    assert {h.note_id for h in widened} == {"complete", "partial-solvent", "partial-amide"}


async def test_a_quoted_phrase_survives_the_widening() -> None:
    """A quoted phrase survives the widening.

    Splitting the top-level conjunction must not take a `'a' <-> 'b'` phrase apart, which would
    silently answer a different question.
    """
    await migrated_db_or_skip()
    async with await connect(settings.postgres_dsn) as conn:
        await conn.execute("TRUNCATE note_index")
        await conn.commit()

    durable = PostgresNoteIndex()
    await _load(durable)
    # "coupling amide" is present as two words in no note in that order, so a phrase match is
    # empty while the same two words unquoted match two notes.
    assert await durable.search_lexical('"coupling amide"', top_k=5) == []
    assert {h.note_id for h in await durable.search_lexical("coupling amide", top_k=5)} == {
        "complete",
        "partial-amide",
    }


async def test_postgres_lexical_still_scopes_and_still_excludes_a_termless_query() -> None:
    """Widening changes which notes match, not the `within` bound or what counts as a query.

    A stop-word-only question has no lexemes to widen to and must return nothing rather than the
    whole corpus — the one way "match any term" could have become "match anything".
    """
    await migrated_db_or_skip()
    async with await connect(settings.postgres_dsn) as conn:
        await conn.execute("TRUNCATE note_index")
        await conn.commit()

    durable = PostgresNoteIndex()
    await _load(durable)
    scoped = await durable.search_lexical(_QUERY, top_k=5, within={"partial-amide"})
    assert [h.note_id for h in scoped] == ["partial-amide"]
    assert await durable.search_lexical("the and of", top_k=5) == []


def _chunks(retriever: str, note_ids: list[str], score: float) -> list[EvidenceChunk]:
    """A retriever's ranked hit-list, every chunk carrying the same (irrelevant) score.

    Identical scores on purpose: RRF must decide the outcome from rank position alone, so a test
    that varied them would not be able to tell the two apart.
    """
    return [
        EvidenceChunk(content=note_id, source_note_id=note_id, retriever=retriever, score=score)
        for note_id in note_ids
    ]


def test_rrf_is_decided_by_rank_and_not_by_the_legs_score_scales() -> None:
    """Two legs whose scores differ by two orders of magnitude fuse purely on position.

    RRF never compares scores across legs, which is also why it could not fix an empty leg.
    """
    dense = _chunks("vector", ["b", "a", "c"], score=0.91)
    lexical = _chunks("lexical", ["a", "b", "c"], score=0.004)
    fused = [chunk.source_note_id for chunk in reciprocal_rank_fusion([dense, lexical], k=60)]
    rescaled = [
        chunk.source_note_id
        for chunk in reciprocal_rank_fusion(
            [_chunks("vector", ["b", "a", "c"], score=0.02), lexical], k=60
        )
    ]
    assert fused == rescaled, "fusion must not depend on either leg's score scale"
    # 'a' and 'b' tie on rank sum (1+2 each); 'c' is last in both. The tie breaks by note id.
    assert fused == ["a", "b", "c"]


def test_three_legs_over_one_corpus_vote_once() -> None:
    """Three legs over one corpus vote once, not three times.

    RRF assumes independent rankers, and `graph`, `lexical` and `vector` over one note tree are
    correlated. Single-stage, a three-leg corpus's shared hit beats another corpus's best hit for
    any `k` and weight. Two-stage fuses each corpus first, so both arrive as one rank-1 vote and
    tie.
    """
    legs = [
        _chunks("graph", ["shared", "filler-a"], score=1.0),
        _chunks("lexical", ["shared", "filler-b"], score=1.0),
        _chunks("vector", ["shared", "filler-c"], score=1.0),
        _chunks("sharedrive", ["other"], score=1.0),
    ]
    corpora = ["knowledge-notes", "knowledge-notes", "knowledge-notes", "sharedrive"]

    single = [chunk.source_note_id for chunk in reciprocal_rank_fusion(legs, k=60)]
    assert single[0] == "shared", "the premise: three correlated legs win on agreement alone"

    two_stage = [
        chunk.source_note_id for chunk in reciprocal_rank_fusion(legs, k=60, corpora=corpora)
    ]
    assert two_stage.index("other") < two_stage.index("shared"), (
        f"one corpus, one vote: {two_stage} still lets the three-leg corpus outrank the one-leg "
        "corpus's best hit"
    )
    # And a chunk still names the leg that found it — the relabelling is internal to the fusion,
    # because a citation and `sources_truncated` are read against `retriever`.
    assert {c.retriever for c in reciprocal_rank_fusion(legs, k=60, corpora=corpora)} <= {
        "graph",
        "lexical",
        "vector",
        "sharedrive",
    }


def test_an_empty_leg_does_not_pull_its_corpus_tier_toward_neutral() -> None:
    """An empty leg does not pull its corpus tier toward neutral.

    An empty list contributes nothing and must not be averaged into its corpus's weight as 1.0.
    Asserted on the weight passed to the cross-corpus stage (by spying on the recursive call), since
    RRF is near-flat at `k=60` and no output order flips.
    """
    from chemclaw.retrieval import hybrid

    legs = [
        _chunks("graph", ["n1", "n2"], score=1.0),
        _chunks("lexical", [], score=1.0),
        _chunks("eln", ["e1", "e2"], score=1.0),
    ]
    corpora = ["notes", "notes", "eln"]
    weights = {"graph": 1.5, "lexical": 1.5, "eln": 1.0}
    seen: list[dict[str, float]] = []
    original = hybrid.reciprocal_rank_fusion

    def _spy(
        lists: Any, *, k: int = 60, weights: Any = None, corpora: Any = None
    ) -> list[EvidenceChunk]:
        if corpora is None:  # the cross-corpus stage, whose weights are the corpus tiers
            seen.append(dict(weights or {}))
        return original(lists, k=k, weights=weights, corpora=corpora)

    try:
        hybrid.reciprocal_rank_fusion = _spy  # type: ignore[assignment]
        original(legs, k=60, weights=weights, corpora=corpora)
    finally:
        hybrid.reciprocal_rank_fusion = original

    assert seen[-1] == {"notes": 1.5, "eln": 1.0}, (
        "a leg that found nothing must not vote on its corpus's tier"
    )


def test_a_corpus_whose_every_leg_came_back_empty_still_has_a_weight() -> None:
    """A corpus whose every leg came back empty still has a weight.

    The mean of nothing would raise on a total miss; the weight applies to no chunk, so 1.0 is
    harmless there.
    """
    legs = [_chunks("graph", ["n1"], score=1.0), _chunks("lexical", [], score=1.0)]
    fused = reciprocal_rank_fusion(
        legs, k=60, weights={"graph": 1.5, "lexical": 1.5}, corpora=["notes", "empty"]
    )
    assert [chunk.source_note_id for chunk in fused] == ["n1"]


def test_all_distinct_corpora_fuse_exactly_as_before() -> None:
    """Naming every list its own corpus is exactly the single-stage fusion.

    Every shipped configuration runs one leg per corpus, and the two-stage path must not re-rank it.
    """
    legs = [
        _chunks("graph", ["a", "b", "c"], score=1.0),
        _chunks("vector", ["b", "c", "a"], score=1.0),
    ]
    assert [c.source_note_id for c in reciprocal_rank_fusion(legs, k=60)] == [
        c.source_note_id for c in reciprocal_rank_fusion(legs, k=60, corpora=["graph", "vector"])
    ]


def test_a_corpus_list_that_does_not_match_the_ranked_lists_is_refused() -> None:
    """The mapping is positional, so a length mismatch would fuse a list under another's corpus."""
    legs = [_chunks("graph", ["a"], score=1.0), _chunks("vector", ["b"], score=1.0)]
    with pytest.raises(ValueError, match="positional"):
        reciprocal_rank_fusion(legs, k=60, corpora=["knowledge-notes"])


def test_a_leg_that_returns_nothing_cannot_be_rescued_by_the_fusion() -> None:
    """A leg that returns nothing cannot be rescued by the fusion.

    `sum(1/(k+rank))` over an empty list is zero for any `k`, so the fix belongs in the backends.
    """
    dense = _chunks("vector", ["b", "a", "c"], score=0.91)
    one_legged = [chunk.source_note_id for chunk in reciprocal_rank_fusion([dense, []], k=60)]
    assert one_legged == ["b", "a", "c"] == [chunk.source_note_id for chunk in dense]


async def test_the_widened_lexical_leg_changes_what_the_fusion_produces() -> None:
    """End to end: the widened lexical leg contributes a ranking, and the fused order reflects it.

    Same dense ranking in both halves; under AND-only semantics the sweep returned it verbatim.
    """
    index = InMemoryNoteIndex()
    await _load(index)
    lexical_hits = await index.search_lexical(_QUERY, top_k=5)
    assert lexical_hits, "the leg must contribute a ranking at all"
    lexical = _chunks("lexical", [h.note_id for h in lexical_hits], score=0.004)
    # The dense leg ranks the note that fully matches the question *last*, so a two-legged
    # fusion and a one-legged one cannot produce the same order.
    dense = _chunks("vector", ["unrelated", "partial-amide", "complete"], score=0.9)
    two_legged = [c.source_note_id for c in reciprocal_rank_fusion([dense, lexical], k=60)]
    one_legged = [c.source_note_id for c in reciprocal_rank_fusion([dense, []], k=60)]
    assert one_legged[0] == "unrelated"
    assert two_legged[0] == "complete"
    assert two_legged != one_legged


def test_the_fusion_constant_is_configured_and_defaults_to_sixty() -> None:
    """K = 60 is the value the RRF paper recommends (Cormack, Clarke & Büttcher, SIGIR 2009).

    Asserted because it is the one number in the fusion, and a default that drifts silently changes
    how much a top-ranked hit from one leg outweighs a mid-ranked hit from another.
    """
    assert settings.retrieval_fusion_k == 60
    dense = _chunks("vector", ["a", "b"], score=0.9)
    fused = reciprocal_rank_fusion([dense], k=settings.retrieval_fusion_k)
    assert [c.source_note_id for c in fused] == ["a", "b"]


@pytest.mark.parametrize("k", [1, 60, 600])
def test_a_larger_k_flattens_the_advantage_of_the_top_rank(k: int) -> None:
    """Whatever `k`, a note ranked first by both legs beats one ranked first by one of them.

    The property that makes RRF tuning-free: `k` changes how *much* rank position matters, never
    which direction it points.
    """
    both = _chunks("vector", ["shared", "solo"], score=0.9)
    other = _chunks("lexical", ["shared"], score=0.1)
    assert [c.source_note_id for c in reciprocal_rank_fusion([both, other], k=k)] == [
        "shared",
        "solo",
    ]


# --- What a source weight can and cannot do to a correlated leg ---


def test_a_weight_in_any_range_a_person_would_try_cannot_undo_a_correlated_leg() -> None:
    """No weight in a range a person would try undoes a correlated leg.

    `gold` is found only by the graph leg at rank 1; `pair` is ranked second by graph and first by
    dense, so dense's vote puts it ahead. A weight divides the rank, and the rank term is nearly
    flat at `k=60`: a rank-1 hit contributes `1/(60 + 1/w)`, about 13% less across a tenfold weight,
    which cannot close the gap the vote creates. So every weight in the documented range leaves the
    order unchanged.
    """
    for weight in (1.0, 0.5, 0.25, 0.1, 0.01, 0.001):
        fused = reciprocal_rank_fusion(
            [
                _chunks("graph", ["gold", "pair"], 1.0),
                _chunks("vector", ["pair"], 1.0),
            ],
            k=60,
            weights={"vector": weight},
        )
        order = [chunk.source_note_id for chunk in fused]
        assert order.index("pair") < order.index("gold"), (
            f"at vector weight {weight} down-weighting the correlated leg restored the gold note "
            "to the top, which would make the weight a remedy for the correlation defect. "
            "Measured over the shipped corpus it is not — re-run `make retrieval-arms` before "
            "believing this"
        )


def test_the_weight_that_would_work_is_small_enough_to_be_a_removal() -> None:
    """The weight that would work is small enough to be a removal.

    `1/(60 + 1/w) < 1/61 - 1/62` gives w < 2.7e-4, i.e. the dense hit fusing as though at rank
    ~3,700. `retrieval_source_weights` accepts it, but that is removal written as a number. Dropping
    the dense leg is not the answer either: it loses gold notes only that leg finds, and recall is
    the gated metric. Asserted in both directions.
    """

    def order_at(weight: float | None) -> list[str]:
        lists = [_chunks("graph", ["gold", "pair"], 1.0)]
        if weight is not None:
            lists.append(_chunks("vector", ["pair"], 1.0))
        fused = reciprocal_rank_fusion(
            lists, k=60, weights={"vector": weight} if weight is not None else None
        )
        return [chunk.source_note_id for chunk in fused]

    just_above = order_at(3.0e-4)
    assert just_above.index("pair") < just_above.index("gold"), (
        "a weight just above the derived 2.7e-4 crossover already restored the gold note, so the "
        "threshold in this docstring is wrong and the sweep above is measuring the wrong range"
    )
    just_below = order_at(2.0e-4)
    assert just_below.index("gold") < just_below.index("pair"), (
        "a weight below the crossover did not restore the gold note, which would mean no weight "
        "ever does — the dial would then be inert rather than impractical, a different finding"
    )
    removed = order_at(None)
    assert removed.index("gold") < removed.index("pair"), (
        "removing the correlated leg did not restore the gold note; if this fails the fusion no "
        "longer sums per-leg contributions and every number in the ADR is worth re-measuring"
    )
