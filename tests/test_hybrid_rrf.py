"""One boolean semantics for the lexical leg, so the rank fusion is not fed an empty list.

The open finding this covers is "the two lexical legs disagree on AND vs OR": `PostgresNoteIndex`
ANDed the query's terms (`websearch_to_tsquery`) while `InMemoryNoteIndex` — the reference the unit
tests stand on — scored any note sharing a single token. The consequence was not a cosmetic
mismatch: an ordinary multi-word question matched nothing in production, so the lexical leg
contributed no chunks and Reciprocal Rank Fusion ran one-legged, while every test passed on the
in-memory OR.

**Widening a query is not the same as widening its lexemes, and the first fix confused the two.**
The widened form was built by ORing `tsvector_to_array(to_tsvector(q))` — every stem in the query
text — and `to_tsvector` does not know `websearch_to_tsquery`'s `-term` exclusion syntax. So a
`-solvent` exclusion came back as a positive OR term and a chemist who typed it got the solvent
notes: measured on the corpus below, `amide coupling -solvent` returned all four notes including
`solvent-guide`, whose whole body is "solvent selection guide". `test_a_negated_term_is_excluded_*`
is what would have caught it, and it is asserted on both backends because a reference that reads an
exclusion as a request is the same defect in the mirror.

**RRF is not the fix for that, and this file is where the distinction is made checkable.** Fusing by
rank is what lets a cosine and a `ts_rank` be combined without agreeing on a score scale, and it is
already how `hybrid` mode merges the sources — but a leg that returns *no rows* contributes nothing
to any fusion rule. So the semantics had to be made one, and the tests below assert both halves:
that the two backends now answer a multi-word question the same way, and that the fusion is
consequently two-legged where it used to be one.

The server-backed half needs a real Postgres and skips in the offline sandbox.
"""

import asyncio

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
# The same question with the solvent notes taken out of it. The corpus needs nothing added to make
# this discriminating: two of its notes carry `solvent` and must drop out, `partial-amide` carries
# both wanted stems and neither excluded one, and `unrelated` carries nothing wanted — so the
# answer is exactly one note, while the shipped widened form returned three.
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


def test_inmemory_lexical_ranks_a_complete_match_above_a_partial_one() -> None:
    """Every term first, then partial matches — a partial match is still a hit, not nothing."""

    async def _run() -> None:
        index = InMemoryNoteIndex()
        await _load(index)
        hits = [h.note_id for h in await index.search_lexical(_QUERY, top_k=5)]
        assert hits[0] == "complete"
        assert set(hits) == {"complete", "partial-solvent", "partial-amide"}
        assert "unrelated" not in hits

    asyncio.run(_run())


def test_inmemory_lexical_returns_partial_matches_when_nothing_matches_every_term() -> None:
    """A question no single note fully answers still returns the notes that answer part of it.

    This is the failure the durable backend had and the in-memory one hid: answering "nothing known"
    to a four-word question about a corpus that holds three relevant notes.
    """

    async def _run() -> None:
        index = InMemoryNoteIndex()
        # Everything but the note that holds all four terms, so nothing matches them all.
        await _load(index, {k: v for k, v in _CORPUS.items() if k != "complete"})
        hits = [h.note_id for h in await index.search_lexical(_QUERY, top_k=5)]
        assert set(hits) == {"partial-solvent", "partial-amide"}

    asyncio.run(_run())


def test_postgres_lexical_states_the_same_boolean_rule_as_the_reference() -> None:
    """The durable backend and the in-memory reference return the same notes, complete first.

    Scores still differ — `ts_rank` against a token count — so this asserts on the *set* and on the
    top position, which is exactly what the two backends are required to agree about.
    """

    async def _run() -> None:
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

    asyncio.run(_run())


def test_a_negated_term_is_excluded_by_the_reference() -> None:
    """`-solvent` removes the solvent notes instead of asking for them.

    The in-memory half of the regression: the reference tokenized the query flat, so the `-` was
    punctuation and `solvent` was a term the chemist had *asked* for. A reference that reads an
    exclusion backwards cannot witness the durable backend reading it backwards either.
    """

    async def _run() -> None:
        index = InMemoryNoteIndex()
        await _load(index)
        hits = [h.note_id for h in await index.search_lexical(_EXCLUDING_QUERY, top_k=5)]
        assert hits == ["partial-amide"]

    asyncio.run(_run())


def test_a_negated_term_is_excluded_by_the_durable_backend() -> None:
    """The live regression, on the backend that shipped it: `-solvent` returned the solvent notes.

    Measured on this corpus against PostgreSQL 16 / pgvector 0.8.0 before the fix — the widened form
    was `'amid' | 'coupl' | 'solvent'`, so `complete` and `partial-solvent` came back as hits and a
    chemist excluding solvent got solvent notes. It is now `( 'amid' | 'coupl' ) & !'solvent'`.
    """

    async def _run() -> None:
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

    asyncio.run(_run())


def test_a_quoted_phrase_survives_the_widening() -> None:
    """A phrase is one clause, so splitting the parsed query on ` & ` leaves it whole.

    The widening turns the parsed query's top-level conjunction into a disjunction, and the one way
    that could go wrong quietly is by taking a `'a' <-> 'b'` phrase apart into two independent
    terms — which would silently answer a different question rather than fail.
    """

    async def _run() -> None:
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

    asyncio.run(_run())


def test_postgres_lexical_still_scopes_and_still_excludes_a_termless_query() -> None:
    """Widening changes which notes match, not the `within` bound or what counts as a query.

    A stop-word-only question has no lexemes to widen to and must return nothing rather than the
    whole corpus — the one way "match any term" could have become "match anything".
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        async with await connect(settings.postgres_dsn) as conn:
            await conn.execute("TRUNCATE note_index")
            await conn.commit()

        durable = PostgresNoteIndex()
        await _load(durable)
        scoped = await durable.search_lexical(_QUERY, top_k=5, within={"partial-amide"})
        assert [h.note_id for h in scoped] == ["partial-amide"]
        assert await durable.search_lexical("the and of", top_k=5) == []

    asyncio.run(_run())


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

    This is why the legs never had to agree on a *score*: a cosine in [0, 1] and a `ts_rank` in the
    hundredths are not comparable quantities, and RRF never compares them. It is also why RRF alone
    could not have fixed the AND/OR disagreement — see the next test.
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
    """The correlated-ranker fix: a corpus read by three legs does not outvote a one-leg corpus.

    RRF's premise is *independent* rankers, and `graph`, `lexical` and `vector` are three rankers
    over one note tree — measured on the shipped corpus, their pairwise agreement is 47/55, 44/55
    and 41/53, because the shipped `embedding_provider` is `hash` and all three are therefore
    term-overlap rankers. Single-stage, that corpus casts three votes for the same note and a
    second corpus's best hit cannot reach it: the agreement term carries no `k`, so `3/(k+1)` beats
    `1/(k+1)` for every positive `k` and every weight.

    Here `shared` is ranked first by all three legs of one corpus and `other` first by the only leg
    of another. Single-stage puts `shared` first on the strength of its corpus having three legs.
    Two-stage fuses each corpus first, so both arrive as one rank-1 vote and the tie breaks by note
    id — which is the correct answer for two corpora that each put their best foot forward.
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


def test_all_distinct_corpora_fuse_exactly_as_before() -> None:
    """The other direction: naming every list its own corpus is the single-stage fusion.

    Without this the two-stage path could be satisfied by changing every ordering, and a
    deployment running one leg per corpus — which is every shipped configuration — would have had
    its retrieval silently re-ranked by a change that was supposed to leave it alone.
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
    """An empty lexical list leaves the dense ranking untouched — the one-legged sweep, exactly.

    The point of asserting this: it is the reason the AND/OR disagreement had to be fixed in the
    backends rather than in the fusion. Whatever `k` is, `sum(1/(k+rank))` over an empty list is
    zero, so no fusion rule can recover evidence a leg never returned.
    """
    dense = _chunks("vector", ["b", "a", "c"], score=0.91)
    one_legged = [chunk.source_note_id for chunk in reciprocal_rank_fusion([dense, []], k=60)]
    assert one_legged == ["b", "a", "c"] == [chunk.source_note_id for chunk in dense]


def test_the_widened_lexical_leg_changes_what_the_fusion_produces() -> None:
    """End to end: the leg now contributes a ranking, and the fused order reflects it.

    Same dense ranking in both halves; the only difference is whether the lexical leg answered the
    multi-word question. Under the old AND semantics it did not, and the sweep returned the dense
    ranking verbatim.
    """

    async def _run() -> None:
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

    asyncio.run(_run())


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


# --- What a source weight can and cannot do to a correlated leg ---------------------------------
#
# Added after the fourth remedy for the correlation row was measured and found to be a no-op
# (`D-2026-09-15-a-weight-small-enough-to-work-is-a-removal-spelled-as-a-number`). `BACKLOG.md`
# already records `retrieval_fusion_k`, `retrieval_source_weights` tiering *up*, and
# one-corpus-one-vote as measured no-ops; down-weighting the correlated leg is the one a reader
# reaches for next, and it fails for a reason that is arithmetic rather than a tuning miss.
#
# Nothing exercised the `weights=` path in this file before this, which is why the property was
# available to be believed either way.


def test_a_weight_in_any_range_a_person_would_try_cannot_undo_a_correlated_leg() -> None:
    """The fourth measured no-op, as the arithmetic that makes it one.

    The defect `BACKLOG.md` measures: three legs over one note corpus agree with each other, so a
    note two of them rank displaces the note the question is about. Down-weighting the third leg
    looks like the remedy.

    The shape, minimised. `gold` is what the question is about and only the graph leg finds it, at
    rank 1. `pair` is a near-miss the graph leg ranks *second* and the dense leg ranks first — so
    the dense leg's vote is exactly what puts `pair` ahead, and removing that vote is what the row
    wants undone.

    A weight divides the **rank**, and the rank term is nearly flat at `k=60` (the
    `reciprocal_rank_fusion` docstring says so for a different purpose): a rank-1 hit contributes
    `1/(60 + 1/w)`, which falls only from 0.01639 to 0.01429 as `w` goes 1.0 → 0.1. That is a 13%
    change across a **tenfold** weight, against a gap the vote has to give up entirely. So every
    weight in the range the config's own ENV example uses leaves the order exactly as it was.

    Driven end to end over the shipped corpus and the 46 labelled pairs, this is what "mean gold
    rank 4.72 → 4.56 → 4.69 → 4.67 at weights 1.0 / 0.5 / 0.25 / 0.1" looks like in one function.
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
    """Where the crossover actually is, which is the finding rather than "no weight works".

    Solving `1/(60 + 1/w) < 1/61 - 1/62` puts it at **w < 2.7e-4**: the dense leg's rank-1 hit has
    to fuse as though it were rank 3,729 before it stops deciding this pair. `retrieval_source_
    weights` accepts that — it refuses non-positive weights and nothing else — so the dial *can*
    reach the behaviour. It reaches it by being a removal written as a number, which is not a
    tuning range any operator would find and not a setting anybody should ship.

    That is why the options left in `BACKLOG.md` are an orthogonal embedding provider or not
    running three legs over one corpus, rather than a dial. **Neither is "drop the dense leg"**,
    and the same measurement is why: dropping it took mean gold rank 4.69 → 3.69 and cost 3 of 39
    gold notes, every one of them found *only* by that leg and one at rank 3. Recall is the gated
    retrieval metric here and rank is the diagnostic, so the trade goes the wrong way.

    Asserted in both directions, because a threshold claim with only its failing side checked is a
    claim that the feature does nothing.
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
