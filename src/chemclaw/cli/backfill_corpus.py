"""Write knowledge notes from a directory of existing documents (gap IDEA-6).

The only ingestion path was the incremental, cursored ELN sync. A real deployment arrives with a
decade of existing reports, SOPs and filings, and its first question is "make our existing documents
answerable" — so the day-one experience of a correctly-installed Chemclaw was an empty graph.

This is the batch driver. It reuses `chemclaw.agent.attachments`' parsers verbatim (one parsing
implementation, not a second one that could drift) and routes every document through the same write
path as every other machine-written note.

**What makes it safe to run over a decade of documents changed, and the answer it changed to is the
better one.** This paragraph used to say "a backfill proposes, humans review — nothing lands in the
graph unreviewed", which stopped being true when the PR-gate was deleted
(`D-2026-09-05-the-gate-follows-behaviour-not-knowledge`), and would have been an unreviewed dump of
thousands of notes if the safety had really rested there. It does not, and the reason is one
paragraph down: this is a **deterministic transcription**, one note per document, verbatim. It
infers nothing, so there is nothing for a reviewer to decide — the argument
`D-2026-08-25-an-eln-transcription-is-data-not-a-claim` makes about an ELN entry, applied to a PDF.
A backfill that summarized would be a different thing entirely and would need a different argument,
which is exactly why it does not.

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
from chemclaw.core.config import settings
from chemclaw.core.ids import stable_hash
from chemclaw.core.logging import configure_logging
from chemclaw.kg.git_writer import BatchingNoteWriter, default_writer
from chemclaw.kg.note import Note
from chemclaw.kg.record import record_note

logger = logging.getLogger(__name__)


def note_for_document(path: Path, raw: bytes, tags: list[str]) -> Note:
    """Build the `report` note for one source document (idempotent id, verbatim body).

    The id is derived from the *content*, not the filename: re-running a backfill after a file is
    renamed or moved must not mint a second note for the same document, and a byte-identical
    rewrite of an existing note then makes a repeat run genuinely free.
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

    An unreadable or unsupported file is skipped with a WARNING, never fatal: a decade of documents
    will contain formats this cannot parse, and one PDF must not abort a backfill of ten thousand
    files (the reject-and-continue discipline the ELN sync uses).
    """
    written = skipped = 0
    # **A backfill batches and the conversational path does not**, which is the whole of what
    # `docs/planning/BACKLOG.md` meant by "a backfill and an incremental sync want different write
    # shapes". Measured: one commit and one push per note is 140.8 ms against a local remote on an
    # empty corpus and 327.3 ms at a 10,000-note corpus against a real one, against 31.6 ms per note
    # at ten to a commit and 8.5 at fifty. `D-2026-09-13-the-lock-is-not-the-bound-the-commit-is`
    # declined batching for the *conversational* path on the product — a queued note is one a
    # chemist cannot read yet — and that argument does not reach an operator command over a
    # directory of existing documents, where nobody is mid-turn and the wait is for the whole run.
    submitter = BatchingNoteWriter(default_writer(), settings.backfill_commit_batch_size)
    # **The trailing flush runs on both exits, and only one of them may swallow it.** Up to
    # `batch_size - 1` notes are held in memory at every instant, so the flush shipped as a bare
    # statement after the loop lost them to anything the inner `except` does not catch — a git
    # failure, a `psycopg` error, a `KeyboardInterrupt` — *after* `written` had counted them and
    # the log had reported each as written "(pending a batch)".
    #
    # **The first repair put it in a `finally` with a blanket `except`, and that was worse.**
    # Measured through the real CLI with a failing inner writer: the loop completed, the flush
    # raised, the exception was logged and swallowed, and `main` printed `wrote 4 note(s)` and
    # returned **0** with nothing in git — the exact "reporting it as written" failure the
    # paragraph above exists to prevent, now on the *common* path (a push rejection, an auth
    # failure, a hook). Pre-fix that case at least exited non-zero.
    #
    # So the two exits are separated. When the loop finished, the flush is the last thing that can
    # fail and its failure **is** the run's failure: it propagates. When the loop is already
    # unwinding, the flush is best-effort — the run is ending badly, the original cause is the one
    # an operator needs, and a flush that also raises would replace it — but it is still attempted
    # and still logged, because dropping the batch in silence is what started all of this.
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
                # A batched write's reference is empty until its commit lands, so the per-note line
                # says what it can: the note, its source, and that the commit is still pending. The
                # batch's own reference is logged when it flushes.
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
        if outcome.written:
            logger.info("committed the final batch -> %s", outcome.reference)
    return written, skipped


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: walk a directory and write one note per readable document.

    **Not a proposal, and this said it was.** The summary line read "PR-gate one note per readable
    document" and `--dry-run`'s help promised a run that opened no branch, both left behind by
    `D-2026-09-05-the-gate-follows-behaviour-not-knowledge`: `record_note` commits onto the notes
    repository's base branch, so a bare invocation writes one note per document straight into
    `knowledge/`. That sentence is the one an operator reads while deciding whether the non-dry-run
    is safe, which is why it is worth more than a wording fix.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="Directory of documents to backfill.")
    parser.add_argument("--tag", action="append", default=[], help="Tag to apply (repeatable).")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be written without committing anything. Run this first.",
    )
    args = parser.parse_args(argv)
    configure_logging()
    if not args.directory.is_dir():
        print(f"not a directory: {args.directory}", file=sys.stderr)
        return 2
    written, skipped = asyncio.run(
        backfill(args.directory, tags=list(args.tag), dry_run=args.dry_run)
    )
    verb = "would write" if args.dry_run else "wrote"
    print(f"{verb} {written} note(s); skipped {skipped} unreadable/unsupported file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
