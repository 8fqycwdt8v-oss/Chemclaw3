"""Knowledge-graph validation, usable as a CLI in CI (plan step 2.4).

Checks a notes directory for the failure modes that would corrupt the graph:
unparseable/invalid notes, duplicate ids, and links to unknown notes — plus the
hazard gate (D-080), which refuses an agent-proposed procedure that does not
document the hazard flags its structures raise. Run as
`python -m chemclaw.kg.validate [notes_dir]`; it exits non-zero if any problem is found,
so it gates the PR that adds or edits notes (D-005).
"""

import sys
from collections.abc import Iterable
from pathlib import Path

from chemclaw.core.config import settings
from chemclaw.kg.graph import dangling_links, scan_notes_dir
from chemclaw.kg.note import KNOWN_NOTE_TYPES, Note, NoteError, read_note
from chemclaw.kg.relations import KNOWN_RELATIONS
from chemclaw.science.safety.notes import hazard_problems


def validate(notes_dir: Path) -> list[str]:
    """Return a list of human-readable problems in `notes_dir` (empty if clean)."""
    problems: list[str] = []
    # Notes with the file each came from, so every message can name a path without a lookup that
    # can miss. It used to be a dict keyed by id, which the duplicate-id branch deliberately does
    # not populate twice — so the two registry checks fell back to a literal `Path('?')` for
    # exactly the notes a reader would most want located.
    located: list[tuple[Note, Path]] = []
    id_to_path: dict[str, Path] = {}

    # The same scan the indexer uses (`chemclaw.kg.graph.scan_notes_dir`), and deliberately not the
    # same *loop*: `load_notes` skips an unparseable note so one bad file cannot block a query,
    # while this must report it. Resilient indexer, strict validator, one definition of which files
    # are in scope.
    for path, _ in scan_notes_dir(notes_dir):
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
        # The filename *is* an index key, not decoration. `chemclaw.kg.graph.note_file_fingerprints`
        # reads a note's id back out of `path.stem` — stat-only, it never parses — and
        # `reindex_notes` looks that map up by the id in the frontmatter. When the two disagree the
        # note is missing from both sides of the diff, which used to read as "unchanged" and left it
        # out of the retrieval index entirely and silently. That half is fixed there; this is the
        # half that keeps a mismatch from merging at all, because the right name is knowable here.
        if path.stem != note.id:
            problems.append(
                f"note {note.id!r} is in {path}, whose filename says {path.stem!r} — "
                f"the file must be named {note.id + '.md'!r} "
                "(the note index keys on the filename and would skip this note)"
            )
        located.append((note, path))

    notes = [note for note, _ in located]
    problems.extend(
        f"note {source!r} links to unknown note {target!r}"
        for source, target in dangling_links(notes)
    )
    # Per-note hazard gate (D-080, from main): an agent-authored procedure whose components
    # trip the rule table must carry a `## Hazards` section before it can merge.
    for note in notes:
        problems.extend(hazard_problems(note))
    # Whole-corpus vocabulary checks (gap KNW-6, STO-8). Both are checked here rather than in the
    # `Note` schema so the agent can *propose* a genuinely new type or relation and a human sees it
    # at the PR-gate — while a typo, which would make the note or the edge unfindable by every
    # filter keyed on it, cannot reach the graph. `kg-validate` runs on that same PR.
    problems.extend(
        _registry_problems(
            ((note, path, note.type) for note, path in located),
            KNOWN_NOTE_TYPES,
            "type",
            "chemclaw.kg.note.KNOWN_NOTE_TYPES",
        )
    )
    problems.extend(
        _registry_problems(
            (
                (note, path, relation.rel)
                for note, path in located
                for relation in note.outgoing_relations()
            ),
            KNOWN_RELATIONS,
            "relation",
            "chemclaw.kg.relations.KNOWN_RELATIONS",
        )
    )
    return problems


def _registry_problems(
    values: Iterable[tuple[Note, Path, str]],
    registry: frozenset[str],
    label: str,
    registry_name: str,
) -> list[str]:
    """Flag every `(note, path, value)` whose value is outside `registry`.

    One function for the note-type check and the relation check, which were the same comprehension
    written twice with two words swapped — and were therefore two places to fix when the message,
    the sentinel path or the "add it to the registry" hint needed changing.
    """
    return [
        f"note {note.id!r} in {path} uses unknown {label} {value!r} "
        f"(add it to {registry_name} if intended)"
        for note, path, value in values
        if value not in registry
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
