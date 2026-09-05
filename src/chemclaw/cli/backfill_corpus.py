"""Propose knowledge notes from a directory of existing documents (gap IDEA-6).

The only ingestion path was the incremental, cursored ELN sync. A real deployment arrives with a
decade of existing reports, SOPs and filings, and its first question is "make our existing documents
answerable" — so the day-one experience of a correctly-installed Chemclaw was an empty graph.

This is the batch driver. It reuses `chemclaw.agent.attachments`' parsers verbatim (one parsing
implementation, not a second one that could drift) and routes every document through the **same
PR-gate** as every other machine-written note: a backfill proposes, humans review. That is the whole
reason this is safe to run over a decade of documents — nothing lands in the graph unreviewed.

**Deliberately one note per document, verbatim.** No summarizing, no fact extraction, no chunking.
A backfill's job is to make existing documents *reachable*; deciding what they *mean* is the
retrieval and synthesis layers' job, and an LLM-summarized backfill would put thousands of
unreviewed paraphrases into the corpus — the fastest way to make a knowledge graph untrustworthy.

Run: `python -m chemclaw.cli.backfill_corpus <directory> [--dry-run] [--tag PROJECT]`
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from chemclaw.agent.attachments import AttachmentError, parse_attachment
from chemclaw.core.ids import stable_hash
from chemclaw.core.logging import configure_logging
from chemclaw.kg.git_writer import default_writer
from chemclaw.kg.note import Note
from chemclaw.kg.record import record_note

logger = logging.getLogger(__name__)


def note_for_document(path: Path, raw: bytes, tags: list[str]) -> Note:
    """Build the `report` note for one source document (idempotent id, verbatim body).

    The id is derived from the *content*, not the filename: re-running a backfill after a file is
    renamed or moved must not mint a second note for the same document, and the PR-gate's
    byte-identical no-op then makes a repeat run genuinely free.
    """
    attachment = parse_attachment(path.name, raw)
    return Note(
        id=f"doc-{stable_hash(attachment.text, chars=12)}",
        type="report",
        created_by="agent",
        source=f"backfill:{path.name}",
        tags=tags,
        body=f"Backfilled from `{path.name}`.\n\n{attachment.text}",
    )


async def backfill(directory: Path, *, tags: list[str], dry_run: bool) -> tuple[int, int]:
    """Propose a note per readable document; return `(proposed, skipped)`.

    An unreadable or unsupported file is skipped with a WARNING, never fatal: a decade of documents
    will contain formats this cannot parse, and one PDF must not abort a backfill of ten thousand
    files (the reject-and-continue discipline the ELN sync uses).
    """
    proposed = skipped = 0
    submitter = default_writer()
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        try:
            note = note_for_document(path, path.read_bytes(), tags)
        except (AttachmentError, OSError) as exc:
            logger.warning("skipping %s: %s", path.name, exc)
            skipped += 1
            continue
        if dry_run:
            logger.info("would propose %s from %s (%d chars)", note.id, path.name, len(note.body))
        else:
            reference = await record_note(note, submitter)
            logger.info("proposed %s from %s -> %s", note.id, path.name, reference)
        proposed += 1
    return proposed, skipped


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: walk a directory and PR-gate one note per readable document."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="Directory of documents to backfill.")
    parser.add_argument("--tag", action="append", default=[], help="Tag to apply (repeatable).")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be proposed without opening any branch. Run this first.",
    )
    args = parser.parse_args(argv)
    configure_logging()
    if not args.directory.is_dir():
        print(f"not a directory: {args.directory}", file=sys.stderr)
        return 2
    proposed, skipped = asyncio.run(
        backfill(args.directory, tags=list(args.tag), dry_run=args.dry_run)
    )
    verb = "would propose" if args.dry_run else "proposed"
    print(f"{verb} {proposed} note(s); skipped {skipped} unreadable/unsupported file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
