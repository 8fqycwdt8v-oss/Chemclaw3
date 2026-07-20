"""Render a Note back to Markdown-with-frontmatter (plan step 2.6).

The inverse of `kg.note.parse_note`: turns a validated `Note` into the exact file
form the graph stores, so the write path (PR-gate) and the read path share one
serialization. Round-trips: `parse_note(write(render_note(n))) == n`.
"""

import frontmatter

from kg.note import Note


def render_note(note: Note) -> str:
    """Serialize a note to a Markdown string with a YAML frontmatter header.

    Null fields are omitted to keep the frontmatter minimal; the body follows the
    header. `valid_from`/`valid_to` serialize as ISO dates via YAML.
    """
    metadata = note.model_dump(exclude={"body"}, exclude_none=True, mode="python")
    post = frontmatter.Post(note.body, **metadata)
    return str(frontmatter.dumps(post))  # dumps() is untyped (returns Any)
