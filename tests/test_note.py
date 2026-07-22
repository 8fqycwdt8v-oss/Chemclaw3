"""Behavioral tests for the note schema and parser (plan steps 2.1, 2.2)."""

from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from chemclaw.errors import ChemclawError
from kg.note import Note, NoteError, parse_note, read_note


def test_is_current_honors_validity_window() -> None:
    """`is_current` treats `valid_from`/`valid_to` as inclusive bounds; absent bounds are open."""
    as_of = date(2026, 6, 1)
    assert Note(id="n", type="reaction").is_current(as_of)  # no bounds → always current
    # Expired: as_of past valid_to (and the boundary day itself is still current).
    assert not Note(id="n", type="reaction", valid_to=date(2026, 5, 31)).is_current(as_of)
    assert Note(id="n", type="reaction", valid_to=date(2026, 6, 1)).is_current(as_of)
    # Not yet valid: as_of before valid_from (boundary inclusive).
    assert not Note(id="n", type="reaction", valid_from=date(2026, 6, 2)).is_current(as_of)
    assert Note(id="n", type="reaction", valid_from=date(2026, 6, 1)).is_current(as_of)


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


def test_frontmatter_body_key_does_not_crash(tmp_path: Path) -> None:
    """A stray `body:` frontmatter key is ignored, not a TypeError (G4)."""
    text = "---\nid: x\ntype: t\nbody: stray\n---\nreal body\n"
    note = parse_note(_write(tmp_path / "f.md", text))
    assert note.body.strip() == "real body"


@pytest.mark.parametrize(
    "bad",
    [
        "a/../../../../etc/x",  # path traversal out of the repo
        "a/b",  # any path separator
        "a..b",  # invalid git ref component even though slug chars
        ".hidden",  # leading dot (dotfile / ref rules)
        "-flag",  # leading dash reads as a CLI flag
        "a b",  # whitespace
        "reaction-x.",  # trailing dot: git rejects `note/reaction-x.` as a ref
        "reaction-x.lock",  # `.lock` suffix: git reserves it, branch creation fails
    ],
)
def test_unsafe_id_and_type_rejected_at_model(bad: str) -> None:
    """Ids/types become file paths and git refs; anything non-slug is refused (G4)."""
    with pytest.raises(ValidationError, match="safe note slug"):
        Note(id=bad, type="compound")
    with pytest.raises(ValidationError, match="safe note slug"):
        Note(id="ok", type=bad)


def test_unsafe_id_from_file_raises_note_error(tmp_path: Path) -> None:
    """A traversal id arriving via parsed frontmatter (external data) is a NoteError."""
    text = "---\nid: a/../../../../etc/x\ntype: t\n---\nbody\n"
    with pytest.raises(NoteError, match="invalid note"):
        parse_note(_write(tmp_path / "g.md", text))


def test_note_error_is_chemclaw_error() -> None:
    """Bad note data joins the one catchable bad-data contract (and stays a ValueError)."""
    assert issubclass(NoteError, ChemclawError)
    assert issubclass(NoteError, ValueError)


def test_agent_authored_provenance(tmp_path: Path) -> None:
    """created_by carries the GxP provenance line for the PR-gate."""
    note = parse_note(
        _write(tmp_path / "e.md", "---\nid: x\ntype: job-result\ncreated_by: agent\n---\n")
    )
    assert note.created_by == "agent"
    assert isinstance(note, Note)
