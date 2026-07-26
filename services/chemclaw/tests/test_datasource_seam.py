"""The generic data-source seam: contract, registry, and re-host (plan F7-T1/T2/T3/T4).

Proves a source may provide either half or both (and neither is rejected), that the config-driven
registry selects active ingest/retrieve halves, that `gather_evidence` fans out over
registry-declared retrievers (a second fake retriever's chunks appear), and that the re-hosted ELN
source rides the seam with its provenance intact — all offline, no DB or Temporal.
"""

import asyncio
from datetime import datetime
from typing import Any

import pytest

import agents.research_tools as research_tools
import sources.registry as registry
from chemclaw.config import settings
from eln.adapter import RawEntry
from report.evidence import EvidenceChunk
from sources.base import DataSource, SourceSpec


class _FakeRetriever:
    """A minimal retrieve half returning one fixed chunk, to prove registry fan-out."""

    name = "fake"

    async def retrieve(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        return [EvidenceChunk(content=f"hit:{query}", source_note_id="fake-1", retriever=self.name)]


class _FakeIngest:
    """A minimal ingest half (structural `ElnAdapter`)."""

    async def fetch_new_entries(self, since: datetime) -> list[RawEntry]:
        return []

    def map_to_ord(self, raw: RawEntry) -> Any:  # pragma: no cover - not exercised here
        raise NotImplementedError


def test_a_source_may_provide_either_half_or_both() -> None:
    """ingest-only, retrieve-only, and both all satisfy the DataSource protocol."""
    ingest_only = SourceSpec(name="i", ingest=_FakeIngest())
    retrieve_only = SourceSpec(name="r", retrieve=_FakeRetriever())
    both = SourceSpec(name="b", ingest=_FakeIngest(), retrieve=_FakeRetriever())
    for source in (ingest_only, retrieve_only, both):
        assert isinstance(source, DataSource)


def test_a_source_with_neither_half_is_rejected() -> None:
    """A source that can be neither ingested from nor retrieved from is a build-time error."""
    with pytest.raises(ValueError, match="must provide an ingest or retrieve half"):
        SourceSpec(name="empty")


def test_registry_selects_active_halves(monkeypatch: pytest.MonkeyPatch) -> None:
    """`data_sources` config picks which ingest/retrieve halves are active."""
    monkeypatch.setattr(settings, "data_sources", "graph,eln-json,eln-ord")
    assert len(registry.active_retrieve_sources()) == 1  # only `graph` has a retrieve half
    assert len(registry.active_ingest_sources()) == 2  # both ELN adapters have ingest halves


def test_unknown_source_is_rejected() -> None:
    """An unregistered source key raises, naming the valid keys."""
    with pytest.raises(ValueError, match="unknown data source"):
        registry.make_data_source("snowflake")  # not yet registered (deferred)


def test_default_preserves_single_graph_retriever(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default config yields exactly the one GraphRetriever gather_evidence used before F7."""
    monkeypatch.setattr(settings, "data_sources", "graph,eln-json")
    retrievers = registry.active_retrieve_sources()
    assert [r.name for r in retrievers] == ["graph"]


def test_gather_evidence_fans_out_over_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """A newly registered retrieve source appears in gather_evidence with zero edit to it."""
    monkeypatch.setitem(
        registry.DATA_SOURCES, "fake", lambda: SourceSpec(name="fake", retrieve=_FakeRetriever())
    )
    monkeypatch.setattr(settings, "data_sources", "fake")
    chunks = asyncio.run(research_tools.gather_evidence("solubility"))
    assert any("hit:solubility" in c.content for c in chunks)  # framed, but the payload survives


def test_rehosted_eln_source_carries_provenance() -> None:
    """The re-hosted ELN source rides the seam; its adapter is the existing one (F7-T4)."""
    from eln.json_adapter import JsonExportAdapter

    source = registry.make_data_source("eln-json")
    assert source.name == "eln-json"
    assert isinstance(source.ingest, JsonExportAdapter)  # the existing adapter, unchanged
    assert source.retrieve is None  # ELN is ingest-only; retrieval is the graph source's job
