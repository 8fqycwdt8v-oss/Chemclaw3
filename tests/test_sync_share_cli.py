"""Costing a mounted share before crawling it, and draining it — `cli/sync_share.py`.

The crawl/parse/chunk/embed loop underneath belongs to `tests/test_document_share.py` and is not
re-tested here. What is tested is the command's own layer, which nothing else covers: resolving a
share out of the *enabled* data sources and refusing everything else by name, the dry-run cost
estimate, the drain loop that walks a share larger than one pass, the pass-size guard whose absence
once swept a whole source, and the merged report `prune_share` is handed as its evidence.

Real throughout. The share is a directory of real files, the source is attached the way an operator
attaches one — a `datasource.yaml` folder plus the name in `CHEMCLAW_DATA_SOURCES` — and the index
is `InMemoryDocumentIndex`, the reference implementation the sync tests run the same loop against.
The command resolves the Postgres backend for itself, so that one call is redirected and nothing
else is; every number asserted below is produced by the real crawl over the real tree.
"""

import asyncio
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

import chemclaw.cli.sync_share as cli
from chemclaw.core.config import settings
from chemclaw.core.embeddings import clear_embedding_cache, embedding_config_key
from chemclaw.ingest.documents.binding import (
    DocumentShareBinding,
    DocumentShareError,
    load_binding,
)
from chemclaw.ingest.documents.index import (
    ChunkRecord,
    DocumentIndex,
    FileRecord,
    InMemoryDocumentIndex,
)
from chemclaw.ingest.documents.sync import SyncReport
from chemclaw.ingest.sources import registry

SOURCE = "sharetest"

# What the fixture share holds, so a changed tree cannot leave a stale number asserted below.
_CANDIDATES = 4  # alpha.txt, beta.txt, nested/gamma.txt, broken.pdf
_READABLE = 3  # broken.pdf is a candidate by name and refused when it is opened


def _binding(mount: Path) -> dict[str, Any]:
    """The share's declared layout — the `binding:` block of its manifest."""
    return {
        "mount": str(mount),
        "required_roles": ["sharetest.reader"],
        "roots": [{"path": "Docs", "tags": ["docs"]}],
        "exclude": ["~$*"],
        "extensions": [".txt", ".pdf"],
        "max_file_bytes": 50_000,
        "chunk_chars": 400,
        "chunk_overlap_chars": 50,
    }


def _chunking(mount: Path) -> str:
    """The chunking key this share's rows are written under; the index gates on it."""
    return load_binding(_binding(mount)).chunking_key


def _build_share(root: Path) -> None:
    """A small departmental drive: what can be read, what cannot, and what is turned away."""
    docs = root / "Docs"
    (docs / "nested").mkdir(parents=True)
    (docs / "alpha.txt").write_text("Toluene is the solvent of record for the acme route.\n")
    (docs / "beta.txt").write_text("The palladium catalyst deactivated above 80 degrees.\n")
    (docs / "nested" / "gamma.txt").write_text("Yield 84 percent, impurity below 0.5 percent.\n")
    # A candidate by name and unreadable in fact. It is the file that separates the two commands:
    # a dry run counts it without opening it, a drain opens it and refuses it.
    (docs / "broken.pdf").write_bytes(b"not a PDF at all")
    # Turned away by format (two of one extension, one of another — the estimate ranks them),
    # by size, and by the lock-file exclusion.
    (docs / "legacy.doc").write_bytes(b"\xd0\xcf\x11\xe0old binary word")
    (docs / "older.doc").write_bytes(b"\xd0\xcf\x11\xe0older binary word")
    (docs / "scratch.log").write_text("noise")
    (docs / "huge.txt").write_text("x" * 60_000)
    (docs / "~$alpha.txt").write_text("lock")


def _write_manifest(directory: Path, name: str, mount: Path) -> None:
    """Attach the share: one folder holding one `datasource.yaml`, and nothing else."""
    folder = directory / name
    folder.mkdir(parents=True)
    (folder / registry.MANIFEST_FILENAME).write_text(
        yaml.safe_dump(
            {
                "name": name,
                "description": "A mounted share built for this test, indexed as cited evidence.",
                "retrieve": "chemclaw.ingest.documents.retriever:ShareDocumentRetriever",
                "config": {"binding": _binding(mount)},
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _fresh_discovery() -> Iterator[None]:
    """Drop the discovery cache around every test here, as `tests/test_datasource_seam.py` does.

    `discovered()` is `@cache`d for production, where the layout is fixed for the process's life.
    These tests move `data_sources_dir`, and a cached entry would answer for the wrong directory —
    silently, by returning a plausible set of sources.
    """
    registry.discovered.cache_clear()
    yield
    registry.discovered.cache_clear()


@pytest.fixture
def share(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real mounted share, attached and enabled. Returns the mount.

    The shipped source directory stays on the discovery path behind the temporary one, so the
    sources this repository ships — `sharedrive` among them — are *discovered* here while only
    this one is *enabled*. That is the distinction the resolver has to make (D-018), and it cannot
    be tested against a directory holding nothing else.
    """
    mount = tmp_path / "mount"
    _build_share(mount)
    sources = tmp_path / "sources"
    _write_manifest(sources, SOURCE, mount)
    monkeypatch.setattr(
        settings, "data_sources_dir", os.pathsep.join([str(sources), settings.data_sources_dir])
    )
    monkeypatch.setattr(settings, "data_sources", SOURCE)
    return mount


@pytest.fixture
def index(monkeypatch: pytest.MonkeyPatch) -> InMemoryDocumentIndex:
    """Redirect the single production dependency the command resolves for itself.

    `_drain` calls `default_document_index()`, which is Postgres under the default config. The
    in-memory index is the reference backend `tests/test_document_share.py` drives the identical
    loop against, so what runs below is the real drain with its storage swapped — not a simulation
    of one.
    """
    backend = InMemoryDocumentIndex()
    monkeypatch.setattr(cli, "default_document_index", lambda: backend)
    return backend


# --- resolving the share ------------------------------------------------------------------------


def test_the_share_is_found_through_the_enabled_data_sources(share: Path) -> None:
    """The command reaches the share the same way retrieval does, through the one registry."""
    resolved = cli._resolve(SOURCE)
    assert resolved.name == SOURCE
    assert resolved.share_binding().mount == str(share)


def test_a_share_that_is_shipped_but_not_enabled_is_refused(share: Path) -> None:
    """Discovery is not enablement, and `sharedrive` points at `/mnt/sharedrive`.

    The shipped manifest is on the discovery path in every one of these tests, so resolving by
    *discovered* name rather than by enabled name would have this command crawl a mount no
    deployment asked for. The refusal names what is actually enabled instead.
    """
    with pytest.raises(DocumentShareError) as refusal:
        cli._resolve("sharedrive")
    assert "no enabled data source named 'sharedrive'" in str(refusal.value)
    assert f"enabled shares: ['{SOURCE}']" in str(refusal.value)


def test_an_enabled_source_that_carries_no_share_is_refused_by_name(
    share: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`graph` is enabled and retrievable and is not a share; the crawl has nothing to walk.

    The marker is structural — a retrieve half with `share_binding` — so this is what stops the
    command from handing a knowledge-graph retriever to a filesystem crawl.
    """
    monkeypatch.setattr(settings, "data_sources", f"graph,{SOURCE}")
    with pytest.raises(DocumentShareError) as refusal:
        cli._resolve("graph")
    assert "no enabled data source named 'graph'" in str(refusal.value)
    assert f"enabled shares: ['{SOURCE}']" in str(refusal.value)
    assert "CHEMCLAW_DATA_SOURCES" in str(refusal.value)


def test_the_refusal_says_none_rather_than_an_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no share enabled at all the message must still read as a sentence."""
    monkeypatch.setattr(settings, "data_sources", "graph")
    with pytest.raises(DocumentShareError, match="enabled shares: none"):
        cli._resolve(SOURCE)


def test_an_unresolvable_share_exits_two_and_prints_to_stderr(
    share: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The operator-visible half: a refusal is a diagnosable exit code, never a traceback."""
    code = cli.main(["not-a-source", "--dry-run"])
    captured = capsys.readouterr()

    assert code == 2
    assert "no enabled data source named 'not-a-source'" in captured.err
    assert captured.out == "", "a failed run must not print an estimate as well"


# --- the dry run --------------------------------------------------------------------------------


def test_a_dry_run_costs_the_share_without_reading_a_file_or_touching_the_index(
    share: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command's reason to exist: know the bill before paying it.

    Two controls, because "reads nothing" is a claim about behaviour. `broken.pdf` is a candidate
    whose bytes are not a PDF, so it is counted here and would raise on any path that opened it;
    and `default_document_index` is replaced by a call that refuses, so a dry run that reached the
    database — the thing this flag exists to avoid — fails instead of passing quietly.
    """

    def refuse() -> DocumentIndex:
        raise AssertionError("a dry run must not open the document index")

    monkeypatch.setattr(cli, "default_document_index", refuse)

    code = cli.main([SOURCE, "--dry-run"])
    out = capsys.readouterr().out

    assert code == 0
    assert f"candidates:        {_CANDIDATES}" in out
    assert "over size limit:   1" in out


def test_the_estimate_ranks_the_unreadable_formats_by_how_many_there_are(
    share: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`.doc` first is the whole point — it is what decides which roots a deployment starts with."""
    cli.main([SOURCE, "--dry-run"])
    lines = [line.strip() for line in capsys.readouterr().out.splitlines()]
    formats = [line for line in lines if line.startswith((".doc", ".log"))]

    assert formats == [".doc       2", ".log       1"]


def test_the_estimate_is_a_range_anchored_on_this_binding_s_chunk_size(share: Path) -> None:
    """A cost stated in chunks, in the unit that is billed, and labelled as an estimate."""
    binding = load_binding(_binding(share))
    said = cli._estimate(binding, SyncReport(source=SOURCE, scanned=250))

    assert "roughly 250 to 2500 chunks" in said
    assert f"cut at {binding.chunk_chars} characters" in said


def test_a_walk_that_stopped_short_says_the_share_is_larger_than_the_pass(share: Path) -> None:
    """The number an operator would otherwise read as the size of the share.

    The dry run walks at most `_DRY_RUN_LIMIT` entries, so on a big share `candidates:` is a floor
    rather than a total, and the line saying so is the difference between an estimate and a wrong
    answer.
    """
    binding = load_binding(_binding(share))
    stopped = cli._estimate(binding, SyncReport(source=SOURCE, scanned=1, has_more=True))
    finished = cli._estimate(binding, SyncReport(source=SOURCE, scanned=1))

    assert f"stopped after {cli._DRY_RUN_LIMIT} entries" in stopped
    assert "stopped after" not in finished


# --- the pass size ------------------------------------------------------------------------------


def test_a_pass_of_zero_documents_is_refused_before_anything_runs(
    share: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The incident this guard is: `--limit 0` scanned nothing and swept the whole source.

    A pass that examines no file still reports `has_more`, and the sweep that follows a drain used
    to read that as "the share is empty". `prune_share` refuses it now too, on the merged report;
    this is the outer half, which stops the run from being started at all.
    """
    with pytest.raises(SystemExit) as exit_code:
        cli.main([SOURCE, "--limit", "0"])

    assert exit_code.value.code == 2
    assert "must be at least 1" in capsys.readouterr().err
    assert cli._positive("1") == 1


# --- the drain ----------------------------------------------------------------------------------


def test_the_drain_walks_a_share_larger_than_one_pass_and_counts_each_entry_once(
    share: Path, index: InMemoryDocumentIndex, capsys: pytest.CaptureFixture[str]
) -> None:
    """Four candidates, one per pass: the loop must resume rather than restart.

    A drain that dropped the cursor would re-examine the same head of the walk forever, and the
    skip tallies are what make that visible — every counter here is the whole share's, counted
    exactly once, which only holds if each pass started where the last one stopped.
    """
    code = cli.main([SOURCE, "--limit", "1"])
    report = json.loads(capsys.readouterr().out)

    assert code == 0
    assert report["source"] == SOURCE
    assert report["scanned"] == _CANDIDATES
    assert report["indexed"] == _READABLE
    assert report["skipped_unreadable"] == 1  # broken.pdf, opened and refused
    assert report["skipped_oversized"] == 1
    assert report["skipped_unsupported"] == {".doc": 2, ".log": 1}
    assert not report["has_more"]
    assert report["pruned"] == 0

    stored = asyncio.run(
        index.fingerprints(
            SOURCE,
            ["Docs/alpha.txt", "Docs/beta.txt", "Docs/nested/gamma.txt"],
            _chunking(share),
        )
    )
    assert len(stored) == _READABLE


def test_a_second_run_over_an_unchanged_share_indexes_nothing(
    share: Path, index: InMemoryDocumentIndex, capsys: pytest.CaptureFixture[str]
) -> None:
    """The scheduled-run cost, through the command an operator actually types."""
    cli.main([SOURCE])
    capsys.readouterr()

    cli.main([SOURCE])
    report = json.loads(capsys.readouterr().out)

    assert report["indexed"] == 0
    assert report["unchanged"] == _READABLE
    assert report["embedded_chunks"] == 0


def test_a_deleted_file_is_swept_once_the_drain_has_seen_the_whole_share(
    share: Path, index: InMemoryDocumentIndex, capsys: pytest.CaptureFixture[str]
) -> None:
    """A citation must not survive the document it points at — and the count is reported."""
    cli.main([SOURCE])
    capsys.readouterr()
    (share / "Docs" / "beta.txt").unlink()

    cli.main([SOURCE])
    report = json.loads(capsys.readouterr().out)

    assert report["pruned"] == 1
    assert asyncio.run(index.fingerprints(SOURCE, ["Docs/beta.txt"], _chunking(share))) == {}
    assert len(asyncio.run(index.fingerprints(SOURCE, ["Docs/alpha.txt"], _chunking(share)))) == 1


def test_a_drain_whose_root_vanished_prunes_nothing(
    share: Path, index: InMemoryDocumentIndex, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unreachable share and an empty one look identical from here.

    The evidence `prune_share` refuses on is the drain's *merged* report, which is this command's
    to assemble — the durable workflow caught a bad drain and the CLI did not, so the two now hand
    over the same object rather than each deriving a boolean from it.
    """
    cli.main([SOURCE])
    capsys.readouterr()
    (share / "Docs").rename(share / "Docs-moved-by-someone")

    code = cli.main([SOURCE])
    report = json.loads(capsys.readouterr().out)

    assert code == 0
    assert report["failed_roots"] == ["Docs"]
    assert report["scanned"] == 0
    assert report["pruned"] == 0
    assert len(asyncio.run(index.fingerprints(SOURCE, ["Docs/alpha.txt"], _chunking(share)))) == 1


def test_a_drain_that_cannot_advance_stops_instead_of_looping_forever(
    share: Path, index: InMemoryDocumentIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass that returns the cursor it was given is a wedge, and the loop breaks on it.

    **The one substituted dependency in this file, and it is substituted because a real crawl
    cannot produce this.** `crawl_share` only sets its cursor on an entry it examined past `after`,
    so a non-advancing pass is unreachable from any directory tree — which is exactly why the guard
    would otherwise be unfalsifiable. The stub therefore fails the test after a handful of calls,
    so removing the guard reports a failure instead of hanging until the suite's timeout.

    What it must also do is refuse to sweep: the drain stopped early, so this run is not evidence
    that anything is gone. The rows from the real drain above are what would be deleted.
    """
    cli.main([SOURCE])
    passes = 0

    async def wedged(
        source: str,
        binding: DocumentShareBinding,
        backend: DocumentIndex,
        *,
        after: str = "",
        limit: int = 1000,
    ) -> SyncReport:
        nonlocal passes
        passes += 1
        assert passes <= 4, "the drain repeated a pass that made no progress"
        return SyncReport(source=source, scanned=1, cursor="Docs/alpha.txt", has_more=True)

    monkeypatch.setattr(cli, "sync_share", wedged)
    merged = asyncio.run(cli._drain(SOURCE, cli._resolve(SOURCE), limit=1000))

    assert passes == 2, "the second pass returned the cursor it was handed; there is no third"
    assert merged.has_more
    assert merged.pruned == 0
    assert len(asyncio.run(index.fingerprints(SOURCE, ["Docs/alpha.txt"], _chunking(share)))) == 1


# --- the re-embedding pass at the head of the drain ----------------------------------------------


def test_the_drain_refreshes_every_stale_vector_however_small_the_batch(
    share: Path,
    index: InMemoryDocumentIndex,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One run of the command leaves no chunk behind on a superseded model.

    The pass is bounded — it has to be, since it runs against the whole corpus — so the command
    loops it until it reports no more. Pinned to a batch of one here, which is what makes a single
    pass visibly insufficient: three chunks are stale and a drain that ran the pass once would
    leave two of them comparable to nothing else in the index.
    """
    cli.main([SOURCE])
    capsys.readouterr()
    live = {_chunking(share)}

    monkeypatch.setattr(settings, "embedding_model", "some-better-model")
    clear_embedding_cache()
    monkeypatch.setattr(settings, "document_reembed_batch_size", 1)
    stale = asyncio.run(index.stale_chunks(embedding_config_key(), 100, live))
    assert len(stale) > 1, "with one stale chunk the batch bound would prove nothing"

    cli.main([SOURCE])

    assert asyncio.run(index.stale_chunks(embedding_config_key(), 100, live)) == []


def test_the_drain_leaves_another_share_s_cutting_alone(
    share: Path,
    index: InMemoryDocumentIndex,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Syncing this share is a statement about this share's rows, and the index is shared.

    A chunk cut under another share's boundaries is either that share's business or about to be
    re-cut by its own crawl; refreshing it here is an embedding call paid for and then discarded.
    So the drain scopes the pass to the cutting its own binding declares, and the foreign row below
    is still on its old configuration afterwards.
    """
    foreign_chunking = "9999:1"
    asyncio.run(
        index.upsert(
            [
                FileRecord(
                    path="Other/report.txt",
                    source="another-share",
                    doc_id="doc-foreign",
                    fingerprint="1:1",
                    chunking_key=foreign_chunking,
                )
            ],
            [
                ChunkRecord(
                    doc_id="doc-foreign",
                    chunking_key=foreign_chunking,
                    ordinal=0,
                    content="a document cut by a share this command was not pointed at",
                    embedding=[0.1] * settings.embedding_dim,
                )
            ],
            "an-older-embedding-configuration",
        )
    )
    monkeypatch.setattr(settings, "embedding_model", "some-better-model")
    clear_embedding_cache()

    cli.main([SOURCE])
    capsys.readouterr()

    assert asyncio.run(index.stale_chunks(embedding_config_key(), 100, {_chunking(share)})) == []
    assert asyncio.run(index.stale_chunks(embedding_config_key(), 100, {foreign_chunking})), (
        "another share's chunks must keep their vectors until that share's own drain runs"
    )
