"""Crawl a mounted document share from the terminal — and, first, cost it.

The scheduled job (`chemclaw.durable.document_sync`) is the production path. This exists for the
step before it: a TB share with 500k files is an embedding bill, and nobody should discover its
size by watching it arrive. `--dry-run` walks the share exactly as the real crawl does — the same
binding, the same filters, the same total order — reads nothing, and reports what *would* be
indexed, what would be turned away, and per extension.

Run it against the real mount before enabling the source. The `.doc` count alone usually changes
which roots a deployment starts with.

    python -m chemclaw.cli.sync_share sharedrive --dry-run
    python -m chemclaw.cli.sync_share sharedrive
"""

import argparse
import asyncio
import logging
import sys

from chemclaw.core.logging import configure_logging
from chemclaw.ingest.documents.binding import DocumentShareBinding, DocumentShareError
from chemclaw.ingest.documents.crawl import crawl_share
from chemclaw.ingest.documents.index import default_document_index
from chemclaw.ingest.documents.sync import (
    DocumentShareSource,
    SyncReport,
    merge_reports,
    prune_share,
    sync_share,
)
from chemclaw.ingest.sources.registry import active_retrieve_sources

logger = logging.getLogger(__name__)

# Enough to be a real sample of a large share, small enough to run in a coffee break.
_DRY_RUN_LIMIT = 100_000


def _resolve(name: str) -> DocumentShareSource:
    """Find the enabled data source carrying this share, or say what is actually enabled."""
    shares = {
        source.name: source
        for source in active_retrieve_sources()
        if isinstance(source, DocumentShareSource)
    }
    share = shares.get(name)
    if share is None:
        raise DocumentShareError(
            f"no enabled data source named {name!r} carries a document share; "
            f"enabled shares: {sorted(shares) or 'none'} "
            "(a source is enabled by naming it in CHEMCLAW_DATA_SOURCES)"
        )
    return share


def _estimate(binding: DocumentShareBinding, report: SyncReport) -> str:
    """Report what a real run would cost, in the unit that is actually billed: chunks.

    An estimate, and labelled as one — the true chunk count depends on how much text each document
    holds, which cannot be known without reading them, which is the thing a dry run refuses to do.
    """
    lines = [
        f"candidates:        {report.scanned}",
        f"over size limit:   {report.skipped_oversized}",
        "unreadable formats:",
    ]
    lines += [
        f"  {extension:10s} {count}"
        for extension, count in sorted(
            report.skipped_unsupported.items(), key=lambda item: -item[1]
        )
    ]
    # Documents are chunked at `chunk_chars`; a typical report yields single digits. This is the
    # order of magnitude a deployment needs before it decides which roots to start with.
    lines.append(
        f"\nembedding calls a first full run would make: roughly {report.scanned} to "
        f"{report.scanned * 10} chunks (documents are cut at {binding.chunk_chars} characters, "
        "and identical copies collapse to one)."
    )
    if report.has_more:
        lines.append(f"\nstopped after {_DRY_RUN_LIMIT} entries — the share is larger than this.")
    return "\n".join(lines)


async def _drain(name: str, share: DocumentShareSource, *, limit: int) -> SyncReport:
    """Run the real sync to completion, then sweep — the CLI mirror of the durable workflow."""
    index = default_document_index()
    started_at = await index.clock()
    reports: list[SyncReport] = []
    after = ""
    while True:
        report = await sync_share(name, share.share_binding(), index, after=after, limit=limit)
        reports.append(report)
        logger.info("%s: %s", name, report.model_dump_json(exclude_defaults=True))
        if not report.has_more or report.cursor <= after:
            break
        after = report.cursor
    merged = merge_reports(reports, name)
    merged.pruned = await prune_share(name, index, started_at, not merged.failed_roots)
    return merged


def main(argv: list[str] | None = None) -> int:
    """CLI: cost a mounted share, or crawl it into the document index."""
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("source", help="The enabled data-source name carrying the share.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Walk the share and report what would be indexed. Reads no file and embeds nothing. "
        "Run this first.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="Documents per pass (ignored by --dry-run).",
    )
    args = parser.parse_args(argv)
    configure_logging()
    try:
        share = _resolve(args.source)
        binding = share.share_binding()
        if args.dry_run:
            crawl = crawl_share(binding, limit=_DRY_RUN_LIMIT)
            report = SyncReport(
                source=args.source,
                scanned=len(crawl.files),
                skipped_oversized=crawl.skipped_oversized,
                skipped_unsupported=dict(crawl.skipped_unsupported),
                failed_roots=list(crawl.failed_roots),
                has_more=crawl.has_more,
            )
            print(_estimate(binding, report))
        else:
            print(asyncio.run(_drain(args.source, share, limit=args.limit)).model_dump_json())
    except DocumentShareError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
