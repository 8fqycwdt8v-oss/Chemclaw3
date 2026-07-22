"""The durable memory-synthesis corpus reader honors the data-source config (DUP-1).

`workflows.memory_jobs._all_reactions` is the corpus every memory job reasons over. After the F7
seam it must read the *configured* active ingest sources (`settings.data_sources`), not a hardcoded
union of every ELN adapter — so toggling `CHEMCLAW_DATA_SOURCES` actually changes what memory sees,
the same guarantee the durable ELN sync already honors. Uses the committed sample exports that the
default config points at (`eln/exports` + `eln/exports/ord`); no server needed.
"""

import asyncio

import pytest

from chemclaw.config import settings
from workflows.memory_jobs import _all_reactions


def test_all_reactions_honors_data_sources_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adding the ORD source to `data_sources` brings its reactions into the memory corpus."""
    # Default: only the free-text JSON ELN source is active.
    monkeypatch.setattr(settings, "data_sources", "graph,eln-json")
    json_only = asyncio.run(_all_reactions())
    # Adding the native-ORD source to the config expands the corpus (config drives it, not code).
    monkeypatch.setattr(settings, "data_sources", "graph,eln-json,eln-ord")
    json_and_ord = asyncio.run(_all_reactions())
    assert len(json_and_ord) > len(json_only)


def test_all_reactions_empty_when_no_ingest_source_active(monkeypatch: pytest.MonkeyPatch) -> None:
    """With only a retrieve-only source active, memory synthesis reads an empty corpus."""
    monkeypatch.setattr(settings, "data_sources", "graph")
    assert asyncio.run(_all_reactions()) == []
