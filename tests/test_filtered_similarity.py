"""Filters narrow structural reaction similarity, applied before truncation.

The fingerprint index knows nothing about notes, so the filter runs after neighbours come back;
filtering the returned page would let an unwanted neighbour cost a wanted one, so the retriever
searches deeper first. The fixture puts the wanted hits below the page boundary to show it.
"""

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
        ],
        "eln-json",
    )
    return store


async def _indexed(reactions: dict[str, str]) -> InMemoryFingerprintStore:
    """A reaction index holding `{id: reaction_smiles}`."""
    store = InMemoryFingerprintStore()
    for record_id, smiles in reactions.items():
        await store.add(record_for_reaction(record_id, smiles))
    return store


async def test_an_unfiltered_search_is_unchanged_including_the_unstored_record() -> None:
    """An unfiltered search is unchanged, including a hit whose record is not stored yet.

    The fingerprint index is written at ingestion separately from the record, so such a hit still
    yields a citation.
    """
    store = await _indexed({"r1": _QUERY})
    retriever = FingerprintReactionRetriever(store, InMemoryReactionRecordStore())
    chunks = await retriever.retrieve(_QUERY, {})
    assert [c.source_note_id for c in chunks] == ["reaction-r1"]  # no record stored


async def test_a_tag_filter_narrows_to_the_records_that_carry_it() -> None:
    """The whole point: "similar, and on this campaign" was previously unanswerable."""
    store = await _indexed({"r1": _QUERY, "r2": "CCO.CC(=O)Cl>>CCOC(C)=O.Cl"})
    retriever = FingerprintReactionRetriever(store, await _records(r1="step-3", r2="step-9"))

    assert len(await retriever.retrieve(_QUERY, {})) == 2
    narrowed = await retriever.retrieve(_QUERY, {"tag": "step-3"})
    assert [c.source_note_id for c in narrowed] == ["reaction-r1"]


async def test_a_type_filter_drops_a_hit_whose_record_is_not_that_type() -> None:
    """`type` is the other half of the gate every note retriever already applies."""
    store = await _indexed({"r1": _QUERY})
    retriever = FingerprintReactionRetriever(store, await _records(r1=None))

    assert len(await retriever.retrieve(_QUERY, {"type": "reaction"})) == 1
    assert await retriever.retrieve(_QUERY, {"type": "playbook"}) == []


async def test_a_filtered_hit_whose_record_is_missing_is_dropped() -> None:
    """A filtered hit whose record is missing is dropped.

    A record nobody can read cannot be shown to match the filter.
    """
    store = await _indexed({"r1": _QUERY})
    retriever = FingerprintReactionRetriever(store, InMemoryReactionRecordStore())
    assert await retriever.retrieve(_QUERY, {"tag": "step-3"}) == []


async def test_the_filter_is_applied_before_truncation_not_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The filter is applied before truncation, not after.

    Twelve reactions, a two-hit page, and the only tagged reaction outside the two nearest:
    filtering the page finds nothing, searching deeper then narrowing finds it.
    """
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


def test_the_deeper_search_is_still_bounded_by_the_index_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The over-fetch may not become a way around the cap on how much of the index a query pulls."""
    monkeypatch.setattr(settings, "fingerprint_max_top_k", 12)
    monkeypatch.setattr(settings, "retrieval_filter_overfetch", 1000)
    assert FingerprintReactionRetriever._depth(10) == 12


async def test_a_page_is_never_exceeded_by_the_deeper_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Searching deeper must widen what is *considered*, never what is returned."""
    reactions = {f"r{i}": f"CCO.CC(=O)O>>CCOC(C)=O.O.{'[Na+].[Cl-].' * i}O" for i in range(8)}
    store = await _indexed(reactions)

    monkeypatch.setattr(settings, "fingerprint_top_k", 3)
    monkeypatch.setattr(settings, "fingerprint_similarity_threshold", 0.0)
    retriever = FingerprintReactionRetriever(
        store, await _records(**dict.fromkeys(reactions, "wanted"))
    )
    assert len(await retriever.retrieve(_QUERY, {"tag": "wanted"})) == 3
