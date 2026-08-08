"""Keep the index in step with the share: crawl, diff, parse, chunk, embed, sweep.

Backend-agnostic and dependency-injected, like `chemclaw.ingest.eln.sync` — the Temporal wrapper in
`chemclaw.durable.document_sync` does the scheduling and the bounding, and this does the work, so
the loop can be run end-to-end in a test against a temporary directory with no database and no
broker.

Three properties carry the whole design at TB scale:

**Nothing is read twice.** A file whose `mtime_ns:size` matches what the index stored is not opened
— it is restamped as seen and skipped. A scheduled run over an unchanged share therefore costs one
`scandir` pass and zero embedding calls.

**Nothing is embedded twice.** A document's identity is the hash of its *parsed text*, so the same
report sitting in four project folders is one set of chunks and one embedding call, and moving or
renaming a file costs nothing at all.

**Nothing is deleted on doubt.** Deletion is a mark-and-sweep, and the sweep runs only when a
crawl walked every root to completion. An unmounted share presents to `scandir` as an empty
directory, which is indistinguishable from "somebody deleted everything" — and of the two possible
mistakes, re-indexing a corpus is recoverable and deleting one is not.

**And nothing is quietly comparable to nothing.** Each stored vector records the embedding
configuration that made it (`embedding_config_key`). Pointing the deployment at a different model
does not move any file's fingerprint, so before this the crawl re-embedded nothing and the table
came to hold a mix of two models' vectors — every cosine between them meaningless, with no error
anywhere. `reembed_stale` closes it from the *stored chunk text*, so a model swap heals itself on
the next run without the share being touched at all. The chunking that cut each row is recorded the
same way and for the same reason (`binding.chunking_key`), and both of the gates below compare it —
the fingerprint gate decides whether a document is re-read, the `known_documents` gate whether it is
re-embedded, and a chunk-size change must be visible to both or one of them skips the file.

**And nothing is skipped silently.** A decade-old share is full of scanned PDFs and of `.doc` files
this system cannot read. Both are counted, per extension, and reported. Silence would be read as
"the share held nothing else", which is the one answer that is never true.
"""

import asyncio
import logging
import os
from collections import Counter
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from chemclaw.core.embeddings import embed_texts, embedding_config_key
from chemclaw.core.ids import stable_hash
from chemclaw.ingest.documents.binding import DocumentShareBinding
from chemclaw.ingest.documents.chunk import chunk_document
from chemclaw.ingest.documents.crawl import CrawlResult, FileRef, crawl_share
from chemclaw.ingest.documents.index import (
    ChunkRecord,
    DocumentIndex,
    FileRecord,
    StaleChunk,
)
from chemclaw.ingest.documents.parse import (
    DocumentParseError,
    ScannedDocumentError,
    parse_document,
)

logger = logging.getLogger(__name__)


@runtime_checkable
class DocumentShareSource(Protocol):
    """A data source that carries a crawlable share — the marker the sync job selects on.

    Structural rather than declared, so enabling a share stays exactly one thing:
    `CHEMCLAW_DATA_SOURCES`. A second list naming which of the enabled sources are *also* document
    shares would be a declaration whose only correct value is computable from the source itself,
    which D-150 spells out is not a configuration point but an opportunity to be wrong.
    """

    name: str

    def share_binding(self) -> DocumentShareBinding:
        """The share this source answers from, and therefore the one to crawl."""
        ...


class SyncReport(BaseModel):
    """What one bounded pass did, and — just as important — what it could not do.

    Every "skipped" number here is a statement about the corpus a chemist will be querying. A
    deployment reading `indexed: 12000` without `skipped_scan: 4300` beside it would conclude the
    share is searchable when a third of it is invisible.
    """

    source: str
    # Candidate documents the crawl surfaced (already past the extension and size filters).
    scanned: int = 0
    indexed: int = 0
    # Fingerprint unchanged — not opened, only restamped as seen.
    unchanged: int = 0
    # A file whose content was already indexed under another path: one more file row, no embedding.
    deduplicated: int = 0
    embedded_chunks: int = 0
    pruned: int = 0
    # A PDF with no text layer at all. Its own counter because it is the population OCR would fix.
    skipped_scan: int = 0
    # Opened and refused, or unreadable on the share (a permission error, a truncated file).
    skipped_unreadable: int = 0
    skipped_oversized: int = 0
    # Parsed successfully to no text at all — an empty workbook, a placeholder file. Indexed as a
    # file row with no chunks, so it is not re-read every run.
    empty: int = 0
    # Per-extension tally of everything the format allowlist turned away.
    skipped_unsupported: dict[str, int] = Field(default_factory=dict)
    failed_roots: list[str] = Field(default_factory=list)
    cursor: str = ""
    has_more: bool = False


class ReembedReport(BaseModel):
    """One bounded re-embedding pass: how many chunks were refreshed, and whether more remain."""

    embedded: int = 0
    # Chunks the provider would not embed even one at a time. They keep a superseded vector, which
    # is reported rather than hidden — a silently wrong vector is what this whole mechanism exists
    # to prevent, so the count of ones it could not fix has to be visible too.
    failed: int = 0
    has_more: bool = False


class _Parsed(BaseModel):
    """One file that was opened and read: its document identity and its text."""

    ref_path: str
    doc_id: str
    text: str


def _read_and_parse(ref: FileRef, max_bytes: int) -> _Parsed:
    """Read one file off the share and extract its text (blocking; called in a worker thread).

    The crawl checked this path minutes ago, in a different activity: it confirmed the entry was
    not a symlink and that its size was under `max_file_bytes`. Both can be false by now, and on a
    share every member can write to, deliberately so. **The open re-checks rather than trusts.**
    `O_NOFOLLOW` refuses a path that became a symlink — pointing at, say, the workload-identity
    token the crawl never saw — and the size is re-read from the *open descriptor*, so a file that
    grew from 1 KB to 20 GB after being accepted is refused rather than read into the worker.

    Raises:
        ScannedDocumentError: A PDF with no text layer.
        DocumentParseError: An unsupported format, or one the library could not open.
        OSError: The share could not be read at this path, or it became a symlink.
    """
    # `os.open` with the flag, not `Path.read_bytes`: the check and the read must be the same
    # operation, or the swap simply moves into the gap between them.
    descriptor = os.open(ref.absolute, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        size = os.fstat(descriptor).st_size
        if size > max_bytes:
            raise DocumentParseError(
                f"{ref.path} is {size} bytes at read time, past the {max_bytes}-byte limit "
                f"(it was {ref.size} when the crawl accepted it)"
            )
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1  # the context manager owns it now
            raw = handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    parsed = parse_document(ref.path, raw)
    # The identity is the content, never the path — so four copies of one report collapse to one
    # document and a rename is free. `backfill_corpus.note_for_document` makes the same call.
    doc_id = f"doc-{stable_hash(parsed.text, chars=16)}"
    return _Parsed(ref_path=ref.path, doc_id=doc_id, text=parsed.text)


def _file_record(source: str, ref: FileRef, doc_id: str) -> FileRecord:
    """The index row for one path: its document, its stat signature, and what its path means."""
    return FileRecord(
        path=ref.path,
        source=source,
        doc_id=doc_id,
        fingerprint=ref.fingerprint,
        tags=list(ref.tags),
        modified_at=datetime.fromtimestamp(ref.mtime_ns / 1_000_000_000, tz=UTC),
    )


async def _parse_changed(
    refs: list[FileRef], report: SyncReport, max_bytes: int
) -> tuple[list[_Parsed], dict[str, FileRef], list[str]]:
    """Read and parse each changed file, tallying every refusal rather than dropping it.

    Reject-and-continue, the discipline the ELN sync uses: one unreadable PDF must not abort a pass
    over ten thousand files.

    Returns the parsed documents, their refs by path, and the paths that were **refused but are
    still on the share** — the caller restamps those, because a file that failed to open did not
    stop existing and the sweep must not read this pass's silence about it as deletion.
    """
    parsed: list[_Parsed] = []
    by_path: dict[str, FileRef] = {}
    refused: list[str] = []
    for ref in refs:
        try:
            result = await asyncio.to_thread(_read_and_parse, ref, max_bytes)
        # A refused file gets **no index row**, so its fingerprint is not stored and the next crawl
        # opens it again. That is a deliberate trade, not an oversight: recording it would make the
        # file look unchanged forever, and `skipped_scan` would then read zero on every run after
        # the first — losing exactly the number that tells an operator how much of the share is
        # invisible. The cost is one read per refused file per cycle and no embedding at all; the
        # alternative costs the measurement. `docs/planning/BACKLOG.md` carries the row for
        # revisiting it if a share's refused population makes that read volume material.
        except ScannedDocumentError:
            report.skipped_scan += 1
            refused.append(ref.path)
            continue
        except (DocumentParseError, OSError) as exc:
            logger.warning("skipping %s: %s", ref.path, exc)
            report.skipped_unreadable += 1
            refused.append(ref.path)
            continue
        if not result.text.strip():
            report.empty += 1
        parsed.append(result)
        by_path[ref.path] = ref
    return parsed, by_path, refused


def _chunks_for(documents: list[_Parsed], binding: DocumentShareBinding) -> list[ChunkRecord]:
    """Chunk and embed every document that needs it, in one batch.

    One `embed_texts` call for the whole pass rather than one per document: the provider seam is a
    batch API, and a per-document call over a bounded chunk of a TB share is the difference between
    one request and a thousand.
    """
    pending: list[tuple[str, int, str, str]] = []
    for document in documents:
        for piece in chunk_document(
            document.text,
            chunk_chars=binding.chunk_chars,
            overlap_chars=binding.chunk_overlap_chars,
        ):
            pending.append((document.doc_id, piece.ordinal, piece.content, piece.coordinate))
    if not pending:
        return []
    embeddings = embed_texts([content for _, _, content, _ in pending])
    return [
        ChunkRecord(
            doc_id=doc_id,
            ordinal=ordinal,
            content=content,
            coordinate=coordinate,
            embedding=embedding,
        )
        for (doc_id, ordinal, content, coordinate), embedding in zip(
            pending, embeddings, strict=True
        )
    ]


async def sync_share(
    source: str,
    binding: DocumentShareBinding,
    index: DocumentIndex,
    *,
    after: str = "",
    limit: int = 1000,
) -> SyncReport:
    """Bring one bounded slice of the share into the index, starting past `after`.

    Args:
        source: The data-source name; the index partitions on it and the citations carry it.
        binding: The share's declared layout.
        index: Where chunks and file rows are stored.
        after: The mount-relative path the previous pass stopped at; `""` starts from the top.
        limit: How many candidate documents this pass may consider.

    Returns:
        What was indexed, deduplicated and skipped, plus the resume cursor and whether more remain.
        Pruning is *not* done here — see `prune_share`, which needs a whole drain to be safe.
    """
    crawl: CrawlResult = await asyncio.to_thread(crawl_share, binding, after=after, limit=limit)
    report = SyncReport(
        source=source,
        scanned=len(crawl.files),
        skipped_oversized=crawl.skipped_oversized,
        skipped_unsupported=dict(crawl.skipped_unsupported),
        failed_roots=list(crawl.failed_roots),
        cursor=crawl.cursor,
        has_more=crawl.has_more,
    )
    if not crawl.files:
        return report

    chunking = binding.chunking_key
    stored = await index.fingerprints(source, [ref.path for ref in crawl.files], chunking)
    changed = [ref for ref in crawl.files if stored.get(ref.path) != ref.fingerprint]
    unchanged = [ref.path for ref in crawl.files if stored.get(ref.path) == ref.fingerprint]
    report.unchanged = len(unchanged)
    # The mark half of the sweep, and its meaning is **"observed to exist"** — not "successfully
    # processed". Everything the walk saw goes in: files whose fingerprint matched, and files it
    # could see but not stat. Marking only what this pass handled is how a transient `EACCES` on a
    # subtree, or a lock on one document, turns into a deletion of rows whose files never moved.
    await index.touch(source, unchanged + crawl.unreadable)
    if not changed:
        return report

    parsed, by_path, refused = await _parse_changed(changed, report, binding.max_file_bytes)
    # Same rule: a file that was opened and refused is still on the share. Its fingerprint is
    # deliberately not stored (so the refusal stays visible in the counters, see above), but its
    # existence is, so the sweep leaves the row it already had alone.
    await index.touch(source, refused)
    if not parsed:
        return report

    # A document already carrying chunks needs no embedding — this is where four copies of one
    # report stop costing four times as much as one.
    key = embedding_config_key()
    known = await index.known_documents({document.doc_id for document in parsed}, key, chunking)
    unseen = {d.doc_id: d for d in parsed if d.doc_id not in known}
    fresh = list(unseen.values())
    # Counted as "files that cost no embedding", which is both duplicates *within* this pass and
    # content already on record from an earlier one. Counting only the latter reported zero for the
    # commonest case there is — the same report filed into two project folders on one crawl.
    report.deduplicated = len(parsed) - len(fresh)
    chunks = await asyncio.to_thread(_chunks_for, fresh, binding)
    report.embedded_chunks = len(chunks)

    files = [_file_record(source, by_path[d.ref_path], d.doc_id) for d in parsed]
    await index.upsert(files, chunks, key, chunking)
    report.indexed = len(files)
    return report


async def reembed_stale(index: DocumentIndex, limit: int = 500) -> ReembedReport:
    """Re-embed up to `limit` chunks whose vectors were made by a superseded configuration.

    **Reads the database, never the share.** The chunk's text was stored beside its vector, so
    changing the embedding model is a database-to-database operation: no crawl, no mount, no
    parse. That is what makes this cheap enough to run at the head of every scheduled sync rather
    than being a flag somebody has to remember at the moment they change a setting — and the
    failure it prevents is silent, so a flag would not have been run.

    Args:
        index: The document index to refresh.
        limit: How many chunks one pass may re-embed.

    Returns:
        The count refreshed and whether more stale chunks remain.
    """
    key = embedding_config_key()
    stale = await index.stale_chunks(key, limit)
    if not stale:
        return ReembedReport()
    try:
        embeddings = await asyncio.to_thread(embed_texts, [chunk.content for chunk in stale])
        refreshed = list(zip(stale, embeddings, strict=True))
        failed = 0
    except Exception:
        # **One chunk must not starve the whole corpus.** `stale_chunks` is deterministic — same
        # `ORDER BY`, same `LIMIT`, same first batch on every attempt — so a chunk the provider
        # refuses (an over-long hard split, a content refusal) failed this activity identically on
        # every retry. And this drain runs *ahead* of the crawl, so that one chunk stopped all
        # document indexing, for every share, permanently. Retrying per chunk isolates it: the rest
        # of the batch is refreshed, and only what genuinely cannot be embedded is left behind.
        logger.warning("batch re-embed failed; retrying %d chunk(s) individually", len(stale))
        refreshed, failed = await _reembed_individually(stale)
    if refreshed:
        await index.store_embeddings(
            [
                ChunkRecord(
                    doc_id=chunk.doc_id,
                    ordinal=chunk.ordinal,
                    content=chunk.content,
                    embedding=embedding,
                )
                for chunk, embedding in refreshed
            ],
            key,
        )
    logger.info("re-embedded %d chunk(s) under %s", len(refreshed), key)
    if failed:
        logger.error(
            "%d chunk(s) could not be re-embedded and keep a superseded vector; they are compared "
            "against queries embedded by the current model until this is fixed",
            failed,
        )
    # A full pass means there may be more — **but only if this pass made progress**. A batch where
    # every chunk failed would otherwise return the identical batch forever, which is the same wedge
    # one layer up.
    return ReembedReport(
        embedded=len(refreshed), failed=failed, has_more=len(stale) == limit and bool(refreshed)
    )


async def _reembed_individually(
    stale: list[StaleChunk],
) -> tuple[list[tuple[StaleChunk, list[float]]], int]:
    """Embed one chunk at a time so a single unembeddable one costs only itself."""
    refreshed: list[tuple[StaleChunk, list[float]]] = []
    failed = 0
    for chunk in stale:
        try:
            vector = await asyncio.to_thread(embed_texts, [chunk.content])
        except Exception as exc:
            logger.warning(
                "chunk %s#%d could not be embedded: %s", chunk.doc_id, chunk.ordinal, exc
            )
            failed += 1
            continue
        refreshed.append((chunk, vector[0]))
    return refreshed, failed


async def prune_share(
    source: str, index: DocumentIndex, started_at: datetime, report: SyncReport
) -> int:
    """Sweep index rows this run never saw — but only when the run actually saw the whole share.

    **This guard is the point of the function.** A CIFS mount that dropped, a root renamed by
    someone reorganizing the share, a permission change on one folder: each presents as "these
    files are not there", and sweeping on that evidence deletes a corpus that took days to build.

    It takes the **drain's own merged report** rather than a boolean the caller worked out, because
    there were two callers computing that boolean and only one of them was right: the durable
    workflow caught a wedged drain and the CLI did not, so `--limit 0` swept a source it had not
    looked at. Evidence a caller derives is a rule each caller can get wrong; evidence a caller
    *hands over* is one rule.

    Three ways a drain fails to be evidence of absence, all refusals:

    - **A root failed to walk.** Half a share is not a share.
    - **The drain never finished** (`has_more` still set). It stopped early or wedged, so the
      unvisited tail is unmarked and would sweep wholesale.
    - **It saw no candidates at all.** A detached CIFS volume leaves its mount point behind as an
      empty directory, and with `roots: [{path: "."}]` there is no missing root to notice. A share
      that is genuinely empty keeps stale rows until it has a file again, which is the harmless
      half of the trade this whole module is built on.

    Args:
        source: The data-source name whose rows may be swept.
        index: The index to sweep.
        started_at: When the run began; anything not restamped since is stale.
        report: The merged report for this source's whole drain.

    Returns:
        How many file rows were removed (zero whenever the drain is not evidence of absence).
    """
    refusal = (
        f"roots that could not be walked: {report.failed_roots}"
        if report.failed_roots
        else "the drain did not finish"
        if report.has_more
        else "it saw no candidate files at all"
        if report.scanned == 0
        else ""
    )
    if refusal:
        logger.warning(
            "%s: nothing is pruned — %s. An unreachable share and an empty one look identical "
            "from here, and of the two mistakes only re-indexing is recoverable",
            source,
            refusal,
        )
        return 0
    removed = await index.prune_stale(source, started_at)
    if removed:
        logger.info("%s: pruned %d file(s) no longer on the share", source, removed)
    return removed


def merge_reports(reports: list[SyncReport], source: str) -> SyncReport:
    """Fold a drain's per-chunk reports into one, so a run is described by a single number set."""
    unsupported: Counter[str] = Counter()
    merged = SyncReport(source=source)
    for report in reports:
        merged.scanned += report.scanned
        merged.indexed += report.indexed
        merged.unchanged += report.unchanged
        merged.deduplicated += report.deduplicated
        merged.embedded_chunks += report.embedded_chunks
        merged.pruned += report.pruned
        merged.skipped_scan += report.skipped_scan
        merged.skipped_unreadable += report.skipped_unreadable
        merged.skipped_oversized += report.skipped_oversized
        merged.empty += report.empty
        unsupported.update(report.skipped_unsupported)
        for root in report.failed_roots:
            if root not in merged.failed_roots:
                merged.failed_roots.append(root)
    merged.skipped_unsupported = dict(unsupported)
    merged.cursor = reports[-1].cursor if reports else ""
    merged.has_more = reports[-1].has_more if reports else False
    return merged
