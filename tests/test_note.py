"""Behavioral tests for the note schema and parser (plan steps 2.1, 2.2)."""

from pathlib import Path

import pytest

from kg.note import Note, NoteError, parse_note, read_note

_VALID = """---
id: compound-aspirin
type: compound
compound_smiles: CC(=O)Oc1ccccc1C(=O)O
tags: [nsaid, analgesic]
created_by: human
confidence: 0.9
---
Aspirin relates to [[reaction-acetylation]] and [[compound-salicylic-acid]].
See [[reaction-acetylation]] again (deduped).
"""


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_valid_note_parses(tmp_path: Path) -> None:
    """A well-formed note yields the typed fields, body, and deduped links."""
    note = parse_note(_write(tmp_path / "a.md", _VALID))
    assert note.id == "compound-aspirin"
    assert note.type == "compound"
    assert note.tags == ["nsaid", "analgesic"]
    assert note.confidence == 0.9
    assert note.outgoing_links() == ["reaction-acetylation", "compound-salicylic-acid"]


def test_missing_required_field_raises(tmp_path: Path) -> None:
    """A note without the required `type` fails validation with the file path (G4)."""
    with pytest.raises(NoteError, match="invalid note"):
        parse_note(_write(tmp_path / "b.md", "---\nid: x\n---\nbody\n"))


def test_malformed_frontmatter_raises(tmp_path: Path) -> None:
    """Broken YAML frontmatter is a clear error, not a crash (G4)."""
    with pytest.raises(NoteError, match="malformed frontmatter"):
        _write(tmp_path / "c.md", "---\nid: x\ntype: [unterminated\n---\nbody\n")
        parse_note(tmp_path / "c.md")


def test_confidence_out_of_range_raises(tmp_path: Path) -> None:
    """Confidence must be within 0–1."""
    with pytest.raises(NoteError):
        parse_note(_write(tmp_path / "d.md", "---\nid: x\ntype: t\nconfidence: 1.5\n---\n"))


def test_file_without_frontmatter_is_not_a_note(tmp_path: Path) -> None:
    """A plain Markdown file (e.g. a README) is not a note: read_note returns None."""
    assert read_note(_write(tmp_path / "README.md", "# Just docs\nno frontmatter\n")) is None
    with pytest.raises(NoteError, match="not a note"):
        parse_note(tmp_path / "README.md")


def test_agent_authored_provenance(tmp_path: Path) -> None:
    """created_by carries the GxP provenance line for the PR-gate."""
    note = parse_note(
        _write(tmp_path / "e.md", "---\nid: x\ntype: job-result\ncreated_by: agent\n---\n")
    )
    assert note.created_by == "agent"
    assert isinstance(note, Note)
