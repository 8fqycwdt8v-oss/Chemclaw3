"""Knowledge-graph validation, usable as a CLI in CI (plan step 2.4).

Checks a notes directory for the failure modes that would corrupt the graph:
unparseable/invalid notes, duplicate ids, and links to unknown notes — plus the
hazard gate (D-080), which refuses an agent-proposed procedure that does not
document the hazard flags its structures raise. Run as
`python -m kg.validate [notes_dir]`; it exits non-zero if any problem is found,
so it gates the PR that adds or edits notes (D-005).
"""

import sys
from pathlib import Path

from chemclaw.config import settings
from kg.note import KNOWN_NOTE_TYPES, Note, NoteError, read_note
from safety.notes import hazard_problems


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
        # Per-note hazard gate (D-080, from main): an agent-authored procedure whose components
        # trip the rule table must carry a `## Hazards` section before it can merge.
        problems.extend(hazard_problems(note))
    # Whole-corpus check (gap KNW-6): a note type outside the registry is almost always a typo,
    # and any retrieval filter keyed on type would then miss it silently.
    problems.extend(_unknown_types(notes, id_to_path))
    return problems


def _unknown_types(notes: list[Note], id_to_path: dict[str, Path]) -> list[str]:
    """Flag any note whose `type` is not in the registry (gap KNW-6).

    A typo previously minted a new type in silence, and every retrieval filter keyed on type then
    missed without an error. Checked here rather than in the schema so the agent can still *propose*
    a genuinely new type — the PR-gate puts a human on it, and this gate runs on that same PR, so an
    unintended type cannot reach the graph while an intended one costs one line in the registry.
    """
    return [
        f"note {note.id!r} in {id_to_path.get(note.id, Path('?'))} has unknown type "
        f"{note.type!r} (add it to kg.note.KNOWN_NOTE_TYPES if intended)"
        for note in notes
        if note.type not in KNOWN_NOTE_TYPES
    ]


def main() -> int:
    """CLI entry point: validate the notes dir; print problems; return exit code."""
    notes_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else settings.knowledge_path
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
