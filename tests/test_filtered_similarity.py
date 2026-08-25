"""A structural hit you cannot qualify is a structural hit you cannot use (D-170).

`FingerprintReactionRetriever` ignored `filters` entirely, so "similar reactions, but only the
ones on this campaign" had no answer: the retriever handed back the ten nearest neighbours
whatever they were, and every other retriever in the same sweep had already narrowed itself.

The part worth pinning is *where* the filter is applied. The fingerprint index stores bits and a
label and knows nothing about notes, so the filter can only run after the neighbours come back —
and running it on the returned page would mean one unwanted neighbour costs a wanted one. It has
to search deeper first. That property is invisible in a small fixture unless a test builds a
corpus where the wanted hits sit below the page boundary, which is what this one does.
"""

import asyncio

import pytest

from chemclaw.core.config import settings
from chemclaw.ingest.eln.records import (
    InMemoryReactionRecordStore,
    ReactionRecord,
)
from chemclaw.retrieval.retrievers import FingerprintReactionRetriever
from chemclaw.science.fingerprints.rxnfp.search import record_for_reaction
from chemclaw.science.fingerprints.store import InMemoryFingerprintStore

_QUERY = "CCO.CC(=O)O>>CCOC(C)=O.O"


async def _records(**projects: str | None) -> InMemoryReactionRecordStore:
    """A record store holding one transcription per reaction id, with its project."""
    store = InMemoryReactionRecordStore()
    await store.record(
        [
            ReactionRecord(
                reaction_id=reaction_id,
                body=f"Body of {reaction_id}.",
                project=project,
                source="eln:test",
            )
            for reaction_id, project in projects.items()
        ]
    )
    return store


async def _indexed(reactions: dict[str, str]) -> InMemoryFingerprintStore:
    """A reaction index holding `{id: reaction_smiles}`."""
    store = InMemoryFingerprintStore()
    for record_id, smiles in reactions.items():
        await store.add(record_for_reaction(record_id, smiles))
    return store


def test_an_unfiltered_search_is_unchanged_including_the_unstored_record() -> None:
    """No filter means no corpus read and no drop — the D-018 pending-note citation survives.

    That citation is deliberate: the fingerprint index is written at ingestion while the note is
    merged separately, so a hit whose note is still in review yields a reference `kg-validate`
    flags on the report PR. Narrowing must not quietly delete a behaviour nobody asked to change.
    """

    async def _run() -> None:
        store = await _indexed({"r1": _QUERY})
        retriever = FingerprintReactionRetriever(store, InMemoryReactionRecordStore())
        chunks = await retriever.retrieve(_QUERY, {})
        assert [c.source_note_id for c in chunks] == ["reaction-r1"]  # no record stored

    asyncio.run(_run())


def test_a_tag_filter_narrows_to_the_records_that_carry_it() -> None:
    """The whole point: "similar, and on this campaign" was previously unanswerable."""

    async def _run() -> None:
        store = await _indexed({"r1": _QUERY, "r2": "CCO.CC(=O)Cl>>CCOC(C)=O.Cl"})
        retriever = FingerprintReactionRetriever(store, await _records(r1="step-3", r2="step-9"))

        assert len(await retriever.retrieve(_QUERY, {})) == 2
        narrowed = await retriever.retrieve(_QUERY, {"tag": "step-3"})
        assert [c.source_note_id for c in narrowed] == ["reaction-r1"]

    asyncio.run(_run())


def test_a_type_filter_drops_a_hit_whose_record_is_not_that_type() -> None:
    """`type` is the other half of the gate every note retriever already applies."""

    async def _run() -> None:
        store = await _indexed({"r1": _QUERY})
        retriever = FingerprintReactionRetriever(store, await _records(r1=None))

        assert len(await retriever.retrieve(_QUERY, {"type": "reaction"})) == 1
        assert await retriever.retrieve(_QUERY, {"type": "playbook"}) == []

    asyncio.run(_run())


def test_a_filtered_hit_whose_record_is_missing_is_dropped() -> None:
    """The one place the pending-note citation does not apply, and deliberately.

    A filter says "only notes that are X". A note nobody can read cannot be *shown* to be X, so
    serving it would answer a narrowed question with an unnarrowed hit — the same rule an undated
    note fails a date window under.
    """

    async def _run() -> None:
        store = await _indexed({"r1": _QUERY})
        retriever = FingerprintReactionRetriever(store, InMemoryReactionRecordStore())
        assert await retriever.retrieve(_QUERY, {"tag": "step-3"}) == []

    asyncio.run(_run())


def test_the_filter_is_applied_before_truncation_not_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The property the whole design turns on, and the one a small fixture would never reveal.

    Twelve indexed reactions, a two-hit page, and the only tagged reaction sits *outside* the two
    nearest neighbours. Filtering the returned page would find nothing at all; searching deeper
    first and then narrowing finds it. As the index gets better at surfacing near-duplicates, the
    naive order returns *fewer* results, which is exactly backwards.
    """

    async def _run() -> None:
        # Near-identical esterifications, so all twelve crowd the top of the ranking together.
        reactions = {f"r{i}": f"CCO.CC(=O)O>>CCOC(C)=O.O.{'[Na+].[Cl-].' * i}O" for i in range(12)}
        store = await _indexed(reactions)
        projects = dict.fromkeys(reactions, "untagged") | {"r11": "wanted"}

        monkeypatch.setattr(settings, "fingerprint_top_k", 2)
        monkeypatch.setattr(settings, "fingerprint_similarity_threshold", 0.0)
        retriever = FingerprintReactionRetriever(store, await _records(**projects))

        page = await retriever.retrieve(_QUERY, {})
        assert len(page) == 2
        assert "reaction-r11" not in [c.source_note_id for c in page]  # outside the page

        narrowed = await retriever.retrieve(_QUERY, {"tag": "wanted"})
        assert [c.source_note_id for c in narrowed] == ["reaction-r11"]

    asyncio.run(_run())


def test_the_deeper_search_is_still_bounded_by_the_index_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The over-fetch may not become a way around the cap on how much of the index a query pulls."""
    monkeypatch.setattr(settings, "fingerprint_max_top_k", 12)
    monkeypatch.setattr(settings, "retrieval_filter_overfetch", 1000)
    assert FingerprintReactionRetriever._depth(10) == 12


def test_a_page_is_never_exceeded_by_the_deeper_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Searching deeper must widen what is *considered*, never what is returned."""

    async def _run() -> None:
        reactions = {f"r{i}": f"CCO.CC(=O)O>>CCOC(C)=O.O.{'[Na+].[Cl-].' * i}O" for i in range(8)}
        store = await _indexed(reactions)

        monkeypatch.setattr(settings, "fingerprint_top_k", 3)
        monkeypatch.setattr(settings, "fingerprint_similarity_threshold", 0.0)
        retriever = FingerprintReactionRetriever(
            store, await _records(**dict.fromkeys(reactions, "wanted"))
        )
        assert len(await retriever.retrieve(_QUERY, {"tag": "wanted"})) == 3

    asyncio.run(_run())
