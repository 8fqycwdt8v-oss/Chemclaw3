"""Costing a mounted share before crawling it, and draining it: `cli/sync_share.py`.

The crawl loop belongs to `tests/test_document_share.py`. Tested here is the command's layer:
resolving a share from the enabled data sources, the dry-run estimate, the drain loop over a
share larger than one pass, the pass-size guard, and the merged report handed to `prune_share`.
The share is real files attached as an operator would, and the index is `InMemoryDocumentIndex`;
only the backend lookup is redirected.
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
    """Drop the discovery cache around every test here.

    `discovered()` is cached, and these tests move `data_sources_dir`.
    """
    registry.forget_discovered()
    yield
    registry.forget_discovered()


@pytest.fixture
def share(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real mounted share, attached and enabled. Returns the mount.

    The shipped sources stay on the discovery path, so they are discovered but not enabled; that is
    the distinction the resolver has to make.
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
    """Redirect `default_document_index()` to the in-memory reference backend.

    The drain below is the real one with its storage swapped.
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
    """A share that is shipped but not enabled is refused.

    Resolving by discovered name would crawl a mount no deployment asked for; the refusal names what
    is enabled.
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
    """A dry run costs the share without reading a file or touching the index.

    `broken.pdf` is not a PDF and would raise if opened, and `default_document_index` is replaced by
    a call that refuses.
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
    """A walk that stopped short says the share is larger than the pass.

    The dry run walks at most `_DRY_RUN_LIMIT` entries, so `candidates:` is then a floor.
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
    """A pass of zero documents is refused before anything runs.

    A pass examining no file still reports `has_more`, and a sweep after it would read the share as
    empty. `prune_share` refuses too; this stops the run from starting.
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
    """Four candidates, one per pass: the loop resumes rather than restarts.

    Every counter covers the whole share exactly once only if each pass starts where the last ended.
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
    """A drain whose root vanished prunes nothing.

    An unreachable share and an empty one look identical, so `prune_share` decides on the drain's
    merged report, the same object the durable workflow hands over.
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

    A real crawl cannot produce this, so the crawl is stubbed and fails the test after a few calls
    rather than hanging. The stopped drain must also refuse to sweep.
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
    """One run of the command refreshes every stale vector, however small the batch.

    With a batch of one and three stale chunks, a single pass would visibly leave two behind.
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
    """The drain leaves chunks cut under another share's boundaries alone.

    The index is shared; the pass is scoped to the cutting this share's binding declares.
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
