"""Behavioral tests for the note schema and parser (plan steps 2.1, 2.2)."""

from datetime import date
from pathlib import Path
from unittest import mock

import pytest
from pydantic import ValidationError

import chemclaw.kg.note as note_module
from chemclaw.core.errors import ChemclawError
from chemclaw.kg.note import (
    Note,
    NoteError,
    external_record_id,
    mentioned_ids,
    parse_note,
    read_note,
)


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


def test_note_is_immutable() -> None:
    """A note is a frozen value object — the graph cache shares instances, so mutation must fail."""
    note = Note(id="n", type="reaction")
    with pytest.raises(ValidationError):
        note.confidence = 0.5


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


def test_bitemporal_window_round_trips(tmp_path: Path) -> None:
    """A note with a well-ordered validity window parses and keeps both bounds (F10-G2)."""
    text = "---\nid: x\ntype: reaction\nvalid_from: 2026-01-01\nvalid_to: 2026-06-30\n---\nbody\n"
    note = parse_note(_write(tmp_path / "d.md", text))
    assert str(note.valid_from) == "2026-01-01"
    assert str(note.valid_to) == "2026-06-30"


def test_reversed_validity_window_is_rejected(tmp_path: Path) -> None:
    """`valid_to` before `valid_from` is a nonsensical window, refused at the schema boundary."""
    text = "---\nid: x\ntype: reaction\nvalid_from: 2026-06-30\nvalid_to: 2026-01-01\n---\nbody\n"
    with pytest.raises(NoteError, match="valid_to .* is before valid_from"):
        parse_note(_write(tmp_path / "e.md", text))


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


def test_non_string_frontmatter_key_raises_note_error(tmp_path: Path) -> None:
    """YAML keys parsed as non-strings (bare dates, ints) are a NoteError, not a TypeError (G4)."""
    text = "---\nid: x\ntype: t\n2020-01-01: oops\n---\nbody\n"
    with pytest.raises(NoteError, match="malformed frontmatter"):
        parse_note(_write(tmp_path / "h.md", text))


def test_non_utf8_note_raises_note_error(tmp_path: Path) -> None:
    """A note saved in a non-UTF-8 encoding (e.g. Latin-1) is a NoteError, not a crash (G4)."""
    path = tmp_path / "latin1.md"
    path.write_bytes("---\nid: x\ntype: t\n---\nl\xf6slich\n".encode("latin-1"))
    with pytest.raises(NoteError, match="unreadable"):
        read_note(path)


def test_vanished_note_file_raises_note_error(tmp_path: Path) -> None:
    """A file that disappears before the read (e.g. a `git pull` mid-scan) is a NoteError (G4)."""
    with pytest.raises(NoteError, match="unreadable"):
        read_note(tmp_path / "gone.md")


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
    """created_by carries the provenance line for the PR-gate."""
    note = parse_note(
        _write(tmp_path / "e.md", "---\nid: x\ntype: job-result\ncreated_by: agent\n---\n")
    )
    assert note.created_by == "agent"
    assert isinstance(note, Note)


def test_mentioned_ids_reads_the_serializations_the_real_tools_emit() -> None:
    """The drift guard `mentioned_ids`' comment promises.

    `mentioned_ids` scans for this system's *own* two note serializations, which is what makes
    scanning honest rather than a guess about arbitrary text — and worthless the moment a tool
    starts emitting a third. These two fixtures are verbatim shapes taken off a live run's tool
    results (`docs/archive/live-grounded-2026-08-03.md`): a `gather_evidence` chunk envelope and a
    JSON-dumped note. A serialization change breaks this instead of silently narrowing the scan
    back to what it was.
    """
    gathered = '[{"content": "<retrieved-note-4216b6a377548e22 id=\\"rxn-suzuki-biaryl\\">\\nSuzuki'
    expanded = '{"note": {"id": "opt-suzuki-conditions", "type": "optimization-campaign", "tags":'

    assert mentioned_ids(gathered) == ["rxn-suzuki-biaryl"]
    assert mentioned_ids(expanded) == ["opt-suzuki-conditions"]


def test_mentioned_ids_counts_an_id_a_retrieved_body_cites() -> None:
    """A wikilink inside a returned note body was in front of the model, so it grounds a citation.

    The alternative reading — only ids the tool named as *its own* result count — would flag an
    answer for repeating a link it demonstrably read, which is a stricter question than "did this
    turn see it" and not the one the grounding check asks.
    """
    body = '{"note": {"id": "campaign-biaryl-scope"}, "body": "supersedes [[playbook-degassing]]"}'
    assert mentioned_ids(body) == ["campaign-biaryl-scope", "playbook-degassing"]


def test_mentioned_ids_deduplicates_and_keeps_first_seen_order() -> None:
    """Same contract as `cited_ids`, so the two readers stay interchangeable to a caller."""
    text = '{"id": "a-note"} {"id": "b-note"} {"id": "a-note"} [[b-note]] [[c-note]]'
    assert mentioned_ids(text) == ["a-note", "b-note", "c-note"]


def test_external_record_id_strips_whichever_prefix_matched() -> None:
    """The strip is driven by the constant, so growing the namespace cannot break the lookup.

    `EXTERNAL_ID_PREFIXES` is a *tuple* and has one entry today, which is exactly what makes a
    hand-rolled `removeprefix("reaction-")` look correct while being a latent defect: it reads
    the constant's only current value rather than the constant. Driving the function over a
    two-entry tuple is what tells the two apart — a hand-rolled strip returns the id with the
    second prefix still attached, and the store is then queried for something that cannot exist.
    """
    with mock.patch.object(note_module, "EXTERNAL_ID_PREFIXES", ("reaction-", "measurement-")):
        assert external_record_id("reaction-EXP-1001") == "EXP-1001"
        assert external_record_id("measurement-EXP-1001") == "EXP-1001"
    # An id in no external namespace is returned whole, so a graph note id survives the call.
    assert external_record_id("rxn-suzuki-biaryl") == "rxn-suzuki-biaryl"


def test_no_reader_hand_rolls_the_external_id_strip() -> None:
    """`external_record_id` has one definition, and this is what keeps it the only one.

    Its own docstring names the hand-rolled form as the defect it exists to prevent, and two
    readers spelled it anyway — `agent.graph_tools.expand_note` and `agent.protocol_tools`, both
    of which already imported from this module. Prose did not stop that and a type checker
    cannot see it, so the rule is a scan: nothing outside this module's own docstring may strip
    an external prefix by literal.
    """
    src = Path(__file__).resolve().parent.parent / "src" / "chemclaw"
    offenders = [
        path.relative_to(src).as_posix()
        for path in src.rglob("*.py")
        if path.name != "note.py"
        for prefix in note_module.EXTERNAL_ID_PREFIXES
        if f'removeprefix("{prefix}")' in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"hand-rolled external-id strip, use external_record_id(): {offenders}"
