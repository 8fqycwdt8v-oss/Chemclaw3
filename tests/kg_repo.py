"""Fixtures for tests that need a notes directory: loose files, or a real git repository.

`write_notes` puts notes on disk the way the corpus holds them. `notes_repo` makes a bare remote and
a clone of it with an empty `knowledge/`, so a test can drive the real git writer against it.
"""

import subprocess
from pathlib import Path

from chemclaw.kg.note import Note, note_relative_path
from chemclaw.kg.render import render_note


def write_notes(directory: Path, *notes: Note) -> None:
    """Write each note to `<directory>/<type>/<id>.md`, rendered as the writer would."""
    for note in notes:
        path = directory / note_relative_path(note.type, note.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_note(note), encoding="utf-8")


def notes_repo(root: Path) -> Path:
    """A bare remote and a clone of it on `main` with an empty `knowledge/`; returns the clone."""
    remote, clone = root / "remote.git", root / "clone"
    steps = (
        ["git", "init", "--bare", "-q", "-b", "main", str(remote)],
        ["git", "clone", "-q", str(remote), str(clone)],
        ["git", "-C", str(clone), "config", "user.email", "store@example.test"],
        ["git", "-C", str(clone), "config", "user.name", "store"],
    )
    for argv in steps:
        subprocess.run(argv, check=True, capture_output=True)
    (clone / "knowledge").mkdir()
    (clone / "knowledge" / ".keep").write_text("", encoding="utf-8")
    for argv in (
        ["git", "-C", str(clone), "add", "-A"],
        ["git", "-C", str(clone), "commit", "-q", "-m", "seed"],
        ["git", "-C", str(clone), "branch", "-M", "main"],
        ["git", "-C", str(clone), "push", "-q", "origin", "HEAD:refs/heads/main"],
    ):
        subprocess.run(argv, check=True, capture_output=True)
    return clone
