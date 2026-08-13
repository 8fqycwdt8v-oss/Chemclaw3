"""One boolean semantics for the lexical leg, so the rank fusion is not fed an empty list.

The open finding this covers is "the two lexical legs disagree on AND vs OR": `PostgresNoteIndex`
ANDed the query's terms (`websearch_to_tsquery`) while `InMemoryNoteIndex` — the reference the unit
tests stand on — scored any note sharing a single token. The consequence was not a cosmetic
mismatch: an ordinary multi-word question matched nothing in production, so the lexical leg
contributed no chunks and Reciprocal Rank Fusion ran one-legged, while every test passed on the
in-memory OR.

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
from chemclaw.core.embeddings import embed_texts, embedding_config_key
from chemclaw.retrieval.evidence import EvidenceChunk
from chemclaw.retrieval.hybrid import reciprocal_rank_fusion
from chemclaw.retrieval.vector_index import (
    InMemoryNoteIndex,
    NoteIndex,
    NoteRecord,
    PostgresNoteIndex,
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
        embedding_config_key(),
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
