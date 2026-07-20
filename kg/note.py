"""Knowledge-graph note: the frontmatter schema and parser (plan steps 2.1, 2.2).

A note is a Markdown file with a YAML frontmatter header (structured, queryable)
and a Markdown body whose `[[wikilinks]]` encode relations to other notes by id
(D-004). This module is the single source of the note schema and the only parser;
malformed frontmatter or an invalid note yields a clear `NoteError` with the file
context, never a crash (G4).
"""

import re
from datetime import date
from pathlib import Path
from typing import Literal

import frontmatter
import yaml
from pydantic import BaseModel, Field, ValidationError

# [[target]] wikilinks in the body. Targets are note ids; `[[ ... ]]` only.
_WIKILINK = re.compile(r"\[\[([^\[\]]+)\]\]")


class Note(BaseModel):
    """One knowledge-graph note: its frontmatter metadata plus its Markdown body.

    `created_by` is the GxP provenance line: `agent`-authored notes must pass the
    PR-gate before merge (D-005). `confidence` (0–1) and `valid_from`/`valid_to`
    let a later query weigh and time-scope evidence.
    """

    id: str = Field(min_length=1)
    type: str = Field(min_length=1)
    compound_smiles: str | None = None
    tags: list[str] = Field(default_factory=list)
    created_by: Literal["human", "agent"] = "human"
    source: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    valid_from: date | None = None
    valid_to: date | None = None
    body: str = ""

    def outgoing_links(self) -> list[str]:
        """The ids this note links to, from `[[wikilinks]]` in its body.

        Deduplicated, preserving first-seen order, so a note that references the
        same target twice yields one edge.
        """
        ordered: dict[str, None] = {}
        for match in _WIKILINK.findall(self.body):
            target = match.strip()
            if target:
                ordered.setdefault(target, None)
        return list(ordered)


class NoteError(ValueError):
    """A note file could not be parsed or failed schema validation."""


def read_note(path: Path) -> Note | None:
    """Parse a note file; return None if it has no frontmatter (not a note).

    A file with malformed YAML frontmatter, or valid frontmatter that fails the
    schema, raises `NoteError` with the path. A plain Markdown file with no
    frontmatter (e.g. a README) is not a note and returns None.
    """
    try:
        post = frontmatter.loads(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise NoteError(f"{path}: malformed frontmatter: {exc}") from exc
    if not post.metadata:
        return None
    # The Markdown body is authoritative; a stray `body:` frontmatter key must not
    # collide with the body kwarg (which would be an uncaught TypeError).
    metadata = {key: value for key, value in post.metadata.items() if key != "body"}
    try:
        return Note(body=post.content, **metadata)
    except ValidationError as exc:
        raise NoteError(f"{path}: invalid note: {exc}") from exc


def parse_note(path: Path) -> Note:
    """Parse a file that must be a note, raising `NoteError` otherwise."""
    note = read_note(path)
    if note is None:
        raise NoteError(f"{path}: no frontmatter — not a note")
    return note
