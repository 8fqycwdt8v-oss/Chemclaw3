"""A note as the columns of `kg_notes` and `kg_edges`, and back.

The mapping is pure so the schema (`infra/sql/128_kg_notes.sql`, `129_kg_note_search.sql`) and the
model agree in one place: `id`, `type`, `created_by`, the validity window and `body` are columns;
every other `Note` field is the `frontmatter` JSONB, in the shape
`Note.model_dump(mode="json", exclude_none=True)` gives. A field the model grows needs no migration.
`content_sha` is the digest of the canonical render of the note as stored: `render_note` of
`canonical_note(n)`, with credentials redacted. A store keeps `canonical_note`, so what it holds is
what that text parses back to. That is the text `record_note` commits unless reading the file back
changes the note (whitespace at the edges of a body, CRLF, U+2028); then the file's bytes and this
digest differ, and so do both from a hand-laid file. Compare stores by canonical note, never by
digest.
"""

import hashlib
from collections.abc import Mapping
from typing import Any

from chemclaw.core.logging import redact_secrets
from chemclaw.kg.note import Note, NoteError, note_from_text
from chemclaw.kg.render import render_note
from chemclaw.kg.search import search_text

#: The tenant every row belongs to until the tenancy wave gives deployments more than one.
DEFAULT_TENANT = "default"

#: The `Note` fields that have a column of their own; the rest live in `frontmatter`.
COLUMN_FIELDS = frozenset({"id", "type", "created_by", "valid_from", "valid_to", "body"})


def canonical_note(note: Note) -> Note:
    """`note` as its redacted canonical file text reads back: what a store keeps and serves.

    Redaction rather than refusal, as in the file writer: a credential pasted into a body must not
    be stored, and a false positive is visible and correctable. Reading the text back is what the
    git store does with the file, so both stores serve the same note, including the whitespace the
    file format normalises. Raises `NoteError` when redaction leaves a value the schema refuses.
    """
    parsed = note_from_text(redact_secrets(render_note(note)), note.id)
    if parsed is None:
        raise NoteError(f"{note.id}: the canonical render of a note has no frontmatter")
    return parsed


def content_sha(note: Note) -> str:
    """`sha256:<hex>` of the redacted render of `note`, as given.

    For a note already in canonical form (`canonical_note`, what a store holds) this is the stored
    digest. For any other it is the digest of the text `kg.record` would write for that note, which
    differs from the stored one when reading that text back changes the note.
    """
    canonical = redact_secrets(render_note(note))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def note_columns(note: Note) -> dict[str, Any]:
    """The values of one `kg_notes` row for `note`; `frontmatter` is a plain dict of JSON values.

    `search_text` is the lowercased haystack of `kg.search.search_text`, which a substring search
    matches against and a trigram index serves.
    """
    return {
        "id": note.id,
        "type": note.type,
        "frontmatter": note.model_dump(mode="json", exclude=set(COLUMN_FIELDS), exclude_none=True),
        "body": note.body,
        "created_by": note.created_by,
        "valid_from": note.valid_from,
        "valid_to": note.valid_to,
        "content_sha": content_sha(note),
        "search_text": search_text(note).lower(),
    }


def edge_columns(note: Note) -> list[dict[str, Any]]:
    """One `kg_edges` row per typed edge `note` asserts, from its body links and `relations:`."""
    return [
        {
            "src": note.id,
            "rel": relation.rel,
            "dst": relation.to,
            "valid_from": relation.valid_from,
            "valid_to": relation.valid_to,
            "confidence": relation.confidence,
        }
        for relation in note.outgoing_relations()
    ]


def note_from_columns(row: Mapping[str, Any]) -> Note:
    """The `Note` a `kg_notes` row holds; an invalid row raises `pydantic.ValidationError`."""
    return Note(
        id=row["id"],
        type=row["type"],
        created_by=row["created_by"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        body=row["body"],
        **row["frontmatter"],
    )
