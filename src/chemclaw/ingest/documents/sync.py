"""Keep the index in step with the share: crawl, diff, parse, chunk, embed, sweep.

Backend-agnostic and dependency-injected; `chemclaw.durable.document_sync` does the scheduling and
bounding, so this loop runs end to end in a test with no database or broker.

- **Nothing is read twice.** A file whose `mtime_ns:size` matches the index is restamped as seen and
  not opened.
- **Nothing is embedded twice.** A document's identity is the hash of its parsed text, so copies
  share one set of chunks and a rename is free.
- **Nothing is deleted on doubt.** The sweep runs only after a crawl walked every root to
  completion; an unmounted share looks like an empty one.
- **Nothing is silently incomparable.** Each vector records its embedding configuration and its
  chunking; `reembed_stale` heals a model change from stored chunk text, and both crawl gates
  compare the chunking key.
- **Nothing is skipped silently.** Scans and unsupported formats are counted per extension and
  reported.
"""

import asyncio
import logging
import os
import time
from collections import Counter
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from chemclaw.core.config import settings
from chemclaw.core.embeddings import embed_texts, embedding_config_key
from chemclaw.core.ids import stable_hash
from chemclaw.core.logging import log_event
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.ingest.documents.binding import DocumentShareBinding
from chemclaw.ingest.documents.chunk import chunk_document
from chemclaw.ingest.documents.crawl import CrawlResult, FileRef, crawl_share
from chemclaw.ingest.documents.index import (
    ChunkRecord,
    DocumentIndex,
    FileRecord,
    StaleChunk,
)
from chemclaw.ingest.documents.isolate import ParseWorkerLost, parse_document_isolated
from chemclaw.ingest.documents.parse import (
    DocumentParseError,
    ScannedDocumentError,
)

logger = logging.getLogger(__name__)


@runtime_checkable
class DocumentShareSource(Protocol):
    """A data source that carries a crawlable share — the marker the sync job selects on.

    Structural rather than declared, so `CHEMCLAW_DATA_SOURCES` stays the only enable switch.
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
    # Still being read when the per-file bound expired. Separate from `skipped_unreadable`: this
    # says the budget ran out, not that the file is bad; see `_parse_changed`.
    skipped_timeout: int = 0
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
    # Chunks the provider would not embed even one at a time; they keep a superseded vector, so the
    # count is reported.
    failed: int = 0
    has_more: bool = False
    # The pass stopped with work left. `has_more` is gated on progress (so an all-failing batch is
    # not retried forever), which makes a total provider outage look like "up to date"; this is the
    # third state.
    stalled: bool = False


class _Parsed(BaseModel):
    """One file that was opened and read: its document identity and its text."""

    ref_path: str
    doc_id: str
    text: str


def _read_and_parse(ref: FileRef, max_bytes: int) -> _Parsed:
    """Read one file off the share and extract its text (blocking; called in a worker thread).

    The crawl's checks are re-done at open time, since the share is writable by its members:
    `O_NOFOLLOW` refuses a path that became a symlink, and the size is re-read from the open
    descriptor so a file that grew past `max_file_bytes` is refused.

    Raises:
        ScannedDocumentError: A PDF with no text layer.
        DocumentParseError: An unsupported format, one the library could not open, or one whose text
        carries a character the index cannot store.
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
    # In a killable child, not on this thread: Python cannot interrupt a parser, and a hostile
    # document would otherwise hold a shared executor thread. Same deadline as uploads
    # (`attachment_parse_timeout_seconds`), since it is the same work.
    parsed = parse_document_isolated(ref.path, raw, None, settings.attachment_parse_timeout_seconds)
    # Refused here, per file: a NUL survives decoding but Postgres refuses it in a `text` column,
    # and failing at `upsert` would abort the whole pass on every run. Same rule as
    # `ingest/eln/records._reject_unstorable`.
    if "\x00" in parsed.text:
        raise DocumentParseError(
            f"{ref.path} contains a NUL (0x00) byte at position {parsed.text.index(chr(0))}; a "
            "document is stored in a Postgres text column, which cannot hold one"
        )
    # The identity is the content, never the path, so copies collapse and a rename is free. An empty
    # extraction is salted with the path, otherwise every empty file on the share would be one
    # document; it has no chunks, so nothing is lost.
    identity = parsed.text if parsed.text.strip() else f"{ref.path}\x1fempty"
    return _Parsed(
        ref_path=ref.path, doc_id=f"doc-{stable_hash(identity, chars=16)}", text=parsed.text
    )


def _file_record(source: str, ref: FileRef, doc_id: str, chunking_key: str) -> FileRecord:
    """The index row for one path: its document, its cutting, its stat signature, its meaning."""
    return FileRecord(
        path=ref.path,
        source=source,
        doc_id=doc_id,
        fingerprint=ref.fingerprint,
        chunking_key=chunking_key,
        tags=list(ref.tags),
        modified_at=datetime.fromtimestamp(ref.mtime_ns / 1_000_000_000, tz=UTC),
    )


def _summarise_skips(what: str, reasons: Counter[str], example: str) -> None:
    """One WARNING for a pass's whole population of one kind of skip; nothing when there was none.

    Log volume must follow the pass, not the corpus: a systematic fault (a permission change, a
    broken parser) would otherwise be one line per file. Per-item lines go to DEBUG; this reports
    the count, the distinct reasons and one example path.
    """
    if not reasons:
        return
    logger.warning(
        "%s: %d file(s) skipped this pass — %s (e.g. %s)",
        what,
        sum(reasons.values()),
        ", ".join(f"{reason} x{count}" for reason, count in sorted(reasons.items())),
        example,
    )


async def _parse_changed(
    refs: list[FileRef], report: SyncReport, max_bytes: int
) -> tuple[list[_Parsed], dict[str, FileRef], list[str]]:
    """Read and parse each changed file, tallying every refusal rather than dropping it.

    Reject-and-continue: one unreadable file must not abort the pass. Each file is bounded by
    `attachment_parse_timeout_seconds` (the upload path's number, since it is the same work). The
    parse runs in a killable child (`isolate.py`), so the deadline ends the thread as well as the
    wait; the `wait_for` here covers the read off the mount and allows
    `attachment_parse_reap_grace_seconds` extra for forkserver start-up.

    Returns the parsed documents, their refs by path, and the paths that were refused but are still
    on the share, which the caller restamps so the sweep does not read them as deleted.
    """
    parsed: list[_Parsed] = []
    by_path: dict[str, FileRef] = {}
    refused: list[str] = []
    unreadable: Counter[str] = Counter()
    first_unreadable = ""
    timed_out: Counter[str] = Counter()
    first_timed_out = ""
    for ref in refs:
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(_read_and_parse, ref, max_bytes),
                timeout=(
                    settings.attachment_parse_timeout_seconds
                    + settings.attachment_parse_reap_grace_seconds
                ),
            )
        # A refused file gets no index row, so the next crawl opens it again. Deliberate: storing
        # its fingerprint would zero `skipped_scan` after the first run and hide how much of the
        # share is invisible. The cost is one read per refused file per cycle.
        except ScannedDocumentError:
            report.skipped_scan += 1
            refused.append(ref.path)
            continue
        # Before the `OSError` arm: `TimeoutError` subclasses `OSError`, so the other order would
        # file timeouts as unreadable. An `ETIMEDOUT` share read lands here too, which fits the
        # counter's meaning.
        except TimeoutError:
            logger.debug(
                "%s exceeded %ss; the pass moved on and its worker thread runs on",
                ref.path,
                settings.attachment_parse_timeout_seconds,
            )
            timed_out[TimeoutError.__name__] += 1
            first_timed_out = first_timed_out or ref.path
            report.skipped_timeout += 1
            refused.append(ref.path)
            continue
        # A killed parse is a timeout, not an unreadable document; `ParseWorkerLost` subclasses
        # `DocumentParseError`, so it needs its own arm.
        except ParseWorkerLost:
            logger.debug(
                "%s was still being read after %ss; its reader process was killed",
                ref.path,
                settings.attachment_parse_timeout_seconds,
            )
            timed_out[ParseWorkerLost.__name__] += 1
            first_timed_out = first_timed_out or ref.path
            report.skipped_timeout += 1
            refused.append(ref.path)
            continue
        except (DocumentParseError, OSError) as exc:
            # DEBUG per file, one WARNING for the pass — see `_summarise_skips`.
            logger.debug("skipping %s: %s", ref.path, exc)
            unreadable[type(exc).__name__] += 1
            first_unreadable = first_unreadable or ref.path
            report.skipped_unreadable += 1
            refused.append(ref.path)
            continue
        if not result.text.strip():
            report.empty += 1
        parsed.append(result)
        by_path[ref.path] = ref
    _summarise_skips("unreadable documents", unreadable, first_unreadable)
    _summarise_skips("documents past the read bound", timed_out, first_timed_out)
    return parsed, by_path, refused


def _chunks_for(documents: list[_Parsed], binding: DocumentShareBinding) -> list[ChunkRecord]:
    """Chunk and embed every document that needs it, in one batch.

    One `embed_texts` call for the whole pass, since the provider seam is a batch API.
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
    embeddings = embed_texts([content for _, _, content, _ in pending], cache=False)
    return [
        ChunkRecord(
            doc_id=doc_id,
            chunking_key=binding.chunking_key,
            ordinal=ordinal,
            content=content,
            coordinate=coordinate,
            embedding=embedding,
        )
        for (doc_id, ordinal, content, coordinate), embedding in zip(
            pending, embeddings, strict=True
        )
    ]


def _count_records(source: str, outcome: str, count: int) -> None:
    """Add `count` to this source's tally of one outcome.

    A named function rather than a lambda in a loop, which would bind the loop variable late.
    """
    record_metric(
        lambda m: m.increment(
            "chemclaw_ingest_records_total", count, {"source": source, "outcome": outcome}
        )
    )


def _record_pass(report: SyncReport, duration_s: float) -> None:
    """Emit the one record a sweep leaves behind: the whole report as fields, plus its duration.

    One structured line per bounded pass (not per file), so a scheduled sweep is observable without
    opening the workflow result.

    `chemclaw_ingest_records_total` partitions the candidates by what happened to the corpus:
    `ingested` has an index row (including deduplicated files), `rejected` was reached but could not
    be read (scan, permission error, timeout), and `skipped` was deliberately not processed
    (unchanged, over the size limit, unsupported format).
    """
    rejected = report.skipped_scan + report.skipped_unreadable + report.skipped_timeout
    skipped = report.unchanged + report.skipped_oversized + sum(report.skipped_unsupported.values())
    for outcome, count in (
        ("ingested", report.indexed),
        ("rejected", rejected),
        ("skipped", skipped),
    ):
        _count_records(report.source, outcome, count)
    log_event(
        logger,
        "ingest.finished",
        "%s: indexed=%d unchanged=%d deduplicated=%d rejected=%d in %.3fs",
        report.source,
        report.indexed,
        report.unchanged,
        report.deduplicated,
        rejected,
        duration_s,
        source=report.source,
        duration_s=round(duration_s, 3),
        next_cursor=report.cursor,
        scanned=report.scanned,
        indexed=report.indexed,
        unchanged=report.unchanged,
        deduplicated=report.deduplicated,
        embedded_chunks=report.embedded_chunks,
        pruned=report.pruned,
        skipped_scan=report.skipped_scan,
        skipped_unreadable=report.skipped_unreadable,
        skipped_timeout=report.skipped_timeout,
        skipped_oversized=report.skipped_oversized,
        empty=report.empty,
        skipped_unsupported=report.skipped_unsupported,
        failed_roots=report.failed_roots,
        has_more=report.has_more,
    )


async def sync_share(
    source: str,
    binding: DocumentShareBinding,
    index: DocumentIndex,
    *,
    after: str = "",
    limit: int = 1000,
) -> SyncReport:
    """Bring one bounded slice of the share into the index, starting past `after`, and record it.

    Every pass, including the early returns, leaves exactly one `ingest.finished` record
    (`_record_pass`), so a missing record means something.

    Args:
        source: The data-source name; the index partitions on it and the citations carry it.
        binding: The share's declared layout.
        index: Where chunks and file rows are stored.
        after: The mount-relative path the previous pass stopped at; `""` starts from the top.
        limit: How many candidate documents this pass may consider.

    Returns:
        What was indexed, deduplicated and skipped, plus the resume cursor and whether more remain.
        Pruning is not done here — see `prune_share`, which needs a whole drain to be safe.
    """
    started = time.perf_counter()
    report = await _index_slice(source, binding, index, after=after, limit=limit)
    _record_pass(report, time.perf_counter() - started)
    return report


async def _index_slice(
    source: str,
    binding: DocumentShareBinding,
    index: DocumentIndex,
    *,
    after: str,
    limit: int,
) -> SyncReport:
    """The pass itself — crawl, diff, parse, embed, upsert. See `sync_share` for the contract."""
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
    # The mark half of the sweep, meaning "observed to exist", not "processed": fingerprint matches
    # and entries seen but not stat'ed, so a transient `EACCES` never turns into a deletion.
    await index.touch(source, unchanged + crawl.unreadable)
    # Summarised once rather than logged per entry (`_summarise_skips`); `OSError` is the only
    # reason an entry lands here.
    _summarise_skips(
        "unreadable share entries",
        Counter({"OSError": len(crawl.unreadable)}) if crawl.unreadable else Counter(),
        crawl.unreadable[0] if crawl.unreadable else "",
    )
    if not changed:
        return report

    parsed, by_path, refused = await _parse_changed(changed, report, binding.max_file_bytes)
    # A refused file is still on the share: its fingerprint is not stored, but its existence is
    # marked so the sweep keeps its row.
    await index.touch(source, refused)
    if not parsed:
        return report

    # A document already carrying chunks needs no embedding — this is where four copies of one
    # report stop costing four times as much as one.
    key = embedding_config_key()
    known = await index.known_documents({document.doc_id for document in parsed}, key, chunking)
    unseen = {d.doc_id: d for d in parsed if d.doc_id not in known}
    fresh = list(unseen.values())
    # Files that cost no embedding: duplicates within this pass as well as content already on
    # record.
    report.deduplicated = len(parsed) - len(fresh)
    chunks = await asyncio.to_thread(_chunks_for, fresh, binding)
    report.embedded_chunks = len(chunks)

    files = [_file_record(source, by_path[d.ref_path], d.doc_id, chunking) for d in parsed]
    await index.upsert(files, chunks, key)
    report.indexed = len(files)
    return report


async def reembed_stale(
    index: DocumentIndex, chunkings: set[str], limit: int = 500
) -> ReembedReport:
    """Re-embed up to `limit` chunks whose vectors were made by a superseded configuration.

    Reads stored chunk text, never the share, so it is cheap enough to run at the head of every sync
    and a model change heals itself. Rows cut under a chunking no enabled share uses are skipped,
    since the crawl will re-cut and re-embed them; `chunkings` is passed in because the caller owns
    which shares are enabled.

    Args:
        index: The document index to refresh.
        chunkings: The chunking keys of the currently enabled shares.
        limit: How many chunks one pass may re-embed.

    Returns:
        The count refreshed and whether more stale chunks remain.
    """
    key = embedding_config_key()
    stale = await index.stale_chunks(key, limit, chunkings)
    if not stale:
        return ReembedReport()
    try:
        embeddings = await asyncio.to_thread(
            lambda: embed_texts([chunk.content for chunk in stale], cache=False)
        )
        refreshed = list(zip(stale, embeddings, strict=True))
        failed = 0
    except Exception:
        # One chunk must not starve the corpus: `stale_chunks` returns the same first batch every
        # time and this drain runs before the crawl, so a batch failure is retried per chunk and
        # only the unembeddable ones are left behind.
        logger.warning("batch re-embed failed; retrying %d chunk(s) individually", len(stale))
        refreshed, failed = await _reembed_individually(stale)
    if refreshed:
        await index.store_embeddings(
            [
                ChunkRecord(
                    doc_id=chunk.doc_id,
                    chunking_key=chunk.chunking_key,
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
    # More may remain only if this pass made progress; otherwise the same failing batch would repeat
    # forever. `stalled` reports that stale rows remain.
    stalled = bool(stale) and not refreshed
    if stalled:
        logger.error(
            "re-embedding made no progress: all %d chunk(s) in this batch failed, so the drain "
            "stops here with stale vectors still in the index. Until this is fixed the affected "
            "chunks are compared against queries embedded by the current model",
            len(stale),
        )
    return ReembedReport(
        embedded=len(refreshed),
        failed=failed,
        has_more=len(stale) == limit and bool(refreshed),
        stalled=stalled,
    )


async def _reembed_individually(
    stale: list[StaleChunk],
) -> tuple[list[tuple[StaleChunk, list[float]]], int]:
    """Embed one chunk at a time so a single unembeddable one costs only itself.

    Distinct failure reasons are summarised once at WARNING (as in `_summarise_skips`), so an
    unreachable provider is distinguishable from individually unembeddable content; per-chunk lines
    stay at DEBUG.
    """
    refreshed: list[tuple[StaleChunk, list[float]]] = []
    reasons: Counter[str] = Counter()
    for chunk in stale:
        try:
            vector = await asyncio.to_thread(embed_texts, [chunk.content])
        except Exception as exc:
            logger.debug("chunk %s#%d could not be embedded: %s", chunk.doc_id, chunk.ordinal, exc)
            reasons[type(exc).__name__] += 1
            continue
        refreshed.append((chunk, vector[0]))
    if reasons:
        logger.warning(
            "re-embedding: %d chunk(s) failed individually — %s",
            sum(reasons.values()),
            ", ".join(f"{reason} x{count}" for reason, count in sorted(reasons.items())),
        )
    return refreshed, sum(reasons.values())


async def prune_share(
    source: str, index: DocumentIndex, started_at: datetime, report: SyncReport
) -> int:
    """Sweep index rows this run never saw — but only when the run actually saw the whole share.

    A dropped mount, a renamed root or a permission change all look like "these files are gone", so
    the guard is the point. It takes the drain's merged report rather than a caller-computed
    boolean, so every caller applies one rule. Refused when:

    - **A root failed to walk.**
    - **The drain never finished** (`has_more` still set), so the unvisited tail is unmarked.
    - **It saw no candidates at all.** A detached volume leaves an empty mount point; a genuinely
      empty share keeps stale rows until it has a file again.

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
        merged.skipped_timeout += report.skipped_timeout
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
