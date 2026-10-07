r"""Render a Note back to Markdown-with-frontmatter, the inverse of `kg.note.parse_note`.

The write path (`kg/record.py`) and the read path share this one serialization.
`parse_note(write(render_note(n))) == n` up to two body normalisations: `python-frontmatter` strips
surrounding whitespace, and `Path.read_text` translates line endings. Every frontmatter field
round-trips exactly.

Empty-default fields are rendered on purpose (`exclude_none`, not `exclude_defaults`): the writer
treats "nothing staged" as "no change", so the rendering must stay byte-stable; changing its shape
is a corpus migration.
"""

import frontmatter

from chemclaw.kg.note import Note


def render_note(note: Note) -> str:
    """Serialize a note to a Markdown string with a YAML frontmatter header.

    Null fields are omitted; `valid_from`/`valid_to` serialize as ISO dates.
    """
    metadata = note.model_dump(exclude={"body"}, exclude_none=True, mode="python")
    post = frontmatter.Post(note.body, **metadata)
    return str(frontmatter.dumps(post))  # dumps() is untyped (returns Any)
