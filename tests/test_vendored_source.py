"""The vendored dataset source (STO-14) — D-089's one sanctioned escalation.

D-089 said: no external data sources. `tests/test_no_egress.py` enforces it, and holds this source
to the same standard rather than exempting it — the two tests there assert that this module can
make no request and that a shipped dataset declares its provenance.

What these tests cover is the other half: that a corpus arriving this way is *pinned* — checksummed
against a manifest, refused when it drifts, and never silently degrading into something a reader
would mistake for curated knowledge.
"""

import hashlib
import json
from pathlib import Path

import pytest

from chemclaw.core.config import settings
from chemclaw.ingest.sources.vendored_dataset import (
    VendoredDatasetError,
    VendoredDatasetRetriever,
    _read_manifest,
    _read_records,
)

_SHIPPED = Path(__file__).resolve().parents[1] / "data" / "vendored"

_ROWS = "name,smiles,role\nacetonitrile,CC#N,solvent\nDIPEA,CCN(C(C)C)C(C)C,base\n"


def _dataset(directory: Path, rows: str = _ROWS, sha: str | None = None) -> Path:
    """Write a minimal valid vendored dataset and return its directory."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "records.csv").write_text(rows, encoding="utf-8")
    (directory / "dataset.json").write_text(
        json.dumps(
            {
                "name": "test-reagents",
                "version": "1.0.0",
                "licence": "CC0-1.0",
                "retrieved_from": "hand-authored for this test",
                "description": "a two-row reagent table",
                "sha256": sha or hashlib.sha256(rows.encode()).hexdigest(),
                "text_column": "name",
                "smiles_column": "smiles",
            }
        ),
        encoding="utf-8",
    )
    return directory


def test_the_shipped_dataset_loads_and_matches_its_own_checksum() -> None:
    """The corpus this repository actually ships, verified the way a deployment will verify it."""
    manifest = _read_manifest(_SHIPPED)
    records = _read_records(_SHIPPED, manifest)
    assert len(records) >= 30
    assert manifest.licence and manifest.version


def test_the_shipped_dataset_covers_names_the_hand_maintained_table_does_not() -> None:
    """The point of vendoring at all: `chemclaw/reagents.py` is the ceiling this raises.

    Asserted against the real resolver rather than by counting rows — "more entries" is not the
    claim, "names that previously resolved to nothing" is.
    """
    from chemclaw.core.reagents import resolve_compound_name

    manifest = _read_manifest(_SHIPPED)
    records = _read_records(_SHIPPED, manifest)
    unknown = [record.text for record in records if resolve_compound_name(record.text) is None]
    assert unknown, "the vendored table adds nothing the hand-maintained one did not already have"


def test_a_dataset_that_does_not_match_its_manifest_is_refused(tmp_path: Path) -> None:
    """The whole value of vendoring is that the shipped data is provably what was reviewed.

    The error names both hashes, because the tempting fix — editing the manifest to agree with the
    bytes — defeats the mechanism entirely, and the message says so.
    """
    directory = _dataset(tmp_path / "d", sha="0" * 64)
    manifest = _read_manifest(directory)
    with pytest.raises(VendoredDatasetError, match="does not match its manifest"):
        _read_records(directory, manifest)


def test_verification_can_be_turned_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An escape hatch for a build that computes checksums out of band — off by default."""
    monkeypatch.setattr(settings, "vendored_dataset_verify", False)
    directory = _dataset(tmp_path / "d", sha="0" * 64)
    assert len(_read_records(directory, _read_manifest(directory))) == 2


def test_a_manifest_missing_its_licence_is_not_a_usable_dataset(tmp_path: Path) -> None:
    """A corpus with no recorded licence is a legal question nobody can answer later."""
    directory = tmp_path / "d"
    directory.mkdir()
    (directory / "records.csv").write_text(_ROWS, encoding="utf-8")
    (directory / "dataset.json").write_text(
        json.dumps({"name": "x", "version": "1", "text_column": "name"}), encoding="utf-8"
    )
    with pytest.raises(VendoredDatasetError, match="not a usable dataset manifest"):
        _read_manifest(directory)


def test_a_manifest_naming_a_column_the_file_lacks_is_refused(tmp_path: Path) -> None:
    """Caught at load with the column named, rather than yielding an empty corpus silently."""
    rows = "compound,smiles\nacetonitrile,CC#N\n"
    directory = _dataset(tmp_path / "d", rows=rows)
    with pytest.raises(VendoredDatasetError, match="text_column"):
        _read_records(directory, _read_manifest(directory))


def test_a_missing_dataset_yields_no_evidence_rather_than_breaking_retrieval(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An optional corpus that is not installed must not break every query in the process."""
    import asyncio
    import logging

    retriever = VendoredDatasetRetriever(dataset_dir=str(tmp_path / "absent"))
    with caplog.at_level(logging.WARNING, logger="chemclaw.ingest.sources.vendored_dataset"):
        assert asyncio.run(retriever.retrieve("acetonitrile", {})) == []
    assert "vendored dataset unavailable" in caplog.text


def test_a_lookup_returns_the_shortest_containing_entry_first(tmp_path: Path) -> None:
    """On a name table the shortest containing entry is the closest thing to an exact match."""
    import asyncio

    rows = (
        "name,smiles,role\n"
        "THF,C1CCOC1,solvent\n"
        "2-methyltetrahydrofuran,CC1CCCO1,solvent\n"
        "tetrahydrofuran,C1CCOC1,solvent\n"
    )
    retriever = VendoredDatasetRetriever(dataset_dir=str(_dataset(tmp_path / "d", rows=rows)))
    chunks = asyncio.run(retriever.retrieve("tetrahydrofuran", {}))
    assert [chunk.content.split(" — ")[0] for chunk in chunks] == [
        "tetrahydrofuran",
        "2-methyltetrahydrofuran",
    ]


def test_a_citation_points_at_the_pinned_row_not_a_pretend_note(tmp_path: Path) -> None:
    """A citation must resolve to something a reader can check.

    For vendored data that is a row in a checksummed file, not a knowledge-graph note id — and the
    prefix says so rather than letting a `vendored` hit look like curated knowledge.
    """
    import asyncio

    retriever = VendoredDatasetRetriever(dataset_dir=str(_dataset(tmp_path / "d")))
    chunk = asyncio.run(retriever.retrieve("acetonitrile", {}))[0]
    assert chunk.source_note_id.startswith("vendored:test-reagents:")
    assert "CC#N" in chunk.content


def test_an_empty_query_matches_nothing(tmp_path: Path) -> None:
    """Substring matching on an empty needle would return the whole table as evidence."""
    import asyncio

    retriever = VendoredDatasetRetriever(dataset_dir=str(_dataset(tmp_path / "d")))
    assert asyncio.run(retriever.retrieve("   ", {})) == []


def test_the_source_is_retrieve_only() -> None:
    """Vendored data is reference material, not experiments.

    An ingest half would give unreviewed third-party records a write path into the knowledge graph
    behind the PR-gate's back, which is a different and much larger decision than reading a table.
    """
    import yaml

    path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "chemclaw"
        / "ingest"
        / "sources"
        / "vendored"
        / "datasource.yaml"
    )
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert manifest.get("ingest") is None
    assert manifest["retrieve"].startswith("chemclaw.ingest.sources.vendored_dataset:")


def test_it_is_not_enabled_by_default() -> None:
    """A deployment that ships no dataset is unaffected by the mechanism existing."""
    assert "vendored" not in settings.data_sources
