"""Knowledge-graph validation, usable as a CLI in CI.

Checks a notes directory for unparseable or invalid notes, duplicate ids, filename and directory
mismatches, links to unknown notes, and note types or relations outside the declared vocabulary. Run
as `python -m chemclaw.kg.validate [notes_dir]`; exits non-zero on any problem. Agent notes are
committed directly, so this catches hand edits and systematic writer breakage on the next CI run,
not at write time.
"""

import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.kg.graph import dangling_links, scan_notes_dir
from chemclaw.kg.note import (
    Note,
    NoteError,
    external_record_id,
    known_note_types,
    note_relative_path,
    read_note,
    require_note_slug,
    resolves_outside_graph,
)
from chemclaw.kg.relations import RELATION_SIGNATURES, known_relations


def validate(notes_dir: Path) -> list[str]:
    """Return a list of human-readable problems in `notes_dir` (empty if clean)."""
    return validate_with_notes(notes_dir)[0]


def validate_with_notes(notes_dir: Path) -> tuple[list[str], list[Note]]:
    """The problems in `notes_dir`, plus the parseable notes the scan already read.

    Returning the notes lets `cli.validate_kg` run its citation checks without a second parse.
    """
    problems: list[str] = []
    # Notes paired with their file, so every message names a path, including for duplicate ids.
    located: list[tuple[Note, Path]] = []
    id_to_path: dict[str, Path] = {}

    # Same file scan as the indexer, but a strict loop: this reports unparseable notes that
    # `load_notes` skips.
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
        # The filename is an index key: `note_file_fingerprints` reads the id from `path.stem`, so a
        # mismatch would drop the note from the retrieval index diff.
        if path.stem != note.id:
            problems.append(
                f"note {note.id!r} is in {path}, whose filename says {path.stem!r} — "
                f"the file must be named {note.id + '.md'!r} "
                "(the note index keys on the filename and would skip this note)"
            )
        # The directory is an index key too: the writer derives the path from the type, so a
        # misfiled note would make the next write create a second file for the id, and the graph
        # keeps the first in path order.
        expected = note_relative_path(note.type, note.id)
        try:
            actual = path.relative_to(notes_dir).as_posix()
        except ValueError:
            actual = path.as_posix()
        if actual != expected:
            problems.append(
                f"note {note.id!r} of type {note.type!r} is at {actual}, but this system files "
                f"that type at {expected} — a re-write would create a second file for this id"
            )
        located.append((note, path))

    notes = [note for note, _ in located]
    problems.extend(
        f"note {source!r} links to unknown note {target!r}"
        for source, target in dangling_links(notes)
    )
    # Whole-corpus vocabulary checks, here rather than in the schema so an extended vocabulary is a
    # corpus-level decision rather than a per-write refusal. The vocabulary is core's plus what the
    # enabled bundles declare; the message names both places to add a name.
    problems.extend(
        _registry_problems(
            ((note, path, note.type) for note, path in located),
            known_note_types(),
            "type",
            "chemclaw.kg.note.KNOWN_NOTE_TYPES or a bundle's `note_types:`",
        )
    )
    problems.extend(
        _registry_problems(
            (
                (note, path, relation.rel)
                for note, path in located
                for relation in note.outgoing_relations()
            ),
            known_relations(),
            "relation",
            "chemclaw.kg.relations.KNOWN_RELATIONS or a bundle's `relations:`",
        )
    )
    problems.extend(_signature_problems(located))
    problems.extend(_malformed_targets(located))
    return problems, notes


def _signature_problems(located: list[tuple[Note, Path]]) -> list[str]:
    """Flag every typed edge whose endpoints contradict the relation's declared direction.

    Only relations in `RELATION_SIGNATURES` are checked, and a target end only when it resolves to a
    note in this corpus; dangling and external targets are other checks' findings. Without this,
    `related(graph, x, "product-of")` would mix both directions.
    """
    type_by_id = {note.id: note.type for note, _ in located}
    problems: list[str] = []
    for note, path in located:
        for relation in note.outgoing_relations():
            signature = RELATION_SIGNATURES.get(relation.rel)
            if signature is None:
                continue
            sources, targets = signature
            if sources is not None and note.type not in sources:
                problems.append(
                    f"note {note.id!r} in {path} asserts {relation.rel!r}, which runs from "
                    f"{sorted(sources)} notes — this note is a {note.type!r} "
                    "(the edge is probably written in the inverse direction)"
                )
            target_type = type_by_id.get(relation.to)
            if targets is not None and target_type is not None and target_type not in targets:
                problems.append(
                    f"note {note.id!r} in {path} asserts {relation.rel!r} toward "
                    f"{relation.to!r}, a {target_type!r} note — that relation targets "
                    f"{sorted(targets)} (the edge is probably written in the inverse direction)"
                )
    return problems


def _malformed_targets(located: list[tuple[Note, Path]]) -> list[str]:
    """Flag every link whose target is not a legal note slug.

    `split_link` returns whatever follows the colon (`[[a:b:c]]` gives `b:c`), which the indexer
    would mint as a node; the author is told it is not a note id rather than "unknown note".
    """
    problems: list[str] = []
    for note, path in located:
        for target in note.outgoing_links():
            if resolves_outside_graph(target):
                continue
            try:
                require_note_slug(target)
            except ValueError:
                problems.append(
                    f"note {note.id!r} in {path} links to {target!r}, which is not a valid "
                    "note id (check the [[...]] syntax — one colon separates relation from id)"
                )
    return problems


def external_citations(notes: list[Note]) -> list[tuple[str, str]]:
    """Every `(source id, target id)` link pointing into an external id namespace.

    `dangling_links` does not report these (it cannot see the store), so this lists the ones a store
    must answer for. A target defined in the corpus is not external whatever its prefix.
    """
    defined = {note.id for note in notes}
    return sorted(
        (note.id, target)
        for note in notes
        for target in note.outgoing_links()
        if resolves_outside_graph(target) and target not in defined
    )


@runtime_checkable
class RecordExistence(Protocol):
    """The one question this check asks of the ELN transcription tier.

    Declared here because `ingest` depends on `kg`; `cli.validate_kg` supplies the store.
    """

    async def known(self, reaction_ids: Sequence[str]) -> set[str]:
        """Which of `reaction_ids` the corpus holds."""
        ...


async def unresolved_citations(
    citations: list[tuple[str, str]], records: RecordExistence
) -> list[str]:
    """Report the external citations whose record `records` does not hold.

    Raises when the store is unreachable: a check that could not look must not pass silently.
    """
    wanted = [external_record_id(target) for _, target in citations]
    known = await records.known(wanted)
    return [
        f"note {source!r} cites {target!r}, and the record store does not hold "
        f"{external_record_id(target)!r} (the id the store was asked for)"
        for source, target in citations
        if external_record_id(target) not in known
    ]


def calc_citations(notes: list[Note]) -> list[tuple[str, str]]:
    """Every `(source id, calculation key)` a note's `calc_refs` cites.

    The schema checks only a key's shape; this feeds the existence check, so a mistyped key that no
    calculation produced is caught instead of handed to every reader.
    """
    return sorted((note.id, ref) for note in notes for ref in note.calc_refs)


@runtime_checkable
class CalculationExistence(Protocol):
    """The one question this check asks of the calculation cache.

    Declared here so `science.calc` is not a `kg` dependency; `cli.validate_kg` supplies the store.
    """

    async def known(self, keys: Sequence[str]) -> set[str]:
        """Which of `keys` the calculation store holds."""
        ...


async def unresolved_calc_refs(
    citations: list[tuple[str, str]], store: CalculationExistence
) -> list[str]:
    """Report the `calc_refs` whose calculation `store` does not hold.

    Raises on an unreachable database, as `unresolved_citations` does.
    """
    known = await store.known([key for _, key in citations])
    return [
        f"note {source!r} cites calculation {key!r} in calc_refs, and the calculation store "
        "does not hold that key"
        for source, key in citations
        if key not in known
    ]


def _registry_problems(
    values: Iterable[tuple[Note, Path, str]],
    registry: frozenset[str],
    label: str,
    registry_name: str,
) -> list[str]:
    """Flag every `(note, path, value)` whose value is outside `registry`.

    Shared by the note-type and relation checks.
    """
    return [
        f"note {note.id!r} in {path} uses unknown {label} {value!r} "
        f"(add it to {registry_name} if intended)"
        for note, path, value in values
        if value not in registry
    ]


def main() -> int:
    """CLI entry point: validate the notes dir; print problems; return exit code.

    A `ChemclawError` (e.g. an enabled connector bundle that does not exist, surfaced while
    resolving the vocabulary) is printed as a problem and fails the run, rather than crashing with a
    traceback.
    """
    notes_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else settings.knowledge_path
    if not notes_dir.exists():
        print(f"notes directory does not exist: {notes_dir}")
        return 1
    try:
        problems = validate(notes_dir)
    except ChemclawError as exc:
        print(f"cannot determine this deployment's note vocabulary: {exc}")
        return 1
    for problem in problems:
        print(problem)
    if problems:
        print(f"\n{len(problems)} problem(s) found in {notes_dir}")
        return 1
    print(f"OK: {notes_dir} is a valid knowledge graph")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
