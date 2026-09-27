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
from chemclaw.retrieval.evidence import EvidenceChunk
from chemclaw.retrieval.fanout import sweep_sources


class _Graph:
    """A healthy source beside the vendored one — what a degraded sweep must still return."""

    name = "graph"

    async def retrieve(self, _query: str, _filters: dict[str, object]) -> list[EvidenceChunk]:
        """One chunk, so a leg that keeps answering is distinguishable from one that stops."""
        return [
            EvidenceChunk(
                content="acetonitrile, solvent", source_note_id="cmp-1", retriever="graph"
            )
        ]


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
                "mirrored": False,
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
    """The point of vendoring at all: `core/reagents.py` is the ceiling this raises.

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


def test_a_corpus_that_is_not_utf8_is_named_bad_data_rather_than_escaping_as_a_decode_error(
    tmp_path: Path,
) -> None:
    """The one failure `_BAD_DATA_TYPES` exists for, and the one shape it could not match.

    `_read_records` caught `OSError` around `read_bytes` and nothing around `data.decode("utf-8")`,
    so a latin-1 `records.csv` left this module as a bare `UnicodeDecodeError` — past this module's
    own promise to "say precisely what is wrong with it", and past
    `durable/publish._BAD_DATA_TYPES`, which matches by class *name* and lists
    `VendoredDatasetError` precisely because "a retry re-reads the same bytes from the same image
    layer". Driven with a latin-1 corpus **whose checksum matched**: `UnicodeDecodeError` out of the
    loader, classified retryable, so Temporal burned `activity_max_attempts` against immutable image
    bytes. The checksum passing is what makes it certainly permanent.

    The byte and its offset are asserted because "not UTF-8" over a 40 MB corpus is not something an
    operator can act on.
    """
    rows = "name,smiles,role\nac\xe9tonitrile,CC#N,solvent\n"
    directory = tmp_path / "d"
    directory.mkdir(parents=True)
    (directory / "records.csv").write_bytes(rows.encode("latin-1"))
    (directory / "dataset.json").write_text(
        json.dumps(
            {
                "name": "latin1-reagents",
                "version": "1.0.0",
                "licence": "CC0-1.0",
                "retrieved_from": "hand-authored for this test",
                "description": "a corpus written by a tool that emitted latin-1",
                "mirrored": False,
                "sha256": hashlib.sha256(rows.encode("latin-1")).hexdigest(),
                "text_column": "name",
            }
        ),
        encoding="utf-8",
    )

    manifest = _read_manifest(directory)
    with pytest.raises(VendoredDatasetError, match=r"not UTF-8.*0xe9 at offset 19") as raised:
        _read_records(directory, manifest)
    assert "checksum matched" in str(raised.value)

    from chemclaw.durable.publish import BAD_DATA_RETRY

    assert "VendoredDatasetError" in (BAD_DATA_RETRY.non_retryable_error_types or []), (
        "the classification is by class name, so the raise and the register must agree"
    )


def test_a_manifest_that_is_not_utf8_is_named_the_same_way(tmp_path: Path) -> None:
    """The sibling site two functions up, which had the same gap and no test either.

    `_read_manifest` enumerated `OSError` and `json.JSONDecodeError` — a `ValueError` *sibling* of
    the decode error rather than its parent — so the same bytes in the manifest escaped as an
    unclassified `UnicodeDecodeError` too. Both call sites are in one commit because the sweep for
    the shape is what the fix is; fixing only the one that was reported leaves the other.
    """
    directory = tmp_path / "d"
    directory.mkdir(parents=True)
    (directory / "dataset.json").write_bytes('{"name": "caf\xe9"}'.encode("latin-1"))
    with pytest.raises(VendoredDatasetError, match="not UTF-8"):
        _read_manifest(directory)


def test_a_row_whose_text_cell_is_empty_is_dropped_out_loud(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The checksum proves these bytes are what was reviewed; the loader then served fewer rows.

    Measured: three rows in, two loaded, and **no log, no count and no error** — the one loss
    this module's own provenance argument cannot explain away: nothing about the file was wrong.
    Still dropped rather than refused: a row with no text has nothing to retrieve, and one blank
    line must not cost a corpus its whole load. What changes is that it says so, with the count,
    which is what makes "the corpus is short" answerable.
    """
    import logging

    rows = "name,smiles,role\nacetonitrile,CC#N,solvent\n,CCO,solvent\nDIPEA,CCN,base\n"
    directory = _dataset(tmp_path / "d", rows=rows)
    with caplog.at_level(logging.WARNING):
        records = _read_records(directory, _read_manifest(directory))
    assert len(records) == 2
    assert "1 of 3 rows" in caplog.text and "records.csv" in caplog.text


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


def test_a_missing_dataset_costs_its_own_leg_of_the_sweep_and_no_other(tmp_path: Path) -> None:
    """An uninstalled corpus must not break every query in the process, nor answer one.

    This asserted `== []` and a WARNING, which made an uninstalled corpus indistinguishable from an
    installed one holding no match. Worse, `_load` cached that empty for the life of the process,
    so the warning fired once and every later query was silent. The sweep's branch is where a dead
    source belongs: `sources_failed` names it, the healthy leg beside it keeps answering, and
    `gather_evidence` raises only when nothing at all could be asked.
    """
    import asyncio

    retriever = VendoredDatasetRetriever(dataset_dir=str(tmp_path / "absent"))
    with pytest.raises(VendoredDatasetError):
        asyncio.run(retriever.retrieve("acetonitrile", {}))

    ranked, failed, _skipped = asyncio.run(
        sweep_sources([("graph", _Graph()), ("vendored", retriever)], "acetonitrile", {})
    )
    assert [len(chunks) for chunks in ranked] == [1, 0]
    assert failed == ["vendored"]


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


def test_a_mirrored_corpus_must_name_who_refreshes_it_and_how_often(tmp_path: Path) -> None:
    """A snapshot with no owner and no cadence goes stale with nobody knowing it has.

    The rule is the fleet's — `Chemclaw3-mcp`'s `MODULES.md` states it as an open question and
    neither repository enforced it: "a stale patent index that nobody knows is stale is worse than
    no patent index". Enforced at *load*, because a review that has to remember a rule is exactly
    the control this repository keeps finding gone
    (`D-2026-09-14-a-mirror-with-no-owner-goes-stale-in-silence`).

    Driven through `_read_manifest`, which is the function every path into a vendored corpus goes
    through, rather than through the model — a validator nothing calls on the loading path would
    pass this test and refuse nothing.
    """
    directory = _dataset(tmp_path / "d")
    manifest = json.loads((directory / "dataset.json").read_text(encoding="utf-8"))
    manifest["mirrored"] = True
    (directory / "dataset.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(VendoredDatasetError) as refusal:
        _read_manifest(directory)
    assert "refresh_owner" in str(refusal.value)
    assert "refresh_cadence" in str(refusal.value)

    manifest["refresh_owner"] = "the process-chemistry data team"
    manifest["refresh_cadence"] = "quarterly, against the upstream release feed"
    (directory / "dataset.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert _read_manifest(directory).refresh_owner == "the process-chemistry data team"


def test_first_party_content_may_not_claim_an_upstream_it_does_not_have(tmp_path: Path) -> None:
    """The other direction, and it is not symmetry for its own sake.

    A refresh owner on a corpus with no upstream sends the next reader looking for a feed that does
    not exist — which is the same failure as a missing one, costing somebody an afternoon instead of
    shipping a stale answer. The shipped `data/vendored` corpus is first-party and names neither.
    """
    directory = _dataset(tmp_path / "d")
    manifest = json.loads((directory / "dataset.json").read_text(encoding="utf-8"))
    manifest["refresh_owner"] = "somebody"
    (directory / "dataset.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(VendoredDatasetError) as refusal:
        _read_manifest(directory)
    assert "no upstream" in str(refusal.value)

    assert _read_manifest(_SHIPPED).mirrored is False
