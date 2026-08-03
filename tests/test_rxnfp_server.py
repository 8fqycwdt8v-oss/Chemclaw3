"""The mcp-rxnfp server advertises the reaction capability as MCP tools (3.4).

Mostly wiring (tool registration + schemas); the capability logic is proven in `test_rxnfp.py`.
The one tool that *is* invoked here — over a substituted in-memory store, never the production
one — is `similar_reactions` against an empty index, because that is the only level at which the
question "does the sentence reach the model?" can be answered: it travels as `model_dump()` output
over MCP, and a signal that does not survive that trip does not exist (see `ScreenResult.verdict`).
"""

import asyncio
from typing import Any

import pytest

from chemclaw.connectors.rxnfp.server import tools
from chemclaw.connectors.rxnfp.server.tools import server
from chemclaw.science.fingerprints.rxnfp.fingerprint import reaction_definition
from chemclaw.science.fingerprints.rxnfp.search import record_for_reaction
from chemclaw.science.fingerprints.store import InMemoryFingerprintStore

_ESTER_ETHYL = "CCO.CC(=O)O>>CCOC(C)=O"
_HALOGENATION = "c1ccccc1.BrBr>>Brc1ccccc1"


def test_server_advertises_the_reaction_tools() -> None:
    """The two reaction tools are registered with input schemas."""
    tools_by_name = {t.name: t for t in asyncio.run(server.list_tools())}
    assert {"similar_reactions", "index_reaction"} <= set(tools_by_name)
    assert "reaction_smiles" in tools_by_name["similar_reactions"].inputSchema["properties"]


def _structured(store: InMemoryFingerprintStore, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Call `similar_reactions` over `store` and return the structured payload MCP sends back."""
    monkeypatch.setattr(tools, "_store", store)
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
