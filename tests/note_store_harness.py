"""The stores `tests/test_note_store.py` and `tests/test_postgres_note_store.py` hold to one suite.

A harness builds an empty store of one kind and puts notes in through that store's own write: the
git writer on a real repository for `GitNoteStore`, `PostgresNoteStore.put` on a migrated database
for the other. The assertions read only through `NoteStore`. `corpus()` is the synthetic graph the
suite uses: links both ways, a dangling target, windows, a supersede chain and cycles.
"""

from collections.abc import Awaitable, Callable
from datetime import date
from pathlib import Path
from typing import Any, Protocol

import pytest

from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.kg.git_writer import GitNoteWriter
from chemclaw.kg.graph import invalidate_cache
from chemclaw.kg.note import Note, Relation, note_relative_path
from chemclaw.kg.postgres_store import PostgresNoteStore
from chemclaw.kg.record import NoteFile, NoteWrite
from chemclaw.kg.render import render_note
from chemclaw.kg.store import GitNoteStore, NoteStore, NoteUnit
from tests.kg_repo import notes_repo
from tests.pg import migrated_db_or_skip

SPRING = date(2026, 3, 1)
AUTUMN = date(2026, 8, 1)


def corpus() -> list[Note]:
    """Nine notes covering links both ways, a dangling target, windows and a supersede chain.

    `old` was replaced by `mid` in January; `mid` was replaced by `new` in July. `future` is not
    current yet and `alpha` points at it with an edge that holds only until March.
    """
    return [
        Note(
            id="alpha",
            type="playbook",
            created_by="agent",
            tags=["suzuki"],
            body="Suzuki coupling solvent screen. See [[beta]], [[precursor-of:old]], "
            "[[missing-note]] and [[reaction-eln.123]].",
            relations=[
                Relation(rel="uses", to="future", valid_from=SPRING.replace(month=1, day=1)),
                Relation(rel="uses", to="beta", valid_to=date(2026, 3, 31)),
            ],
        ),
        Note(id="beta", type="reaction", created_by="agent", body="Base screen for a Suzuki run."),
        Note(
            id="old",
            type="compound",
            created_by="agent",
            valid_to=date(2026, 1, 31),
            relations=[Relation(rel="superseded-by", to="mid")],
            body="The first entry.",
        ),
        Note(
            id="mid",
            type="compound",
            created_by="agent",
            valid_from=date(2026, 2, 1),
            valid_to=date(2026, 6, 30),
            relations=[
                Relation(rel="supersedes", to="old"),
                Relation(rel="superseded-by", to="new"),
            ],
            body="The second entry.",
        ),
        Note(
            id="new",
            type="compound",
            created_by="agent",
            valid_from=date(2026, 7, 1),
            relations=[Relation(rel="supersedes", to="mid")],
            body="The third entry.",
        ),
        Note(
            id="future",
            type="failure-mode",
            created_by="agent",
            valid_from=date(2027, 1, 1),
            body="A solvent problem not yet found.",
        ),
        Note(id="loner", type="interaction", created_by="agent", body="Nobody links here."),
        Note(
            id="cyclic-a",
            type="playbook",
            created_by="agent",
            relations=[Relation(rel="superseded-by", to="cyclic-b")],
            body="Replaced by b.",
        ),
        Note(
            id="cyclic-b",
            type="playbook",
            created_by="agent",
            relations=[Relation(rel="superseded-by", to="cyclic-a")],
            body="Replaced by a, which makes a cycle.",
        ),
    ]


def changed(note: Note, changes: dict[str, Any]) -> Note:
    """`note` with `changes` applied through the constructor, so the schema checks the result."""
    return Note.model_validate({**note.model_dump(), **changes})


class Harness(Protocol):
    """A store under test and the one way the suite puts notes into it."""

    @property
    def store(self) -> NoteStore:
        """The store the assertions read from."""
        ...

    async def add(self, *notes: Note) -> None:
        """Write `notes` through the store's own write path, each as its own subject."""
        ...


class GitHarness:
    """A real bare remote and clone, written by the real git writer, read by `GitNoteStore`."""

    def __init__(self, root: Path) -> None:
        """Make the remote and the clone under `root`."""
        self.clone = notes_repo(root)
        self._writer = GitNoteWriter(repo_dir=str(self.clone), base_branch="main", remote="origin")
        self.store = GitNoteStore(self.clone / "knowledge")

    async def add(self, *notes: Note) -> None:
        """One commit holding every note."""
        files = [
            NoteFile(path=f"knowledge/{note_relative_path(n.type, n.id)}", content=render_note(n))
            for n in notes
        ]
        await self._writer.write(NoteWrite(files=files, message=f"Add {len(notes)} note(s)"))


class PostgresHarness:
    """`PostgresNoteStore` over the test schema, written by its own `put`."""

    def __init__(self) -> None:
        """Serve the default tenant of the test schema."""
        self.store = PostgresNoteStore()

    async def add(self, *notes: Note) -> None:
        """One transaction per note: `put` writes a subject with what must land beside it."""
        for note in notes:
            await self.store.put(NoteUnit(note))


async def empty_note_tables() -> None:
    """Start from no notes. Revisions are not cascaded from notes, so they go first."""
    await migrated_db_or_skip()
    async with await connect(settings.postgres_dsn) as conn:
        await conn.execute("DELETE FROM kg_note_revisions")
        await conn.execute("DELETE FROM kg_notes")
        await conn.commit()


async def _git(root: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    harness = GitHarness(root)
    monkeypatch.setattr(settings, "note_repo_dir", str(harness.clone))
    invalidate_cache()
    return harness


async def _postgres(root: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    await empty_note_tables()
    return PostgresHarness()


#: The stores under test; adding one is one entry here.
STORES: dict[str, Callable[[Path, pytest.MonkeyPatch], Awaitable[Harness]]] = {
    "git": _git,
    "postgres": _postgres,
}
