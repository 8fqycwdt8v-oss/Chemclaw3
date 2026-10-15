"""Write knowledge notes from a directory of existing documents.

One note per document, verbatim, through `chemclaw.agent.attachments`' parsers and the normal note
write path. A deterministic transcription infers nothing, so there is nothing to review; summarizing
would put unreviewed paraphrases into the corpus.

Run: `python -m chemclaw.cli.backfill_corpus <directory> [--dry-run] [--tag PROJECT]`
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from chemclaw.agent.attachments import AttachmentError, parse_attachment
from chemclaw.core.config import settings
from chemclaw.core.ids import stable_hash
from chemclaw.core.logging import configure_logging
from chemclaw.core.tenancy import TenantError
from chemclaw.core.tenant_cli import add_tenant_flags, for_chosen_tenants, refusal
from chemclaw.kg.git_writer import BatchingNoteWriter, default_writer
from chemclaw.kg.note import Note
from chemclaw.kg.record import (
    NoteWrite,
    NoteWriter,
    WriteOutcome,
    count_notes_recorded,
    record_note,
)

logger = logging.getLogger(__name__)


class _Unbatched:
    """The git writer with nothing to flush: each note is committed, then mirrored, on its own.

    Under `kg_store=dual` a batch would commit after `record_note` had already written Postgres,
    leaving Postgres ahead of the tree if the commit failed.
    """

    def __init__(self, inner: NoteWriter) -> None:
        """Wrap `inner`."""
        self._inner = inner

    async def write(self, write: NoteWrite) -> WriteOutcome:
        """Commit this note now."""
        return await self._inner.write(write)

    async def flush(self) -> WriteOutcome:
        """Nothing is held, so nothing is committed."""
        return WriteOutcome(reference="", notes=0)


def note_for_document(path: Path, raw: bytes, tags: list[str]) -> Note:
    """Build the `report` note for one source document (idempotent id, verbatim body).

    The id derives from the content, not the filename, so a renamed or moved file does not mint a
    second note and a repeat run is free.
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
    """Write a note per readable document; return `(written, skipped)`.

    An unreadable or unsupported file is skipped with a WARNING, never fatal, so one bad PDF cannot
    abort a large backfill.
    """
    written = skipped = 0
    # A backfill batches commits; the conversational path does not, because there a queued note is
    # one a chemist cannot read yet. An operator command over existing documents has nobody waiting
    # mid-turn, and per-note commits are several times slower.
    submitter: BatchingNoteWriter | _Unbatched = (
        BatchingNoteWriter(default_writer(), settings.backfill_commit_batch_size)
        if settings.kg_store == "git"
        else _Unbatched(default_writer())
    )
    # Up to `batch_size - 1` notes are held in memory, so the trailing flush runs on both exits.
    # When the loop finished, the flush's failure is the run's failure and propagates. When the loop
    # is already unwinding, the flush is best-effort and logged, so it cannot replace the original
    # cause.
    try:
        for path in sorted(p for p in directory.rglob("*") if p.is_file()):
            try:
                note = note_for_document(path, path.read_bytes(), tags)
            except (AttachmentError, OSError) as exc:
                logger.warning("skipping %s: %s", path.name, exc)
                skipped += 1
                continue
            if dry_run:
                logger.info("would write %s from %s (%d chars)", note.id, path.name, len(note.body))
            else:
                reference = await record_note(note, submitter)
                # A batched write's reference is empty until its commit lands; the batch's reference
                # is logged at flush.
                logger.info(
                    "wrote %s from %s -> %s", note.id, path.name, reference or "(pending a batch)"
                )
            written += 1
    except BaseException:
        if not dry_run:
            try:
                await submitter.flush()
            except Exception:
                logger.exception(
                    "the final batch could not be committed either; its notes are not in git"
                )
        raise
    if not dry_run:
        outcome = await submitter.flush()
        # Booked here because the final partial batch is flushed by the driver, which `record_note`
        # never sees.
        count_notes_recorded(outcome)
        if outcome.written:
            logger.info("committed the final batch -> %s", outcome.reference)
    return written, skipped


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: walk a directory and write one note per readable document.

    Without `--dry-run`, `record_note` commits each note straight into `knowledge/` on the notes
    repository's base branch.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="Directory of documents to backfill.")
    parser.add_argument("--tag", action="append", default=[], help="Tag to apply (repeatable).")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be written without committing anything. Run this first.",
    )
    add_tenant_flags(parser, all_tenants=False)
    args = parser.parse_args(argv)
    configure_logging()
    if not args.directory.is_dir():
        print(f"not a directory: {args.directory}", file=sys.stderr)
        return 2
    try:
        done = asyncio.run(
            for_chosen_tenants(
                args,
                lambda _tenant: backfill(args.directory, tags=list(args.tag), dry_run=args.dry_run),
            )
        )
    except TenantError as exc:
        return refusal(exc)
    ((written, skipped),) = done.values()
    verb = "would write" if args.dry_run else "wrote"
    print(f"{verb} {written} note(s); skipped {skipped} unreadable/unsupported file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
