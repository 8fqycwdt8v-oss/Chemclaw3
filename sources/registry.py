"""The config-driven data-source registry (plan F7-T2).

Generalizes `eln/registry.py`'s `{name: factory}` pattern to the whole seam: one dict maps a source
key to a factory that builds its `DataSource`, and the active set is chosen by
`settings.data_sources`. The two consumers read this registry instead of hardcoding their sources —
`gather_evidence` fans out over `active_retrieve_sources()`, the ELN sync ingests
`active_ingest_sources()` — so attaching a new source (the first live one being a custom Snowflake
ELN connector) is one entry here plus one config token, with no edit to either consumer.
"""

from collections.abc import Callable

from chemclaw.config import settings
from eln.json_adapter import JsonExportAdapter
from eln.ord_adapter import OrdJsonAdapter
from report.retrievers import GraphRetriever, LexicalRetriever, VectorRetriever
from report.vector_index import default_note_index
from sources.base import DataSource, IngestHalf, RetrieveHalf, SourceSpec

# Each factory builds a fresh `DataSource` so per-call config (e.g. a monkeypatched knowledge_dir in
# tests, or a rotated export dir) is honored — exactly as the old hardcoded constructions did.
DATA_SOURCES: dict[str, Callable[[], DataSource]] = {
    # The knowledge graph is a retrieve-only source (it is written via the PR-gate, not "ingested").
    "graph": lambda: SourceSpec(name="graph", retrieve=GraphRetriever()),
    # Hybrid-retrieval entry points over the derived note index (F10-A), retrieve-only like `graph`.
    # They are off until a deployment adds `vector`/`lexical` to `data_sources` — registry
    # membership is the enable switch (D-018: one config token), no second boolean to keep in sync.
    # Both read the same `note_index`, populated by `report.vector_index.reindex_notes`.
    "vector": lambda: SourceSpec(name="vector", retrieve=VectorRetriever(default_note_index())),
    "lexical": lambda: SourceSpec(name="lexical", retrieve=LexicalRetriever(default_note_index())),
    # The ELN adapters are ingest-only: reactions flow in and become graph notes, which the `graph`
    # source then retrieves — so the ELN source does not also carry the graph retriever (no double
    # count). They re-host the existing adapters verbatim.
    "eln-json": lambda: SourceSpec(name="eln-json", ingest=JsonExportAdapter()),
    "eln-ord": lambda: SourceSpec(name="eln-ord", ingest=OrdJsonAdapter()),
}


def make_data_source(name: str) -> DataSource:
    """Build the registered `DataSource` for `name`, or raise `ValueError` naming the valid keys."""
    factory = DATA_SOURCES.get(name)
    if factory is None:
        valid = ", ".join(sorted(DATA_SOURCES))
        raise ValueError(f"unknown data source {name!r}; valid sources: {valid}")
    return factory()


def _active_sources() -> list[DataSource]:
    """Build every source named in `settings.data_sources` (config order preserved)."""
    return [make_data_source(name) for name in settings.data_source_list]


def active_ingest_sources() -> list[IngestHalf]:
    """The ingest halves of the active sources, for the durable sync to pull new entries from."""
    return [source.ingest for source in _active_sources() if source.ingest is not None]


def active_retrieve_sources() -> list[RetrieveHalf]:
    """The retrieve halves of the active sources, for `gather_evidence` to fan out over."""
    return [source.retrieve for source in _active_sources() if source.retrieve is not None]
