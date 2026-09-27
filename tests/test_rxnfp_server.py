"""The mcp-rxnfp server advertises the reaction capability as MCP tools (3.4).

Mostly wiring (tool registration + schemas); the capability logic is proven in `test_rxnfp.py`.
The one tool that *is* invoked here — over a substituted in-memory store, never the production
one — is `similar_reactions` against an empty index, because that is the only level at which the
question "does the sentence reach the model?" can be answered: it travels as `model_dump()` output
over MCP, and a signal that does not survive that trip does not exist (see `ScreenResult.verdict`).
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from chemclaw.connectors.rxnfp.server import tools
from chemclaw.connectors.rxnfp.server.tools import server
from chemclaw.ingest.eln.records import (
    InMemoryReactionRecordStore,
    ReactionRecord,
)
from chemclaw.science.fingerprints.rxnfp.fingerprint import reaction_definition
from chemclaw.science.fingerprints.rxnfp.search import record_for_reaction
from chemclaw.science.fingerprints.store import InMemoryFingerprintStore

_ESTER_ETHYL = "CCO.CC(=O)O>>CCOC(C)=O"
_HALOGENATION = "c1ccccc1.BrBr>>Brc1ccccc1"


def test_server_advertises_the_reaction_tools() -> None:
    """The two reaction tools are registered with input schemas."""
    tools_by_name = {t.name: t for t in asyncio.run(server.list_tools())}
    assert {"similar_reactions"} <= set(tools_by_name)
    assert "index_reaction" not in tools_by_name, (
        "the write tool was served on the unauthenticated /mcp port while no manifest named "
        "it and nothing in the tree called it; the ingestion path uses FingerprintStore.add()"
    )
    assert "reaction_smiles" in tools_by_name["similar_reactions"].inputSchema["properties"]


def _structured(
    store: InMemoryFingerprintStore,
    monkeypatch: pytest.MonkeyPatch,
    records: InMemoryReactionRecordStore | None = None,
) -> dict[str, Any]:
    """Call `similar_reactions` over `store` and return the structured payload MCP sends back.

    The record store is substituted too, and must be: the tool now asks it which hits the source
    has withdrawn, and the module-level default is the Postgres one — so leaving it in place would
    turn every test in this file into a database test, silently.
    """
    monkeypatch.setattr(tools, "_store", store)
    monkeypatch.setattr(tools, "_records", records or InMemoryReactionRecordStore())
    _content, structured = asyncio.run(
        server.call_tool("similar_reactions", {"reaction_smiles": _ESTER_ETHYL})
    )
    assert isinstance(structured, dict)
    return structured


def test_an_empty_index_tells_the_model_the_question_was_not_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The payload a model reads must say the search did not run — not merely return `[]`.

    The end-to-end form of the live-run defect: with the fingerprint table unpopulated the tool
    answered `{"result": []}` and the model reported "we have never made anything like this".
    """
    payload = _structured(InMemoryFingerprintStore(definition=reaction_definition()), monkeypatch)

    assert payload["hits"] == []
    assert payload["index_empty"] is True
    assert "SEARCH NOT RUN" in payload["verdict"]
    assert "NOT evidence" in payload["verdict"]


def test_a_populated_index_with_no_precedent_reads_differently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same empty hit list over a real corpus is a genuine negative, and says so."""
    store = InMemoryFingerprintStore(definition=reaction_definition())
    asyncio.run(store.add(record_for_reaction("halogenation", _HALOGENATION)))

    payload = _structured(store, monkeypatch)

    assert payload["hits"] == []
    assert payload["index_empty"] is False
    assert "genuine negative" in payload["verdict"]
    assert "SEARCH NOT RUN" not in payload["verdict"]


def test_a_hit_still_carries_its_note_id_through_the_new_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: the note-id remapping survived the move from a bare list to a search.

    A hit's `id` is the *note* id (`reaction-<stem>`) so it can go straight to `expand_note`; the
    remapping now happens inside the envelope, which is exactly the step a refactor drops.
    """
    store = InMemoryFingerprintStore(definition=reaction_definition())
    asyncio.run(store.add(record_for_reaction("rxn-1", _ESTER_ETHYL)))

    payload = _structured(store, monkeypatch)

    assert [hit["id"] for hit in payload["hits"]] == ["reaction-rxn-1"]
    assert payload["index_empty"] is False
    assert payload["verdict"].startswith("1 indexed reaction(s) matched")


def test_the_identical_reaction_scores_a_perfect_similarity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A query matching an indexed reaction exactly comes back at Tanimoto 1.0, ranked first.

    Inherited from `test_search_tools.py`, which proved it against the deleted in-process wrapper
    (D-2026-08-05). The score is what the model reads as "this is the same transformation, not
    merely a relative", so it is worth pinning on the path a turn actually takes rather than on a
    lookalike of it — the wrapper had already drifted from this tool in both its arguments and its
    result model.
    """
    store = InMemoryFingerprintStore(definition=reaction_definition())
    asyncio.run(store.add(record_for_reaction("rxn-1", _ESTER_ETHYL)))
    asyncio.run(store.add(record_for_reaction("rxn-2", _HALOGENATION)))

    payload = _structured(store, monkeypatch)

    assert payload["hits"][0]["id"] == "reaction-rxn-1"
    assert payload["hits"][0]["similarity"] == 1.0


def test_a_withdrawn_reaction_is_not_served_as_a_precedent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bundle tool asked the record store nothing, so it served withdrawn runs.

    The fingerprint index holds bits and a label; whether the run still stands is the
    transcription store's to say. This is the one of the five retraction readers a chemist reaches
    *directly* — `similar_reactions` is what "have we made anything like this?" resolves to — and
    it was the reader furthest from the record: it never opened the store at all.

    Two reactions are indexed and one is withdrawn, so the assertion is a difference rather than an
    emptiness: a tool that had simply stopped returning hits would pass "the withdrawn one is
    absent" and fail this.
    """
    store = InMemoryFingerprintStore(definition=reaction_definition())
    asyncio.run(store.add(record_for_reaction("rxn-live", _ESTER_ETHYL)))
    asyncio.run(store.add(record_for_reaction("rxn-pulled", _ESTER_ETHYL.replace("CCO", "CCCO"))))
    records = InMemoryReactionRecordStore()
    asyncio.run(
        records.record(
            [
                ReactionRecord(reaction_id="rxn-live", body="b", source="eln"),
                ReactionRecord(
                    reaction_id="rxn-pulled",
                    body="b",
                    source="eln",
                    retracted_at=datetime(2026, 3, 4, tzinfo=UTC),
                ),
            ],
            "eln-json",
        )
    )

    payload = _structured(store, monkeypatch, records)

    served = [hit["id"] for hit in payload["hits"]]
    assert "reaction-rxn-live" in served, (
        "the live reaction was not served either, so the withdrawn one's absence proves nothing"
    )
    assert "reaction-rxn-pulled" not in served, (
        "a run the source withdrew is still offered as a precedent by the tool a chemist asks "
        "directly"
    )


def test_two_sites_behind_one_entry_id_are_cited_apart(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool a chemist asks directly returned two hits citing one id, and neither resolved.

    `063` keyed `reaction_fingerprints` by `(source, id)` so one site's chemistry cannot overwrite
    another's, and this tool kept spelling the bare `reaction-<id>` — so a two-source deployment
    answered "have we made anything like this?" with two hits carrying the same citation, and
    `records._one_of` refused both when either was expanded
    (`D-2026-09-13-a-citation-names-the-source-it-was-found-in`).

    The two records are byte-identical apart from their source, which is the whole point: nothing
    but the citation can tell them apart, so a tool that drops the source is offering the chemist a
    link to neither run.
    """
    store = InMemoryFingerprintStore(definition=reaction_definition())
    for site in ("site-alpha", "site-beta"):
        asyncio.run(
            store.add(
                record_for_reaction("EXP-9001", _ESTER_ETHYL).model_copy(update={"source": site})
            )
        )

    payload = _structured(store, monkeypatch)

    assert sorted(hit["id"] for hit in payload["hits"]) == [
        "reaction-site-alpha.EXP-9001",
        "reaction-site-beta.EXP-9001",
    ], "two sites behind one entry id are still cited by one id, which resolves to neither run"
