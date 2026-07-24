"""The config-driven data-source registry (plan F7-T2).

Generalizes `eln/registry.py`'s `{name: factory}` pattern to the whole seam: one dict maps a source
key to a factory that builds its `DataSource`, and the active set is chosen by
`settings.data_sources`. The two consumers read this registry instead of hardcoding their sources —
`gather_evidence` fans out over `active_retrieve_sources()`, the ELN sync ingests
`active_ingest_sources()` — so attaching a new source (the first live one being a custom Snowflake
ELN connector) is one entry here plus one config token, with no edit to either consumer.
"""

from collections.abc import Callable
from typing import assert_never

from chemclaw.config import (
    DataSourceSpec,
    JsonElnSourceSpec,
    OrdElnSourceSpec,
    settings,
)
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


def build_data_source(spec: DataSourceSpec) -> DataSource:
    """Build the `DataSource` a typed `DataSourceSpec` describes, dispatching on its `type`.

    The typed counterpart of `make_data_source` (which resolves a bare registry key): a
    config-carrying source nests its own per-instance config — here, an ELN adapter's `export_dir` —
    and is built from it. Adding a config-carrying type (the deferred Snowflake connector, nesting
    its connection/credential/mapping config) is one variant in `DataSourceSpec` plus one branch
    here. A spec must not reuse a built-in registry key as its name: that name would resolve to the
    built-in via `make_data_source` (the string-keyed Temporal boundary), silently shadowing the
    spec — so a collision is a loud config error, not a quiet surprise at sync time.
    """
    if spec.name in DATA_SOURCES:
        valid = ", ".join(sorted(DATA_SOURCES))
        raise ValueError(
            f"data source spec name {spec.name!r} collides with a built-in registry key; "
            f"rename it (built-in keys: {valid})"
        )
    if isinstance(spec, JsonElnSourceSpec):
        return SourceSpec(name=spec.name, ingest=JsonExportAdapter(export_dir=spec.export_dir))
    if isinstance(spec, OrdElnSourceSpec):
        return SourceSpec(name=spec.name, ingest=OrdJsonAdapter(export_dir=spec.export_dir))
    assert_never(spec)  # exhaustive over the union — a new variant without a branch is a bug


def make_data_source(name: str) -> DataSource:
    """Build the `DataSource` for `name`, or raise `ValueError` naming the valid keys.

    Resolves a built-in registry key first, then a configured `DataSourceSpec` by name — so the
    string-keyed Temporal boundary (`sync_eln_entries(source=name)`) rebuilds either kind of source
    from just its name, keeping in-flight workflow histories byte-identical.
    """
    factory = DATA_SOURCES.get(name)
    if factory is not None:
        return factory()
    for spec in settings.data_source_specs:
        if spec.name == name:
            return build_data_source(spec)
    valid = ", ".join(sorted([*DATA_SOURCES, *(s.name for s in settings.data_source_specs)]))
    raise ValueError(f"unknown data source {name!r}; valid sources: {valid}")


def _active_sources() -> list[DataSource]:
    """Build every active source: the bare-key `data_sources` set, then the typed `DataSourceSpec`s.

    Config order is preserved within each token, comma-list sources first — matching how the two
    tokens are read (bare keys are the default/keyless sources; specs are the config-carrying ones).
    """
    keyed = [make_data_source(name) for name in settings.data_source_list]
    spec_built = [build_data_source(spec) for spec in settings.data_source_specs]
    return [*keyed, *spec_built]


def active_ingest_sources() -> list[IngestHalf]:
    """The ingest halves of the active sources, for the durable sync to pull new entries from."""
    return [source.ingest for source in _active_sources() if source.ingest is not None]


def active_ingest_source_names() -> list[str]:
    """The registry names of the active sources that have an ingest half (config order kept).

    The durable ELN sync iterates these and keys one high-water cursor per name, so two ingest
    sources advance independently — neither's furthest cursor can skip the other's lagging entries.
    """
    return [source.name for source in _active_sources() if source.ingest is not None]


def active_retrieve_sources() -> list[RetrieveHalf]:
    """The retrieve halves of the active sources, for `gather_evidence` to fan out over."""
    return [source.retrieve for source in _active_sources() if source.retrieve is not None]
