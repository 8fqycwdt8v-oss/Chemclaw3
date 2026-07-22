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
from pydantic import BaseModel, Field, ValidationError, field_validator

from chemclaw.errors import ChemclawError

# [[target]] wikilinks in the body. Targets are note ids; `[[ ... ]]` only. Public because
# the report layer strips the same markup from evidence excerpts — one pattern, no drift.
WIKILINK = re.compile(r"\[\[([^\[\]]+)\]\]")

# `id` and `type` become file-path segments (`knowledge/<type>/<id>.md`) and a git
# branch (`note/<id>`) in the PR-gate, and ELN entry ids flow in from external JSON.
# Constraining them to a plain slug at the model is the traversal/ref-injection
# barrier: no `/`, no leading `.`, nothing git or the filesystem could reinterpret.
# `_` is included because BO note ids embed registry objective names (e.g.
# `bo-reizman_suzuki-<sha>`).
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class Note(BaseModel):
    """One knowledge-graph note: its frontmatter metadata plus its Markdown body.

    `created_by` is the GxP provenance line: `agent`-authored notes must pass the
    PR-gate before merge (D-005). `confidence` (0–1) and `valid_from`/`valid_to`
    let a later query weigh and time-scope evidence.
    """

    id: str = Field(min_length=1)
    type: str = Field(min_length=1)

    @field_validator("id", "type")
    @classmethod
    def _slug_only(cls, value: str) -> str:
        """Reject path/ref metacharacters — see the `_SLUG` rationale above.

        A few git ref rules the character class alone does not cover are refused
        explicitly (defense in depth), because the slug becomes the `note/<id>`
        branch in the PR-gate: `..` (an invalid ref component, e.g. `a..b`), a
        trailing `.`, and a `.lock` suffix — git rejects all three, so an id that
        passed the schema would otherwise only fail later at branch creation.
        """
        if (
            ".." in value
            or value.endswith(".")
            or value.endswith(".lock")
            or not _SLUG.fullmatch(value)
        ):
            raise ValueError(
                f"{value!r} is not a safe note slug (allowed: {_SLUG.pattern}; "
                "no '..', trailing '.', or '.lock' suffix)"
            )
        return value

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
        for match in WIKILINK.findall(self.body):
            target = match.strip()
            if target:
                ordered.setdefault(target, None)
        return list(ordered)

    def is_current(self, as_of: date) -> bool:
        """Whether the note is inside its validity window on `as_of` (bounds inclusive).

        `valid_from`/`valid_to` time-scope a note; either may be absent (open-ended). Discovery
        retrieval excludes non-current notes so a not-yet-valid or superseded/expired entry is not
        served as *current* evidence (GxP freshness — audit KM-7). The note is never deleted: it
        stays in Git and is still reachable by explicit id, it is only dropped from current-evidence
        sweeps.
        """
        if self.valid_from is not None and as_of < self.valid_from:
            return False
        if self.valid_to is not None and as_of > self.valid_to:
            return False
        return True


class NoteError(ChemclawError):
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
