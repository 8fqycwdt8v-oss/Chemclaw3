"""Knowledge-graph validation, usable as a CLI in CI (plan step 2.4).

Checks a notes directory for the failure modes that would corrupt the graph:
unparseable/invalid notes, duplicate ids, links to unknown notes, and note types or
relations outside the declared vocabulary. Run as
`python -m chemclaw.kg.validate [notes_dir]`; it exits non-zero if any problem is found,
so it gates the PR that adds or edits notes (D-005).

**What it no longer checks is the hazard content of a procedure.** D-080's per-note gate
screened an agent-authored `## Procedure` and refused it if the flags its structures raised
were not documented. Safety became an ordinary MCP capability
(`D-2026-08-15-safety-is-a-tool-not-a-gate`), so the screen that gate called no longer lives
in this repository and CI no longer runs one on a reviewer's behalf. The corpus is still
gated — by the human who reviews the PR, which is what a PR-gate always meant.
"""

import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.kg.graph import dangling_links, scan_notes_dir
from chemclaw.kg.note import Note, NoteError, known_note_types, read_note, resolves_outside_graph
from chemclaw.kg.relations import known_relations


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
    # Whole-corpus vocabulary checks (gap KNW-6, STO-8). Both are checked here rather than in the
    # `Note` schema so the agent can *propose* a genuinely new type or relation and a human sees it
    # at the PR-gate — while a typo, which would make the note or the edge unfindable by every
    # filter keyed on it, cannot reach the graph. `kg-validate` runs on that same PR.
    #
    # The vocabulary is core's own set **plus what the enabled bundles declare**: `bo-candidate` is
    # minted by a connector, so a deployment's vocabulary is a property of which bundles it runs,
    # not of this package alone. Both accessors resolve that union; the message names both places a
    # reader can add a name.
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
    return problems


def external_citations(notes: list[Note]) -> list[tuple[str, str]]:
    """Every `(source id, target id)` link pointing into an external id namespace.

    Since D-2026-08-25 an ELN transcription is a row in `reaction_records` rather than a file in
    `knowledge/`, so `dangling_links` deliberately does not report `[[reaction-<id>]]` as broken —
    it cannot see the store. That leaves the citations campaigns and playbooks are built from
    unchecked by anything, which is how a typo'd run id would merge. This is the other half: the
    links a *store* has to answer for.
    """
    return sorted(
        (note.id, target)
        for note in notes
        for target in note.outgoing_links()
        if resolves_outside_graph(target)
    )


@runtime_checkable
class RecordExistence(Protocol):
    """The one question this check asks of the ELN transcription tier.

    Declared here rather than imported, for the reason `retrieval.retrievers.ReactionMetadata`
    gives: `ingest` depends on `kg`, so importing the store back would invert the layering for a
    one-method need. The caller supplies it — `cli.validate_kg`, which is allowed to see both.
    """

    async def known(self, reaction_ids: Sequence[str]) -> set[str]:
        """Which of `reaction_ids` the corpus holds."""
        ...


async def unresolved_citations(
    citations: list[tuple[str, str]], records: RecordExistence
) -> list[str]:
    """Report the external citations whose record `records` does not hold.

    Raises whatever the store raises when the database is unreachable — the caller decides what an
    unrunnable check means, because a validator that silently passes when it could not look is a
    claim that a control exists.
    """
    wanted = [target.removeprefix("reaction-") for _, target in citations]
    known = await records.known(wanted)
    return [
        f"note {source!r} cites reaction record {target!r}, which the corpus does not hold"
        for source, target in citations
        if target.removeprefix("reaction-") not in known
    ]


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
    """CLI entry point: validate the notes dir; print problems; return exit code.

    A `ChemclawError` is reported as a problem rather than raised. `validate` resolves the effective
    vocabulary through `known_note_types()`, which asks the connector registry what the enabled
    bundles declare — so a deployment whose `CHEMCLAW_CONNECTORS_ENABLED` names a bundle it does not
    ship makes *this* gate die, with a traceback, about connectors. That is a real misconfiguration
    and must still fail; it must not fail looking like a crash in the graph validator. Every sibling
    validator prints its configuration errors, and this one now does too.
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


def notes_in(notes_dir: Path) -> list[Note]:
    """Every parseable note under `notes_dir` — the corpus the citation check reads."""
    parsed: list[Note] = []
    for path, _ in scan_notes_dir(notes_dir):
        try:
            note = read_note(path)
        except NoteError:
            continue  # already reported by `validate`
        if note is not None:
            parsed.append(note)
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
