"""A mounted SMB share, end to end: crawl, index, retrieve, and the two rules that protect it.

Built against a real directory tree of real documents (`tests/document_fixtures.py` writes each one
with its own format's writer), an in-memory index, and no database or broker — so what these tests
exercise is the actual crawl/parse/chunk/embed loop rather than a set of mocks agreeing with each
other.

The two properties worth reading the file for:

- **A complete crawl may sweep; an incomplete one may not.** A CIFS mount that dropped presents to
  `scandir` as an empty directory, and pruning on that evidence deletes a corpus that took days to
  build. `test_a_failed_root_prunes_nothing` is the guard.
- **Cost is measured, not asserted.** The dedup and no-re-embed tests count real `embed_texts`
  calls, because "an unchanged share re-embeds nothing" is a claim about behaviour and the only
  honest way to hold it is a counter (D-2026-08-01: measure it, don't argue it).
"""

import asyncio
import shutil
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

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
from chemclaw.ingest.documents.parse import DocumentParseError
from chemclaw.ingest.documents.retriever import ShareDocumentRetriever
from chemclaw.ingest.documents.sync import (
    SyncReport,
    prune_share,
    reembed_stale,
    sync_share,
)
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

    A `ContextVar` set inside a test leaks into every later one in the same process, and the leak
    is invisible: a retrieval test would pass because a *previous* test happened to leave an
    entitled actor bound. Resetting is what keeps each assertion about its own setup.
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

    def counting(texts: list[str]) -> list[list[float]]:
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
    """The width check the config validator cannot make, made where both numbers are known.

    `note_index`'s equivalent lives in the config validator because `vector`/`lexical` are shipped
    names it can enumerate. A share's name is chosen by the deployment, so no name set finds one —
    the guard sits on the constructors instead. Without it a deployment starts cleanly and pgvector
    rejects every chunk write hours later, inside a worker.
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
    """The defect this closes is silent: a fingerprint does not move when the model does.

    So the crawl re-embedded nothing, the table came to hold a mix of two models' vectors, and
    every cosine between them was meaningless with no error anywhere.

    **The share is deleted before the re-embed runs**, which is the point of the test: the chunk's
    text was stored beside its vector, so refreshing it is a database-to-database operation. If
    this ever starts needing the mount, this assertion is what says so.
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
    """The re-embed drain runs ahead of the crawl, and paid for text the crawl then re-cut.

    Migrations 038 and 040 land together, so the first run after an upgrade has *both* keys moved:
    every chunk is stale by embedding key, and every file is stale by chunking. The re-embed pass
    therefore refreshed the whole old cutting from stored text, and the crawl then re-parsed,
    re-cut and re-embedded the same text — twice the documented cost. Measured here: 17 embeddings
    for a document worth 1.

    It cannot be fixed by stamping the chunking during a re-embed: the chunking is *part of the
    row's identity* (041), and a re-embed does not re-cut anything, so writing the current chunking
    onto rows cut under the old one would be a lie the search then serves. What is true is that a
    cutting no enabled share uses any more is about to be replaced, so re-embedding it is work
    thrown away — and that is what the drain now skips.
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
    """The defect: neither gate could see a chunking change, so `chunk_chars` did nothing.

    The file's `mtime_ns:size` does not move when a setting does, and the content hash does not
    either — so the crawl skipped every file, and the sizes measured before this fix were unchanged
    at `[1248, 1951, 1962, 1962]` after halving `chunk_chars`.
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
    """The corruption half: the superseded cutting must be deleted, not merely superseded.

    A stranded chunk belongs to no current cutting of the document, is cited as though it did, and
    `reembed_stale` then stamps it with the current key — after which nothing can tell it apart.
    Measured before any of this: 400 → 4000 chars left 19 leftovers beside the 2 real chunks.

    The mechanism changed with the chunk row's identity (041): what is deleted is every cutting of
    the documents just written that no file row claims any more, not "every ordinal above the new
    count". The old form could not tell this share's superseded cutting from another share's live
    one, because both are the same `doc_id`.
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


def _served_chunk_sizes(index: InMemoryDocumentIndex, source: str) -> list[int]:
    """The length of every chunk this source's search can actually cite, longest first.

    Measured through `search_dense` rather than off the index's internals, because what a share
    serves is the property at stake — and because the internals' key shape is exactly what the
    defect below is about, so a test reading them could not describe both sides of it.
    """
    query = embed_texts(["charge the vessel and hold"])[0]
    hits = asyncio.run(index.search_dense(source, query, 500, DocumentFilter()))
    return sorted((len(hit.content) for hit in hits), reverse=True)


def test_a_second_share_that_chunks_differently_leaves_the_first_share_intact(
    tmp_path: Path,
) -> None:
    """Two shares, one document, two chunk sizes — and the second share's crawl destroyed the first.

    `doc_id` is the *content* hash and is shared across sources by design, while `chunking_key`
    comes from the binding and is per-share. Keying chunk rows on `(doc_id, ordinal)` alone
    therefore made two shares fight over the same rows: the coarse share's write took ordinal 0 and
    its tail-drop deleted ordinals 1..15, and the fine share never repaired, because its own file
    fingerprint had not moved and its gate read `unchanged` forever. Measured before the fix: the
    fine share served one chunk of 6259 characters in place of its own sixteen of at most 400.
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
    assert asyncio.run(retriever.retrieve("palladium catalyst", {})) == []


def test_a_gated_share_refuses_when_there_is_no_identity_to_check(share: dict[str, Any]) -> None:
    """`require_actor`'s reject-if-absent rule, applied to a corpus instead of a tool.

    This is why the report workflow — which runs with no ambient identity — sees nothing from a
    gated share. Correct by construction, and stated rather than discovered.
    """
    index = InMemoryDocumentIndex()
    asyncio.run(sync_share(SOURCE, load_binding(share), index))
    retriever = _entitled_retriever(share, index)

    assert asyncio.run(retriever.retrieve("palladium catalyst", {})) == []


def test_an_ungated_share_needs_no_identity(share: dict[str, Any]) -> None:
    """Demanding an actor to check an empty requirement would block reports for no benefit."""
    index = InMemoryDocumentIndex()
    asyncio.run(sync_share(SOURCE, load_binding(share), index))
    ungated = {**share, "required_roles": [], "public": True}
    retriever = ShareDocumentRetriever(binding=ungated, name=SOURCE, index=index)

    assert asyncio.run(retriever.retrieve("palladium catalyst", {})) != []


def test_a_backend_failure_yields_no_evidence_instead_of_raising(
    share: dict[str, Any], as_user: Callable[[str, set[str]], None]
) -> None:
    """`gather_evidence` fans out with a bare gather, so one raising leg fails the question."""

    class Broken(InMemoryDocumentIndex):
        async def search_dense(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("index unreachable")

    retriever = ShareDocumentRetriever(binding=share, name=SOURCE, index=Broken())
    as_user("user-1", {"sharedrive.reader"})
    assert asyncio.run(retriever.retrieve("anything", {})) == []


def test_an_embedding_provider_failure_costs_this_leg_and_no_other(
    share: dict[str, Any], as_user: Callable[[str, set[str]], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The query is embedded *inside* this leg, so the provider's own errors are this leg's too.

    They are not `DocumentIndexError`, `ConnectionError`, `OSError` or `DocumentShareError`, so an
    `openai.APIError` from a rate-limited endpoint escaped into `gather_evidence`'s `gather` —
    which has no `return_exceptions` — and failed the whole turn, including the answer the
    knowledge graph had already produced. The `except Exception` backstop is what makes the
    "**Never raises**" docstring true; deleting the whole block left all 52 of this file's tests
    passing, which is why this one exists. Its sibling in `ingest/eln/warehouse/retriever.py` got
    the identical test in the same change.
    """

    class _ProviderError(Exception):
        """Stands in for a vendor client's own error type, which is in none of the lists above."""

    def _refusing(texts: list[str]) -> list[list[float]]:
        raise _ProviderError("429 rate limited")

    index = InMemoryDocumentIndex()
    asyncio.run(sync_share(SOURCE, load_binding(share), index))
    monkeypatch.setattr(retriever_module, "embed_texts", _refusing)
    as_user("user-1", {"sharedrive.reader"})

    assert asyncio.run(_entitled_retriever(share, index).retrieve("yield", {})) == []


def test_a_note_type_filter_returns_nothing_rather_than_ignoring_it(
    share: dict[str, Any], as_user: Callable[[str, set[str]], None]
) -> None:
    """A file on a share has no knowledge-graph note type; answering anyway would ignore the ask."""
    index = InMemoryDocumentIndex()
    asyncio.run(sync_share(SOURCE, load_binding(share), index))
    retriever = _entitled_retriever(share, index)

    as_user("user-1", {"sharedrive.reader"})
    assert asyncio.run(retriever.retrieve("palladium", {"type": "reaction"})) == []


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
# Every prune test above this line used to hand `prune_share` a `crawl_was_complete` it decided
# itself, so none of them ever asked the crawl whether sweeping was safe. These do. Each one is a
# file **present and readable on the share** that got deleted from the index anyway.


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
    """The walk's order must be the order `after` is compared in, or a resumed drain skips files.

    Siblings are sorted by bare name, but `after` is compared against the joined path. `Report`
    (a directory) sorts before `Report.pdf`, yet `Report.pdf` sorts before `Report/a.pdf` — so a
    chunk that stops inside the directory skips the file forever on every later pass.
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
    """A dropped mount with root `.` presents as an empty directory and no failed root.

    `crawl_share` is loud only when the mount path is *gone*. A CIFS volume that detaches leaves the
    mount point behind as an empty directory — the exact case the whole mark-and-sweep exists for —
    and with `roots: [{path: "."}]` there is no root left to report as missing.
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
    """The evidence `prune_share` reads must survive the workflow's own state compaction.

    A drain of a large share is thousands of chunks, so `DocumentShareSyncWorkflow` folds them with
    `_merge_by_source` before each `continue_as_new` — the carried state is the *input* of the next
    run. If that fold dropped a failed root or the unfinished tail, the guard would pass on the
    following run and sweep a share it never finished walking. This is the property that replaced
    the `degraded` flag, so it is the one that has to hold.
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
    """The command count must come from history, never from the replaying worker's config.

    `document_sync_max_iterations` decides when `continue_as_new` is emitted, so it decides how
    many activity commands the run schedules — exactly what `resolve_notes_per_run` was added to
    the memory jobs for (`D-2026-08-08-an-outage-is-not-a-missing-job`). Read live, a redeploy that
    lowers it mid-drain replays `continue_as_new` earlier than history records it: a
    non-determinism error, which is a workflow *task* failure, which retries forever and wedges the
    run (the trap D-093 documents).

    Temporal is unavailable in this environment, so this asserts the *structure* that makes the
    replay safe rather than executing a replay: the value is captured once in the activity, carried
    on the plan, and carried on the state across `continue_as_new`. The workflow body must contain
    no live read of it — an AST check, because that is the property that actually broke.
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


def test_a_backend_failure_returns_no_evidence_rather_than_failing_the_turn(
    share: dict[str, Any],
) -> None:
    """`gather_evidence` fans retrievers out with a bare `asyncio.gather`, no return_exceptions.

    So a raising leg does not degrade a question — it fails the whole thing, knowledge graph and
    all. `psycopg.Error` descends from `Exception`, not `OSError`, so it used to sail straight
    through the handler whose docstring promises this never happens.
    """
    import psycopg

    class Exploding(InMemoryDocumentIndex):
        async def search_dense(self, *args: Any, **kwargs: Any) -> Any:
            raise DocumentIndexError("search failed: statement timeout") from psycopg.Error()

    retriever = ShareDocumentRetriever(
        {**share, "required_roles": [], "public": True}, name=SOURCE, index=Exploding()
    )
    assert asyncio.run(retriever.retrieve("catalyst", {})) == []


def test_a_backend_failure_is_reported_without_the_driver_s_connection_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`DocumentIndexError`'s message keeps `SubsystemUnavailableError`'s contract, at the raiser.

    `api/middleware._subsystem_unavailable` relays a `SubsystemUnavailableError`'s message to the
    HTTP client verbatim, and its docstring says why that is safe: the type "carries no hostname,
    port or driver text — those live on `__cause__`". This raiser was built as
    `f"document search failed: {exc}"` around a `psycopg.Error`, whose string is exactly
    `connection to server at "…" (…), port 5432 failed: …`. Nothing reaches the handler from here
    today — both retrievers swallow this type — so the defect was a contract one raiser did not
    keep, which is the kind that becomes a leak the day a route stops swallowing.

    Asserted at the raiser rather than at the handler for that reason: the promise belongs to the
    exception, and the handler is only one of the places it travels.
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

    def refusing(texts: list[str]) -> Any:
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
    """The per-entry guard protects everything *inside* a root and never the root directory itself.

    `walk.mount / root.path` is handed straight to `scandir`: `is_dir()` follows the link, and the
    entries it then yields are ordinary files, so `entry.is_symlink()` is False and no check fires.
    `Projects -> /` would index the container filesystem — the knowledge repo included — as cited
    evidence, under paths that look mount-relative in the cursor, the citation and the logs.
    `follow_symlinks: false` does not help: it only makes `descend` skip symlink *entries*.
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
    """Omission must not mean "everyone". The documented workflow is to hand-author a manifest.

    A binding naming `mount` and `roots` but not `required_roles` served the whole AD-gated share to
    every authenticated user, with no warning and nothing to distinguish it from a correctly gated
    one. The security model was opt-in by default.
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
    """An AD group entitlement is `group:<claim value>`, and the bare object-id is not one.

    `api.auth` namespaces every group claim with `GROUP_ROLE_PREFIX` before it reaches the turn's
    roles, because this same set gates every write tool and skill — an unprefixed directory group
    named like an app role would silently grant it. The consequence for a share is what this test
    pins: a binding written against the bare object-id matches nothing, and because a declining
    retriever returns *no evidence* rather than an error, the whole failure is a corpus that
    answers nothing with no log line and no exception anywhere.
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
    """The prose that tells an operator how to write a group entitlement must name `group:`.

    Four hand-typed copies of one security-relevant string, and three of them were wrong: the
    shipped manifest, this package's README and the retriever's own docstring all said to name the
    group's *object-id*, while `api.auth` has always prefixed it. Only the operator guide was
    right. An operator following the manifest's own comment configures a gate that matches nothing,
    and the share then returns no evidence — silently, because declining is how the gate is
    supposed to behave.

    Checked as a claim rather than trusted as prose (D-2026-08-01-a-path-in-prose-is-a-claim-a-gate-
    can-check): the string these documents must agree on is now a constant, so the check is whether
    each of them contains it.
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
    """The crawl and the read are different activities, minutes apart, on a writable share.

    So the read is given a `FileRef` describing the file as it *was*: not a symlink, and under the
    size limit. Publish an ordinary file, let it be accepted, then replace it with a link to
    something the crawl never checked. `follow_symlinks: false` does not help — it is consulted at
    crawl time, and this is after.
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
    """`**/Archive/**` is the shipped pattern, and `fnmatch` gives `**` no special meaning.

    It translates to `.*?/Archive/`, which requires a separator before `Archive` — so a *top-level*
    `Archive/` was not excluded at all. On a share with `roots: [{path: "."}]` that is the whole
    archive tree an operator believed they had kept out.
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


def test_the_postgres_backend_gates_on_the_chunking_and_sweeps_only_unclaimed_cuttings() -> None:
    """The same rules as the in-memory reference, in SQL — and migrations 040/041 applied.

    The durable backend had no test at all, so its statements were only ever exercised in
    production. This one round-trips the gates that decide whether a re-chunk happens, and the two
    halves of what a re-chunk may delete: a cutting no file row claims goes, and a cutting another
    share still claims stays. Before 041 the second half was false — the delete was `doc_id` plus
    an ordinal floor, and `doc_id` is content, so one share's re-chunk truncated the other's rows.
    """

    async def _run() -> None:
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

        assert await index.fingerprints(SOURCE, [fine_file.path], "400:40") == {
            fine_file.path: "1:2"
        }
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

    asyncio.run(_run())


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


def test_the_external_store_backend_carries_the_chunking_through_every_write() -> None:
    """The other `DocumentIndex`, against the real catalogue and the reference `VectorStore`.

    It had no live-Postgres test at all, and every method it overrides predates the chunking key —
    so all four properties below were false and unobserved, reachable the day a deployment sets
    `vector_store_provider` to anything but `pgvector`. Each was measured against this database
    before the fix:

    1. `point_id` was `doc_id#ordinal`, so two shares holding one document wrote **one** point and
       the second overwrote the first — a share then answering every query with another share's
       vector.
    2. `store_embeddings` keyed its `embedding_key` update on `(doc_id, ordinal)`, so re-embedding
       one cutting stamped the new key on the other cutting too, whose vector was never touched.
       That row reads as current forever and `reembed_stale` skips it — precisely the
       silent-wrong-vector failure `embedding_key` exists to prevent.
    3. `prune_stale` spelled "orphan" as `f.doc_id = c.doc_id`, a third definition disagreeing with
       `CLAIMED_SQL`: a superseded cutting survived here and the base class deleted it next call.
    4. `upsert` delegates to the base, which deletes unclaimed cuttings per write — and did so
       without naming them, so their points stayed in the store with nothing left to address them.
    """

    async def _run() -> None:
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

        # (1) One point per row rather than per `(doc_id, ordinal)`, and each share still answers
        # with *its own* vector. The **score** is the assertion, not the content: querying with a
        # chunk's own embedding must score 1.0, and under the collision it did not — the content
        # still came back right, because `CITATION_SQL` dropped the other share's row on the way
        # out, so only the score ever showed that the wrong vector had been searched.
        (fine_hit,) = await index.search_dense(SOURCE, fine_vector, 5, DocumentFilter())
        assert fine_hit.content == "the fine cutting of a protocol"
        assert fine_hit.score == pytest.approx(1.0)
        (coarse_hit,) = await index.search_dense("sharedrive-2", coarse_vector, 5, DocumentFilter())
        assert coarse_hit.content == "the whole protocol at once"
        assert coarse_hit.score == pytest.approx(1.0)

        # (2) Re-embedding one cutting marks that row and no other.
        await index.store_embeddings(fine, "key-of-the-next-model")
        assert await _stored_keys("doc-1") == [
            ("4000:400", 0, key),
            ("400:40", 0, "key-of-the-next-model"),
        ]

        # (4) The fine share is re-chunked coarsely. The base's per-write cleanup deletes the row
        # it superseded, and the point that addressed it goes with it — the obligation the subclass
        # previously had no way to see.
        await index.upsert([fine_file.model_copy(update={"chunking_key": "4000:400"})], coarse, key)
        assert await _stored_cuttings() == [("4000:400", 0)]
        # Asked of the store through its own interface: the fine cutting's point is gone and the
        # coarse one is still there, which is what "the vectors went with the rows" means.
        assert {m.id for m in await store.search("chunks", fine_vector, 10)} == {"doc-1#4000:400#0"}

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

    asyncio.run(_run())
