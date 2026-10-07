"""A recorded note is visible to readers immediately, by design.

What makes that safe is provenance: a note carries `created_by: agent` through the loader, so a
reader can tell machine-written content from curated content. Real git throughout, because the
property is what is on disk in the tree readers scan.
"""

import asyncio
import subprocess
from pathlib import Path

import pytest

from chemclaw.kg.git_writer import GitNoteWriter, _checkout_lock
from chemclaw.kg.graph import invalidate_cache, load_notes
from chemclaw.kg.record import NoteFile, NoteWrite

_UNREVIEWED = "---\nid: agent-proposal\ntype: reaction\ncreated_by: agent\n---\n\nUnreviewed.\n"


def _git(repo: Path, *args: str) -> str:
    """Run one git command in `repo`, failing loudly — a silent setup failure fakes a pass."""
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": "/usr/bin:/bin",
            "HOME": str(repo),
        },
    )
    return result.stdout


def _submission(note_id: str = "agent-proposal") -> NoteWrite:
    """The real shape `record._build_write` builds: one agent note under `knowledge/<type>/`."""
    return NoteWrite(
        files=[NoteFile(path=f"knowledge/reaction/{note_id}.md", content=_UNREVIEWED)],
        message=f"Add reaction note: {note_id}",
    )


@pytest.fixture()
def knowledge_clone(tmp_path: Path) -> Path:
    """A real bare remote plus a real working clone with one merged note in it.

    The clone carries its own committer identity because the writer commits via
    `asyncio.create_subprocess_exec` and does not inherit the per-command env `_git` uses.
    """
    bare = tmp_path / "remote.git"
    bare.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True
    )

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(bare), str(clone)], check=True, capture_output=True)
    _git(clone, "config", "user.email", "t@t")
    _git(clone, "config", "user.name", "t")
    notes = clone / "knowledge" / "reaction"
    notes.mkdir(parents=True)
    (notes / "merged-note.md").write_text(
        "---\nid: merged-note\ntype: reaction\ncreated_by: human\n---\n\nA merged note.\n",
        encoding="utf-8",
    )
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "seed")
    _git(clone, "push", "origin", "main")
    return clone


def _submitter(clone: Path) -> GitNoteWriter:
    """A submitter pointed at the clone, with the config defaults the fixture establishes."""
    return GitNoteWriter(repo_dir=str(clone), base_branch="main", remote="origin")


def test_a_reader_sees_the_note_as_soon_as_it_is_written(knowledge_clone: Path) -> None:
    """`load_notes` returns the agent note as soon as the write completes.

    Read from the directory `settings.knowledge_path` resolves to; the writer drops the graph cache
    so an earlier reader does not keep serving a graph without it.
    """
    notes_dir = knowledge_clone / "knowledge"
    invalidate_cache(notes_dir)
    assert "agent-proposal" not in {note.id for note in load_notes(notes_dir)}

    writer = GitNoteWriter(repo_dir=str(knowledge_clone), base_branch="main", remote="origin")
    asyncio.run(writer.write(_submission()))

    after = {note.id for note in load_notes(notes_dir)}
    assert "agent-proposal" in after, "a recorded note is readable at once"
    assert "merged-note" in after, "and it does not displace what was already there"


def test_the_note_records_who_authored_it(knowledge_clone: Path) -> None:
    """An agent-authored note carries `created_by: agent` through the writer and out of the loader.

    With no review step, this field and the note's citations are what a chemist has to judge it by.
    Driven through `GitNoteWriter` so the writer path is covered, not only the loader.
    """
    notes_dir = knowledge_clone / "knowledge"
    writer = GitNoteWriter(repo_dir=str(knowledge_clone), base_branch="main", remote="origin")
    asyncio.run(writer.write(_submission()))
    invalidate_cache(notes_dir)
    by_id = {note.id: note for note in load_notes(notes_dir)}
    assert by_id["agent-proposal"].created_by == "agent"
    assert by_id["merged-note"].created_by == "human"


def test_a_reader_is_not_excluded_while_the_writer_holds_the_checkout(
    knowledge_clone: Path,
) -> None:
    """`load_notes` takes no lock while the writer holds the checkout.

    The writer's process lock and `flock` (`git_writer._checkout_lock`) are writer-only; this pin
    surfaces any reader lock added later.
    """
    notes_dir = knowledge_clone / "knowledge"
    invalidate_cache(notes_dir)
    with _checkout_lock(str(knowledge_clone)):
        assert {note.id for note in load_notes(notes_dir)} == {"merged-note"}
