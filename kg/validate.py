"""Knowledge-graph validation, usable as a CLI in CI (plan step 2.4).

Checks a notes directory for the three failure modes that would corrupt the
graph: unparseable/invalid notes, duplicate ids, and links to unknown notes.
Run as `python -m kg.validate [notes_dir]`; it exits non-zero if any problem is
found, so it gates the PR that adds or edits notes (D-005).
"""

import sys
from pathlib import Path

from chemclaw.config import settings
from kg.note import NoteError, read_note


def validate(notes_dir: Path) -> list[str]:
    """Return a list of human-readable problems in `notes_dir` (empty if clean)."""
    problems: list[str] = []
    id_to_path: dict[str, Path] = {}
    notes = []

    for path in sorted(notes_dir.rglob("*.md")):
        try:
            note = read_note(path)
        except NoteError as exc:
            problems.append(str(exc))
            continue
        if note is None:
            continue
        if note.id in id_to_path:
            problems.append(f"duplicate id {note.id!r} in {path} and {id_to_path[note.id]}")
        else:
            id_to_path[note.id] = path
        notes.append(note)

    known = set(id_to_path)
    for note in notes:
        for target in note.outgoing_links():
            if target not in known:
                problems.append(f"note {note.id!r} links to unknown note {target!r}")
    return problems


def main() -> int:
    """CLI entry point: validate the notes dir; print problems; return exit code."""
    notes_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(settings.knowledge_dir)
    if not notes_dir.exists():
        print(f"notes directory does not exist: {notes_dir}")
        return 1
    problems = validate(notes_dir)
    for problem in problems:
        print(problem)
    if problems:
        print(f"\n{len(problems)} problem(s) found in {notes_dir}")
        return 1
    print(f"OK: {notes_dir} is a valid knowledge graph")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
