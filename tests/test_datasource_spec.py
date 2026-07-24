"""The typed `DataSourceSpec` discriminated union (config-extensibility item 4, Stage 1).

Proves the union adds per-*instance* data-source config without regressing the bare-key seam: a
spec carries its own `export_dir` (so two instances of one type with different directories coexist —
the capability a single global `eln_export_dir` cannot provide), the `type` discriminator dispatches
to the right adapter, the string-keyed Temporal boundary (`make_data_source(name)`) resolves a spec
by name, and the collision / uniqueness / extra-field guards fail loud at build or startup. All
offline — no DB, no Temporal. See `docs/audit/10-config-extensibility.md` §5 (Spike 3).
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

import sources.registry as registry
from chemclaw.config import (
    JsonElnSourceSpec,
    OrdElnSourceSpec,
    Settings,
    settings,
)
from eln.json_adapter import JsonExportAdapter
from eln.ord_adapter import OrdJsonAdapter

_EPOCH = datetime.min.replace(tzinfo=UTC)


def _write_entry(directory: Path, entry_id: str) -> None:
    """Drop one minimally-valid JSON-export ELN entry (enough for `fetch_new_entries`)."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{entry_id}.json").write_text(
        f'{{"id": "{entry_id}", "timestamp": "2020-01-01T00:00:00Z"}}', encoding="utf-8"
    )


def test_json_spec_builds_adapter_reading_its_own_dir(tmp_path: Path) -> None:
    """A `JsonElnSourceSpec` builds a source whose adapter reads the spec's own `export_dir`."""
    _write_entry(tmp_path, "e1")
    source = registry.build_data_source(
        JsonElnSourceSpec(name="eln-json-staging", export_dir=str(tmp_path))
    )
    assert source.name == "eln-json-staging"
    assert isinstance(source.ingest, JsonExportAdapter)
    assert source.retrieve is None  # ELN is ingest-only
    entries = asyncio.run(source.ingest.fetch_new_entries(_EPOCH))
    assert [e.entry_id for e in entries] == ["e1"]  # read from the spec's own directory


def test_ord_spec_dispatches_to_the_ord_adapter(tmp_path: Path) -> None:
    """The `type` discriminator routes an `eln-ord` spec to the ORD adapter, not the JSON one."""
    source = registry.build_data_source(
        OrdElnSourceSpec(name="eln-ord-prod", export_dir=str(tmp_path))
    )
    assert isinstance(source.ingest, OrdJsonAdapter)


def test_two_instances_with_distinct_dirs_coexist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two JSON-ELN instances pointing at different dirs are both active and read their own dir."""
    staging, prod = tmp_path / "staging", tmp_path / "prod"
    _write_entry(staging, "s1")
    _write_entry(prod, "p1")
    _write_entry(prod, "p2")
    monkeypatch.setattr(settings, "data_sources", "graph")  # no default eln-json in the way
    monkeypatch.setattr(
        settings,
        "data_source_specs",
        [
            JsonElnSourceSpec(name="eln-json-staging", export_dir=str(staging)),
            JsonElnSourceSpec(name="eln-json-prod", export_dir=str(prod)),
        ],
    )
    assert registry.active_ingest_source_names() == ["eln-json-staging", "eln-json-prod"]
    # Each name resolves to a source reading its own directory — the per-instance capability.
    staged = registry.make_data_source("eln-json-staging").ingest
    prod_src = registry.make_data_source("eln-json-prod").ingest
    assert staged is not None and prod_src is not None
    assert len(asyncio.run(staged.fetch_new_entries(_EPOCH))) == 1
    assert len(asyncio.run(prod_src.fetch_new_entries(_EPOCH))) == 2


def test_make_data_source_resolves_spec_by_name_at_the_temporal_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`make_data_source(name)` (the string-keyed sync boundary) rebuilds a spec source by name."""
    monkeypatch.setattr(
        settings,
        "data_source_specs",
        [JsonElnSourceSpec(name="eln-json-staging", export_dir="/tmp/x")],
    )
    source = registry.make_data_source("eln-json-staging")  # reachable only via spec fallthrough
    assert source.name == "eln-json-staging"


def test_builtin_keys_still_win_and_unknown_names_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A built-in key wins over the spec fallthrough; an unknown name lists both key sets."""
    monkeypatch.setattr(
        settings,
        "data_source_specs",
        [JsonElnSourceSpec(name="eln-json-staging", export_dir="/tmp/x")],
    )
    assert registry.make_data_source("graph").name == "graph"  # built-in, not a spec
    with pytest.raises(ValueError, match="eln-json-staging"):  # valid keys include the spec name
        registry.make_data_source("nope")


def test_spec_name_colliding_with_a_builtin_key_is_a_loud_error() -> None:
    """A spec reusing a built-in key would be shadowed by it — so it is rejected up front."""
    with pytest.raises(ValueError, match="collides with a built-in registry key"):
        registry.build_data_source(JsonElnSourceSpec(name="eln-json", export_dir="/tmp/x"))


def test_no_specs_is_no_regression(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no specs, the active set is exactly the bare-key sources — the pre-item-4 behavior."""
    monkeypatch.setattr(settings, "data_sources", "graph,eln-json")
    monkeypatch.setattr(settings, "data_source_specs", [])
    assert [s.name for s in registry._active_sources()] == ["graph", "eln-json"]


def test_extra_field_on_a_variant_is_rejected() -> None:
    """`extra="forbid"` turns a misspelled per-variant field into an error, not a silent drop."""
    with pytest.raises(ValueError):
        JsonElnSourceSpec(name="x", export_dir="/tmp/x", warehouse="oops")  # type: ignore[call-arg]


def test_duplicate_source_names_rejected_at_startup() -> None:
    """Two sources sharing a name (a shared cursor key) is a startup ValidationError."""
    with pytest.raises(ValueError, match="unique across data_sources"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            data_source_specs=[
                JsonElnSourceSpec(name="dup", export_dir="/a"),
                OrdElnSourceSpec(name="dup", export_dir="/b"),
            ],
        )


def test_spec_name_colliding_with_comma_list_key_rejected_at_startup() -> None:
    """A spec name equal to an active comma-list key collides on its cursor row — rejected."""
    with pytest.raises(ValueError, match="unique across data_sources"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            data_sources="graph,eln-json",
            data_source_specs=[JsonElnSourceSpec(name="graph", export_dir="/a")],
        )


def test_unknown_discriminator_type_is_rejected() -> None:
    """An unknown `type` tag fails discriminated-union parsing (no silent fallback variant)."""
    with pytest.raises(ValueError):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            data_source_specs=[{"type": "snowflake", "name": "sf", "export_dir": "/a"}],  # type: ignore[list-item]
        )
