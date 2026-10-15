"""`kg_store=dual`: a note is written to the tree first and Postgres second, and they stay equal.

Real git against a real bare remote, and a real Postgres schema. The tree is the authority: its
failure fails the write; a Postgres failure never does, and is counted so the comparison and the
repair can be seen to find it. `kg_store=git` must leave Postgres untouched, and `postgres` (which
the settings refuse, so the tests set it on the live object) writes Postgres only.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from chemclaw.core.config import Settings, settings
from chemclaw.core.db import connect, pooling
from chemclaw.core.metrics import METRICS
from chemclaw.kg.compare import compare_once, repair_from_tree
from chemclaw.kg.git_writer import GitNoteWriter, GitWriteError
from chemclaw.kg.graph import invalidate_cache
from chemclaw.kg.importer import import_notes
from chemclaw.kg.note import Note, Relation, note_relative_path
from chemclaw.kg.postgres_store import PostgresNoteStore
from chemclaw.kg.record import NoteFile, NoteWrite, NoteWriter, WriteOutcome, record_note
from chemclaw.kg.render import render_note
from chemclaw.kg.store import GitNoteStore, NoteStore, note_store
from chemclaw.memory.failure import close_refuted_note, failure_note
from tests.kg_repo import notes_repo
from tests.note_store_harness import empty_note_tables

_FAILED = "chemclaw_kg_dual_write_failed_total"
_RECORDED = "chemclaw_notes_recorded_total"


@dataclass
class Dual:
    """A dual-mode deployment: the clone, its real writer and both stores."""

    clone: Path
    writer: GitNoteWriter
    git: NoteStore
    postgres: PostgresNoteStore

    async def record(self, note: Note, **kwargs: Any) -> str:
        """Record `note` through the one write path, as a tool does."""
        return await record_note(note, self.writer, **kwargs)

    async def curated(self, note: Note) -> None:
        """A person's note, committed straight to the tree as a person does."""
        path = f"knowledge/{note_relative_path(note.type, note.id)}"
        await self.writer.write(
            NoteWrite(files=[NoteFile(path=path, content=render_note(note))], message="curated")
        )

    async def differences(self) -> dict[str, tuple[str, ...]]:
        """Every difference between the stores, by note id."""
        found = await compare_once(self.git, self.postgres)
        return {d.note_id: d.kinds for d in found.differences}


@pytest.fixture
async def dual(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Dual]:
    """`kg_store=dual` over a fresh clone and empty notes tables."""
    await empty_note_tables()
    clone = notes_repo(tmp_path)
    monkeypatch.setattr(settings, "note_repo_dir", str(clone))
    monkeypatch.setattr(settings, "kg_store", "dual")
    invalidate_cache()
    async with pooling():
        yield Dual(
            clone=clone,
            writer=GitNoteWriter(repo_dir=str(clone), base_branch="main", remote="origin"),
            git=GitNoteStore(clone / "knowledge"),
            postgres=PostgresNoteStore(),
        )


def _note(note_id: str, body: str = "A note.", **fields: Any) -> Note:
    return Note(id=note_id, type="playbook", created_by="agent", body=body, **fields)


async def _count(table: str) -> int:
    async with await connect(settings.postgres_dsn) as conn:
        cursor = await conn.execute(f"SELECT count(*) FROM {table}")
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


class _Refusing:
    """A writer that fails the way a rejected push does."""

    async def write(self, write: NoteWrite) -> WriteOutcome:
        raise GitWriteError("push rejected")


def test_the_settings_accept_dual_and_refuse_postgres_until_the_reads_flip() -> None:
    """`dual` is a supported mode; `postgres` is refused at startup with the reason."""
    assert Settings(_env_file=None, kg_store="dual").kg_store == "dual"  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="kg_store='postgres' is not accepted yet"):
        Settings(_env_file=None, kg_store="postgres")  # type: ignore[call-arg]


async def test_both_stores_are_equal_after_many_writes_with_retirements_and_a_human_note(
    dual: Dual,
) -> None:
    """Writes of every shape land in both stores, and the comparison finds nothing different."""
    await dual.curated(
        Note(id="curated", type="playbook", created_by="human", body="A person's playbook.")
    )
    await import_notes(dual.clone / "knowledge")
    compound = Note(id="compound-x", type="compound", created_by="agent", body="Compound X.")
    before = METRICS.value(_RECORDED)
    await dual.record(_note("first", "Uses [[compound-x]]."), dependencies=[compound])
    await dual.record(_note("second", "Replaces [[first]]."))
    retired = _note("first", "Uses [[compound-x]].", valid_to=date(2026, 3, 31))
    retired = retired.model_copy(update={"relations": [Relation(rel="superseded-by", to="second")]})
    await dual.record(_note("third", "Replaces [[first]] too."), superseded=[retired])
    # A retirement of a person's note is left alone by both writers; the subject still lands.
    closed_curated = Note(
        id="curated", type="playbook", created_by="human", body="x", valid_to=date(2026, 1, 1)
    )
    await dual.record(_note("fourth", "Contradicts [[curated]]."), superseded=[closed_curated])
    await dual.record(_note("second", "Replaces [[first]], edited.", tags=["edited"]))
    unchanged = await dual.record(_note("second", "Replaces [[first]], edited.", tags=["edited"]))
    assert unchanged  # a byte-identical repeat is still a successful write
    assert await dual.differences() == {}
    postgres_ids = {n.id for n in await dual.postgres.notes()}
    assert postgres_ids == {"curated", "compound-x", "first", "second", "third", "fourth"}
    assert (await dual.postgres.get("curated")).valid_to is None  # type: ignore[union-attr]
    assert (await dual.postgres.get("first")).valid_to == date(2026, 3, 31)  # type: ignore[union-attr]
    assert await _count("kg_note_revisions") == 1 + 1 + 2 + 2 + 1 + 1
    # The notes counter books the tree's outcome once per write that changed a subject: not twice.
    assert METRICS.value(_RECORDED) - before == 5


async def test_a_refutation_that_retires_a_note_agrees_in_both_stores(dual: Dual) -> None:
    """The retirement helper appends a sentence to the body; Postgres keeps it, as the file does."""
    target = _note("claim", "Runs well at 80 C.")
    await dual.record(target)
    failure = failure_note("claim", "It failed at 80 C.", reported_by="chemist")
    closed = close_refuted_note(target, failure.id, date(2026, 3, 1))
    await dual.record(failure, superseded=[closed])
    stored = await dual.postgres.get("claim")
    assert stored is not None and stored.valid_to == date(2026, 3, 1)
    assert f"[[{failure.id}]]" in stored.body
    assert await dual.differences() == {}


async def test_a_postgres_failure_does_not_fail_the_write_and_the_repair_fixes_it(
    dual: Dual, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Git has the note, the chemist's call succeeded, the failure is loud, the compare sees it."""
    await dual.record(_note("before", "Written while Postgres was up."))
    real_put = PostgresNoteStore.put

    async def down(self: PostgresNoteStore, unit: Any) -> Any:
        raise ConnectionError("postgres is down")

    monkeypatch.setattr(PostgresNoteStore, "put", down)
    failed = METRICS.value(_FAILED)
    with caplog.at_level(logging.ERROR):
        reference = await dual.record(_note("during", "Written while Postgres was down."))
    assert reference  # the commit
    assert (dual.clone / "knowledge" / "playbook" / "during.md").exists()
    assert METRICS.value(_FAILED) == failed + 1
    assert any(
        "kg.dual_write.failed" in r.getMessage() and "during" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.ERROR
    )
    assert await dual.differences() == {"during": ("missing_in_postgres",)}

    monkeypatch.setattr(PostgresNoteStore, "put", real_put)
    found = await compare_once(dual.git, dual.postgres)
    repaired = await repair_from_tree(dual.git, dual.postgres, found.differences)
    assert repaired.repaired == ("during",) and repaired.unrepaired == ()
    assert await dual.differences() == {}


async def test_recording_the_same_note_again_repairs_a_failed_mirror(
    dual: Dual, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A put is idempotent, so a retry of the chemist's write is itself a repair."""
    note = _note("retry", "Written once.")
    real_put = PostgresNoteStore.put

    async def down(self: PostgresNoteStore, unit: Any) -> Any:
        raise ConnectionError("postgres is down")

    monkeypatch.setattr(PostgresNoteStore, "put", down)
    await dual.record(note)
    monkeypatch.setattr(PostgresNoteStore, "put", real_put)
    failed = METRICS.value(_FAILED)
    await dual.record(note)
    assert await dual.differences() == {}
    assert METRICS.value(_FAILED) == failed


async def test_a_tree_failure_fails_the_write_and_leaves_postgres_alone(dual: Dual) -> None:
    """The tree is written first, so Postgres is never ahead of it: nothing reaches Postgres."""
    refusing: NoteWriter = _Refusing()
    failed = METRICS.value(_FAILED)
    with pytest.raises(GitWriteError):
        await record_note(_note("lost"), refusing)
    assert await _count("kg_notes") == 0
    assert METRICS.value(_FAILED) == failed
    assert await dual.differences() == {}


async def test_a_human_note_in_the_tree_cannot_be_replaced_by_an_agent_in_either_store(
    dual: Dual,
) -> None:
    """The tree refuses; the write fails there and Postgres is not asked."""
    await dual.curated(Note(id="mine", type="playbook", created_by="human", body="Curated."))
    await import_notes(dual.clone / "knowledge")
    with pytest.raises(GitWriteError):
        await dual.record(_note("mine", "An agent's version."))
    stored = await dual.postgres.get("mine")
    assert stored is not None and stored.body == "Curated." and stored.created_by == "human"


async def test_concurrent_writers_of_different_notes_leave_the_stores_equal(dual: Dual) -> None:
    """Eight writers at once: the tree serialises them, Postgres takes each as a transaction."""
    await asyncio.gather(*(dual.record(_note(f"note-{i}", f"Body {i}.")) for i in range(8)))
    assert len(await dual.postgres.notes()) == 8
    assert await dual.differences() == {}


async def test_concurrent_writers_of_one_note_end_with_the_tree_authoritative(
    dual: Dual,
) -> None:
    """The two stores can see the writers in different orders; the comparison sees it.

    Which store ends up holding which writer's body is not fixed, so the test asserts only the
    invariants: the tree holds one of the bodies, and after a repair the stores agree.
    """
    await asyncio.gather(*(dual.record(_note("shared", f"Writer {i}.")) for i in range(4)))
    held = await dual.git.get("shared")
    assert held is not None and held.body in {f"Writer {i}." for i in range(4)}
    found = await compare_once(dual.git, dual.postgres)
    await repair_from_tree(dual.git, dual.postgres, found.differences)
    assert await dual.differences() == {}
    stored = await dual.postgres.get("shared")
    assert stored is not None and stored.body == held.body


async def test_dual_reads_from_the_tree(dual: Dual) -> None:
    """Reads stay on the authority until the flip."""
    assert isinstance(note_store(), GitNoteStore)


async def test_git_mode_never_touches_postgres(dual: Dual, monkeypatch: pytest.MonkeyPatch) -> None:
    """`kg_store=git` is the behaviour before any of this: the tree only."""
    monkeypatch.setattr(settings, "kg_store", "git")
    await dual.record(_note("only-in-git", "Written in git mode."))
    assert (dual.clone / "knowledge" / "playbook" / "only-in-git.md").exists()
    assert await _count("kg_notes") == 0
    assert await _count("kg_note_revisions") == 0


async def test_the_postgres_arm_writes_postgres_only_and_books_the_note(
    dual: Dual, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The arm the settings keep unreachable until the flip: no tree write, a reference, a count."""
    monkeypatch.setattr(settings, "kg_store", "postgres")
    before = METRICS.value(_RECORDED)
    reference = await dual.record(_note("pg-only", "Written to Postgres alone."))
    assert reference == "kg_notes:pg-only@1"
    assert not (dual.clone / "knowledge" / "playbook" / "pg-only.md").exists()
    assert METRICS.value(_RECORDED) == before + 1
    again = await dual.record(_note("pg-only", "Written to Postgres alone."))
    assert again == "kg_notes:pg-only@1"
    assert METRICS.value(_RECORDED) == before + 1
    assert isinstance(note_store(), PostgresNoteStore)
    edited = await dual.record(_note("pg-only", "Edited."))
    assert edited == "kg_notes:pg-only@2"


async def test_a_backfill_under_dual_commits_and_mirrors_each_note_on_its_own(
    dual: Dual, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One commit per note keeps Postgres from being written before the tree has the note."""
    import chemclaw.cli.backfill_corpus as backfill_corpus

    monkeypatch.setattr(backfill_corpus, "default_writer", lambda: dual.writer)
    monkeypatch.setattr(settings, "backfill_commit_batch_size", 3)
    documents = dual.clone.parent / "documents"
    documents.mkdir()
    for index in range(5):
        (documents / f"sop-{index}.md").write_text(f"Standard operating procedure {index}.\n")
    commits_before = len(_commit_log(dual.clone))
    written, skipped = await backfill_corpus.backfill(documents, tags=[], dry_run=False)
    assert (written, skipped) == (5, 0)
    assert len(_commit_log(dual.clone)) - commits_before == 5
    assert len(await dual.postgres.notes()) == 5
    assert await dual.differences() == {}


def _commit_log(clone: Path) -> list[str]:
    import subprocess

    done = subprocess.run(
        ["git", "-C", str(clone), "log", "--format=%H"], capture_output=True, text=True, check=True
    )
    return done.stdout.split()


async def test_a_retirement_of_a_persons_note_postgres_lacks_is_left_alone_as_the_file_is(
    dual: Dual,
) -> None:
    """Postgres does not insert the closed copy of a note the tree leaves alone."""
    human = Note(id="curated", type="playbook", created_by="human", body="A person's playbook.")
    await dual.curated(human)
    closed = Note(
        id="curated",
        type="playbook",
        created_by="human",
        body="A person's playbook.",
        valid_to=date(2026, 1, 1),
    )
    await dual.record(_note("successor", "Contradicts [[curated]]."), superseded=[closed])
    assert (await dual.git.get("curated")).valid_to is None  # type: ignore[union-attr]
    assert await dual.postgres.get("curated") is None
    assert await dual.differences() == {"curated": ("missing_in_postgres",)}


async def test_a_cancellation_after_the_tree_commit_is_counted_logged_and_re_raised(
    dual: Dual, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The note is in the tree whatever happened to the task: both counters say so."""
    started = asyncio.Event()
    real_put = PostgresNoteStore.put

    async def hang(self: PostgresNoteStore, unit: Any) -> Any:
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(PostgresNoteStore, "put", hang)
    failed, recorded = METRICS.value(_FAILED), METRICS.value(_RECORDED)
    task = asyncio.ensure_future(dual.record(_note("interrupted", "Cancelled mid-mirror.")))
    await asyncio.wait_for(started.wait(), 60)
    task.cancel()
    with caplog.at_level(logging.WARNING), pytest.raises(asyncio.CancelledError):
        await task
    assert (dual.clone / "knowledge" / "playbook" / "interrupted.md").exists()
    assert METRICS.value(_FAILED) == failed + 1
    assert METRICS.value(_RECORDED) == recorded + 1
    assert any(
        "kg.dual_write.failed" in r.getMessage() and "interrupted" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
    )
    monkeypatch.setattr(PostgresNoteStore, "put", real_put)
    assert await dual.differences() == {"interrupted": ("missing_in_postgres",)}
