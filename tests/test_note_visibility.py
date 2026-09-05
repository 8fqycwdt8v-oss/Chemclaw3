"""When does a recorded note become visible to a reader? Measured: immediately, on purpose.

**This file is the inversion of the one it replaces, and the inversion is the decision.** It used
to ask whether a reader could see an *unreviewed* note while the PR-gate was submitting it, and
every assertion existed to prove they could not: the gate was the review line, and the whole
control rested on an agent-authored note being invisible as knowledge until a human merged it.
`D-2026-09-05-the-gate-is-deleted-not-dormant` removed that control, so the question no longer has
a subject — a note is *meant* to be readable the moment it is written.

What survives is the mitigation that used to be secondary and is now the whole of it: a note
carries `created_by: agent` through the loader, so a reader can tell machine-written content from
curated content at the point of use. That assertion is kept, promoted from "the mitigation that
also exists" to the thing the deletion rests on.

Real git throughout, not a stub, for the reason the old file gave: the property under test is what
is on disk in the tree readers scan, so a fake writer would test the fake.
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

    Real git, not a stub: the window under test was created by a working tree being switched, so a
    fake submitter would test the fake. The merged note gives the reader something legitimate to
    see, which is what makes "and also the unreviewed one" a detectable difference.

    The clone carries its own committer identity because the submitter commits inside the worktree
    through `asyncio.create_subprocess_exec` and does not inherit the per-command env `_git` uses.
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
    """The headline inversion: no window, because visibility is the point.

    The old assertion was that `load_notes` never returns the agent note at any instant of the
    submission. It now returns it as soon as the write completes, from the same directory
    `settings.knowledge_path` resolves to — and the writer drops the graph cache, so a reader that
    scanned a moment earlier does not go on serving a graph without it.
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
    """The control the deletion rests on, promoted from a secondary mitigation to the whole of it.

    An agent-authored note carries `created_by: agent` through the loader, so a reader can tell
    machine-written content from curated content. Under the gate this sat behind a human review
    step; there is no review step now, so this field and the citations beside it are what a chemist
    has. A change to it is a change to the control.
    """
    notes_dir = knowledge_clone / "knowledge"
    (notes_dir / "reaction" / "agent-proposal.md").write_text(_UNREVIEWED, encoding="utf-8")
    invalidate_cache(notes_dir)
    by_id = {note.id: note for note in load_notes(notes_dir)}
    assert by_id["agent-proposal"].created_by == "agent"
    assert by_id["merged-note"].created_by == "human"


def test_a_reader_is_not_excluded_while_the_writer_holds_the_checkout(
    knowledge_clone: Path,
) -> None:
    """`load_notes` still takes no lock, and that is now harmless rather than the mechanism.

    The writer holds two locks — a process-wide `asyncio.Lock` and an OS `flock`
    (`git_writer._checkout_lock`) — and neither is a *reader* lock. Kept, rather than deleted with
    the rest of the old subject, because it is the pin that says no reader lock was ever added: if
    one is, this is where that decision surfaces.
    """
    notes_dir = knowledge_clone / "knowledge"
    invalidate_cache(notes_dir)
    with _checkout_lock(str(knowledge_clone)):
        assert {note.id for note in load_notes(notes_dir)} == {"merged-note"}
