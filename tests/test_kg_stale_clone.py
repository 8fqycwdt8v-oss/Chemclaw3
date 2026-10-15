"""A command that copies the tree into Postgres refuses a clone that is behind the remote.

A second clone taken before a newer write is the reviewer's experiment: Postgres holds version 2,
the stale clone holds version 1, so the comparison calls Postgres's note a difference and a repair
from that clone would put version 1 back, then compare against the same clone and say OK. Real git
against a real bare remote and a real Postgres schema.
"""

import asyncio
import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from chemclaw.cli.kg_import import main as import_main
from chemclaw.cli.validate_kg import main as validate_main
from chemclaw.core.config import settings
from chemclaw.core.db import pooling
from chemclaw.kg.git_writer import GitNoteWriter
from chemclaw.kg.graph import invalidate_cache
from chemclaw.kg.note import Note
from chemclaw.kg.postgres_store import PostgresNoteStore
from chemclaw.kg.record import record_note
from tests.kg_repo import notes_repo
from tests.note_store_harness import empty_note_tables


def _run(*argv: str) -> str:
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout.strip()


@dataclass
class Clones:
    """The deployment's clone (which writes), a second clone of the same remote, and Postgres."""

    writer_clone: Path
    other_clone: Path
    writer: GitNoteWriter

    async def record(self, note_id: str, body: str) -> None:
        """Record a note through the one write path, on the deployment's clone."""
        await record_note(
            Note(id=note_id, type="playbook", created_by="agent", body=body), self.writer
        )

    def catch_up(self) -> None:
        """Bring the second clone to the remote's tip, as the sync sidecar does."""
        _run("git", "-C", str(self.other_clone), "pull", "-q", "--ff-only")
        invalidate_cache()

    @property
    def stale_notes(self) -> Path:
        """The second clone's notes directory."""
        return self.other_clone / "knowledge"


@pytest.fixture
async def clones(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Clones]:
    """Version 1 of `n` written through dual mode, then a second clone taken, then version 2."""
    await empty_note_tables()
    writer_clone = notes_repo(tmp_path)
    monkeypatch.setattr(settings, "note_repo_dir", str(writer_clone))
    monkeypatch.setattr(settings, "kg_store", "dual")
    monkeypatch.setattr(settings, "kg_compare_settle_seconds", 0.0)
    invalidate_cache()
    writer = GitNoteWriter(repo_dir=str(writer_clone), base_branch="main", remote="origin")
    other_clone = tmp_path / "other"
    async with pooling():
        held = Clones(writer_clone, other_clone, writer)
        await held.record("n", "Version one.")
        _run("git", "clone", "-q", str(tmp_path / "remote.git"), str(other_clone))
        await held.record("n", "Version two.")
        yield held


async def _main(entry: object, *argv: str) -> int:
    return await asyncio.to_thread(entry, list(argv))  # type: ignore[arg-type]


async def test_a_repair_from_a_clone_behind_the_remote_is_refused_and_postgres_keeps_its_note(
    clones: Clones, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await _main(validate_main, "--compare", "--repair", str(clones.stale_notes))
    assert code == 2
    assert "--repair refused: this checkout is behind origin/main" in capsys.readouterr().err
    stored = await PostgresNoteStore().get("n")
    assert stored is not None and stored.body == "Version two."


async def test_a_comparison_from_a_stale_clone_says_so_and_still_reports(
    clones: Clones, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await _main(validate_main, "--compare", str(clones.stale_notes)) == 1
    captured = capsys.readouterr()
    assert "WARNING: this checkout is behind origin/main" in captured.err
    assert "DIFFERENT n: canonical" in captured.out


async def test_an_import_from_a_clone_behind_the_remote_is_refused_unless_allowed(
    clones: Clones, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await _main(import_main, str(clones.stale_notes)) == 2
    assert "the import was refused: this checkout is behind origin/main" in capsys.readouterr().err
    stored = await PostgresNoteStore().get("n")
    assert stored is not None and stored.body == "Version two."
    assert await _main(import_main, "--allow-stale", "--dry-run", str(clones.stale_notes)) == 0


async def test_a_clone_at_the_tip_repairs_and_imports(
    clones: Clones, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same commands succeed once the clone has caught up."""
    clones.catch_up()
    # A note only the tree has: written in git mode, so Postgres never saw it.
    monkeypatch.setattr(settings, "kg_store", "git")
    await clones.record("m", "Only in the tree.")
    monkeypatch.setattr(settings, "kg_store", "dual")
    clones.catch_up()
    assert await PostgresNoteStore().get("m") is None
    assert await _main(validate_main, "--compare", "--repair", str(clones.stale_notes)) == 0
    stored = await PostgresNoteStore().get("m")
    assert stored is not None and stored.body == "Only in the tree."
    assert await _main(import_main, str(clones.stale_notes)) == 0


async def test_a_clone_holding_an_unpushed_commit_is_refused_as_well(clones: Clones) -> None:
    """Ahead is not current either: its notes are not on the remote yet."""
    clones.catch_up()
    path = clones.other_clone / "knowledge" / "playbook" / "local.md"
    path.write_text("---\ntype: playbook\ncreated_by: agent\n---\nLocal only.\n", encoding="utf-8")
    _run("git", "-C", str(clones.other_clone), "add", "-A")
    _run(
        "git",
        "-C",
        str(clones.other_clone),
        "-c",
        "user.email=t@example.test",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "-m",
        "unpushed",
    )
    reason = await GitNoteWriter(repo_dir=str(clones.stale_notes)).lagging_reason()
    assert reason is not None and "holds commits origin/main lacks" in reason


async def test_a_directory_that_is_no_checkout_has_no_tip_to_be_behind(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()
    assert await GitNoteWriter(repo_dir=str(tmp_path / "plain")).lagging_reason() is None
