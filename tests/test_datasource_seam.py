"""The generic data-source seam: contract, discovery, and re-host (plan F7-T1/T2/T3/T4, D-120).

Proves a source may provide either half or both (and neither is rejected), that discovery plus the
`data_sources` enable token select the active ingest/retrieve halves, that `gather_evidence` fans
out over discovered retrievers, and that the re-hosted ELN source rides the seam with its
provenance intact — all offline, no DB or Temporal.

The fan-out test is the seam's **acceptance test**: it attaches a new source the way an operator
would — writing a `datasource.yaml` into a directory and naming it in `data_sources` — and touches
no core Python at all. Before D-120 the same test had to `monkeypatch.setitem` a dict inside
`chemclaw.ingest.sources.registry`, which is precisely the core edit the seam is supposed to
remove; a test that
has to reach into core to add a source is evidence the seam does not work.
"""

import asyncio
import os
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

import chemclaw.agent.research_tools as research_tools
import chemclaw.ingest.sources.registry as registry
from chemclaw.core.config import settings
from chemclaw.ingest.eln.adapter import RawEntry
from chemclaw.ingest.sources.base import DataSource, SourceSpec
from chemclaw.retrieval.evidence import EvidenceChunk


@pytest.fixture(autouse=True)
def _fresh_discovery() -> Any:
    """Drop the discovery cache around every test in this module.

    `discovered()` is `@cache`d for production, where the layout is fixed for the process's life.
    Tests move `data_sources_dir`, so a cache entry from a previous test would answer for the wrong
    directory — and would do it silently, by returning a *plausible* set of sources.
    """
    registry.discovered.cache_clear()
    yield
    registry.discovered.cache_clear()


def _write_source(directory: Path, name: str, body: str) -> None:
    """Create `directory/name/datasource.yaml` — the whole of what attaching a source requires."""
    folder = directory / name
    folder.mkdir(parents=True)
    (folder / registry.MANIFEST_FILENAME).write_text(textwrap.dedent(body), encoding="utf-8")


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
    """A source no manifest declares raises, naming the valid keys."""
    with pytest.raises(ValueError, match="unknown data source"):
        registry.make_data_source("snowflake")  # not yet declared (deferred)


def test_enabling_an_undeclared_source_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo in `data_sources` is a startup error, not a corpus that silently stops being read.

    The failure this seam is most exposed to: a retrieval returning nothing looks exactly like a
    corpus with no matches, so a missing source must never degrade quietly.
    """
    monkeypatch.setattr(settings, "data_sources", "graph,elm-json")  # note the typo
    with pytest.raises(ValueError, match="enabled in `data_sources` but no manifest declares it"):
        registry.active_retrieve_sources()


def test_default_preserves_single_graph_retriever(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default config yields exactly the one GraphRetriever gather_evidence used before F7."""
    monkeypatch.setattr(settings, "data_sources", "graph,eln-json")
    retrievers = registry.active_retrieve_sources()
    assert [r.name for r in retrievers] == ["graph"]


def test_a_new_source_is_a_folder_and_a_config_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Attaching a source touches zero core Python: one `datasource.yaml`, one name in config.

    The seam's acceptance test. Everything here is what an operator does — write a manifest, point
    discovery at the directory holding it, enable the name — and `gather_evidence` picks it up with
    no edit to the registry, the config models, or the retrieval code.
    """
    _write_source(
        tmp_path,
        "fake",
        """\
        name: fake
        description: A stand-in corpus, to prove discovery needs no core edit.
        retrieve: tests.test_datasource_seam:_FakeRetriever
        """,
    )
    monkeypatch.setattr(settings, "data_sources_dir", str(tmp_path))
    monkeypatch.setattr(settings, "data_sources", "fake")

    chunks = asyncio.run(research_tools.gather_evidence("solubility"))
    assert any("hit:solubility" in c.content for c in chunks)  # framed, but the payload survives


def test_manifest_config_reaches_the_adapter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A manifest's `config:` becomes the half's constructor kwargs.

    This is what replaced the typed `data_source_specs` union: two ELN drops with different
    directories are two manifests, not two pydantic variants plus a branch in core.
    """
    from chemclaw.ingest.eln.json_adapter import JsonExportAdapter

    manifests, drop = tmp_path / "manifests", tmp_path / "drop"
    drop.mkdir()
    _write_source(
        manifests,
        "eln-json-staging",
        f"""\
        name: eln-json-staging
        description: The staging ELN drop, with its own export directory.
        ingest: chemclaw.ingest.eln.json_adapter:JsonExportAdapter
        config:
          export_dir: {drop}
        """,
    )
    monkeypatch.setattr(settings, "data_sources_dir", str(manifests))

    source = registry.make_data_source("eln-json-staging")
    assert isinstance(source.ingest, JsonExportAdapter)
    assert source.ingest._dir == drop  # the manifest's dir, not the global `eln_export_dir`


def test_a_config_key_the_adapter_rejects_names_both_sides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A mistyped `config:` key fails naming the source and the callable, not deep in a ctor.

    `config` is free-form by design — the callable's signature is the schema — so this is the one
    error that a typed union would have caught for free, and it has to be caught well here instead.
    """
    _write_source(
        tmp_path,
        "eln-typo",
        """\
        name: eln-typo
        description: A source whose config names a kwarg the adapter does not take.
        ingest: chemclaw.ingest.eln.json_adapter:JsonExportAdapter
        config:
          exprot_dir: /mnt/eln
        """,
    )
    monkeypatch.setattr(settings, "data_sources_dir", str(tmp_path))

    with pytest.raises(ValueError, match=r"eln-typo.*JsonExportAdapter rejected config"):
        registry.make_data_source("eln-typo")


def test_a_source_declaring_neither_half_is_rejected_at_the_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The manifest refuses a source with no halves, before anything is built."""
    _write_source(
        tmp_path,
        "hollow",
        """\
        name: hollow
        description: Declares no halves at all.
        """,
    )
    monkeypatch.setattr(settings, "data_sources_dir", str(tmp_path))

    with pytest.raises(ValueError, match="declares neither"):
        registry.discovered()


def test_a_manifest_name_must_match_its_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The folder name is the enable token, so a manifest that disagrees with it is an error.

    Otherwise `data_sources: fake` would resolve by folder while every message named the manifest,
    and the two would drift without anything failing.
    """
    _write_source(
        tmp_path,
        "on-disk",
        """\
        name: in-manifest
        description: A manifest whose name disagrees with its folder.
        retrieve: tests.test_datasource_seam:_FakeRetriever
        """,
    )
    monkeypatch.setattr(settings, "data_sources_dir", str(tmp_path))

    with pytest.raises(ValueError, match="does not match its folder"):
        registry.discovered()


def test_an_earlier_dir_overrides_a_shipped_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A mounted folder can override a repo-shipped source of the same name (first dir wins).

    How a deployment re-points `eln-json` at its own drop directory without touching the image.
    """
    _write_source(
        tmp_path,
        "graph",
        """\
        name: graph
        description: A deployment's own stand-in for the shipped graph source.
        retrieve: tests.test_datasource_seam:_FakeRetriever
        """,
    )
    # Prepend to whatever the shipped directory currently is, rather than restating it: since
    # D-141 the default is resolved against the installed package, not the process's CWD, so a
    # literal here would be asserting the layout instead of the override rule.
    shipped = settings.data_sources_dir
    monkeypatch.setattr(settings, "data_sources_dir", f"{tmp_path}{os.pathsep}{shipped}")

    assert "eln-json" in registry.discovered()  # the shipped dir is still searched
    assert registry.discovered()["graph"].retrieve == "tests.test_datasource_seam:_FakeRetriever"


def test_rehosted_eln_source_carries_provenance() -> None:
    """The re-hosted ELN source rides the seam; its adapter is the existing one (F7-T4)."""
    from chemclaw.ingest.eln.json_adapter import JsonExportAdapter

    source = registry.make_data_source("eln-json")
    assert source.name == "eln-json"
    assert isinstance(source.ingest, JsonExportAdapter)  # the existing adapter, unchanged
    assert source.retrieve is None  # ELN is ingest-only; retrieval is the graph source's job
