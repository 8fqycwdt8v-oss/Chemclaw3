"""`make kg-import`: load the notes directory into the Postgres note store, idempotently.

Run: `python -m chemclaw.cli.kg_import [DIR] [--tenant T] [--dry-run] [--json]`.

Exit codes: 0 every note file was imported or was already there; 1 some were not (unparseable,
a second file for one id, a note the store refuses, a database error on that note; all named); 2 the
import could not run (no such directory, database unreachable, or the directory is a checkout that
is not at the remote's tip: it would copy old notes over newer ones; `--allow-stale` is for a
directory that is a copy, not the deployment's clone). The logic is `kg.importer`.
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from chemclaw.core.config import settings
from chemclaw.core.db import pooling
from chemclaw.core.logging import configure_logging
from chemclaw.kg.git_writer import GitNoteWriter
from chemclaw.kg.importer import ImportSummary, import_notes
from chemclaw.kg.rows import DEFAULT_TENANT


async def _run(source: Path, tenant: str, dry_run: bool) -> ImportSummary:
    async with pooling():
        return await import_notes(source, tenant=tenant, dry_run=dry_run)


def _print(summary: ImportSummary) -> None:
    verb = "would write" if summary.dry_run else "wrote"
    print(
        f"{verb} {summary.written} note(s) ({summary.created} new, {summary.updated} changed); "
        f"{summary.unchanged} already there; tenant {summary.tenant!r}, from {summary.source}"
    )
    for note_id, reason in summary.skipped:
        print(f"SKIPPED {note_id}: {reason}")
    for file, reason in summary.unparseable:
        print(f"UNPARSEABLE {file}: {reason}")
    if summary.index_keys_linked is not None:
        print(f"linked {summary.index_keys_linked} note_index row(s) to their notes")


def main(argv: Sequence[str] | None = None) -> int:
    """Import the directory, print the summary, return the exit code."""
    parser = argparse.ArgumentParser(prog="python -m chemclaw.cli.kg_import", description=__doc__)
    parser.add_argument(
        "source",
        nargs="?",
        type=Path,
        default=None,
        help=f"the notes directory (default: {settings.knowledge_path}).",
    )
    parser.add_argument("--tenant", default=DEFAULT_TENANT, help="the tenant to import into.")
    parser.add_argument("--dry-run", action="store_true", help="report; write nothing.")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON.")
    parser.add_argument(
        "--allow-stale",
        action="store_true",
        help="import even if the directory is a checkout that is not at the remote's tip.",
    )
    options = parser.parse_args(argv)
    source = options.source if options.source is not None else settings.knowledge_path
    if not source.is_dir():
        print(f"not a directory: {source}", file=sys.stderr)
        return 2
    configure_logging()
    try:
        lagging = (
            None
            if options.allow_stale
            else asyncio.run(GitNoteWriter(repo_dir=str(source)).lagging_reason())
        )
        if lagging is not None:
            print(f"the import was refused: {lagging}", file=sys.stderr)
            return 2
        summary = asyncio.run(_run(source, options.tenant, options.dry_run))
    except Exception as exc:
        print(f"the import could not run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if options.json:
        print(json.dumps(summary.as_json(), indent=2))
    else:
        _print(summary)
    return 0 if summary.complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
