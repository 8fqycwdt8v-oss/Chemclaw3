"""A mounted SMB share, end to end: crawl, index, retrieve, and the two rules that protect it.

Built on a real directory tree of real documents (`tests/document_fixtures.py`), an in-memory
index, and no database or broker, so the actual crawl/parse/chunk/embed loop runs.

- **A complete crawl may sweep; an incomplete one may not.** A dropped CIFS mount looks like an
  empty directory, and pruning on that deletes the corpus
  (`test_a_failed_root_prunes_nothing`).
- **Cost is measured.** The dedup and no-re-embed tests count real `embed_texts` calls.
"""

import asyncio
import fnmatch
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pathspec
import pytest
import yaml

from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.core.embeddings import clear_embedding_cache, embed_texts, embedding_config_key
from chemclaw.core.identity_context import (
    GROUP_ROLE_PREFIX,
    reset_current_identity,
    set_current_identity,
)
from chemclaw.ingest.documents import retriever as retriever_module
from chemclaw.ingest.documents import sync as sync_module
from chemclaw.ingest.documents.binding import DocumentShareError, load_binding
from chemclaw.ingest.documents.chunk import chunk_document
from chemclaw.ingest.documents.crawl import crawl_share
from chemclaw.ingest.documents.external_index import ExternalVectorDocumentIndex
from chemclaw.ingest.documents.index import (
    ChunkRecord,
    DocumentFilter,
    DocumentIndexError,
    FileRecord,
    InMemoryDocumentIndex,
    PostgresDocumentIndex,
)
from chemclaw.ingest.documents.parse import (
    DocumentParseError,
    ParsedDocument,
    parse_document,
)
from chemclaw.ingest.documents.retriever import ShareDocumentRetriever
from chemclaw.ingest.documents.sync import (
    SyncReport,
    prune_share,
    reembed_stale,
    sync_share,
)
from chemclaw.retrieval.evidence import EvidenceChunk, RetrieverSkip
from chemclaw.retrieval.fanout import sweep_sources
from chemclaw.retrieval.vectors.base import stored_embedding_key
from chemclaw.retrieval.vectors.memory import InMemoryVectorStore
from tests.document_fixtures import (
    _blank_pdf_bytes,
    _docx_bytes,
    _text_pdf_bytes,
    _xlsx_bytes,
)
from tests.pg import migrated_db_or_skip

SOURCE = "sharedrive"


def _share(root: Path) -> dict[str, Any]:
    """A share tree that looks like a real departmental drive, including what cannot be read."""
    projects = root / "Projects"
    (projects / "acme-17" / "2024").mkdir(parents=True)
    (projects / "beta-9").mkdir(parents=True)
    (root / "SOPs").mkdir()
    (root / "Archive").mkdir()

    report = _text_pdf_bytes(["Yield 84 percent for the acme route", "Impurity below 0.5 percent"])
    (projects / "acme-17" / "2024" / "report.pdf").write_bytes(report)
    # The same report, filed again in another project. This is what a classical share does.
    (projects / "beta-9" / "report-copy.pdf").write_bytes(report)
    (projects / "acme-17" / "notes.docx").write_bytes(
        _docx_bytes(["The palladium catalyst deactivated above 80 degrees."])
    )
    (root / "SOPs" / "handling.xlsx").write_bytes(
        _xlsx_bytes({"Limits": [["solvent", "limit"], ["toluene", 890]]})
    )
    # Refused by name rather than returned as an empty document.
    (projects / "beta-9" / "scanned.pdf").write_bytes(_blank_pdf_bytes(3))
    # Formats and paths the crawl must turn away, each for a different reason.
    (projects / "beta-9" / "legacy.doc").write_bytes(b"\xd0\xcf\x11\xe0old binary word")
    (projects / "acme-17" / "~$notes.docx").write_bytes(b"lock")
    (root / "Archive" / "ancient.pdf").write_bytes(report)
    (root / "SOPs" / "huge.txt").write_bytes(b"x " * 200_000)

    return {
        "mount": str(root),
        "required_roles": ["sharedrive.reader"],
        "roots": [
            {"path": "Projects", "tags": ["project-work"], "tag_from_path": {"segment": 0}},
            {"path": "SOPs", "tags": ["sop"]},
        ],
        "exclude": ["~$*", "**/Archive/**"],
        "extensions": [".pdf", ".docx", ".xlsx", ".txt"],
        "max_file_bytes": 200_000,
        "chunk_chars": 400,
        "chunk_overlap_chars": 50,
    }


@pytest.fixture
def share(tmp_path: Path) -> dict[str, Any]:
    """The raw binding mapping for a freshly-built fixture share."""
    return _share(tmp_path)


@pytest.fixture
def as_user() -> Iterator[Callable[[str, set[str]], None]]:
    """Bind an ambient identity for one test and unbind it afterwards.

    A `ContextVar` set in a test leaks into later ones, so a retrieval test could pass on an actor a
    previous test left bound.
    """
    tokens: list[tuple[object, object]] = []

    def bind(actor: str, roles: set[str]) -> None:
        tokens.append(set_current_identity(actor, frozenset(roles)))

    yield bind
    for token in reversed(tokens):
        reset_current_identity(token)


@pytest.fixture
def counted_embeddings(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[int]]:
    """Count how many texts the sync actually embeds — the cost claim, made checkable."""
    calls: list[int] = []
    real = embed_texts

    def counting(texts: list[str], **kwargs: object) -> list[list[float]]:
        calls.append(len(texts))
        return real(texts)

    monkeypatch.setattr(sync_module, "embed_texts", counting)
    yield calls


def _chunking(share: dict[str, Any]) -> str:
    """The chunking key this share's rows are written under — the index gates on it too."""
    return load_binding(share).chunking_key


# --- the walk -----------------------------------------------------------------------------------


def test_the_crawl_reads_nothing_and_still_knows_what_to_skip(share: dict[str, Any]) -> None:
    """Extension, exclusion and size filters all run on the directory entry, before any read."""
    result = crawl_share(load_binding(share))
    paths = {ref.path for ref in result.files}

    assert "Projects/acme-17/2024/report.pdf" in paths
    assert "SOPs/handling.xlsx" in paths
    # Excluded by glob (lock file), by root (Archive is not a declared root *and* is excluded),
    # by format (.doc), and by size (huge.txt is over max_file_bytes).
    assert not any(name in path for path in paths for name in ("~$", "Archive", "legacy.doc"))
    assert "SOPs/huge.txt" not in paths
    assert result.skipped_oversized == 1
    assert result.skipped_unsupported[".doc"] == 1
    assert not result.failed_roots


def test_a_project_code_is_lifted_out_of_the_path(share: dict[str, Any]) -> None:
    """The commonest thing a classical share encodes is the folder a file sits in."""
    files = {ref.path: ref.tags for ref in crawl_share(load_binding(share)).files}
    assert set(files["Projects/acme-17/2024/report.pdf"]) == {"project-work", "acme-17"}
    assert set(files["SOPs/handling.xlsx"]) == {"sop"}


def test_a_bounded_crawl_resumes_without_double_counting(share: dict[str, Any]) -> None:
    """Chunked walks must together see each entry exactly once — counters included.

    The cursor is the last entry *examined*, not the last one accepted; if it were the latter,
    everything skipped between them would be re-examined and tallied twice.
    """
    binding = load_binding(share)
    whole = crawl_share(binding, limit=1000)

    seen: list[str] = []
    unsupported = 0
    after = ""
    for _ in range(20):
        chunk = crawl_share(binding, after=after, limit=1)
        seen += [ref.path for ref in chunk.files]
        unsupported += sum(chunk.skipped_unsupported.values())
        if not chunk.has_more:
            break
        after = chunk.cursor

    assert seen == [ref.path for ref in whole.files]
    assert unsupported == sum(whole.skipped_unsupported.values())


def test_an_unmounted_share_is_loud_rather_than_empty(tmp_path: Path) -> None:
    """The one failure that must not degrade to "the share is empty"."""
    binding = load_binding(
        {"mount": str(tmp_path / "nope"), "roots": [{"path": "."}], "public": True}
    )
    with pytest.raises(DocumentShareError, match="not mounted"):
        crawl_share(binding)


def test_a_symlink_out_of_the_mount_is_not_followed(tmp_path: Path) -> None:
    """A share full of links must not publish a corpus nobody meant to expose."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("payroll")
    mount = tmp_path / "mount"
    (mount / "Docs").mkdir(parents=True)
    (mount / "Docs" / "link").symlink_to(outside)

    binding = load_binding({"mount": str(mount), "roots": [{"path": "Docs"}], "public": True})
    assert crawl_share(binding).files == []


def test_a_symlink_cycle_inside_the_mount_is_walked_once(tmp_path: Path) -> None:
    """A symlink cycle inside the mount is walked once.

    `_within_mount` checks escape, not cycles (`Projects/sub/current -> ..`). Recursing through one
    emits the same file under unboundedly many paths until `scandir` fails, which marks the root
    failed and stops every future prune, and eats the chunk's `limit` before real files are reached.
    """
    mount = tmp_path / "mount"
    (mount / "Projects" / "sub").mkdir(parents=True)
    (mount / "Projects" / "sub" / "a.txt").write_text("real content")
    (mount / "Projects" / "sub" / "loop").symlink_to(mount / "Projects")

    binding = load_binding(
        {
            "mount": str(mount),
            "roots": [{"path": "Projects"}],
            "public": True,
            "follow_symlinks": True,
        }
    )
    result = crawl_share(binding)

    assert [ref.path for ref in result.files] == ["Projects/sub/a.txt"]
    assert result.failed_roots == [], "a cycle disabled the share's pruning for good"


def test_two_roots_linked_to_one_directory_index_it_once(tmp_path: Path) -> None:
    """Two roots linked to one directory index it once.

    Otherwise the same file is indexed under two paths. The walk's visited set is keyed on the
    directory's identity, not its path, which covers both shapes.
    """
    mount = tmp_path / "mount"
    (mount / "Data").mkdir(parents=True)
    (mount / "Data" / "report.txt").write_text("one report")
    (mount / "Archive").mkdir()
    (mount / "Archive" / "all").symlink_to(mount / "Data")

    binding = load_binding(
        {
            "mount": str(mount),
            "roots": [{"path": "Archive"}, {"path": "Data"}],
            "public": True,
            "follow_symlinks": True,
        }
    )
    result = crawl_share(binding)

    assert [ref.path for ref in result.files] == ["Archive/all/report.txt"]
    assert result.failed_roots == []


# --- the binding --------------------------------------------------------------------------------


def test_an_extension_nothing_can_read_is_refused_at_load() -> None:
    """The quiet failure: `.pdff` matches nothing, so the share indexes cleanly and is empty."""
    with pytest.raises(DocumentShareError, match="unreadable extension"):
        load_binding({"mount": "/mnt/x", "roots": [{"path": "."}], "extensions": [".pdff"]})


def test_overlapping_roots_are_refused() -> None:
    """Two roots covering one file would index it twice under two tag sets, last write winning."""
    with pytest.raises(DocumentShareError, match="overlap"):
        load_binding(
            {"mount": "/mnt/x", "roots": [{"path": "Projects"}, {"path": "Projects/acme"}]}
        )


# --- chunking -----------------------------------------------------------------------------------


def test_a_chunk_never_spans_two_pages() -> None:
    """A citation to the wrong page is worse than a citation to none."""
    text = "[page 1]\n" + "alpha " * 200 + "\n\n[page 2]\nbeta"
    chunks = chunk_document(text, chunk_chars=400, overlap_chars=50)
    assert {chunk.coordinate for chunk in chunks} == {"page 1", "page 2"}
    assert all(("beta" in c.content) == (c.coordinate == "page 2") for c in chunks)


def test_a_single_oversized_line_is_split_rather_than_dropped() -> None:
    """A CSV export whose one row is longer than any chunk still has to be retrievable."""
    chunks = chunk_document("x" * 5000, chunk_chars=400, overlap_chars=50)
    assert len(chunks) == 13
    assert all(len(chunk.content) <= 400 for chunk in chunks)


# --- the sync loop ------------------------------------------------------------------------------


def test_the_share_is_indexed_and_every_refusal_is_counted(
    share: dict[str, Any], counted_embeddings: list[int]
) -> None:
    """A first pass indexes what it can read and *reports* what it could not, per reason."""
    index = InMemoryDocumentIndex()
    report = asyncio.run(sync_share(SOURCE, load_binding(share), index))

    assert report.indexed == 4  # report.pdf, report-copy.pdf, notes.docx, handling.xlsx
    assert report.skipped_scan == 1  # the blank PDF, refused by name
    assert report.skipped_oversized == 1
    assert report.skipped_unsupported[".doc"] == 1
    assert not report.failed_roots
    assert report.embedded_chunks > 0


def test_unrelated_empty_files_are_each_their_own_document(
    share: dict[str, Any], counted_embeddings: list[int], tmp_path: Path
) -> None:
    """Identity is the content, and "no content" is not content unrelated files can share.

    Empty files are a real population on a share (`SyncReport.empty`); hashing them all to one
    `doc_id` would fold them into one logical document, count the rest as copies and cite one
    arbitrary path for all.
    """
    # Three formats, three different files, one shared extraction: the empty string.
    (tmp_path / "SOPs" / "placeholder.txt").write_bytes(b"")
    (tmp_path / "Projects" / "beta-9" / "tbd.docx").write_bytes(_docx_bytes([]))
    (tmp_path / "Projects" / "beta-9" / "blank.xlsx").write_bytes(_xlsx_bytes({"Limits": []}))
    index = InMemoryDocumentIndex()
    report = asyncio.run(sync_share(SOURCE, load_binding(share), index))

    assert report.empty == 3
    # Still exactly the one duplicate the fixture share carries on purpose (`report-copy.pdf`).
    assert report.deduplicated == 1, "three empty files are three documents, not one and two copies"


def test_the_same_document_in_two_folders_is_embedded_once(
    share: dict[str, Any], counted_embeddings: list[int]
) -> None:
    """The property that makes a TB share affordable: identity is the content, not the path."""
    index = InMemoryDocumentIndex()
    report = asyncio.run(sync_share(SOURCE, load_binding(share), index))

    assert report.deduplicated == 1  # report-copy.pdf carries content already indexed
    # Both paths are on record, so either can be cited...
    stored = asyncio.run(
        index.fingerprints(
            SOURCE,
            ["Projects/acme-17/2024/report.pdf", "Projects/beta-9/report-copy.pdf"],
            _chunking(share),
        )
    )
    assert len(stored) == 2
    # ...but only three distinct documents were ever chunked and embedded.
    assert sum(counted_embeddings) == report.embedded_chunks


def test_an_unchanged_share_re_embeds_nothing(
    share: dict[str, Any], counted_embeddings: list[int]
) -> None:
    """The scheduled-run cost claim, counted rather than asserted."""
    index = InMemoryDocumentIndex()
    binding = load_binding(share)
    asyncio.run(sync_share(SOURCE, binding, index))
    first = sum(counted_embeddings)

    second = asyncio.run(sync_share(SOURCE, binding, index))

    assert sum(counted_embeddings) == first  # not one further embedding call
    assert second.indexed == 0
    assert second.unchanged == 4
    assert second.embedded_chunks == 0


def test_an_edited_file_is_re_read_and_re_indexed(share: dict[str, Any], tmp_path: Path) -> None:
    """A stat signature that moved is the only thing that costs a read."""
    index = InMemoryDocumentIndex()
    binding = load_binding(share)
    asyncio.run(sync_share(SOURCE, binding, index))

    edited = tmp_path / "Projects" / "acme-17" / "notes.docx"
    edited.write_bytes(_docx_bytes(["The nickel catalyst survived above 80 degrees."]))

    report = asyncio.run(sync_share(SOURCE, binding, index))
    assert report.indexed == 1
    assert report.unchanged == 3


def test_two_shares_holding_the_same_relative_path_do_not_evict_each_other(
    share: dict[str, Any], tmp_path: Path
) -> None:
    """`Projects/report.pdf` is not an unusual name, and a share is not the only one mounted.

    Keyed on `path` alone — which is what the first migration said — the second share's crawl
    overwrote the first share's row and the first share's next sweep then deleted it, silently.
    """
    index = InMemoryDocumentIndex()
    second_root = tmp_path / "second"
    (second_root / "Projects" / "acme-17" / "2024").mkdir(parents=True)
    (second_root / "Projects" / "acme-17" / "2024" / "report.pdf").write_bytes(
        _text_pdf_bytes(["A different report that happens to live at the same relative path"])
    )
    second = {**share, "mount": str(second_root), "roots": [{"path": "Projects"}]}

    asyncio.run(sync_share(SOURCE, load_binding(share), index))
    asyncio.run(sync_share("sharedrive-2", load_binding(second), index))

    path = "Projects/acme-17/2024/report.pdf"
    assert asyncio.run(index.fingerprints(SOURCE, [path], _chunking(share)))
    assert asyncio.run(index.fingerprints("sharedrive-2", [path], _chunking(second)))

    # And a sweep of one share leaves the other's row alone.
    later = asyncio.run(index.clock())
    second_report = asyncio.run(sync_share("sharedrive-2", load_binding(second), index))
    asyncio.run(prune_share("sharedrive-2", index, later, second_report))
    assert asyncio.run(index.fingerprints(SOURCE, [path], _chunking(share)))


# --- the sweep, and its guard ---------------------------------------------------------------


def test_a_deleted_file_leaves_the_index_after_a_complete_crawl(
    share: dict[str, Any], tmp_path: Path
) -> None:
    """A citation must not survive the document it points at."""
    index = InMemoryDocumentIndex()
    binding = load_binding(share)
    started = asyncio.run(index.clock())
    asyncio.run(sync_share(SOURCE, binding, index))

    (tmp_path / "SOPs" / "handling.xlsx").unlink()
    later = asyncio.run(index.clock())
    report = asyncio.run(sync_share(SOURCE, binding, index))
    removed = asyncio.run(prune_share(SOURCE, index, later, report))

    assert removed == 1
    assert asyncio.run(index.fingerprints(SOURCE, ["SOPs/handling.xlsx"], _chunking(share))) == {}
    # Everything the crawl *did* see survives, because the pass restamped it.
    assert (
        len(
            asyncio.run(
                index.fingerprints(SOURCE, ["Projects/acme-17/notes.docx"], _chunking(share))
            )
        )
        == 1
    )
    assert started <= later


def test_a_failed_root_prunes_nothing(share: dict[str, Any], tmp_path: Path) -> None:
    """The rule the corpus depends on: an unreachable share and an empty one look identical.

    A dropped CIFS mount, a renamed root, a permission change — each presents as "these files are
    not there". Of the two possible mistakes, re-indexing is recoverable and deleting is not.
    """
    index = InMemoryDocumentIndex()
    binding = load_binding(share)
    asyncio.run(sync_share(SOURCE, binding, index))

    # The share "unmounts": a declared root disappears, so the crawl reports it as failed.
    for path in sorted((tmp_path / "SOPs").rglob("*"), reverse=True):
        path.unlink()
    (tmp_path / "SOPs").rmdir()

    later = asyncio.run(index.clock())
    report = asyncio.run(sync_share(SOURCE, binding, index))
    assert report.failed_roots == ["SOPs"]

    removed = asyncio.run(prune_share(SOURCE, index, later, report))
    assert removed == 0
    assert (
        len(asyncio.run(index.fingerprints(SOURCE, ["SOPs/handling.xlsx"], _chunking(share)))) == 1
    )


def test_a_dimension_the_column_cannot_hold_is_refused_at_construction(
    share: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dimension the column cannot hold is refused at construction.

    A share's name is deployment-chosen, so the config validator cannot enumerate it; the guard sits
    on the constructors, or pgvector would reject every chunk write hours later in a worker.
    """
    monkeypatch.setattr(settings, "embedding_dim", 3072)
    with pytest.raises(DocumentShareError, match="3072.*document_chunks.*1536"):
        ShareDocumentRetriever(binding=share, name=SOURCE, index=InMemoryDocumentIndex())


# --- the embedding configuration is part of the vector ------------------------------------------


def _use_model(monkeypatch: pytest.MonkeyPatch, model: str) -> None:
    """Point the deployment at a different embedding model, as an operator would."""
    monkeypatch.setattr(settings, "embedding_model", model)
    clear_embedding_cache()


def test_changing_the_model_re_embeds_the_corpus_without_touching_the_share(
    share: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changing the model re-embeds the corpus without touching the share.

    A file fingerprint does not move when the model does, so without an embedding key the table
    would silently mix two models' vectors. The share is deleted before the re-embed runs: the chunk
    text is stored beside its vector, so refreshing is database-to-database.
    """
    index = InMemoryDocumentIndex()
    asyncio.run(sync_share(SOURCE, load_binding(share), index))
    live = {_chunking(share)}
    before = asyncio.run(index.stale_chunks(embedding_config_key(), 100, live))
    assert before == [], "a freshly indexed corpus carries the current configuration"

    _use_model(monkeypatch, "some-better-model")
    stale = asyncio.run(index.stale_chunks(embedding_config_key(), 100, live))
    assert stale, "every stored vector is stale once the model changes"

    shutil.rmtree(tmp_path / "Projects")
    shutil.rmtree(tmp_path / "SOPs")

    report = asyncio.run(reembed_stale(index, live, limit=100))

    assert report.embedded == len(stale)
    assert not report.has_more
    assert asyncio.run(index.stale_chunks(embedding_config_key(), 100, live)) == []


def test_a_second_re_embedding_pass_does_nothing(
    share: dict[str, Any], counted_embeddings: list[int]
) -> None:
    """The pass runs at the head of every scheduled sync, so its no-op case has to be free."""
    index = InMemoryDocumentIndex()
    asyncio.run(sync_share(SOURCE, load_binding(share), index))
    spent = sum(counted_embeddings)

    report = asyncio.run(reembed_stale(index, {_chunking(share)}, limit=100))

    assert report.embedded == 0
    assert not report.has_more
    assert sum(counted_embeddings) == spent  # not one further embedding call


def test_a_bounded_re_embedding_drain_converges(
    share: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workflow loops on `has_more`, so a batch smaller than the corpus must still finish."""
    index = InMemoryDocumentIndex()
    asyncio.run(sync_share(SOURCE, load_binding(share), index))
    _use_model(monkeypatch, "another-model")
    live = {_chunking(share)}
    total = len(asyncio.run(index.stale_chunks(embedding_config_key(), 1000, live)))

    refreshed = 0
    for _ in range(50):
        report = asyncio.run(reembed_stale(index, live, limit=1))
        refreshed += report.embedded
        if not report.has_more:
            break

    assert refreshed == total
    assert asyncio.run(index.stale_chunks(embedding_config_key(), 1000, live)) == []


def test_an_upgrade_that_moves_both_keys_embeds_the_corpus_once(
    tmp_path: Path, counted_embeddings: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An upgrade that moves both keys embeds the corpus once.

    When the embedding key and the chunking both move, re-embedding the old cutting is work the
    crawl immediately throws away. The chunking is part of a row's identity, so it cannot be
    restamped during a re-embed; instead the drain skips cuttings no enabled share uses.
    """
    index = InMemoryDocumentIndex()
    share = _long_share(tmp_path, chunk_chars=400, chunk_overlap_chars=40)
    asyncio.run(sync_share(SOURCE, load_binding(share), index))

    _use_model(monkeypatch, "the-upgraded-model")
    upgraded = load_binding({**share, "chunk_chars": 20000, "chunk_overlap_chars": 200})
    counted_embeddings.clear()

    asyncio.run(reembed_stale(index, {upgraded.chunking_key}, limit=500))
    asyncio.run(sync_share(SOURCE, upgraded, index))

    assert sum(counted_embeddings) == 1, counted_embeddings
    assert (
        asyncio.run(index.stale_chunks(embedding_config_key(), 100, {upgraded.chunking_key})) == []
    )


def test_a_stale_document_is_re_embedded_even_when_its_content_is_already_known(
    share: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`known_documents` is keyed on the configuration, not merely on presence.

    Otherwise a copy of an already-indexed document arriving under a new path would inherit the
    old model's vector — one document in the corpus that nothing else is comparable to.
    """
    index = InMemoryDocumentIndex()
    binding = load_binding(share)
    asyncio.run(sync_share(SOURCE, binding, index))
    doc_ids = {
        chunk.doc_id
        for chunk in asyncio.run(index.stale_chunks("never-used", 100, {_chunking(share)}))
    }

    _use_model(monkeypatch, "third-model")
    assert (
        asyncio.run(index.known_documents(doc_ids, embedding_config_key(), _chunking(share)))
        == set()
    )

    # A new path with content already on record: it must not be treated as "already embedded".
    (tmp_path / "Projects" / "acme-17" / "copy.docx").write_bytes(
        _docx_bytes(["The palladium catalyst deactivated above 80 degrees."])
    )
    report = asyncio.run(sync_share(SOURCE, binding, index))
    assert report.deduplicated == 0, report


# --- the chunking is part of the chunk, too -----------------------------------------------------


def _chunk_sizes(index: InMemoryDocumentIndex) -> list[int]:
    """Every stored chunk's length, ordered — the shape a chunking setting decides."""
    return [len(chunk.content) for _, chunk in sorted(index._chunks.items())]


def _long_share(tmp_path: Path, **chunking: int) -> dict[str, Any]:
    """A share holding one document long enough for the chunk size to actually decide something.

    The main fixture's documents are one sentence each, so every chunking cuts them identically —
    which would make the assertions below pass against the unfixed code.
    """
    root = tmp_path / "long-share"
    (root / "SOPs").mkdir(parents=True)
    (root / "SOPs" / "protocol.txt").write_text(
        " ".join(f"Step {n}: charge the vessel and hold for {n} minutes." for n in range(120)),
        encoding="utf-8",
    )
    return {
        "mount": str(root),
        "public": True,
        "roots": [{"path": "SOPs"}],
        "extensions": [".txt"],
        **chunking,
    }


def test_changing_the_chunk_size_re_chunks_the_corpus(
    tmp_path: Path, counted_embeddings: list[int]
) -> None:
    """Changing the chunk size re-chunks the corpus.

    Neither the file's `mtime_ns:size` nor the content hash moves when `chunk_chars` does, so the
    chunking key must gate the crawl too.
    """
    index = InMemoryDocumentIndex()
    share = _long_share(tmp_path, chunk_chars=2000, chunk_overlap_chars=200)
    asyncio.run(sync_share(SOURCE, load_binding(share), index))
    before = _chunk_sizes(index)
    assert before, "sanity: the share indexed something"

    smaller = {**share, "chunk_chars": 400, "chunk_overlap_chars": 40}
    counted_embeddings.clear()
    asyncio.run(sync_share(SOURCE, load_binding(smaller), index))

    after = _chunk_sizes(index)
    assert max(after) <= 400, f"every chunk respects the new size, not {max(after)}"
    assert after != before
    assert counted_embeddings, "re-chunking means re-embedding, and it happened"

    # And the run after that is free again — the new chunking is on record, both sides of it.
    counted_embeddings.clear()
    asyncio.run(sync_share(SOURCE, load_binding(smaller), index))
    assert counted_embeddings == []
    assert _chunk_sizes(index) == after


def test_a_coarser_re_chunk_leaves_no_trace_of_the_finer_one(tmp_path: Path) -> None:
    """A coarser re-chunk leaves no trace of the finer one.

    A stranded chunk belongs to no current cutting yet would be cited and then restamped by
    `reembed_stale`, after which nothing distinguishes it. What is deleted is every cutting of the
    written documents that no file row claims, so another share's live cutting of the same `doc_id`
    survives.
    """
    index = InMemoryDocumentIndex()
    fine = _long_share(tmp_path, chunk_chars=400, chunk_overlap_chars=40)
    asyncio.run(sync_share(SOURCE, load_binding(fine), index))
    fine_count = len(_chunk_sizes(index))

    coarse = {**fine, "chunk_chars": 20000, "chunk_overlap_chars": 200}
    asyncio.run(sync_share(SOURCE, load_binding(coarse), index))
    coarse_sizes = _chunk_sizes(index)

    assert len(coarse_sizes) < fine_count, "sanity: the coarse cutting really is coarser"
    assert {row[1] for row in index._chunks} == {load_binding(coarse).chunking_key}, (
        "only the current cutting survives"
    )
    assert max(coarse_sizes) <= 20000
    assert _served_chunk_sizes(index, SOURCE) == sorted(coarse_sizes, reverse=True)


def test_a_share_indexed_under_the_previous_text_rule_is_re_read_and_re_cut(
    tmp_path: Path,
) -> None:
    """A share indexed under the previous text rule is re-read and re-cut.

    Fixing what `upsert` writes does not repair stored chunks, and the crawl skips unchanged files.
    `_CHUNK_TEXT_VERSION` in `chunking_key` forces the re-read, as `_NOTE_TEXT_VERSION` does for
    notes. Driven by aging stored rows back to the old spelling.
    """
    index = InMemoryDocumentIndex()
    share = _long_share(tmp_path, chunk_chars=400, chunk_overlap_chars=40)
    binding = load_binding(share)
    asyncio.run(sync_share(SOURCE, binding, index))

    aged = f"{binding.chunk_chars}:{binding.chunk_overlap_chars}"
    assert binding.chunking_key != aged, "the text rule is not part of the chunk's identity"
    index._files = {
        key: file.model_copy(update={"chunking_key": aged}) for key, file in index._files.items()
    }
    index._chunks = {
        (doc, aged, ordinal): chunk.model_copy(update={"chunking_key": aged})
        for (doc, _, ordinal), chunk in index._chunks.items()
    }
    index._keys = {(doc, aged, ordinal): key for (doc, _, ordinal), key in index._keys.items()}

    report = asyncio.run(sync_share(SOURCE, binding, index))

    assert report.indexed > 0, "the aged rows were adopted as current and never rewritten"
    assert {row[1] for row in index._chunks} == {binding.chunking_key}


def _served_chunk_sizes(index: InMemoryDocumentIndex, source: str) -> list[int]:
    """The length of every chunk this source's search can actually cite, longest first.

    Measured through `search_dense`, because what a share serves is the property at stake and the
    index's internal key shape is what the defect below is about.
    """
    query = embed_texts(["charge the vessel and hold"])[0]
    hits = asyncio.run(index.search_dense(source, query, 500, DocumentFilter()))
    return sorted((len(hit.content) for hit in hits), reverse=True)


def test_a_second_share_that_chunks_differently_leaves_the_first_share_intact(
    tmp_path: Path,
) -> None:
    """A second share that chunks differently leaves the first share intact.

    `doc_id` is the content hash, shared across sources, while `chunking_key` is per share; keyed on
    `(doc_id, ordinal)` alone, one share's write and tail-delete would destroy the other's chunks,
    and the victim would never repair because its own fingerprint had not moved.
    """
    index = InMemoryDocumentIndex()
    fine = _long_share(tmp_path, chunk_chars=400, chunk_overlap_chars=40)
    second_root = tmp_path / "second-share"
    (second_root / "SOPs").mkdir(parents=True)
    shutil.copy(
        Path(fine["mount"]) / "SOPs" / "protocol.txt", second_root / "SOPs" / "protocol.txt"
    )
    coarse = {**fine, "mount": str(second_root), "chunk_chars": 20000, "chunk_overlap_chars": 200}

    asyncio.run(sync_share(SOURCE, load_binding(fine), index))
    served = _served_chunk_sizes(index, SOURCE)
    assert len(served) > 1 and max(served) <= 400, served

    asyncio.run(sync_share("coarse-share", load_binding(coarse), index))

    assert len(_served_chunk_sizes(index, "coarse-share")) == 1, (
        "sanity: the second share is coarse"
    )
    assert _served_chunk_sizes(index, SOURCE) == served, "the first share kept its own cutting"

    # And it stays that way. This is the half that makes the loss permanent: the fine share's
    # fingerprint has not moved, so it never even attempts a repair.
    report = asyncio.run(sync_share(SOURCE, load_binding(fine), index))
    assert (report.unchanged, report.indexed) == (1, 0), report
    assert _served_chunk_sizes(index, SOURCE) == served


# --- retrieval ----------------------------------------------------------------------------------


def _entitled_retriever(
    share: dict[str, Any], index: InMemoryDocumentIndex
) -> ShareDocumentRetriever:
    """The retriever a member of the share's AD group would be served by."""
    return ShareDocumentRetriever(binding=share, name=SOURCE, index=index)


def test_a_hit_cites_the_file_and_the_page_it_came_from(
    share: dict[str, Any], as_user: Callable[[str, set[str]], None]
) -> None:
    """Evidence a chemist cannot check is not evidence."""
    index = InMemoryDocumentIndex()
    asyncio.run(sync_share(SOURCE, load_binding(share), index))
    retriever = _entitled_retriever(share, index)

    as_user("user-1", {"sharedrive.reader"})
    chunks = asyncio.run(retriever.retrieve("palladium catalyst deactivated", {}))

    assert chunks, "the indexed docx should be findable by its own words"
    assert all(chunk.retriever == SOURCE for chunk in chunks)
    assert any("notes.docx" in chunk.source for chunk in chunks)
    assert all(chunk.source_note_id.startswith(f"{SOURCE}:doc-") for chunk in chunks)


def test_a_pdf_hit_keeps_its_page_coordinate(
    share: dict[str, Any], as_user: Callable[[str, set[str]], None]
) -> None:
    """`[page 3]` has to survive parsing, chunking, indexing and retrieval to be worth carrying."""
    index = InMemoryDocumentIndex()
    asyncio.run(sync_share(SOURCE, load_binding(share), index))
    retriever = _entitled_retriever(share, index)

    as_user("user-1", {"sharedrive.reader"})
    chunks = asyncio.run(retriever.retrieve("impurity below percent", {}))

    assert any("[page " in chunk.source for chunk in chunks)


def test_a_caller_outside_the_group_gets_nothing(
    share: dict[str, Any], as_user: Callable[[str, set[str]], None]
) -> None:
    """Getting onto the share is the AD group's decision, and this is where it is honoured."""
    index = InMemoryDocumentIndex()
    asyncio.run(sync_share(SOURCE, load_binding(share), index))
    retriever = _entitled_retriever(share, index)

    as_user("user-2", {"some.other.role"})
    with pytest.raises(RetrieverSkip, match="entitled actor"):
        asyncio.run(retriever.retrieve("palladium catalyst", {}))


def test_a_gated_share_refuses_when_there_is_no_identity_to_check(share: dict[str, Any]) -> None:
    """`require_actor`'s reject-if-absent rule, applied to a corpus instead of a tool.

    This is why the report workflow — which runs with no ambient identity — sees nothing from a
    gated share. Correct by construction, and stated rather than discovered.
    """
    index = InMemoryDocumentIndex()
    asyncio.run(sync_share(SOURCE, load_binding(share), index))
    retriever = _entitled_retriever(share, index)

    with pytest.raises(RetrieverSkip, match="entitled actor"):
        asyncio.run(retriever.retrieve("palladium catalyst", {}))


def test_an_ungated_share_needs_no_identity(share: dict[str, Any]) -> None:
    """Demanding an actor to check an empty requirement would block reports for no benefit."""
    index = InMemoryDocumentIndex()
    asyncio.run(sync_share(SOURCE, load_binding(share), index))
    ungated = {**share, "required_roles": [], "public": True}
    retriever = ShareDocumentRetriever(binding=ungated, name=SOURCE, index=index)

    assert asyncio.run(retriever.retrieve("palladium catalyst", {})) != []


def test_a_backend_failure_raises_so_the_sweep_can_report_it(
    share: dict[str, Any], as_user: Callable[[str, set[str]], None]
) -> None:
    """A backend failure raises, so the sweep can report it.

    An unreachable index is a fact about the deployment, not "this share holds nothing". `_sweep`
    catches it, degrades this branch and names the source in `failed`.
    """

    class Broken(InMemoryDocumentIndex):
        async def search_dense(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("index unreachable")

    retriever = ShareDocumentRetriever(binding=share, name=SOURCE, index=Broken())
    as_user("user-1", {"sharedrive.reader"})
    with pytest.raises(ConnectionError):
        asyncio.run(retriever.retrieve("anything", {}))

    ranked, failed, _skipped = asyncio.run(sweep_sources([(SOURCE, retriever)], "anything", {}))
    assert ranked == [[]] and failed == [SOURCE], (
        "the branch must report a failure, not the zero-chunk answer of a healthy quiet source"
    )


def test_an_embedding_provider_failure_costs_this_leg_and_no_other(
    share: dict[str, Any], as_user: Callable[[str, set[str]], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An embedding provider failure costs this leg and no other.

    The query is embedded inside this leg, so any provider exception is this leg's. The sweep's
    branch catches everything; what is pinned is that the leg is *reported*, not silently emptied.
    """

    class _ProviderError(Exception):
        """Stands in for a vendor client's own error type, which no handler here enumerates."""

    def _refusing(texts: list[str], **kwargs: object) -> list[list[float]]:
        raise _ProviderError("429 rate limited")

    index = InMemoryDocumentIndex()
    asyncio.run(sync_share(SOURCE, load_binding(share), index))
    monkeypatch.setattr(retriever_module, "embed_texts", _refusing)
    as_user("user-1", {"sharedrive.reader"})

    retriever = _entitled_retriever(share, index)
    with pytest.raises(_ProviderError):
        asyncio.run(retriever.retrieve("yield", {}))

    ranked, failed, _skipped = asyncio.run(sweep_sources([(SOURCE, retriever)], "yield", {}))
    assert ranked == [[]] and failed == [SOURCE]


def test_a_note_type_filter_returns_nothing_rather_than_ignoring_it(
    share: dict[str, Any], as_user: Callable[[str, set[str]], None]
) -> None:
    """A file on a share has no knowledge-graph note type; answering anyway would ignore the ask."""
    index = InMemoryDocumentIndex()
    asyncio.run(sync_share(SOURCE, load_binding(share), index))
    retriever = _entitled_retriever(share, index)

    as_user("user-1", {"sharedrive.reader"})
    with pytest.raises(RetrieverSkip, match="note-type filter"):
        asyncio.run(retriever.retrieve("palladium", {"type": "reaction"}))


def test_a_tag_filter_scopes_to_one_project(
    share: dict[str, Any], as_user: Callable[[str, set[str]], None]
) -> None:
    """The project code lifted out of the path is what makes "in ACME-17" answerable."""
    index = InMemoryDocumentIndex()
    asyncio.run(sync_share(SOURCE, load_binding(share), index))
    retriever = _entitled_retriever(share, index)

    as_user("user-1", {"sharedrive.reader"})
    scoped = asyncio.run(retriever.retrieve("catalyst deactivated toluene", {"tag": "acme-17"}))
    assert scoped
    assert all("SOPs/" not in chunk.source for chunk in scoped)


def test_a_date_window_excludes_a_file_modified_outside_it(
    share: dict[str, Any], as_user: Callable[[str, set[str]], None]
) -> None:
    """`until` windows on whole days, so it must not drop everything touched after midnight."""
    index = InMemoryDocumentIndex()
    asyncio.run(sync_share(SOURCE, load_binding(share), index))
    retriever = _entitled_retriever(share, index)

    as_user("user-1", {"sharedrive.reader"})
    today = datetime.now(UTC).date()
    assert asyncio.run(retriever.retrieve("palladium catalyst", {"until": today})) != []
    stale = today - timedelta(days=365)
    assert asyncio.run(retriever.retrieve("palladium catalyst", {"until": stale})) == []


# --- the sweep only deletes what a complete pass really did not see ------------------------------
#
# These ask the crawl itself whether sweeping is safe, rather than handing `prune_share` a
# `crawl_was_complete` decided by the test. Each case is a file present and readable on the share
# that must not be deleted from the index.


def _drain(binding: Any, index: InMemoryDocumentIndex, limit: int = 1000) -> Any:
    """Drain a share the way the durable workflow does, so the sweep sees the real evidence."""
    reports = []
    after = ""
    for _ in range(200):
        report = asyncio.run(sync_share(SOURCE, binding, index, after=after, limit=limit))
        reports.append(report)
        if not report.has_more or report.cursor <= after:
            break
        after = report.cursor
    return sync_module.merge_reports(reports, SOURCE)


def test_a_directory_that_prefixes_a_sibling_file_does_not_hide_it(tmp_path: Path) -> None:
    """A directory that prefixes a sibling file does not hide it.

    The walk's order must match how `after` compares: `Report` (a directory) sorts before
    `Report.pdf` by name, but `Report.pdf` sorts before `Report/a.pdf` as a path, so a chunk
    stopping inside the directory would skip the file forever.
    """
    mount = tmp_path / "mount"
    (mount / "Docs" / "Report").mkdir(parents=True)
    (mount / "Docs" / "Report" / "a.txt").write_text("inner one")
    (mount / "Docs" / "Report" / "b.txt").write_text("inner two")
    (mount / "Docs" / "Report.txt").write_text("the sibling report")
    binding = load_binding({"mount": str(mount), "roots": [{"path": "Docs"}], "public": True})

    whole = {ref.path for ref in crawl_share(binding, limit=1000).files}
    assert "Docs/Report.txt" in whole

    seen: set[str] = set()
    after = ""
    for _ in range(20):
        chunk = crawl_share(binding, after=after, limit=1)
        seen |= {ref.path for ref in chunk.files}
        if not chunk.has_more:
            break
        after = chunk.cursor
    assert seen == whole


def test_sibling_roots_that_share_a_prefix_are_both_walked(tmp_path: Path) -> None:
    """`Data` and `Data-Archive` pass every binding check, and `-` sorts below `/`."""
    mount = tmp_path / "mount"
    (mount / "Data").mkdir(parents=True)
    (mount / "Data-Archive").mkdir(parents=True)
    (mount / "Data" / "z.txt").write_text("live data")
    (mount / "Data-Archive" / "old.txt").write_text("archived data")
    binding = load_binding(
        {
            "mount": str(mount),
            "roots": [{"path": "Data"}, {"path": "Data-Archive"}],
            "public": True,
        }
    )

    whole = {ref.path for ref in crawl_share(binding, limit=1000).files}
    seen: set[str] = set()
    after = ""
    for _ in range(20):
        chunk = crawl_share(binding, after=after, limit=1)
        seen |= {ref.path for ref in chunk.files}
        if not chunk.has_more:
            break
        after = chunk.cursor
    assert seen == whole == {"Data/z.txt", "Data-Archive/old.txt"}


def test_a_share_that_went_empty_is_not_swept(share: dict[str, Any], tmp_path: Path) -> None:
    """A share that went empty is not swept.

    A detached CIFS volume leaves an empty mount point, and with `roots: [{path: "."}]` there is no
    missing root to report; `crawl_share` is loud only when the mount path itself is gone.
    """
    index = InMemoryDocumentIndex()
    binding = load_binding({**share, "roots": [{"path": "."}]})
    _drain(binding, index)
    assert asyncio.run(index.fingerprints(SOURCE, ["SOPs/handling.xlsx"], _chunking(share)))

    for path in sorted(tmp_path.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()

    later = asyncio.run(index.clock())
    report = _drain(binding, index)
    assert report.scanned == 0 and not report.failed_roots
    assert asyncio.run(prune_share(SOURCE, index, later, report)) == 0
    assert asyncio.run(index.fingerprints(SOURCE, ["SOPs/handling.xlsx"], _chunking(share)))


def test_a_file_that_cannot_be_stat_ed_keeps_its_row(
    share: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ACL push or a DFS flap makes `stat` fail on files that are still there."""
    index = InMemoryDocumentIndex()
    binding = load_binding(share)
    _drain(binding, index)

    import os as os_module

    real = os_module.DirEntry.stat
    victim = "handling.xlsx"

    def failing(self: Any, **kwargs: Any) -> Any:
        if self.name == victim:
            raise PermissionError("cifs: permission denied")
        return real(self, **kwargs)

    monkeypatch.setattr(os_module.DirEntry, "stat", failing, raising=False)

    later = asyncio.run(index.clock())
    report = _drain(binding, index)
    asyncio.run(prune_share(SOURCE, index, later, report))
    assert asyncio.run(index.fingerprints(SOURCE, ["SOPs/handling.xlsx"], _chunking(share)))


def test_a_file_that_changed_and_cannot_be_read_keeps_its_row(
    share: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A document open in Word: its mtime moved, and the read fails. It is still on the share."""
    index = InMemoryDocumentIndex()
    binding = load_binding(share)
    _drain(binding, index)

    target = tmp_path / "Projects" / "acme-17" / "notes.docx"
    target.write_bytes(target.read_bytes() + b"\x00")  # fingerprint moves

    import os as os_module

    real_open = os_module.open

    def failing(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if str(path).endswith("notes.docx"):
            raise PermissionError("sharing violation")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os_module, "open", failing)

    later = asyncio.run(index.clock())
    report = _drain(binding, index)
    assert report.skipped_unreadable == 1
    asyncio.run(prune_share(SOURCE, index, later, report))
    assert asyncio.run(
        index.fingerprints(SOURCE, ["Projects/acme-17/notes.docx"], _chunking(share))
    )


def test_a_drain_that_never_finished_sweeps_nothing(share: dict[str, Any]) -> None:
    """`--limit 0` scans nothing and reports more to come. It must not be read as "all gone"."""
    index = InMemoryDocumentIndex()
    binding = load_binding(share)
    _drain(binding, index)

    later = asyncio.run(index.clock())
    stalled = asyncio.run(sync_share(SOURCE, binding, index, after="", limit=0))
    assert stalled.has_more and stalled.scanned == 0
    assert asyncio.run(prune_share(SOURCE, index, later, stalled)) == 0
    assert asyncio.run(index.fingerprints(SOURCE, ["SOPs/handling.xlsx"], _chunking(share)))


def test_compaction_carries_the_sweep_guard_across_continue_as_new() -> None:
    """Compaction carries the sweep guard across `continue_as_new`.

    `DocumentShareSyncWorkflow` folds chunk reports with `_merge_by_source` before each
    `continue_as_new`; dropping a failed root or the unfinished tail there would let the next run
    sweep a share it never finished walking.
    """
    from chemclaw.durable.document_sync import _merge_by_source

    chunks = [
        SyncReport(source="a", scanned=5, cursor="p1", has_more=True),
        SyncReport(source="a", scanned=5, failed_roots=["SOPs"], cursor="p2", has_more=True),
        SyncReport(source="a", scanned=2, cursor="p3", has_more=False),
        SyncReport(source="b", scanned=3, cursor="q1", has_more=False),
    ]
    compacted = {report.source: report for report in _merge_by_source(chunks)}

    assert compacted["a"].failed_roots == ["SOPs"]
    assert compacted["a"].scanned == 12
    # Folding again must be a fixed point — it happens once per continue_as_new, not once per drain.
    twice = {r.source: r for r in _merge_by_source(list(compacted.values()))}
    assert twice["a"].failed_roots == ["SOPs"] and twice["a"].scanned == 12

    index = InMemoryDocumentIndex()
    started = asyncio.run(index.clock())
    assert asyncio.run(prune_share("a", index, started, compacted["a"])) == 0
    # Share "b" finished cleanly, so its sweep is allowed — it just has nothing to remove.
    assert asyncio.run(prune_share("b", index, started, compacted["b"])) == 0


def test_the_continue_as_new_bound_is_carried_in_state_not_read_live() -> None:
    """The `continue_as_new` bound is carried in state, not read live.

    `document_sync_max_iterations` decides how many commands a run schedules, so read live a
    redeploy that changes it would replay non-deterministically and wedge the run
    (`D-2026-08-08-an-outage-is-not-a-missing-job`). No Temporal here, so this asserts the
    structure: captured once in the activity, carried on the plan and the state, and never read live
    in the workflow body (an AST check).
    """
    import ast
    import inspect
    import textwrap

    from chemclaw.durable import document_sync
    from chemclaw.durable.document_sync import DocumentSyncPlan, DocumentSyncState

    assert "max_iterations" in DocumentSyncPlan.model_fields
    assert "max_iterations" in DocumentSyncState.model_fields

    # The activity is where a live read belongs: it runs once per drain and its result is recorded.
    plan_src = inspect.getsource(document_sync.plan_document_sync)
    assert "settings.document_sync_max_iterations" in plan_src

    run_src = textwrap.dedent(inspect.getsource(document_sync.DocumentShareSyncWorkflow.run))
    tree = ast.parse(run_src)
    live_reads = [
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "settings"
    ]
    assert "document_sync_max_iterations" not in live_reads, (
        "the continue-as-new bound must come from `state.max_iterations`, captured in the plan "
        f"activity; the workflow body still reads settings for: {sorted(set(live_reads))}"
    )


def test_a_wedged_drain_leaves_has_more_set_for_the_guard() -> None:
    """A pass that reports more work but no cursor advance must not merge into "finished"."""
    from chemclaw.durable.document_sync import _merge_by_source

    wedged = [
        SyncReport(source="a", scanned=4, cursor="p1", has_more=True),
        SyncReport(source="a", scanned=4, cursor="p1", has_more=True),
    ]
    assert _merge_by_source(wedged)[0].has_more is True


# --- one bad thing must not stop everything -----------------------------------------------------


def test_a_statement_timeout_degrades_this_leg_and_leaves_the_others_answering(
    share: dict[str, Any],
) -> None:
    """A statement timeout degrades this leg and leaves the others answering.

    `psycopg.Error` is not an `OSError`; wrapping it in `DocumentIndexError` at the raiser gives it
    a nameable type, and the sweep reports the share in `failed` while the graph leg still answers.
    """
    import psycopg

    class Exploding(InMemoryDocumentIndex):
        async def search_dense(self, *args: Any, **kwargs: Any) -> Any:
            raise DocumentIndexError("search failed: statement timeout") from psycopg.Error()

    class _Graph:
        """The healthy leg beside it, which must keep its hits."""

        name = "graph"

        async def retrieve(self, _q: str, _f: dict[str, Any]) -> list[EvidenceChunk]:
            return [
                EvidenceChunk(
                    content="Pd/C 5% in ethanol", source_note_id="rxn-1", retriever="graph"
                )
            ]

    retriever = ShareDocumentRetriever(
        {**share, "required_roles": [], "public": True}, name=SOURCE, index=Exploding()
    )
    ranked, failed, _skipped = asyncio.run(
        sweep_sources([("graph", _Graph()), (SOURCE, retriever)], "catalyst", {})
    )

    assert [len(chunks) for chunks in ranked] == [1, 0]
    assert failed == [SOURCE]


def test_a_backend_failure_is_reported_without_the_driver_s_connection_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backend failure is reported without the driver's connection details.

    `api/middleware._subsystem_unavailable` relays a `SubsystemUnavailableError` message verbatim,
    relying on it carrying no host, port or driver text (those live on `__cause__`). Asserted at the
    raiser, since the promise belongs to the exception wherever it travels.
    """
    import psycopg

    leak = psycopg.OperationalError(
        'connection to server at "chemclaw-pg.internal" (10.4.2.7), port 5432 failed: timeout'
    )

    def exploding(self: PostgresDocumentIndex) -> Any:
        raise leak

    monkeypatch.setattr(PostgresDocumentIndex, "_connection", exploding)
    with pytest.raises(DocumentIndexError) as caught:
        asyncio.run(PostgresDocumentIndex().search_lexical(SOURCE, "catalyst", 5, DocumentFilter()))

    message = str(caught.value)
    for secret in ("chemclaw-pg.internal", "10.4.2.7", "5432"):
        assert secret not in message, f"{secret!r} reached the message a 503 body relays"
    # The detail is not lost — it is where the contract says it is, for the log.
    assert caught.value.__cause__ is leak


def test_one_unembeddable_chunk_does_not_starve_the_corpus(
    share: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`stale_chunks` is deterministic, and this drain runs *ahead* of the crawl.

    So a chunk the provider refuses failed the activity identically on every retry and stopped all
    document indexing, for every share, permanently. The rest of the batch must still be refreshed.
    """
    index = InMemoryDocumentIndex()
    asyncio.run(sync_share(SOURCE, load_binding(share), index))

    live = {_chunking(share)}
    stale = asyncio.run(index.stale_chunks("some-other-key", 500, live))
    assert len(stale) > 1
    poison = stale[0].content
    real = embed_texts

    def refusing(texts: list[str], **kwargs: object) -> Any:
        if poison in texts:
            raise ValueError("content refused by the provider")
        return real(texts)

    monkeypatch.setattr(sync_module, "embed_texts", refusing)
    monkeypatch.setattr(sync_module, "embedding_config_key", lambda: "some-other-key", raising=True)

    report = asyncio.run(reembed_stale(index, live, 500))
    assert report.failed == 1
    assert report.embedded == len(stale) - 1  # everything else was refreshed
    # And the drain terminates rather than returning the identical batch forever.
    assert report.has_more is False


# --- the mount is a boundary, and the entitlement is not optional --------------------------------


def test_a_root_that_is_itself_a_symlink_does_not_escape_the_mount(tmp_path: Path) -> None:
    """A root that is itself a symlink does not escape the mount.

    The per-entry guard covers everything inside a root, not the root: `Projects -> /` would index
    the container filesystem under mount-looking paths. `follow_symlinks: false` only skips symlink
    *entries*, so the root needs its own check.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "payroll.txt").write_text("salaries")
    mount = tmp_path / "mount"
    mount.mkdir()
    (mount / "Projects").symlink_to(outside)

    binding = load_binding({"mount": str(mount), "roots": [{"path": "Projects"}], "public": True})
    result = crawl_share(binding)
    assert result.files == []
    # And it is reported, not silently empty — an escape and an empty root are not the same thing.
    assert result.failed_roots == ["Projects"]


def test_a_manifest_that_forgets_its_entitlement_is_refused_at_load() -> None:
    """A manifest that forgets its entitlement is refused at load.

    Manifests are hand-authored, and omitting `required_roles` must not serve an AD-gated share to
    every authenticated user.
    """
    with pytest.raises(DocumentShareError, match="required_roles"):
        load_binding({"mount": "/mnt/x", "roots": [{"path": "."}]})


def test_a_share_everyone_may_read_says_so_out_loud() -> None:
    """The opt-out exists, because some shares genuinely are open to every account holder."""
    binding = load_binding({"mount": "/mnt/x", "roots": [{"path": "."}], "public": True})
    assert binding.required_role_set == set()


def test_public_and_required_roles_together_are_refused() -> None:
    """One says "anyone", the other says "only these". A manifest must not claim both."""
    with pytest.raises(DocumentShareError, match="public"):
        load_binding(
            {"mount": "/mnt/x", "roots": [{"path": "."}], "public": True, "required_roles": ["r"]}
        )


def test_a_group_gated_share_answers_for_the_prefixed_claim_and_not_the_bare_one() -> None:
    """A group-gated share answers for the prefixed claim and not the bare one.

    `api.auth` prefixes every group claim with `GROUP_ROLE_PREFIX` (so a directory group cannot pose
    as an app role), so a binding naming the bare object-id matches nothing, and a declining
    retriever fails silently by returning no evidence.
    """
    group = "11111111-2222-3333-4444-555555555555"
    claimed = f"{GROUP_ROLE_PREFIX}{group}"

    def _share_gated_on(entitlement: str) -> ShareDocumentRetriever:
        return ShareDocumentRetriever(
            {"mount": "/mnt/x", "roots": [{"path": "."}], "required_roles": [entitlement]},
            name=SOURCE,
            index=InMemoryDocumentIndex(),
        )

    prefixed = _share_gated_on(claimed)
    bare = _share_gated_on(group)
    tokens = set_current_identity("chemist", frozenset({claimed}))
    try:
        assert prefixed._entitled() is True
        assert bare._entitled() is False
    finally:
        reset_current_identity(tokens)


def test_every_place_that_teaches_a_group_gate_names_the_real_prefix() -> None:
    """Every place that teaches a group gate names the real prefix.

    The manifest, the README, the retriever docstring and the operator guide must all name `group:`;
    following a wrong copy configures a gate that silently matches nothing. The string is a
    constant, and the check is that each document contains it.
    """
    root = Path(__file__).resolve().parent.parent
    teaches_the_gate = [
        root / "src/chemclaw/ingest/sources/sharedrive/datasource.yaml",
        root / "src/chemclaw/ingest/documents/README.md",
        root / "src/chemclaw/ingest/documents/retriever.py",
        root / "src/chemclaw/ingest/documents/binding.py",
        root / "src/chemclaw/core/config/entra.py",
        root / "docs/guides/sharedrive-concept.md",
    ]
    silent = [
        path.relative_to(root).as_posix()
        for path in teaches_the_gate
        if GROUP_ROLE_PREFIX not in path.read_text(encoding="utf-8")
    ]
    assert not silent, (
        f"these teach a group-gated entitlement without naming {GROUP_ROLE_PREFIX!r}: {silent}. "
        "An operator following them writes the bare claim value, which matches nothing and fails "
        "silently"
    )

    # And the refusal an operator actually hits carries it, rather than sending them to a guide.
    with pytest.raises(DocumentShareError) as refusal:
        load_binding({"mount": "/mnt/x", "roots": [{"path": "."}]})
    assert GROUP_ROLE_PREFIX in str(refusal.value)


def test_an_identical_vector_scores_one_and_does_not_raise() -> None:
    """A chemist pasting a sentence back is an exact match, and it must be answerable.

    Cosine of a vector with itself rounds above 1.0 for about half of all normalised vectors — two
    square roots in the denominator — and `DocumentHit.score` is bounded `le=1.0`.
    """
    import math
    import random

    from chemclaw.ingest.documents.index import _cosine

    random.seed(11)
    worst = 0.0
    for _ in range(2000):
        vector = [random.gauss(0, 1) for _ in range(64)]
        norm = math.sqrt(sum(x * x for x in vector))
        vector = [x / norm for x in vector]
        worst = max(worst, _cosine(vector, vector))
    assert worst <= 1.0


def test_a_bracketed_line_of_prose_cannot_forge_a_citation_coordinate() -> None:
    """`[Figure 2: …]` is a caption, not a page label. It must not become the chunk's coordinate.

    Two failures in one: the citation named a location the chunk did not come from, and the caption
    text was stripped out of the indexed body, so it stopped being searchable.
    """
    text = (
        "[page 1]\nIntro text here\n\n"
        "[Figure 2: yield vs time]\nThe figure shows a plateau.\n\n"
        "[page 2]\nReal page two"
    )
    chunks = chunk_document(text, chunk_chars=400, overlap_chars=50)
    coordinates = {chunk.coordinate for chunk in chunks}
    assert coordinates == {"page 1", "page 2"}
    assert "Figure 2: yield vs time" in "\n".join(chunk.content for chunk in chunks)


def test_a_file_swapped_for_a_symlink_after_the_crawl_is_not_followed(
    share: dict[str, Any], tmp_path: Path
) -> None:
    """A file swapped for a symlink after the crawl is not followed.

    The crawl and the read are separate activities on a writable share, so the read must re-check
    the `FileRef` it was given; `follow_symlinks: false` is consulted only at crawl time.
    """
    binding = load_binding(share)
    secret = tmp_path.parent / "token"
    secret.write_text("workload-identity-assertion")

    target = tmp_path / "SOPs" / "swapped.txt"
    target.write_text("ordinary content")
    ref = next(r for r in crawl_share(binding).files if r.path == "SOPs/swapped.txt")

    target.unlink()
    target.symlink_to(secret)
    with pytest.raises(OSError):
        sync_module._read_and_parse(ref, binding.max_file_bytes)


def test_a_file_that_grew_past_the_limit_after_the_crawl_is_refused(
    share: dict[str, Any], tmp_path: Path
) -> None:
    """`max_file_bytes` was enforced against a stat taken in another activity, minutes earlier.

    A 1 KB `.csv` accepted by the crawl and grown to 20 GB before the read used to be pulled into
    the worker's memory whole. The size is re-read from the open descriptor instead.
    """
    binding = load_binding({**share, "max_file_bytes": 20_000})
    target = tmp_path / "SOPs" / "grower.txt"
    target.write_text("small")
    ref = next(r for r in crawl_share(binding).files if r.path == "SOPs/grower.txt")

    target.write_text("x" * 50_000)
    with pytest.raises(DocumentParseError, match="at read time"):
        sync_module._read_and_parse(ref, binding.max_file_bytes)


def test_a_top_level_archive_is_excluded_by_the_pattern_that_says_so(tmp_path: Path) -> None:
    """A top-level archive is excluded by the pattern that says so.

    `fnmatch` gives `**` no meaning, so `**/Archive/**` would require a separator before `Archive`
    and miss a top-level `Archive/` under `roots: [{path: "."}]`.
    """
    mount = tmp_path / "mount"
    (mount / "Archive").mkdir(parents=True)
    (mount / "Projects" / "Archive").mkdir(parents=True)
    (mount / "Archive" / "ancient.txt").write_text("decade-old")
    (mount / "Projects" / "Archive" / "old.txt").write_text("also old")
    (mount / "Projects" / "live.txt").write_text("current")

    binding = load_binding(
        {
            "mount": str(mount),
            "roots": [{"path": "."}],
            "public": True,
            "exclude": ["**/Archive/**"],
        }
    )
    assert {ref.path for ref in crawl_share(binding).files} == {"Projects/live.txt"}


def test_the_shipped_exclusions_mean_the_same_under_gitignore_semantics() -> None:
    """The shipped exclusions mean the same under gitignore semantics.

    Gitignore matching replaced three `fnmatch` arms (bare, `/`-anchored, basename) and is not
    identical (no basename matching for patterns with a separator), so both policies are run over
    the shipped patterns. The old policy is transcribed here, since comparing against the
    replacement would agree with itself. The paths separate the semantics by depth, anchoring,
    basename position and a near-miss (`Archived/`) that must stay indexed.
    """
    manifest = (
        Path(__file__).resolve().parent.parent
        / "src/chemclaw/ingest/sources/sharedrive/datasource.yaml"
    )
    patterns = yaml.safe_load(manifest.read_text(encoding="utf-8"))["config"]["binding"]["exclude"]
    # Read off the shipped manifest rather than typed here, so a deployment-shaped pattern added to
    # it lands in this comparison instead of beside it. Pinned as well, because a pattern that
    # arrives *after* this measurement has not been measured by it.
    assert patterns == ["~$*", "**/Archive/**", "*.tmp"], "the shipped patterns moved; re-measure"

    def by_fnmatch(relative: str) -> bool:
        name = relative.rsplit("/", 1)[-1]
        return any(
            fnmatch.fnmatch(relative, pattern)
            or fnmatch.fnmatch(f"/{relative}", pattern)
            or fnmatch.fnmatch(name, pattern)
            for pattern in patterns
        )

    spec = pathspec.GitIgnoreSpec.from_lines(patterns)
    files = [
        "Projects/acme-17/2024/report.pdf",
        "Projects/acme-17/~$notes.docx",
        "~$toplevel.docx",
        "Projects/acme-17/notes~$.docx",
        "Archive/ancient.pdf",
        "Archive/1998/ancient.pdf",
        "Projects/Archive/old.pdf",
        "Projects/acme-17/Archive/deep/old.pdf",
        "Projects/Archived/keep.pdf",
        "Projects/scratch.tmp",
        "scratch.tmp",
        "SOPs/handling.xlsx",
        "Projects/tmp",
    ]
    diverged = [path for path in files if by_fnmatch(path) != spec.match_file(path)]
    assert not diverged, f"gitignore semantics change what these files do: {diverged}"

    # As predicates the policies do differ (gitignore excludes everything under a matched
    # directory); the old walk reached the same corpus because the basename arm fired on the
    # directory itself.
    assert not by_fnmatch("scratch.tmp/report.pdf")
    assert spec.match_file("scratch.tmp/report.pdf")
    assert by_fnmatch("scratch.tmp")

    # And the half that *is* a change, stated as one: no directory matched under the old policy in
    # any of its three arms, which is why `descend` walked every excluded folder in full.
    directories = ["Archive", "Projects/Archive", "Projects/acme-17/Archive"]
    assert not any(by_fnmatch(path) for path in directories)
    assert all(spec.match_file(f"{path}/") for path in directories)
    assert not spec.match_file("Projects/acme-17/")


def test_an_excluded_directory_is_never_listed_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An excluded directory is never listed at all.

    Counted `scandir` calls, because the files were excluded either way; the saving is not opening
    the excluded subtree, and the `scandir` pass is the share's cost model.
    """
    mount = tmp_path / "mount"
    (mount / "Archive" / "1998" / "q1").mkdir(parents=True)
    (mount / "Projects").mkdir(parents=True)
    (mount / "Archive" / "1998" / "q1" / "old.txt").write_text("decade-old")
    (mount / "Projects" / "live.txt").write_text("current")

    listed: list[str] = []
    real_scandir = os.scandir

    def recording(path: Any) -> Any:
        listed.append(str(path))
        return real_scandir(path)

    # Patched by dotted path rather than through an imported module object: `crawl` does not
    # re-export `os`, so mypy refuses the attribute form under `make type`. Same object.
    monkeypatch.setattr("chemclaw.ingest.documents.crawl.os.scandir", recording)

    binding = load_binding(
        {
            "mount": str(mount),
            "roots": [{"path": "."}],
            "public": True,
            "exclude": ["**/Archive/**"],
        }
    )
    assert {ref.path for ref in crawl_share(binding).files} == {"Projects/live.txt"}
    assert not any("Archive" in entry for entry in listed), listed


def test_a_pattern_gitignore_cannot_parse_is_refused_at_load_not_mid_crawl() -> None:
    """A pattern gitignore cannot parse is refused at load, not mid-crawl.

    At first use it would be a library exception inside a crawl chunk, outside the non-retryable
    `DocumentShareError` family, so the sync would retry forever and index nothing.
    """
    with pytest.raises(DocumentShareError) as refusal:
        load_binding(
            {"mount": "/mnt/x", "roots": [{"path": "."}], "public": True, "exclude": ["!"]}
        )
    assert "gitignore pattern" in str(refusal.value)


def test_a_utf16_document_on_the_share_is_indexed_instead_of_counted_unreadable(
    tmp_path: Path,
) -> None:
    """A UTF-16 document on the share is indexed instead of counted unreadable.

    Under the single-encoding decode its NULs survived and the NUL guard filed it under
    `skipped_unreadable` with a Postgres reason that said nothing about the encoding.
    """
    mount = tmp_path / "mount"
    (mount / "SOPs").mkdir(parents=True)
    text = "Handling: hold the reactor at 60 °C for two hours before sampling.\n"
    (mount / "SOPs" / "handling.txt").write_bytes(text.encode("utf-16"))

    binding = load_binding(
        {
            "mount": str(mount),
            "roots": [{"path": "SOPs"}],
            "public": True,
            "extensions": [".txt"],
        }
    )
    index = InMemoryDocumentIndex()
    report = _drain(binding, index)
    assert report.skipped_unreadable == 0, report
    assert report.indexed == 1, report

    hits = asyncio.run(index.search_lexical(SOURCE, "reactor sampling", 5, DocumentFilter()))
    assert hits, "the document should be retrievable"
    assert "60 °C" in hits[0].content
    assert "\x00" not in hits[0].content


# --- the durable backend, against the real database ---------------------------------------------


async def _stored_cuttings() -> list[tuple[str, int]]:
    """Every `(chunking_key, ordinal)` `document_chunks` holds for `doc-1`, in key order."""
    async with await connect(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT chunking_key, ordinal FROM document_chunks WHERE doc_id = %s "
                "ORDER BY chunking_key, ordinal",
                ("doc-1",),
            )
            return [(row[0], row[1]) for row in await cur.fetchall()]


async def test_the_postgres_backend_gates_on_the_chunking_and_sweeps_only_unclaimed_cuttings() -> (
    None
):
    """The Postgres backend applies the same rules as the in-memory reference, in SQL.

    Round-trips the re-chunk gates and both halves of what a re-chunk may delete: a cutting no file
    row claims goes, and a cutting another share still claims stays (`doc_id` is content, shared
    across shares).
    """
    await migrated_db_or_skip()
    async with await connect(settings.postgres_dsn) as conn:
        await conn.execute("TRUNCATE document_files, document_chunks")
        await conn.commit()

    index = PostgresDocumentIndex()
    key = embedding_config_key()
    (vector,) = await asyncio.to_thread(embed_texts, ["a chunk of a protocol"])
    fine_file = FileRecord(
        path="SOPs/protocol.txt",
        source=SOURCE,
        doc_id="doc-1",
        fingerprint="1:2",
        chunking_key="400:40",
    )
    fine = [
        ChunkRecord(
            doc_id="doc-1",
            chunking_key="400:40",
            ordinal=n,
            content=f"piece {n} of a protocol",
            embedding=vector,
        )
        for n in range(3)
    ]
    await index.upsert([fine_file], fine, key)

    assert await index.fingerprints(SOURCE, [fine_file.path], "400:40") == {fine_file.path: "1:2"}
    assert await index.fingerprints(SOURCE, [fine_file.path], "2000:200") == {}
    assert await index.known_documents({"doc-1"}, key, "400:40") == {"doc-1"}
    assert await index.known_documents({"doc-1"}, key, "2000:200") == set()

    # A second share holding the same content and cutting it coarsely. Its write must not touch
    # the first share's rows — this is the destruction 041 closes.
    coarse_file = fine_file.model_copy(
        update={"source": "sharedrive-2", "chunking_key": "2000:200"}
    )
    coarse = [
        ChunkRecord(
            doc_id="doc-1",
            chunking_key="2000:200",
            ordinal=0,
            content="all of the protocol at once",
            embedding=vector,
        )
    ]
    await index.upsert([coarse_file], coarse, key)
    assert await _stored_cuttings() == [
        ("2000:200", 0),
        ("400:40", 0),
        ("400:40", 1),
        ("400:40", 2),
    ]
    assert await index.known_documents({"doc-1"}, key, "400:40") == {"doc-1"}

    # And each share searches its own cutting, never the other's.
    hits = await index.search_dense(SOURCE, vector, 10, DocumentFilter())
    assert {hit.ordinal for hit in hits} == {0, 1, 2}
    assert [
        hit.content
        for hit in await index.search_dense("sharedrive-2", vector, 10, DocumentFilter())
    ] == ["all of the protocol at once"]

    # Now the first share is re-chunked coarsely too. Nothing claims 400:40 any more: it goes.
    await index.upsert([fine_file.model_copy(update={"chunking_key": "2000:200"})], coarse, key)
    assert await _stored_cuttings() == [("2000:200", 0)], "the superseded cutting was swept"


async def _stored_keys(doc_id: str) -> list[tuple[str, int, str]]:
    """Every `(chunking_key, ordinal, embedding_key)` the catalogue holds for one document."""
    async with await connect(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT chunking_key, ordinal, embedding_key FROM document_chunks "
                "WHERE doc_id = %s ORDER BY chunking_key, ordinal",
                (doc_id,),
            )
            return [(row[0], row[1], row[2]) for row in await cur.fetchall()]


async def test_the_external_store_backend_carries_the_chunking_through_every_write() -> None:
    """The external-store backend carries the chunking through every write.

    Tested against the real catalogue and the reference `VectorStore`; reachable whenever
    `vector_store_provider` is not `pgvector`. Four properties:

    1. `point_id` is per row, so two shares holding one document get two points.
    2. `store_embeddings` marks only the re-embedded cutting, so another cutting's stale vector is
       not stamped current.
    3. `prune_stale` uses the same "unclaimed" definition as `CLAIMED_SQL`.
    4. Unclaimed cuttings deleted by the base `upsert` have their points removed from the store too.
    """
    await migrated_db_or_skip()
    async with await connect(settings.postgres_dsn) as conn:
        await conn.execute("TRUNCATE document_files, document_chunks")
        await conn.commit()

    store = InMemoryVectorStore()
    index = ExternalVectorDocumentIndex(store, collection="chunks")
    key = embedding_config_key()
    fine_vector, coarse_vector = await asyncio.to_thread(
        embed_texts, ["the fine cutting of a protocol", "the whole protocol at once"]
    )

    fine_file = FileRecord(
        path="SOPs/protocol.txt",
        source=SOURCE,
        doc_id="doc-1",
        fingerprint="1:2",
        chunking_key="400:40",
    )
    fine = [
        ChunkRecord(
            doc_id="doc-1",
            chunking_key="400:40",
            ordinal=0,
            content="the fine cutting of a protocol",
            embedding=fine_vector,
        )
    ]
    coarse_file = fine_file.model_copy(
        update={"source": "sharedrive-2", "chunking_key": "4000:400"}
    )
    coarse = [
        ChunkRecord(
            doc_id="doc-1",
            chunking_key="4000:400",
            ordinal=0,
            content="the whole protocol at once",
            embedding=coarse_vector,
        )
    ]
    await index.upsert([fine_file], fine, key)
    await index.upsert([coarse_file], coarse, key)

    # (1) Each share answers with its own vector. The score is the assertion: a chunk's own
    # embedding must score 1.0, since `CITATION_SQL` would return the right content even from a
    # colliding vector.
    (fine_hit,) = await index.search_dense(SOURCE, fine_vector, 5, DocumentFilter())
    assert fine_hit.content == "the fine cutting of a protocol"
    assert fine_hit.score == pytest.approx(1.0)
    (coarse_hit,) = await index.search_dense("sharedrive-2", coarse_vector, 5, DocumentFilter())
    assert coarse_hit.content == "the whole protocol at once"
    assert coarse_hit.score == pytest.approx(1.0)

    # (2) Re-embedding one cutting marks that row and no other. The stored key is namespaced by
    # store (`retrieval/vectors/base.stored_embedding_key`); what matters here is which row got it.
    stored = partial(
        stored_embedding_key,
        provider=settings.vector_store_provider,
        collection=index._collection,
    )
    await index.store_embeddings(fine, "key-of-the-next-model")
    assert await _stored_keys("doc-1") == [
        ("4000:400", 0, stored(key)),
        ("400:40", 0, stored("key-of-the-next-model")),
    ]

    # (4) The fine share is re-chunked coarsely. The base's per-write cleanup deletes the row
    # it superseded, and the point that addressed it goes with it — the obligation the subclass
    # previously had no way to see.
    await index.upsert([fine_file.model_copy(update={"chunking_key": "4000:400"})], coarse, key)
    assert await _stored_cuttings() == [("4000:400", 0)]
    # Asked of the store through its own interface: the fine cutting's point is gone and the
    # coarse one is still there, which is what "the vectors went with the rows" means.
    assert {m.id for m in await store.search("chunks", fine_vector, 10)} == {"doc-1@4000:400#0"}

    # (3) And the sweep agrees with `CLAIMED_SQL` rather than a local spelling of it: a cutting
    # no file row claims is an orphan even while the *document* still has one.
    async with await connect(settings.postgres_dsn) as conn:
        await conn.execute(
            "UPDATE document_files SET chunking_key = '400:40' WHERE source = %s", (SOURCE,)
        )
        await conn.commit()
    assert await index.prune_stale("sharedrive-2", await index.clock()) == 1
    assert await _stored_cuttings() == [], "the unclaimed cutting was swept here, not later"
    assert await store.search("chunks", coarse_vector, 10) == [], "and its point went with it"


def test_a_chunked_document_can_be_read_back_whole(tmp_path: Path) -> None:
    """A chunked document can be read back whole.

    A chunk hit cites `sharedrive:doc-…#3`, and a protocol is atomic, so a turn must be able to
    fetch the other pieces (the parsed text is not kept elsewhere).
    """
    index = InMemoryDocumentIndex()
    share = _long_share(tmp_path, chunk_chars=400, chunk_overlap_chars=40)
    asyncio.run(sync_share(SOURCE, load_binding(share), index))
    retriever = ShareDocumentRetriever(binding=share, name=SOURCE, index=index)

    hits = asyncio.run(retriever.retrieve("charge the vessel", {}))
    assert hits, "sanity: the share answers at all"
    doc_id = hits[0].source_note_id.split(":", 1)[1].split("#", 1)[0]

    whole = asyncio.run(retriever.read_document(doc_id))
    assert whole is not None
    assert whole.chunks > 1, "the fixture must actually be cut into several pieces"
    # The document is *whole*: the first and last step both present, from one read.
    assert "Step 0:" in whole.text and "Step 119:" in whole.text
    assert whole.path == "SOPs/protocol.txt"
    assert not whole.truncated


def test_reading_a_document_this_share_does_not_hold_is_none_not_empty(tmp_path: Path) -> None:
    """`None` means "could not be read". An empty document would be a different, false claim."""
    index = InMemoryDocumentIndex()
    share = _long_share(tmp_path, chunk_chars=400, chunk_overlap_chars=40)
    asyncio.run(sync_share(SOURCE, load_binding(share), index))
    retriever = ShareDocumentRetriever(binding=share, name=SOURCE, index=index)
    assert asyncio.run(retriever.read_document("doc-nothing-here")) is None
    assert asyncio.run(retriever.read_document("")) is None


def test_a_whole_document_read_is_refused_without_the_shares_group(
    tmp_path: Path, as_user: Callable[[str, set[str]], None]
) -> None:
    """A whole read is a strictly larger disclosure than a ranked excerpt, so it asks the same gate.

    Including the reject-if-absent half: a gated share with no actor on the turn returns nothing,
    which is `require_actor` applied to a corpus rather than to a tool.
    """
    index = InMemoryDocumentIndex()
    public = _long_share(tmp_path, chunk_chars=400, chunk_overlap_chars=40)
    asyncio.run(sync_share(SOURCE, load_binding(public), index))
    doc_id = next(iter(index._files.values())).doc_id

    gated = {**public, "required_roles": ["chemclaw.sharedrive.reader"]}
    del gated["public"]
    retriever = ShareDocumentRetriever(binding=gated, name=SOURCE, index=index)

    # No actor at all on the turn.
    assert asyncio.run(retriever.read_document(doc_id)) is None

    as_user("someone", {"chemclaw.other"})
    assert asyncio.run(retriever.read_document(doc_id)) is None

    as_user("someone", {"chemclaw.sharedrive.reader"})
    assert asyncio.run(retriever.read_document(doc_id)) is not None, (
        "and the entitled caller still gets it"
    )


def test_an_oversized_document_comes_back_short_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shortened document that does not say so reads as a complete one."""
    index = InMemoryDocumentIndex()
    share = _long_share(tmp_path, chunk_chars=400, chunk_overlap_chars=40)
    asyncio.run(sync_share(SOURCE, load_binding(share), index))
    retriever = ShareDocumentRetriever(binding=share, name=SOURCE, index=index)
    doc_id = next(iter(index._files.values())).doc_id

    monkeypatch.setattr(settings, "document_read_max_chars", 100)
    whole = asyncio.run(retriever.read_document(doc_id))
    assert whole is not None
    assert whole.truncated is True
    assert len(whole.text) == 100


async def test_both_backends_read_the_same_whole_document() -> None:
    """The reference backend and Postgres must agree, or a test proves nothing about production."""
    await migrated_db_or_skip()
    async with await connect(settings.postgres_dsn) as conn:
        await conn.execute("TRUNCATE document_files, document_chunks")
        await conn.commit()

    (vector,) = await asyncio.to_thread(embed_texts, ["a chunk of a protocol"])
    key = embedding_config_key()
    file_row = FileRecord(
        path="SOPs/protocol.txt",
        source=SOURCE,
        doc_id="doc-1",
        fingerprint="1:2",
        chunking_key="400:40",
    )
    chunks = [
        ChunkRecord(
            doc_id="doc-1",
            chunking_key="400:40",
            ordinal=n,
            content=f"Step {n}: charge and hold.",
            coordinate=f"page {n + 1}",
            embedding=vector,
        )
        for n in range(4)
    ]
    # Two copies with different modification times, the cited one not the newest (`Archive/…` sorts
    # first and wins the citation, `SOPs/…` is newer), so a backend taking the cited path's time
    # instead of `max` across copies would fail.
    cited_but_older = file_row.model_copy(
        update={
            "path": "Archive/protocol.txt",
            "modified_at": datetime(2026, 1, 1, tzinfo=UTC),
        }
    )
    newer_copy = file_row.model_copy(update={"modified_at": datetime(2026, 3, 4, 12, tzinfo=UTC)})

    results = []
    for index in (PostgresDocumentIndex(), InMemoryDocumentIndex()):
        await index.upsert([cited_but_older, newer_copy], chunks, key)
        stored = await index.stored_document(SOURCE, "doc-1", "400:40", 1_000_000)
        assert stored is not None, f"{type(index).__name__} did not read the document back"
        results.append(
            (
                stored.path,
                stored.modified_at,
                [(p.ordinal, p.content, p.coordinate) for p in stored.pieces],
            )
        )
        # The smallest path is the citation; the most recent copy is the time.
        assert stored.path == "Archive/protocol.txt"
        assert stored.modified_at == datetime(2026, 3, 4, 12, tzinfo=UTC), (
            f"{type(index).__name__} reported {stored.modified_at}, not the newest copy"
        )
        # A share that does not hold it reads as absent on both.
        assert await index.stored_document("other-share", "doc-1", "400:40", 1_000_000) is None

    assert results[0] == results[1], "the two backends disagree about the stored document"


async def test_the_durable_backend_stores_the_documents_own_text_and_still_finds_the_number() -> (
    None
):
    """A stored chunk is the document's text; only what feeds the tsvector is normalised.

    Binding one normalised string to both `content` and `to_tsvector` would store "cool to -78 °C"
    as " 78 °C" — a sign flip in what a chemist cites. Both halves: the stored text is verbatim, and
    search still matches `78` to `-78` and `108-24-7` (`core.fulltext`).
    """
    raw = "The mixture was cooled to -78 C over -0.5 h; CAS 108-24-7 was charged."

    await migrated_db_or_skip()
    async with await connect(settings.postgres_dsn) as conn:
        await conn.execute("TRUNCATE document_files, document_chunks")
        await conn.commit()

    (vector,) = await asyncio.to_thread(embed_texts, [raw])
    key = embedding_config_key()
    file_row = FileRecord(
        path="SOPs/cryo.txt",
        source=SOURCE,
        doc_id="doc-1",
        fingerprint="1:2",
        chunking_key="400:40",
    )
    chunk = ChunkRecord(
        doc_id="doc-1",
        chunking_key="400:40",
        ordinal=0,
        content=raw,
        embedding=vector,
    )
    for index in (PostgresDocumentIndex(), InMemoryDocumentIndex()):
        await index.upsert([file_row], [chunk], key)
        stored = await index.stored_document(SOURCE, "doc-1", "400:40", 1_000_000)
        assert stored is not None
        assert stored.pieces[0].content == raw, (
            f"{type(index).__name__} rewrote the document's own text"
        )

    durable = PostgresDocumentIndex()
    hits = await durable.search_lexical(SOURCE, "cooled", 5, DocumentFilter())
    assert [hit.content for hit in hits] == [raw], "the served excerpt is not the stored text"
    # The normalisation is still doing its job on the derivation, which is the whole reason
    # the two are bound separately rather than the parameter simply removed.
    assert await durable.search_lexical(SOURCE, "78", 5, DocumentFilter()), (
        "a cryogenic temperature is unreachable again"
    )
    assert await durable.search_lexical(SOURCE, "108-24-7", 5, DocumentFilter()), (
        "the CAS number stopped matching"
    )


async def test_one_unstorable_document_costs_the_document_and_not_the_pass(tmp_path: Path) -> None:
    """One unstorable document costs the document and not the pass, against the real database.

    A NUL is valid UTF-8 and Postgres refuses it in `text`; without a per-file guard the batch
    fails, and with no cross-run cursor every later run dies on the same file. Only the Postgres
    backend has the fault, so it is driven there.
    """
    await migrated_db_or_skip()
    async with await connect(settings.postgres_dsn) as conn:
        await conn.execute("TRUNCATE document_files, document_chunks")
        await conn.commit()

    root = tmp_path / "nul-share"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "good.txt").write_text("A report about amide couplings.")
    (root / "docs" / "mid.txt").write_bytes(b"Batch record\x00 for lot 42, yield 88%.")
    (root / "docs" / "zzz.txt").write_text("A report sorted after the bad one.")
    binding = load_binding(
        {
            "mount": str(root),
            "public": True,
            "roots": [{"path": "docs"}],
            "extensions": [".txt"],
        }
    )

    report = await sync_share(SOURCE, binding, PostgresDocumentIndex(), limit=100)

    assert report.scanned == 3
    assert report.indexed == 2, "the readable documents did not survive the unstorable one"
    assert report.skipped_unreadable == 1


def test_an_oversized_document_is_bounded_at_the_fetch_not_after_assembly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An oversized document is bounded at the fetch, not after assembly.

    `document_read_max_chars` exists so a huge report is never pulled into the chat pod, so this
    asserts on the pieces fetched, not on the trimmed text.
    """
    index = InMemoryDocumentIndex()
    share = _long_share(tmp_path, chunk_chars=400, chunk_overlap_chars=40)
    asyncio.run(sync_share(SOURCE, load_binding(share), index))
    retriever = ShareDocumentRetriever(binding=share, name=SOURCE, index=index)
    doc_id = next(iter(index._files.values())).doc_id

    whole = asyncio.run(retriever.read_document(doc_id))
    assert whole is not None and whole.chunks > 4, "the fixture must be worth bounding"

    monkeypatch.setattr(settings, "document_read_max_chars", 500)
    bounded = asyncio.run(retriever.read_document(doc_id))

    assert bounded is not None
    assert bounded.truncated is True
    assert bounded.chunks < whole.chunks, (
        f"every piece was still fetched ({bounded.chunks} of {whole.chunks}), so the ceiling is "
        "being applied after assembly rather than at the fetch"
    )
    # Enough pieces to reach the ceiling, and not many more.
    assert bounded.chunks <= 3


def test_a_document_that_fits_is_never_reported_truncated(tmp_path: Path) -> None:
    """The false-positive direction: a bound that cuts too eagerly lies the other way."""
    index = InMemoryDocumentIndex()
    share = _long_share(tmp_path, chunk_chars=400, chunk_overlap_chars=40)
    asyncio.run(sync_share(SOURCE, load_binding(share), index))
    retriever = ShareDocumentRetriever(binding=share, name=SOURCE, index=index)
    doc_id = next(iter(index._files.values())).doc_id

    whole = asyncio.run(retriever.read_document(doc_id))
    assert whole is not None
    assert whole.truncated is False
    assert "Step 0:" in whole.text and "Step 119:" in whole.text


async def test_both_backends_stop_at_the_same_piece() -> None:
    """A bound applied differently on each side would make the agreement test assert two things."""
    await migrated_db_or_skip()
    async with await connect(settings.postgres_dsn) as conn:
        await conn.execute("TRUNCATE document_files, document_chunks")
        await conn.commit()

    (vector,) = await asyncio.to_thread(embed_texts, ["a chunk of a protocol"])
    key = embedding_config_key()
    file_row = FileRecord(
        path="SOPs/protocol.txt",
        source=SOURCE,
        doc_id="doc-cap",
        fingerprint="1:2",
        chunking_key="400:40",
    )
    chunks = [
        ChunkRecord(
            doc_id="doc-cap",
            chunking_key="400:40",
            ordinal=n,
            content="x" * 100,
            coordinate=f"page {n + 1}",
            embedding=vector,
        )
        for n in range(10)
    ]
    seen = []
    for index in (PostgresDocumentIndex(), InMemoryDocumentIndex()):
        await index.upsert([file_row], chunks, key)
        # 250 characters spans two 100-character pieces and crosses into the third.
        stored = await index.stored_document(SOURCE, "doc-cap", "400:40", 250)
        assert stored is not None
        seen.append((len(stored.pieces), stored.truncated))
        full = await index.stored_document(SOURCE, "doc-cap", "400:40", 10_000)
        assert full is not None and full.truncated is False, (
            f"{type(index).__name__} reported a complete document as truncated"
        )

    assert seen[0] == seen[1], f"the backends cut differently: {seen}"
    assert seen[0] == (3, True)


async def test_moving_the_document_corpus_to_another_store_re_embeds_it() -> None:
    """Moving the document corpus to another store re-embeds it.

    `stale_chunks` selects on `embedding_key IS DISTINCT FROM`, so the key must name the store as
    well as the model; otherwise a provider switch leaves every row matching and dense search
    answers from an empty collection. The catalogue is shared between backends, so the switch must
    be visible in the row.
    """
    await migrated_db_or_skip()
    async with await connect(settings.postgres_dsn) as conn:
        await conn.execute("TRUNCATE document_files, document_chunks")
        await conn.commit()

    key = embedding_config_key()
    (vector,) = await asyncio.to_thread(embed_texts, ["a protocol worth finding"])
    file_row = FileRecord(
        doc_id="doc-1",
        source="sharedrive",
        path="a.md",
        fingerprint="1:1",
        chunking_key="400:40",
        tags=[],
        modified_at=None,
    )
    chunks = [
        ChunkRecord(
            doc_id="doc-1",
            chunking_key="400:40",
            ordinal=0,
            content="a protocol worth finding",
            embedding=vector,
        )
    ]

    first = ExternalVectorDocumentIndex(InMemoryVectorStore(), collection="chunks")
    await first.upsert([file_row], chunks, key)
    assert await first.stale_chunks(key, 10, {"400:40"}) == [], "settled in its own store"

    # The move: same catalogue, same model, a store that has never seen this corpus.
    moved_to = InMemoryVectorStore()
    with patch.object(settings, "vector_store_provider", "databricks"):
        moved = ExternalVectorDocumentIndex(moved_to, collection="chunks")
        stale = await moved.stale_chunks(key, 10, {"400:40"})
        assert [(c.doc_id, c.ordinal) for c in stale] == [("doc-1", 0)], (
            "every chunk must read as stale: its vector is in the store we just left"
        )
        await moved.store_embeddings(chunks, key)
        assert await moved.stale_chunks(key, 10, {"400:40"}) == []

    assert [m.id for m in await moved_to.search("chunks", vector, 5)], (
        "and the vector landed in the new store, which is what search will ask"
    )


def test_neither_backend_ranks_a_chunk_from_a_superseded_embedding_configuration() -> None:
    """Neither backend ranks a chunk from a superseded embedding configuration.

    A same-width model switch raises nothing at insert, and old-model vectors still score positive
    against new-model queries, returning arbitrary text with a real path until the re-embed drain
    runs. Asserted on both backends, so the reference keeps answering as production does.
    """

    async def _run(monkeypatch: pytest.MonkeyPatch) -> None:
        await migrated_db_or_skip()
        async with await connect(settings.postgres_dsn) as conn:
            await conn.execute("TRUNCATE document_files, document_chunks")
            await conn.commit()

        (vector,) = await asyncio.to_thread(embed_texts, ["palladium catalyst deactivation"])
        stale = embedding_config_key()
        file_row = FileRecord(
            path="SOPs/protocol.txt",
            source=SOURCE,
            doc_id="doc-1",
            fingerprint="1:2",
            chunking_key="400:40",
        )
        chunk = ChunkRecord(
            doc_id="doc-1",
            chunking_key="400:40",
            ordinal=0,
            content="The palladium catalyst deactivated above 80 degrees.",
            coordinate="page 1",
            embedding=vector,
        )

        for index in (PostgresDocumentIndex(), InMemoryDocumentIndex()):
            await index.upsert([file_row], [chunk], stale)
            found = await index.search_dense(SOURCE, vector, 5, DocumentFilter())
            assert found, f"{type(index).__name__}: sanity, findable under its own key"

        with monkeypatch.context() as swapped:
            swapped.setattr(settings, "embedding_model", "another-model-of-the-same-width")
            assert embedding_config_key() != stale
            for index in (PostgresDocumentIndex(), InMemoryDocumentIndex()):
                hits = await index.search_dense(SOURCE, vector, 5, DocumentFilter())
                assert hits == [], (
                    f"{type(index).__name__} cited a model-A chunk against a model-B query: {hits}"
                )

    with pytest.MonkeyPatch.context() as monkeypatch:
        asyncio.run(_run(monkeypatch))


def test_a_systematic_read_failure_costs_log_lines_by_the_pass_not_by_the_corpus(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A systematic read failure costs log lines by the pass, not by the corpus.

    Per-file WARNINGs suit one bad PDF, but failures are usually systematic (changed permissions, a
    broken OCR pass), making log volume a function of the share and slowing the failing pass under
    backpressure. The pass logs reasons and totals; `SyncReport` carries the count.
    """
    root = tmp_path / "share"
    (root / "Docs").mkdir(parents=True)
    for index_ in range(40):
        (root / "Docs" / f"broken-{index_:02d}.pdf").write_bytes(b"%PDF-1.4 not a pdf at all")
    binding = load_binding(
        {
            "mount": str(root),
            "roots": [{"path": "Docs"}],
            "public": True,
            "extensions": [".pdf"],
        }
    )

    with caplog.at_level(logging.DEBUG, logger="chemclaw.ingest.documents"):
        report = asyncio.run(sync_share(SOURCE, binding, InMemoryDocumentIndex()))

    assert report.skipped_unreadable == 40
    # This tree's own records only: `pypdf` logs one line of its own per malformed file, which is
    # the same volume problem one library down and is silenced by logging configuration rather
    # than by this module.
    warnings = [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING and record.name.startswith("chemclaw.")
    ]
    assert len(warnings) == 1, (
        f"40 unreadable documents produced {len(warnings)} WARNING line(s); at share scale that is "
        "one line per file in the corpus, and the reason is in every one of them"
    )
    summary = warnings[0].getMessage()
    # The reason is the class name `sync.py` counts by, which moves when a refusal is classified
    # more precisely, so the suffix is asserted rather than one name.
    assert "40" in summary and "ParseError x40" in summary, (
        f"the one line an operator reads must carry the count and the distinct reasons: {summary!r}"
    )
    # The individual paths are not lost, they are moved: DEBUG is where a per-file trail belongs.
    debug = [record for record in caplog.records if record.levelno == logging.DEBUG]
    assert sum("broken-" in record.getMessage() for record in debug) == 40


# --- one pathological document must not hold the whole pass -------------------------------------
#
# `_parse_changed` waits on each file with a bound. These tests use a real blocking read rather
# than a patched clock, because what is proved is that the pass returns while the read is still
# running.


class _BlockingParse:
    """A read that never comes back for one named file, and is the real parse for every other.

    Stands in for `parse_document_isolated`. The parse itself runs in a child killed on its own
    deadline (`ingest/documents/isolate.py`); the crawl's `wait_for` is the backstop over a hung
    *read* (a share that stopped answering), which blocking in the worker thread models. A
    `threading.Event`, so "still running" is unambiguous; the wait is bounded so a regression fails
    rather than hangs.
    """

    def __init__(self, blocked_name: str) -> None:
        """Arm the trap for `blocked_name`; nothing blocks until that file is opened."""
        self.blocked_name = blocked_name
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(
        self, name: str, raw: bytes, declared_type: str | None = None, timeout: float = 0.0
    ) -> ParsedDocument:
        """Block in the calling worker thread for the armed file; otherwise parse for real."""
        if name.endswith(self.blocked_name):
            self.entered.set()
            self.release.wait(timeout=30.0)
        return parse_document(name, raw, declared_type)


def _share_with_a_slow_file(tmp_path: Path) -> dict[str, Any]:
    """Two readable text files on a share, one of which the trap above will hold."""
    mount = tmp_path / "mount"
    (mount / "Docs").mkdir(parents=True)
    (mount / "Docs" / "quick.txt").write_text("the palladium catalyst deactivated above 80 degrees")
    (mount / "Docs" / "slow.txt").write_text("a document whose parser never comes back")
    return {
        "mount": str(mount),
        "roots": [{"path": "Docs"}],
        "public": True,
        "extensions": [".txt"],
    }


def _pass_with_a_blocked_file(
    binding: Any, index: InMemoryDocumentIndex, trap: _BlockingParse, *, after: str = ""
) -> tuple[SyncReport, float]:
    """Run one pass while `trap` holds a file, and report how long the pass itself took.

    The release happens inside the loop's lifetime, because `asyncio.run` joins the default executor
    and would otherwise time the blocked thread rather than the pass.
    """

    async def _run() -> tuple[SyncReport, float]:
        started = time.perf_counter()
        report = await sync_share(SOURCE, binding, index, after=after, limit=100)
        elapsed = time.perf_counter() - started
        trap.release.set()
        return report, elapsed

    return asyncio.run(_run())


def test_a_document_that_never_finishes_parsing_does_not_hold_the_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound frees the pass — the rest of the share is indexed while that parse runs on."""
    share = _share_with_a_slow_file(tmp_path)
    trap = _BlockingParse("slow.txt")
    monkeypatch.setattr(sync_module, "parse_document_isolated", trap)
    monkeypatch.setattr(settings, "attachment_parse_timeout_seconds", 0.1)
    # The pass's backstop is the parse budget *plus* this, because the forkserver's own first
    # start happens before a child's clock begins. Set here so the assertions below measure the
    # bound rather than the shipped five-second margin.
    monkeypatch.setattr(settings, "attachment_parse_reap_grace_seconds", 0.5)
    index = InMemoryDocumentIndex()

    report, elapsed = _pass_with_a_blocked_file(load_binding(share), index, trap)

    assert trap.entered.is_set(), "the trap never fired; this test proved nothing about the bound"
    assert elapsed < 5.0, (
        f"the pass took {elapsed:.1f}s with a parse that blocks for 30s — it waited for the thread"
    )
    assert report.skipped_timeout == 1
    # The counter is the timeout's own: a bound that fired is not a file that could not be read,
    # and `TimeoutError` is an `OSError` subclass, so the two are one `except` order apart.
    assert report.skipped_unreadable == 0
    # And the honest half of the claim: the *other* file was read, chunked and indexed anyway.
    assert report.indexed == 1
    chunking = load_binding(share).chunking_key
    stored = asyncio.run(index.fingerprints(SOURCE, ["Docs/quick.txt", "Docs/slow.txt"], chunking))
    assert "Docs/quick.txt" in stored
    # No row for the timed-out file, the same trade every other refusal makes: storing its
    # fingerprint would make it look unchanged forever and zero the counter from the next run on.
    assert "Docs/slow.txt" not in stored


def test_a_timed_out_document_is_visible_in_the_run_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A timed-out document is visible in the run summary.

    The count must survive the activity's report, the `ingest.finished` record and `merge_reports`
    (a missing field reads as zero for a whole drain).
    """
    share = _share_with_a_slow_file(tmp_path)
    trap = _BlockingParse("slow.txt")
    monkeypatch.setattr(sync_module, "parse_document_isolated", trap)
    monkeypatch.setattr(settings, "attachment_parse_timeout_seconds", 0.1)
    # The pass's backstop is the parse budget *plus* this, because the forkserver's own first
    # start happens before a child's clock begins. Set here so the assertions below measure the
    # bound rather than the shipped five-second margin.
    monkeypatch.setattr(settings, "attachment_parse_reap_grace_seconds", 0.5)

    with caplog.at_level(logging.DEBUG, logger="chemclaw.ingest.documents"):
        report, _ = _pass_with_a_blocked_file(load_binding(share), InMemoryDocumentIndex(), trap)

    finished = [r for r in caplog.records if getattr(r, "event", "") == "ingest.finished"]
    assert len(finished) == 1
    assert finished[0].__dict__["skipped_timeout"] == 1
    # One WARNING for the pass, not one per file — `_summarise_skips`'s rule, which a share full of
    # documents past the bound would otherwise turn into a line per file.
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING and record.name.startswith("chemclaw.")
    ]
    assert len(warnings) == 1 and "TimeoutError" in warnings[0], warnings
    assert any("slow.txt" in r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG)
    assert sync_module.merge_reports([report, report], SOURCE).skipped_timeout == 2


def test_a_document_that_times_out_keeps_the_row_it_already_had(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A document that times out keeps the row it already had.

    A timed-out file is still on the share; reading its absence from the processed set as deletion
    would drop it every time the parse is slow.
    """
    share = _share_with_a_slow_file(tmp_path)
    binding = load_binding(share)
    index = InMemoryDocumentIndex()
    asyncio.run(sync_share(SOURCE, binding, index, limit=100))

    target = tmp_path / "mount" / "Docs" / "slow.txt"
    target.write_text(target.read_text() + " and now it has changed")  # the fingerprint moves
    trap = _BlockingParse("slow.txt")
    monkeypatch.setattr(sync_module, "parse_document_isolated", trap)
    monkeypatch.setattr(settings, "attachment_parse_timeout_seconds", 0.1)
    # The pass's backstop is the parse budget *plus* this, because the forkserver's own first
    # start happens before a child's clock begins. Set here so the assertions below measure the
    # bound rather than the shipped five-second margin.
    monkeypatch.setattr(settings, "attachment_parse_reap_grace_seconds", 0.5)

    later = asyncio.run(index.clock())
    report, _ = _pass_with_a_blocked_file(binding, index, trap)

    assert report.skipped_timeout == 1
    assert asyncio.run(prune_share(SOURCE, index, later, report)) == 0
    assert asyncio.run(index.fingerprints(SOURCE, ["Docs/slow.txt"], binding.chunking_key))


def test_a_normal_share_is_untouched_by_the_bound(share: dict[str, Any]) -> None:
    """The bound must cost nothing when no file is near it — the ordinary pass, unchanged."""
    index = InMemoryDocumentIndex()
    report = asyncio.run(sync_share(SOURCE, load_binding(share), index))

    assert report.skipped_timeout == 0
    assert report.indexed == 4 and report.deduplicated == 1  # the fixture share's own numbers


def test_a_share_document_is_parsed_in_a_process_the_crawl_can_kill(tmp_path: Path) -> None:
    """A share document is parsed in a process the crawl can kill.

    A thread cannot be interrupted, so a `wait_for` around `to_thread` would leave each hostile
    document consuming a shared-executor thread for the life of the Temporal worker. Driven through
    `sync_share` and the real `parse_document_isolated`, substituting only what the stalled child
    does. It lands on `skipped_timeout`, not `skipped_unreadable` (`ParseWorkerLost` subclasses
    `DocumentParseError`, so it needs its own arm).

    A stalled child rather than a large document, because a large one can end on the memory ceiling
    instead, racing two bounds; a child that never answers and allocates nothing ends only on the
    deadline. The stall lives in the forkserver preload (`tests/parse_stalls.py`).
    """
    mount = tmp_path / "mount"
    (mount / "Docs").mkdir(parents=True)
    (mount / "Docs" / "quick.txt").write_text("the palladium catalyst deactivated above 80 degrees")
    (mount / "Docs" / "stall.csv").write_text("a,b\n1,2\n")
    probe = subprocess.run(
        [sys.executable, "-c", f"from tests.parse_stalls import crawl; crawl({str(mount)!r})"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    lines = [line for line in probe.stdout.splitlines() if line.startswith("crawl|")]
    assert len(lines) == 1, (probe.stdout, probe.stderr[-2000:])
    ended, timed_out, unreadable, indexed, kills, children = lines[0].split("|")[1:]
    # The property, not a duration: the stalled child never answers, so a crawl that nobody kills
    # never returns, and the probe's patience is far past the deadline that must end it.
    assert ended == "ended", (lines[0], probe.stderr[-2000:])
    assert (timed_out, unreadable, indexed) == ("1", "0", "1"), (lines[0], probe.stderr[-2000:])
    assert kills == "1", (
        "no parse child was killed, so the crawl parsed that document on its own worker thread: "
        f"{lines[0]}"
    )
    assert children == "0", f"a killed parse child outlived the pass: {lines[0]}"
