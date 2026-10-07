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
    """`mentioned_ids` reads the two note serializations real tools emit.

    The fixtures are verbatim shapes from tool results (a `gather_evidence` chunk envelope and a
    JSON-dumped note), so a serialization change breaks this rather than silently narrowing the
    scan.
    """
    gathered = '[{"content": "<retrieved-note-4216b6a377548e22 id=\\"rxn-suzuki-biaryl\\">\\nSuzuki'
    expanded = '{"note": {"id": "opt-suzuki-conditions", "type": "optimization-campaign", "tags":'

    assert mentioned_ids(gathered) == ["rxn-suzuki-biaryl"]
    assert mentioned_ids(expanded) == ["opt-suzuki-conditions"]


def test_mentioned_ids_counts_an_id_a_retrieved_body_cites() -> None:
    """A wikilink inside a returned note body was in front of the model, so it grounds a citation.

    The grounding check asks "did this turn see it", not "did the tool name it as its own result".
    """
    body = '{"note": {"id": "campaign-biaryl-scope"}, "body": "supersedes [[playbook-degassing]]"}'
    assert mentioned_ids(body) == ["campaign-biaryl-scope", "playbook-degassing"]


def test_mentioned_ids_deduplicates_and_keeps_first_seen_order() -> None:
    """Same contract as `cited_ids`, so the two readers stay interchangeable to a caller."""
    text = '{"id": "a-note"} {"id": "b-note"} {"id": "a-note"} [[b-note]] [[c-note]]'
    assert mentioned_ids(text) == ["a-note", "b-note", "c-note"]


def test_external_record_id_strips_whichever_prefix_matched() -> None:
    """The strip is driven by `EXTERNAL_ID_PREFIXES`, so growing the namespace cannot break the
    lookup.

    A two-entry tuple distinguishes this from a hand-rolled `removeprefix` of the one current value.
    """
    with mock.patch.object(note_module, "EXTERNAL_ID_PREFIXES", ("reaction-", "measurement-")):
        assert external_record_id("reaction-EXP-1001") == "EXP-1001"
        assert external_record_id("measurement-EXP-1001") == "EXP-1001"
    # An id in no external namespace is returned whole, so a graph note id survives the call.
    assert external_record_id("rxn-suzuki-biaryl") == "rxn-suzuki-biaryl"


def test_no_reader_hand_rolls_the_external_id_strip() -> None:
    """`external_record_id` stays the only definition of the external-prefix strip.

    A type checker cannot see a hand-rolled copy, so the rule is a scan: nothing outside this module
    strips an external prefix by literal.
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


# --------------------------------------------------------------------------------------------
# A calculation citation must accept every key the calculation store can write.
#
# `_CALC_REF` restates `CalculationKey`'s field patterns because `kg` cannot import `science`;
# these tests hold the restatement to its original so a cached key is always citable.
# --------------------------------------------------------------------------------------------


def test_every_calculation_key_the_store_accepts_can_be_cited() -> None:
    """Round-trip: `CalculationKey.as_str()` in, `Note.calc_refs` accepts it.

    Includes the calibrated key, the shape the calculation server returns and the cache holds.
    """
    from chemclaw.science.calc.store import CalculationKey

    keys = [
        CalculationKey(
            calc_type="xtb",
            calc_version="GFN2-xTB+tblite+0.4.0",
            input_hash="9ac385b135af0125",
            params_hash="72dc4f72005af2e5",
        ),
        # The `@` inside a version: `esol-delaney@2004`, named in the store's own comment.
        CalculationKey(
            calc_type="solubility",
            calc_version="esol-delaney@2004",
            input_hash="9ac385b135af0125",
            params_hash="72dc4f72005af2e5",
        ),
        # The `:` inside a version — a calibration offset. This is the arm that was refused.
        CalculationKey(
            calc_type="pka",
            calc_version="GFN2-xTB+tblite+0.4.0/cal-0.28733:-29.3116",
            input_hash="9ac385b135af0125",
            params_hash="72dc4f72005af2e5",
        ),
        # A hash the store admits and the note-side pattern's `[0-9a-f]+` did not.
        CalculationKey(
            calc_type="xtb",
            calc_version="GFN2-xTB",
            input_hash="ABC123",
            params_hash="Zz-_09",
        ),
    ]
    for key in keys:
        flat = key.as_str()
        assert Note(id="n", type="observation", calc_refs=[flat]).calc_refs == [flat]
        assert Note(
            id="n", type="observation", artifact_refs=[f"{flat}#hessian"]
        ).artifact_refs == [f"{flat}#hessian"]


def test_the_citation_pattern_restates_the_store_s_own_field_patterns() -> None:
    """The citation pattern restates the store's field patterns, segment by segment.

    Read off `model_fields` rather than retyped, so loosening or tightening a `CalculationKey` field
    fails here.
    """
    from chemclaw.science.calc.store import CalculationKey

    def store_pattern(field: str) -> str:
        (meta,) = [m for m in CalculationKey.model_fields[field].metadata if hasattr(m, "pattern")]
        return str(meta.pattern).removeprefix("^").removesuffix("$")

    assert note_module._CALC_TYPE == store_pattern("calc_type")
    assert note_module._CALC_VERSION == store_pattern("calc_version")
    assert note_module._CALC_HASH == store_pattern("input_hash")
    assert note_module._CALC_HASH == store_pattern("params_hash")


def test_a_calculation_citation_still_refuses_what_is_not_a_key() -> None:
    """Widening the version and the hashes must not widen the refusal this field exists for.

    The point of validating the shape at all is that a note citing "the GFN2 run" is a crosslink
    nothing can resolve. A key needs its `@` and both of its colons.
    """
    from pydantic import ValidationError as _ValidationError

    for bad in ["the GFN2 run", "xtb@GFN2-xTB", "xtb@GFN2-xTB:onlyonehash", "@:::", "xtb:a:b"]:
        with pytest.raises(_ValidationError):
            Note(id="n", type="observation", calc_refs=[bad])


def test_a_note_written_to_the_graph_carries_no_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A note written to the graph carries no configured credential.

    Notes are committed and pushed as soon as they are learned, and a note body is model prose that
    may contain a pasted secret; a secret in a pushed commit cannot be corrected away. So
    `render_note` redacts the process's credential inventory to `***` rather than refusing: a false
    positive stays visible and correctable.
    """
    from chemclaw.kg.record import _note_file

    monkeypatch.setenv("CHEMCLAW_LLM_API_KEY", "MARKERLLMKEY9a4x")
    monkeypatch.setattr(
        "chemclaw.core.config.settings.postgres_dsn",
        "postgresql://svc:MARKERPGPW9a1x@wh.internal/db",
    )
    note = Note(
        id="leaky-note",
        type="observation",
        created_by="agent",
        body=(
            "The chemist pasted: export CHEMCLAW_LLM_API_KEY=MARKERLLMKEY9a4x and the warehouse "
            "DSN postgresql://svc:MARKERPGPW9a1x@wh.internal/db while asking about CX-4711."
        ),
    )
    content = _note_file(note, "knowledge").content
    assert "MARKERLLMKEY9a4x" not in content, content
    assert "MARKERPGPW9a1x" not in content, content
    # The note is still the note: the prose around the secret, the frontmatter and the compound
    # id all survive. Redaction is not truncation.
    assert "CX-4711" in content
    assert "created_by: agent" in content
    assert "id: leaky-note" in content


def test_a_committed_note_carries_no_credential_a_driver_quoted_back_as_json() -> None:
    r"""A credential this process does not hold, quoted back as escaped JSON, is redacted too.

    The structural rules must match a driver's error carrying `{\"password\": \"...\"}` inside a
    JSON string, where a literal backslash sits before the separator (`core/logging._KEY_FRAMING`).
    """
    import json

    from chemclaw.kg.record import _note_file

    # Two credentials from the two different key-anchored rules, so a regression in either is
    # visible on *this* exit path rather than only in `tests/test_logging.py`: `password` belongs to
    # the libpq rule, `api_key` to the compound key-name rule.
    quoted = json.dumps(
        json.dumps({"connection": {"password": "W4rehousePw1", "api_key": "sk_live_9f3a2b1c8d7"}})
    )
    note = Note(
        id="driver-error-note",
        type="observation",
        created_by="agent",
        body=f"The warehouse refused the binding for CX-4711. Its error was: {quoted}",
    )

    content = _note_file(note, "knowledge").content

    for credential in ("W4rehousePw1", "sk_live_9f3a2b1c8d7"):
        assert credential not in content, content
    assert "CX-4711" in content, content
