"""The importer loads a notes directory into Postgres: in id order, once, with arrival dates.

A real Postgres schema and, where the arrival date matters, a real git history with dated commits.
The assertions are on the rows and on what the summary names, so a note that was not imported is
found by its id and its reason rather than by a count that moved.
"""

import asyncio
import json
import os
import subprocess
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from chemclaw.cli.kg_import import main as import_main
from chemclaw.core.config import settings
from chemclaw.core.db import connect, pooling
from chemclaw.kg.importer import import_notes
from chemclaw.kg.note import Note
from chemclaw.kg.postgres_store import PostgresNoteStore
from chemclaw.kg.render import render_note
from chemclaw.kg.store import NoteUnit
from tests.kg_repo import notes_repo, write_notes
from tests.note_store_harness import empty_note_tables

_SHIPPED = Path(__file__).resolve().parents[1] / "knowledge"


@pytest.fixture
async def source(tmp_path: Path) -> AsyncIterator[Path]:
    """An empty notes directory over empty notes tables, with the pool on."""
    await empty_note_tables()
    directory = tmp_path / "notes"
    directory.mkdir()
    async with pooling():
        yield directory


def _note(note_id: str, body: str = "A note.", **fields: Any) -> Note:
    return Note(id=note_id, type="playbook", created_by="agent", body=body, **fields)


async def _rows(sql: str, *params: Any) -> list[tuple[Any, ...]]:
    async with await connect(settings.postgres_dsn) as conn:
        cursor = await conn.execute(sql, params)
        return await cursor.fetchall()


async def _rows_none(sql: str, *params: Any) -> None:
    async with await connect(settings.postgres_dsn) as conn:
        await conn.execute(sql, params)
        await conn.commit()


async def test_every_note_is_put_once_in_id_order_and_a_repeat_changes_nothing(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_notes(
        source, *(_note(i, f"Body of {i}.") for i in ("delta", "alpha", "charlie", "bravo"))
    )
    order: list[str] = []
    real_put = PostgresNoteStore.put

    async def spy(self: PostgresNoteStore, unit: NoteUnit) -> Any:
        order.append(unit.subject.id)
        return await real_put(self, unit)

    monkeypatch.setattr(PostgresNoteStore, "put", spy)
    first = await import_notes(source)
    assert order == ["alpha", "bravo", "charlie", "delta"]
    assert (first.created, first.updated, first.unchanged, first.complete) == (4, 0, 0, True)
    revisions = await _rows("SELECT note_id, revision FROM kg_note_revisions ORDER BY note_id")
    stamps = await _rows("SELECT id, updated_at FROM kg_notes ORDER BY id")

    again = await import_notes(source)
    assert (again.created, again.updated, again.unchanged) == (0, 0, 4)
    assert await _rows("SELECT note_id, revision FROM kg_note_revisions ORDER BY note_id") == (
        revisions
    )
    assert await _rows("SELECT id, updated_at FROM kg_notes ORDER BY id") == stamps


async def test_a_changed_file_is_the_next_revision_and_nothing_else_moves(source: Path) -> None:
    write_notes(source, _note("one"), _note("two"))
    await import_notes(source)
    write_notes(source, _note("two", "Edited."))
    summary = await import_notes(source)
    assert (summary.created, summary.updated, summary.unchanged) == (0, 1, 1)
    assert await _rows("SELECT id, revision FROM kg_notes ORDER BY id") == [("one", 1), ("two", 2)]


async def test_a_notes_created_at_is_the_day_its_file_arrived_in_the_tree(tmp_path: Path) -> None:
    """The commit that added each file dates it; a note added later has the later date."""
    await empty_note_tables()
    clone = notes_repo(tmp_path)
    notes_dir = clone / "knowledge"

    def commit(note: Note, when: str) -> None:
        write_notes(notes_dir, note)
        env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
        for argv in (["add", "-A"], ["commit", "-q", "-m", f"add {note.id}"]):
            subprocess.run(
                ["git", "-C", str(clone), *argv], check=True, env=env, capture_output=True
            )

    commit(_note("early"), "2025-03-04T10:00:00+00:00")
    commit(_note("late"), "2026-06-20T23:30:00+00:00")
    async with pooling():
        await import_notes(notes_dir)
        arrived = await PostgresNoteStore().arrivals()
    assert arrived == {"early": date(2025, 3, 4), "late": date(2026, 6, 20)}


async def test_a_note_in_a_directory_that_is_not_a_repository_arrives_now(source: Path) -> None:
    write_notes(source, _note("undated"))
    await import_notes(source)
    arrived = await PostgresNoteStore().arrivals()
    assert arrived["undated"] == (await _rows("SELECT (now() AT TIME ZONE 'UTC')::date"))[0][0]


async def test_a_humans_note_is_imported_as_a_subject_and_keeps_its_author(source: Path) -> None:
    write_notes(
        source,
        Note(id="curated", type="playbook", created_by="human", body="A person's playbook."),
        _note("generated"),
    )
    summary = await import_notes(source)
    assert summary.complete and summary.created == 2
    assert await _rows("SELECT id, created_by FROM kg_notes ORDER BY id") == [
        ("curated", "human"),
        ("generated", "agent"),
    ]


async def test_what_is_not_imported_is_named(source: Path) -> None:
    write_notes(source, _note("good"))
    (source / "playbook" / "broken.md").write_text("---\ntype: [oops\n---\nx\n", encoding="utf-8")
    (source / "README.md").write_text("# Not a note\n\nNo frontmatter.\n", encoding="utf-8")
    write_notes(
        source,
        Note(id="good", type="reaction", created_by="agent", body="The second file for the id."),
    )
    summary = await import_notes(source)
    assert summary.created == 1 and summary.not_notes == 1 and not summary.complete
    assert [f for f, _ in summary.unparseable] == [str(source / "playbook" / "broken.md")]
    assert [i for i, _ in summary.skipped] == ["good"]
    assert "another file already defines it" in summary.skipped[0][1]
    assert (await PostgresNoteStore().get("good")).type == "playbook"  # type: ignore[union-attr]


async def test_a_dry_run_writes_nothing_and_predicts_the_real_run(source: Path) -> None:
    write_notes(source, _note("one"), _note("two"))
    planned = await import_notes(source, dry_run=True)
    assert (planned.created, planned.updated, planned.unchanged) == (2, 0, 0)
    assert planned.dry_run and planned.index_keys_linked is None
    assert await _rows("SELECT count(*) FROM kg_notes") == [(0,)]
    await import_notes(source)
    write_notes(source, _note("two", "Edited."), _note("three"))
    planned = await import_notes(source, dry_run=True)
    assert (planned.created, planned.updated, planned.unchanged) == (1, 1, 1)
    assert await _rows("SELECT count(*) FROM kg_notes") == [(2,)]


async def test_the_import_reads_one_note_at_a_time(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No call reads the corpus whole: not the store's `notes()`, not the tree's loader."""
    write_notes(source, *(_note(f"n{i}") for i in range(5)))

    async def forbidden(self: PostgresNoteStore) -> list[Note]:
        raise AssertionError("the import read every stored note")

    monkeypatch.setattr(PostgresNoteStore, "notes", forbidden)
    monkeypatch.setattr("chemclaw.kg.graph.load_notes", lambda _: pytest.fail("loaded the tree"))
    summary = await import_notes(source)
    assert summary.created == 5


async def test_the_note_index_is_linked_to_the_notes_and_a_repeat_links_new_ones(
    source: Path,
) -> None:
    """Only the keys change; rows for ids with no note stay unlinked, and linking is idempotent.

    The index rows are the test's own and are removed afterwards: other tests read the table whole
    (`/readyz` counts rows by embedding key).
    """
    ids = ("link-a", "link-b", "link-ghost")
    write_notes(source, _note("link-a"))
    try:
        async with await connect(settings.postgres_dsn) as conn:
            for note_id in ids:
                await conn.execute(
                    "INSERT INTO note_index (note_id, fingerprint) VALUES (%s, %s)",
                    (note_id, f"f-{note_id}"),
                )
            await conn.commit()
        first = await import_notes(source)
        assert first.index_keys_linked == 1
        write_notes(source, _note("link-b"))
        second = await import_notes(source)
        assert second.index_keys_linked == 1
        third = await import_notes(source)
        assert third.index_keys_linked == 0
        linked = await _rows(
            "SELECT note_id, kg_tenant_id, kg_note_id, fingerprint FROM note_index "
            "WHERE note_id = ANY(%s) ORDER BY note_id",
            list(ids),
        )
        assert linked == [
            ("link-a", "default", "link-a", "f-link-a"),
            ("link-b", "default", "link-b", "f-link-b"),
            ("link-ghost", None, None, "f-link-ghost"),
        ]
    finally:
        await _rows_none("DELETE FROM note_index WHERE note_id = ANY(%s)", list(ids))


async def test_a_tenant_is_imported_into_its_own_rows(source: Path) -> None:
    write_notes(source, _note("shared-id"))
    summary = await import_notes(source, tenant="acme")
    assert summary.tenant == "acme"
    assert await PostgresNoteStore("acme").get("shared-id") is not None
    assert await PostgresNoteStore().get("shared-id") is None
    assert await _rows("SELECT DISTINCT tenant_id FROM kg_notes") == [("acme",)]


async def test_the_committed_corpus_imports_completely(source: Path) -> None:
    summary = await import_notes(_SHIPPED)
    assert summary.complete, (summary.skipped, summary.unparseable)
    assert summary.created >= 40 and summary.updated == 0
    assert (await import_notes(_SHIPPED)).written == 0


# --- The command ---------------------------------------------------------------------------------


async def _run(*argv: str) -> int:
    return await asyncio.to_thread(import_main, list(argv))


async def test_the_command_exits_zero_one_or_two(
    source: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_notes(source, _note("one"))
    assert await _run(str(source)) == 0
    assert "wrote 1 note(s) (1 new, 0 changed); 0 already there" in capsys.readouterr().out
    assert await _run(str(source), "--dry-run") == 0
    assert "would write 0 note(s)" in capsys.readouterr().out

    (source / "playbook" / "bad.md").write_text("---\ntype: [oops\n---\nx\n", encoding="utf-8")
    assert await _run(str(source), "--json") == 1
    report = json.loads(capsys.readouterr().out)
    assert report["complete"] is False and report["unchanged"] == 1
    assert report["unparseable"][0]["file"].endswith("bad.md")

    assert await _run(str(source / "nowhere")) == 2
    assert "not a directory" in capsys.readouterr().err
    monkeypatch.setattr(settings, "postgres_dsn", "postgresql://nobody@127.0.0.1:1/none")
    assert await _run(str(source)) == 2
    assert "the import could not run" in capsys.readouterr().err


async def test_a_duplicate_id_is_won_by_the_first_file_in_path_order_as_the_graph_resolves_it(
    source: Path,
) -> None:
    """`a/` sorts before `a-b/` as a path, and after it as a string: the graph goes by the path."""
    for directory, body in (("a", "From a."), ("a-b", "From a-b.")):
        (source / directory).mkdir()
        (source / directory / "dup.md").write_text(
            render_note(_note("dup", body)), encoding="utf-8"
        )
    summary = await import_notes(source)
    stored = await PostgresNoteStore().get("dup")
    assert stored is not None and stored.body == "From a."
    assert [i for i, _ in summary.skipped] == ["dup"]


async def test_a_database_error_on_one_note_is_that_notes_and_the_rest_are_imported(
    source: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_notes(source, *(_note(i) for i in ("one", "two", "three")))
    real_put = PostgresNoteStore.put

    async def flaky(self: PostgresNoteStore, unit: NoteUnit) -> Any:
        if unit.subject.id == "three":
            raise RuntimeError("deadlock detected")
        return await real_put(self, unit)

    monkeypatch.setattr(PostgresNoteStore, "put", flaky)
    summary = await import_notes(source)
    assert summary.created == 2 and not summary.complete
    assert summary.skipped == [("three", "RuntimeError: deadlock detected")]
    assert {n.id for n in await PostgresNoteStore().notes()} == {"one", "two"}
    assert await _run(str(source)) == 1
    assert "SKIPPED three: RuntimeError: deadlock detected" in capsys.readouterr().out
