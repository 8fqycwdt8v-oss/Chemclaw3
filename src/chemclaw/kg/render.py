r"""Render a Note back to Markdown-with-frontmatter (plan step 2.6).

The inverse of `chemclaw.kg.note.parse_note`: turns a validated `Note` into the exact file
form the graph stores, so the write path (PR-gate) and the read path share one serialization.

**Round-trips: `parse_note(write(render_note(n))) == n`, up to two normalisations of the body**,
both measured by generating notes rather than assumed (`tests/test_properties_core.py`). The
equation used to be written here unqualified and is not one:

- `python-frontmatter` strips the content it parses, so a body of `" "` returns as `""`.
- `Path.read_text` translates line endings, so a body containing `\r` returns with `\n`.

Neither distinction survives Markdown rendering, so neither is worth defending — but a note whose
body is *only* whitespace comes back empty, and a docstring that promised equality would have sent
whoever hit that looking for a schema bug. Every frontmatter field round-trips exactly.
"""

import frontmatter

from chemclaw.kg.note import Note


def render_note(note: Note) -> str:
    """Serialize a note to a Markdown string with a YAML frontmatter header.

    Null fields are omitted to keep the frontmatter minimal; the body follows the
    header. `valid_from`/`valid_to` serialize as ISO dates via YAML.
    """
    metadata = note.model_dump(exclude={"body"}, exclude_none=True, mode="python")
    post = frontmatter.Post(note.body, **metadata)
    return str(frontmatter.dumps(post))  # dumps() is untyped (returns Any)
