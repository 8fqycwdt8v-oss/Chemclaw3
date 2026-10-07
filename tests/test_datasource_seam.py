"""The generic data-source seam: contract, discovery, and re-host (D-120).

A source may provide either half or both (neither is rejected); discovery plus the
`data_sources` token select the active halves; `gather_evidence` fans out over discovered
retrievers; and the re-hosted ELN source keeps its provenance. All offline.

The fan-out test is the acceptance test: it attaches a source the way an operator would (a
`datasource.yaml` in a directory, its name in `data_sources`) and touches no core Python.
"""

import asyncio
import os
import textwrap
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

import chemclaw.agent.research_tools as research_tools
import chemclaw.ingest.sources.registry as registry
from chemclaw.cli.validate_datasources import validate_datasources
from chemclaw.core.config import settings
from chemclaw.ingest.eln.adapter import RawEntry
from chemclaw.ingest.sources.base import DataSource, SourceSpec
from chemclaw.retrieval.evidence import EvidenceChunk


@pytest.fixture(autouse=True)
def _allow_test_package_drivers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let this suite's manifests name halves that live in this file.

    `retrieve:`/`ingest:` references are imported and called, so they are restricted to `chemclaw`
    plus operator-named packages (`D-2026-09-06-a-manifest-is-data-in-every-field-that-executes`).
    These fixtures are the third-party case, so the suite sets the one env var a deployment would.
    """
    monkeypatch.setattr(settings, "manifest_driver_packages", "tests")


def _write_source(directory: Path, name: str, body: str) -> None:
    """Create `directory/name/datasource.yaml` — the whole of what attaching a source requires."""
    folder = directory / name
    folder.mkdir(parents=True)
    (folder / registry.MANIFEST_FILENAME).write_text(textwrap.dedent(body), encoding="utf-8")


class _FakeRetriever:
    """A minimal retrieve half returning one fixed chunk, to prove registry fan-out.

    Takes `name` because every retrieve half does: the registry passes the manifest's name, so a
    source's identity comes from its folder, not the half's default.
    """

    def __init__(self, name: str = "fake") -> None:
        self.name = name

    async def retrieve(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        return [EvidenceChunk(content=f"hit:{query}", source_note_id="fake-1", retriever=self.name)]


class _NamelessRetriever:
    """A retrieve half that refuses the source name — the shape the registry must reject."""

    name = "nameless"

    async def retrieve(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        return []  # pragma: no cover - never built


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
    with pytest.raises(ValueError, match="must provide an ingest, retrieve or commitments half"):
        SourceSpec(name="empty")


def test_registry_selects_active_halves(monkeypatch: pytest.MonkeyPatch) -> None:
    """`data_sources` config picks which ingest/retrieve halves are active."""
    monkeypatch.setattr(settings, "data_sources", "graph,eln-json,eln-ord")
    assert len(registry.active_retrieve_sources()) == 1  # only `graph` has a retrieve half
    assert len(registry.active_ingest_sources()) == 2  # both ELN adapters have ingest halves


def test_unknown_source_is_rejected() -> None:
    """A source no manifest declares raises, naming the valid keys."""
    with pytest.raises(ValueError, match="unknown data source"):
        registry.make_data_source("teradata")  # no manifest declares one


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


def test_the_three_note_legs_declare_one_corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    """`graph`, `lexical` and `vector` read one note tree, and the manifests have to say so.

    Without `corpus: knowledge-notes` they are three correlated votes in `reciprocal_rank_fusion`
    (with the `hash` embedder all three are term-overlap rankers). Asserted on the manifests,
    because the fusion's unit tests build their corpus list by hand.
    """
    monkeypatch.setattr(settings, "data_sources", "graph,lexical,vector")
    corpora = registry.active_retrieve_corpora()
    assert set(corpora) == {"graph", "lexical", "vector"}
    assert len(set(corpora.values())) == 1, (
        f"{corpora} — the three legs over the note tree must name one corpus, or RRF counts their "
        "agreement three times"
    )


def test_a_source_that_declares_no_corpus_is_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default, and what keeps every existing deployment's fusion unchanged.

    The other direction of the test above: if `active_retrieve_corpora` answered one corpus for
    everything, the assertion there would pass while collapsing unrelated sources into one vote.
    """
    monkeypatch.setattr(settings, "data_sources", "graph,vendored")
    corpora = registry.active_retrieve_corpora()
    assert corpora["vendored"] == "vendored"
    assert corpora["graph"] != "vendored"


def test_a_new_source_is_a_folder_and_a_config_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Attaching a source touches zero core Python: one `datasource.yaml`, one name in config.

    The acceptance test: write a manifest, point discovery at its directory, enable the name, and
    `gather_evidence` picks it up.
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

    chunks = asyncio.run(research_tools.gather_evidence("solubility")).chunks
    assert any("hit:solubility" in c.content for c in chunks)  # framed, but the payload survives


def test_a_retrieve_half_is_named_by_its_manifest_not_by_its_own_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The source's name wins over whatever default the half's constructor carries.

    `_FakeRetriever` defaults to `"fake"`; mounted under `borrowed`, it must answer `borrowed`. The
    partition rests on this (see the sibling test below).
    """
    _write_source(
        tmp_path,
        "borrowed",
        """\
        name: borrowed
        description: The fake retriever, mounted under a name it does not know about.
        retrieve: tests.test_datasource_seam:_FakeRetriever
        """,
    )
    monkeypatch.setattr(settings, "data_sources_dir", str(tmp_path))
    monkeypatch.setattr(settings, "data_sources", "borrowed")

    assert [r.name for r in registry.active_retrieve_sources()] == ["borrowed"]


def test_two_instances_of_one_engine_get_distinct_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two sources sharing one retrieve engine are two corpora, not one.

    If a half named itself, two mounted shares would both answer `sharedrive`: `share_sources()`
    would collapse them, only one would be crawled, and its sweep would delete the other's rows
    (`document_files` is keyed `(source, path)`). Asserted on names, since distinct names are what
    make every keyed consumer (citations, weights, the sweep) partition.
    """
    for name in ("alpha", "beta"):
        _write_source(
            tmp_path,
            name,
            f"""\
            name: {name}
            description: One of two corpora sharing a single retrieve engine.
            retrieve: tests.test_datasource_seam:_FakeRetriever
            """,
        )
    monkeypatch.setattr(settings, "data_sources_dir", str(tmp_path))
    monkeypatch.setattr(settings, "data_sources", "alpha,beta")

    names = [r.name for r in registry.active_retrieve_sources()]
    assert names == ["alpha", "beta"], "two instances of one engine must not share a name"


def test_a_retrieve_half_that_refuses_a_name_fails_naming_the_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Taking the source name is the contract, and breaking it fails loudly at build time.

    Stamping the name on after construction would accept such a half silently.
    """
    _write_source(
        tmp_path,
        "nameless",
        """\
        name: nameless
        description: A half whose constructor takes no source name.
        retrieve: tests.test_datasource_seam:_NamelessRetriever
        """,
    )
    monkeypatch.setattr(settings, "data_sources_dir", str(tmp_path))
    monkeypatch.setattr(settings, "data_sources", "nameless")

    with pytest.raises(registry.DataSourceError, match="nameless"):
        registry.active_retrieve_sources()


def test_manifest_config_reaches_the_adapter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A manifest's `config:` becomes the half's constructor kwargs.

    This is what replaced the typed `data_source_specs` union: two ELN drops with different
    directories are two manifests, not two pydantic variants plus a branch in core.
    """
    from chemclaw.ingest.eln.adapter import DatedIngest
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
    # The registry hands back the adapter inside the seam's normalisation (`DatedIngest`), so the
    # config assertion goes through `.inner`. The wrapper is asserted too, not just unwrapped:
    # a normalisation that silently stopped being applied is the failure it exists to prevent.
    assert isinstance(source.ingest, DatedIngest)
    assert isinstance(source.ingest.inner, JsonExportAdapter)
    # The manifest's dir, not the global `eln_export_dir`.
    assert source.ingest.inner._dir == drop


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

    with pytest.raises(ValueError, match="declares no `ingest:`"):
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
    # D-148 the default is resolved against the installed package, not the process's CWD, so a
    # literal here would be asserting the layout instead of the override rule.
    shipped = settings.data_sources_dir
    monkeypatch.setattr(settings, "data_sources_dir", f"{tmp_path}{os.pathsep}{shipped}")

    assert "eln-json" in registry.discovered()  # the shipped dir is still searched
    assert registry.discovered()["graph"].retrieve == "tests.test_datasource_seam:_FakeRetriever"


def test_rehosted_eln_source_carries_provenance() -> None:
    """The re-hosted ELN source rides the seam; its adapter is the existing one (F7-T4)."""
    from chemclaw.ingest.eln.adapter import DatedIngest
    from chemclaw.ingest.eln.json_adapter import JsonExportAdapter

    source = registry.make_data_source("eln-json")
    assert source.name == "eln-json"
    assert isinstance(source.ingest, DatedIngest)  # inside the seam's normalisation
    assert isinstance(source.ingest.inner, JsonExportAdapter)  # the adapter itself, unchanged
    assert source.retrieve is None  # ELN is ingest-only; retrieval is the graph source's job


def test_a_config_that_shadows_the_source_name_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A `name` in `config:` cannot be built, so the manifest refuses it.

    The registry passes `name=` on top of `**config`, so a second `name` is a duplicate keyword at
    startup; the validator must refuse what startup refuses, not merge it silently as a dict would.
    """
    _write_source(
        tmp_path,
        "shadowed",
        """\
        name: shadowed
        description: A source trying to name itself in its own config block.
        retrieve: tests.test_datasource_seam:_FakeRetriever
        config:
          name: something-else
        """,
    )
    monkeypatch.setattr(settings, "data_sources_dir", str(tmp_path))
    with pytest.raises(registry.DataSourceError, match="`config:` block"):
        registry.discovered()


def test_the_seam_dates_a_record_the_adapter_left_undated() -> None:
    """`performed_at` falls back to the entry timestamp every entry is required to carry.

    `memory.progression` orders on `performed_at`, so without it a source produces no timeline.
    `RawEntry.created_at` is always present (the sync watermark needs it). Both directions: a
    chemist-entered experiment date, where a source has one, is the better fact and is never
    overwritten.
    """
    from chemclaw.ingest.eln.adapter import DatedIngest
    from chemclaw.ingest.eln.ord import Component, OrdReaction, Role

    class _Adapter:
        """Maps a record dated only when the entry payload says so — a bound column, in effect."""

        async def fetch_new_entries(self, since: datetime) -> list[RawEntry]:
            return []

        def map_to_ord(self, raw: RawEntry) -> OrdReaction:
            return OrdReaction(
                reaction_id=raw.entry_id,
                provenance="test",
                inputs=[Component(smiles="CC", role=Role.REACTANT)],
                outcomes=[Component(smiles="CCO", role=Role.PRODUCT)],
                performed_at=raw.payload.get("run_on"),
            )

    written = datetime(2026, 5, 4, 9, 0, tzinfo=UTC)
    seam = DatedIngest(_Adapter())

    undated = RawEntry(entry_id="E1", created_at=written, payload={})
    assert _Adapter().map_to_ord(undated).performed_at is None, "the adapter alone has no date"
    assert seam.map_to_ord(undated).performed_at == date(2026, 5, 4)

    stated = RawEntry(entry_id="E2", created_at=written, payload={"run_on": date(2026, 4, 1)})
    assert seam.map_to_ord(stated).performed_at == date(2026, 4, 1), "the adapter's date wins"


def test_both_ingest_readers_get_the_same_normalisation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The sync and the corpus miner build through one construction point, so a rule reaches both.

    `map_to_ord` has many call sites and no shared downstream, so a normalisation in either caller
    would miss the other; it lives in the registry and both entry points are asserted to carry it.
    """
    from chemclaw.ingest.eln.adapter import DatedIngest

    manifests = tmp_path / "manifests"
    _write_source(
        manifests,
        "eln-json-two-readers",
        """\
        name: eln-json-two-readers
        description: A JSON ELN drop reached through both registry entry points.
        ingest: chemclaw.ingest.eln.json_adapter:JsonExportAdapter
        """,
    )
    monkeypatch.setattr(settings, "data_sources_dir", str(manifests))
    monkeypatch.setattr(settings, "data_sources", "eln-json-two-readers")

    by_name = registry.make_data_source("eln-json-two-readers").ingest
    active = registry.active_ingest_sources()
    assert isinstance(by_name, DatedIngest), "make_data_source: the durable sync's entry point"
    assert [type(half) for half in active] == [DatedIngest], (
        "active_ingest_sources: the corpus miner's entry point, and the one that validates nothing"
    )


def test_an_entry_dated_series_does_not_claim_it_was_ordered_by_experiment() -> None:
    """An entry-dated series does not claim it was ordered by experiment.

    `DatedIngest` uses when the record was *written*, which for a batch transcribed in one afternoon
    carries no sequence. Unstamped, the campaign note would claim "Runs in the order they were
    performed"; the fallback must say where the date came from
    (`D-2026-08-26-silence-is-not-a-successful-run`).
    """
    from chemclaw.ingest.eln.adapter import DatedIngest
    from chemclaw.ingest.eln.ord import Component, OrdReaction, Role
    from chemclaw.memory.comparison import ordering_caveat
    from chemclaw.memory.progression import progression

    class _Adapter:
        async def fetch_new_entries(self, since: datetime) -> list[RawEntry]:
            return []

        def map_to_ord(self, raw: RawEntry) -> OrdReaction:
            return OrdReaction(
                reaction_id=raw.entry_id,
                provenance="test",
                inputs=[Component(smiles="CC", role=Role.REACTANT)],
                outcomes=[Component(smiles="CCO", role=Role.PRODUCT)],
                performed_at=raw.payload.get("run_on"),
            )

    seam = DatedIngest(_Adapter())
    written = datetime(2026, 5, 4, 9, 0, tzinfo=UTC)
    filled = seam.map_to_ord(RawEntry(entry_id="E1", created_at=written, payload={}))
    stated = seam.map_to_ord(
        RawEntry(entry_id="E2", created_at=written, payload={"run_on": date(2026, 4, 1)})
    )
    assert filled.date_source == "entry", "the seam stamps what it filled in"
    assert stated.date_source == "stated", "and leaves the source's own date alone"

    second = seam.map_to_ord(RawEntry(entry_id="E3", created_at=written, payload={}))
    entry_series = progression([filled, second])
    assert entry_series.is_timeline(), "it is still an ordering — the weaker kind, not none"
    caveat = ordering_caveat(entry_series)
    assert "order they were recorded" in caveat
    assert "not proof of the order they were run" in caveat
    assert "order they were performed." not in caveat, "the strong claim must not survive"

    assert ordering_caveat(progression([stated])) == "Runs in the order they were performed."


class _DocumentedIngest:
    """An ingest half written to exactly the contract `ingest/sources/README.md` documented.

    Two methods, no constructor arguments — which is what a site reading that README would write,
    and what `make datasource-validate` used to pass while the registry then refused to build it.
    """

    async def fetch_new_entries(self, since: datetime) -> list[RawEntry]:
        return []  # pragma: no cover - never built

    def map_to_ord(self, raw: RawEntry) -> Any:  # pragma: no cover - never built
        raise NotImplementedError


class _DocumentedCommitments:
    """A commitments half that takes no source name — the same shape, on the third half."""

    name = "documented-commitments"

    async def fetch_commitments(self, since: datetime | None) -> list[Any]:
        return []  # pragma: no cover - never built


def test_the_gate_binds_every_half_as_the_registry_actually_calls_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`make datasource-validate` and worker startup must agree, in both directions.

    The callable's signature is the config schema, so the gate binds the manifest's kwargs against
    it offline, including the `name` the registry passes to every half. A gate that passes what the
    runtime refuses is worse than none. Asserted as agreement, so it holds whichever way the
    contract moves.
    """
    for half, reference in (
        ("ingest", "tests.test_datasource_seam:_DocumentedIngest"),
        ("commitments", "tests.test_datasource_seam:_DocumentedCommitments"),
    ):
        folder = tmp_path / half
        folder.mkdir()
        _write_source(
            folder,
            "documented",
            f"""\
            name: documented
            description: A half written to the documented contract, with no `name` parameter.
            {half}: {reference}
            """,
        )
        monkeypatch.setattr(settings, "data_sources_dir", str(folder))
        monkeypatch.setattr(settings, "data_sources", "documented")
        registry.forget_discovered()

        built: Exception | None = None
        try:
            registry.make_data_source("documented")
        except registry.DataSourceError as exc:
            built = exc

        assert built is not None, f"the registry passes `name` to every {half} half"
        assert "name" in str(built)
        problems = validate_datasources()
        assert [p for p in problems if "documented" in p and half in p], (
            f"the {half} half fails to build, and the gate that exists to say so before a deploy "
            f"reported: {problems}"
        )


def test_a_quoted_flag_in_a_manifest_is_refused_rather_than_read_as_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A quoted flag in a manifest is refused rather than read as true.

    `snapshot` on `commitments-json` licenses a destructive sweep, and YAML parses `"false"` as a
    non-empty, truthy string, so the quoted spelling of "off" would arm it. Values in a `config:`
    block are behaviour, not addresses, so their types are checked. Driven through a real manifest,
    because the gate **and** the build must agree.
    """
    folder = tmp_path / "quoted"
    folder.mkdir()
    (folder / "datasource.yaml").write_text(
        textwrap.dedent(
            """\
            name: quoted
            description: A commitments export whose snapshot flag is written as a quoted word.
            commitments: chemclaw.ingest.commitments.json_export:json_commitment_export
            config:
              snapshot: "false"
            """
        ),
    )
    monkeypatch.setattr(settings, "data_sources_dir", str(folder.parent))
    monkeypatch.setattr(settings, "data_sources", "quoted")
    registry.forget_discovered()

    with pytest.raises(registry.DataSourceError) as refused:
        registry.make_data_source("quoted")
    message = str(refused.value)
    assert "snapshot" in message and "bool" in message, message

    problems = validate_datasources()
    assert [p for p in problems if "quoted" in p and "snapshot" in p], (
        f"the build refuses this manifest and the gate that exists to say so first did not: "
        f"{problems}"
    )


def test_a_flag_written_the_way_yaml_spells_one_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction, so "refuses everything" cannot pass as "checks the type".

    Both spellings of a real boolean, on the same manifest shape the test above rejects.
    """
    for written, expected in (("true", True), ("false", False)):
        name = f"flag-{written}"
        folder = tmp_path / name
        folder.mkdir()
        (folder / "datasource.yaml").write_text(
            textwrap.dedent(
                f"""\
                name: {name}
                description: A commitments export whose snapshot flag is a real YAML boolean.
                commitments: chemclaw.ingest.commitments.json_export:json_commitment_export
                config:
                  snapshot: {written}
                """
            ),
        )
        monkeypatch.setattr(settings, "data_sources_dir", str(tmp_path))
        monkeypatch.setattr(settings, "data_sources", name)
        registry.forget_discovered()
        source = registry.make_data_source(name)
        assert source.commitments is not None
        assert source.commitments.snapshot is expected
