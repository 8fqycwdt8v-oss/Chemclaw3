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
the next run without the share being touched at all.

**And nothing is skipped silently.** A decade-old share is full of scanned PDFs and of `.doc` files
this system cannot read. Both are counted, per extension, and reported. Silence would be read as
"the share held nothing else", which is the one answer that is never true.
"""

import asyncio
import logging
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from chemclaw.core.embeddings import embed_texts, embedding_config_key
from chemclaw.core.ids import stable_hash
from chemclaw.ingest.documents.binding import DocumentShareBinding
from chemclaw.ingest.documents.chunk import chunk_document
from chemclaw.ingest.documents.crawl import CrawlResult, FileRef, crawl_share
from chemclaw.ingest.documents.index import ChunkRecord, DocumentIndex, FileRecord
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
    has_more: bool = False


class _Parsed(BaseModel):
    """One file that was opened and read: its document identity and its text."""

    ref_path: str
    doc_id: str
    text: str


def _read_and_parse(ref: FileRef) -> _Parsed:
    """Read one file off the share and extract its text (blocking; called in a worker thread).

    Raises:
        ScannedDocumentError: A PDF with no text layer.
        DocumentParseError: An unsupported format, or one the library could not open.
        OSError: The share could not be read at this path.
    """
    raw = Path(ref.absolute).read_bytes()
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
    refs: list[FileRef], report: SyncReport
) -> tuple[list[_Parsed], dict[str, FileRef]]:
    """Read and parse each changed file, tallying every refusal rather than dropping it.

    Reject-and-continue, the discipline the ELN sync uses: one unreadable PDF must not abort a pass
    over ten thousand files.
    """
    parsed: list[_Parsed] = []
    by_path: dict[str, FileRef] = {}
    for ref in refs:
        try:
            result = await asyncio.to_thread(_read_and_parse, ref)
        # A refused file gets **no index row**, so its fingerprint is not stored and the next crawl
        # opens it again. That is a deliberate trade, not an oversight: recording it would make the
        # file look unchanged forever, and `skipped_scan` would then read zero on every run after
        # the first — losing exactly the number that tells an operator how much of the share is
        # invisible. The cost is one read per refused file per cycle and no embedding at all; the
        # alternative costs the measurement. `docs/planning/BACKLOG.md` carries the row for
        # revisiting it if a share's refused population makes that read volume material.
        except ScannedDocumentError:
            report.skipped_scan += 1
            continue
        except (DocumentParseError, OSError) as exc:
            logger.warning("skipping %s: %s", ref.path, exc)
            report.skipped_unreadable += 1
            continue
        if not result.text.strip():
            report.empty += 1
        parsed.append(result)
        by_path[ref.path] = ref
    return parsed, by_path


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

    stored = await index.fingerprints(source, [ref.path for ref in crawl.files])
    changed = [ref for ref in crawl.files if stored.get(ref.path) != ref.fingerprint]
    unchanged = [ref.path for ref in crawl.files if stored.get(ref.path) == ref.fingerprint]
    report.unchanged = len(unchanged)
    # The mark half of the sweep: an untouched file must still count as seen, or the next complete
    # crawl would prune the entire unchanged corpus.
    await index.touch(source, unchanged)
    if not changed:
        return report

    parsed, by_path = await _parse_changed(changed, report)
    if not parsed:
        return report

    # A document already carrying chunks needs no embedding — this is where four copies of one
    # report stop costing four times as much as one.
    key = embedding_config_key()
    known = await index.known_documents({document.doc_id for document in parsed}, key)
    unseen = {d.doc_id: d for d in parsed if d.doc_id not in known}
    fresh = list(unseen.values())
    # Counted as "files that cost no embedding", which is both duplicates *within* this pass and
    # content already on record from an earlier one. Counting only the latter reported zero for the
    # commonest case there is — the same report filed into two project folders on one crawl.
    report.deduplicated = len(parsed) - len(fresh)
    chunks = await asyncio.to_thread(_chunks_for, fresh, binding)
    report.embedded_chunks = len(chunks)

    files = [_file_record(source, by_path[d.ref_path], d.doc_id) for d in parsed]
    await index.upsert(files, chunks, key)
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
    embeddings = await asyncio.to_thread(embed_texts, [chunk.content for chunk in stale])
    await index.store_embeddings(
        [
            ChunkRecord(
                doc_id=chunk.doc_id,
                ordinal=chunk.ordinal,
                content=chunk.content,
                embedding=embedding,
            )
            for chunk, embedding in zip(stale, embeddings, strict=True)
        ],
        key,
    )
    logger.info("re-embedded %d chunk(s) under %s", len(stale), key)
    # A full pass means there may be more; a short one means the corpus is current.
    return ReembedReport(embedded=len(stale), has_more=len(stale) == limit)


async def prune_share(
    source: str, index: DocumentIndex, started_at: datetime, crawl_was_complete: bool
) -> int:
    """Sweep index rows this run never saw — but only when the run actually saw the whole share.

    **This guard is the point of the function.** A CIFS mount that dropped, a root renamed by
    someone reorganizing the share, a permission change on one folder: each presents as "these
    files are not there", and sweeping on that evidence deletes a corpus that took days to build.
    So the caller must have drained every root to completion with no failure, and this refuses
    rather than trusts.

    Args:
        source: The data-source name whose rows may be swept.
        index: The index to sweep.
        started_at: When the run began; anything not restamped since is stale.
        crawl_was_complete: Whether every root was walked to the end without error.

    Returns:
        How many file rows were removed (zero when the crawl was incomplete).
    """
    if not crawl_was_complete:
        logger.warning(
            "%s: crawl did not complete, so nothing is pruned — an unreachable share and an empty "
            "one look identical from here",
            source,
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
