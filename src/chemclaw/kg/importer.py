"""Loading a directory of Markdown notes into the Postgres store, once or again.

The import is the way a corpus that already exists gets into `kg_notes`: the notes of the shipped
`knowledge/` tree, a deployment's checkout before `kg_store=dual` is switched on, or the disaster
recovery of the store from its tree. Every note is a `put` of its own, in id order, so a run
stopped half way leaves whole notes and the next run finishes it; a repeat changes nothing.

Invariants: memory is one note at a time (the paths are listed, the notes are not); a note keeps
the date its file arrived in the tree as its `created_at`; a human's note goes in as a subject, so
it keeps its author; what is not imported is named, never counted away.
"""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chemclaw.kg.graph import note_arrivals, scan_notes_dir
from chemclaw.kg.note import NoteError, read_note
from chemclaw.kg.postgres_store import PostgresNoteStore
from chemclaw.kg.rows import DEFAULT_TENANT, canonical_note, content_sha
from chemclaw.kg.store import NoteUnit


@dataclass
class ImportSummary:
    """What an import did, with every note it did not import named and the reason.

    `created` and `updated` are notes that received a revision; `unchanged` already held exactly
    that content. `skipped` is `(note id, reason)`: a second file claiming an id, or a note the
    store refuses. `unparseable` is `(file, reason)`. `index_keys_linked` is `None` for a dry run.
    """

    source: str
    tenant: str
    dry_run: bool
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    not_notes: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    unparseable: list[tuple[str, str]] = field(default_factory=list)
    index_keys_linked: int | None = None

    @property
    def written(self) -> int:
        """Notes that received a revision (a dry run: that would)."""
        return self.created + self.updated

    @property
    def complete(self) -> bool:
        """Whether every note file was imported or was already there."""
        return not self.skipped and not self.unparseable

    def as_json(self) -> dict[str, Any]:
        """The summary as plain JSON values."""
        return {
            "source": self.source,
            "tenant": self.tenant,
            "dry_run": self.dry_run,
            "complete": self.complete,
            "written": self.written,
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "not_notes": self.not_notes,
            "skipped": [{"id": i, "reason": why} for i, why in self.skipped],
            "unparseable": [{"file": f, "reason": why} for f, why in self.unparseable],
            "index_keys_linked": self.index_keys_linked,
        }


async def import_notes(
    source: Path, *, tenant: str = DEFAULT_TENANT, dry_run: bool = False
) -> ImportSummary:
    """Put every note under `source` into `tenant`'s store in id order; link the note index to them.

    A dry run reads the store's digests and writes nothing; it does not predict a refusal (an
    agent's note over a human's), which only the write can know. A second file for an id already
    taken is skipped, as the graph skips it (first in path order wins). The note index is linked
    after the last note, for every note the tenant holds, so a repeat also links notes recorded
    since.
    """
    store = PostgresNoteStore(tenant)
    summary = ImportSummary(source=str(source), tenant=tenant, dry_run=dry_run)
    arrivals = await asyncio.to_thread(note_arrivals, source)
    # Read before any note is attempted: a database that is not there is the run's failure, while
    # one that fails on a single note is that note's.
    await store.signature()
    held = await store.fingerprints() if dry_run else {}
    # Id order; files of one id stay in path order, as the graph resolves a duplicate.
    files = sorted((path for path, _ in scan_notes_dir(source)), key=lambda p: (p.stem, p))
    claimed: dict[str, Path] = {}
    for path in files:
        try:
            note = read_note(path)
        except NoteError as exc:
            summary.unparseable.append((str(path), str(exc)))
            continue
        if note is None:
            summary.not_notes += 1
            continue
        if note.id in claimed:
            summary.skipped.append(
                (note.id, f"another file already defines it: {claimed[note.id]}")
            )
            continue
        claimed[note.id] = path
        try:
            if dry_run:
                digest = content_sha(canonical_note(note))
                if note.id not in held:
                    summary.created += 1
                elif held[note.id] == digest:
                    summary.unchanged += 1
                else:
                    summary.updated += 1
                continue
            outcome = await store.put(NoteUnit(note, arrived=arrivals.get(note.id)))
        except Exception as exc:
            # One note's failure (a refusal, or a database error on that statement) does not stop
            # the notes after it: the run is incomplete, and says which.
            summary.skipped.append((note.id, f"{type(exc).__name__}: {exc}"))
            continue
        if not outcome.written:
            summary.unchanged += 1
        elif outcome.revisions[note.id] == 1:
            summary.created += 1
        else:
            summary.updated += 1
    if not dry_run:
        summary.index_keys_linked = await store.link_note_index()
    return summary
