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
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from chemclaw.core.errors import ChemclawError
from chemclaw.kg import relations

# [[target]] wikilinks in the body. Targets are note ids; `[[ ... ]]` only. Public because
# the report layer strips the same markup from evidence excerpts — one pattern, no drift.
WIKILINK = re.compile(r"\[\[([^\[\]]+)\]\]")


def split_link(target: str) -> tuple[str, str]:
    """Split a wikilink's inside into `(relation, note id)`.

    `[[precursor-of:compound-x]]` is a *typed* edge; a bare `[[compound-x]]` is a citation and
    yields `DEFAULT_RELATION`. The syntax was free to take: `_SLUG` excludes `:`, so a colon form
    previously parsed as one dangling id and failed `kg-validate` — nothing in any corpus could
    already be relying on it (STO-8).

    A target containing a colon but no relation before it (`[[:x]]`), or no id after it
    (`[[rel:]]`), is returned as a plain citation of the whole string, which then fails the
    unknown-note check with the text the author actually wrote rather than a silently repaired
    version of it.
    """
    relation, separator, note_id = target.partition(":")
    relation, note_id = relation.strip(), note_id.strip()
    if not separator or not relation or not note_id:
        return relations.DEFAULT_RELATION, target.strip()
    return relation, note_id


def cited_links(text: str) -> list[tuple[str, str]]:
    """Every `(relation, note id)` a body cites, deduplicated by pair in first-seen order.

    Deduplicated on the *pair*, not the id: a note may legitimately stand in two relations to the
    same target (a compound that is both a precursor and a product of a reaction), and collapsing
    those would lose the very information typing the edges exists to record.
    """
    ordered: dict[tuple[str, str], None] = {}
    for match in WIKILINK.findall(text):
        relation, note_id = split_link(match)
        if note_id:
            ordered.setdefault((relation, note_id), None)
    return list(ordered)


def note_id_for_reaction(record_id: str) -> str:
    """The `reaction` note id for a fingerprint-index record id.

    One definition, because three callers were each spelling `f"reaction-{id}"` themselves and one
    of them did not. `connectors.rxnfp.similar_reactions` returned the raw index key while
    `retrieval.retrievers` and the ELN ingest both prefixed it, so a chemist handed a search hit
    straight to `expand_note` was told the note did not exist — while it sat on disk under the
    prefixed name. Two spellings of one id is how a search stops reaching the thing it found.
    """
    return f"reaction-{record_id}"


def cited_ids(text: str) -> list[str]:
    """Extract the note ids a body of text cites via `[[wikilinks]]`, stripped and deduped.

    The one extraction every citation reader shares (`Note.outgoing_links`, the answer verifier):
    each target is stripped (a padded `[[ id ]]` resolves to `id`, matching the slug schema) and
    empties are dropped, preserving first-seen order so a repeated citation yields one id. Kept here
    beside `WIKILINK` so the pattern and its normalization have exactly one home and cannot drift.

    Relation-typed links contribute their *target*, so a caller asking "what does this note point
    at" is unaffected by whether the author typed the edge — which is what let typed links land
    without touching `chemclaw.kg.validate`'s dangling-link check or the answer verifier.
    """
    ordered: dict[str, None] = {}
    for _, note_id in cited_links(text):
        ordered.setdefault(note_id, None)
    return list(ordered)


# `id` and `type` become file-path segments (`knowledge/<type>/<id>.md`) and a git
# branch (`note/<id>`) in the PR-gate, and ELN entry ids flow in from external JSON.
# Constraining them to a plain slug at the model is the traversal/ref-injection
# barrier: no `/`, no leading `.`, nothing git or the filesystem could reinterpret.
# `_` is included because BO note ids embed registry objective names (e.g.
# `bo-reizman_suzuki-<sha>`).
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

# `CalculationKey.as_str()`: `calc_type@calc_version:input_hash:params_hash`. The version segment
# is the loose one on purpose — it carries a method name and a build string
# (`GFN2-xTB+tblite+0.4.0`), so it is matched as "anything but a colon" rather than enumerated.
# The point of validating the shape at all is that a note citing `"the GFN2 run"` in this field is
# a crosslink nothing can resolve, and it should fail at the PR-gate rather than silently.
_CALC_REF = re.compile(r"^[^\s@:]+@[^\s:]+:[0-9a-f]+:[0-9a-f]+$")


# Every note type this system mints, with what it means. Previously `type` was an unconstrained
# slug written from nine different call sites, so a typo minted a *new* type silently and any
# retrieval filter keyed on type (the committed `retrieval-coupling-playbook-filter` eval case does
# exactly this) then missed with no error (gap KNW-6).
#
# Enforced by `kg-validate` rather than by this schema, and that placement is deliberate: the agent
# may legitimately propose a genuinely new type, and a hard schema rejection would block that at the
# tool. The PR-gate is where a new type belongs — a human sees it, and `kg-validate` runs on that
# same PR, so an unknown type cannot reach the graph unreviewed while an intended one costs one
# line here.
KNOWN_NOTE_TYPES: frozenset[str] = frozenset(
    {
        "reaction",  # one ELN experiment (eln/note.py)
        "compound",  # one molecule as a graph citizen
        "campaign",  # an episodic chain of linked reactions (memory/campaign.py)
        "optimization-campaign",  # repeated runs of one transformation (memory/optimization.py)
        "playbook",  # a transferable rule distilled across projects (memory/playbook.py)
        "interaction",  # a chemist-confirmed answer (memory/interaction.py)
        "report",  # a drafted development report (report/harness.py)
        "job-result",  # a durable calculation's result (connectors/qm/knowledge.py)
        "bo-candidate",  # a BO campaign's recommendation (connectors/bo/knowledge.py)
        # The agent's reasoned proposal for the next run in a series, argued from the record
        # rather than from a surrogate model (D-162) — the non-BO sibling of `bo-candidate`.
        "experiment-proposal",
        "failure-mode",  # a negative result worth not repeating (gap KNW-3)
    }
)


class Relation(BaseModel):
    """One typed edge to another note, optionally scoped in time and confidence (STO-8/STO-9).

    Two ways to write an edge exist because they serve different authors. A `[[rel:target]]` in
    the body is what a person writing prose reaches for; this frontmatter form is what a machine
    emits and the only place per-edge metadata can live.

    **Validity belongs on the edge, not only on the note.** `Note.valid_from`/`valid_to` can say
    that a *fact* stopped being true; nothing could say that a *relation* did — that this catalyst
    was used for that transformation until the process changed, while both notes remain perfectly
    current. Bi-temporal edges with invalidation rather than deletion are how Graphiti/Zep model
    exactly this, and the node half was already here.
    """

    model_config = ConfigDict(frozen=True)

    rel: str = Field(min_length=1)
    to: str = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    valid_from: date | None = None
    valid_to: date | None = None

    @model_validator(mode="after")
    def _valid_interval(self) -> "Relation":
        """An edge's validity window must not end before it starts — as a note's must not."""
        if (
            self.valid_from is not None
            and self.valid_to is not None
            and self.valid_to < self.valid_from
        ):
            raise ValueError(
                f"relation {self.rel} -> {self.to}: valid_to {self.valid_to} is before "
                f"valid_from {self.valid_from}"
            )
        return self

    def is_current(self, as_of: date) -> bool:
        """Whether this edge is inside its validity window on `as_of` (bounds inclusive)."""
        if self.valid_from is not None and as_of < self.valid_from:
            return False
        if self.valid_to is not None and as_of > self.valid_to:
            return False
        return True


class Note(BaseModel):
    """One knowledge-graph note: its frontmatter metadata plus its Markdown body.

    `created_by` is the GxP provenance line: `agent`-authored notes must pass the
    PR-gate before merge (D-005). `confidence` (0–1) and `valid_from`/`valid_to`
    let a later query weigh and time-scope evidence.

    Frozen: a note is an immutable value object. The graph indexer caches parsed notes and
    hands the same instances to every reader (KM-14); immutability makes that sharing safe —
    no caller can mutate a cached note and corrupt it for the next query.
    """

    model_config = ConfigDict(frozen=True)

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
    # Calculations and stored by-products this note's claims rest on (STO-7). These are
    # `CalculationKey.as_str()` and `ArtifactRef.as_str()` values — they point *out* of the graph
    # into the calculation store, which is exactly why they are frontmatter fields and not
    # `[[wikilinks]]`: an edge to something the graph does not contain is a dangling link, and
    # `kg-validate` would fail the very PR that added the note. Shape-validated here; whether the
    # target exists is a question only a database can answer, and `kg-validate` runs without one.
    calc_refs: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    # Typed edges in structured form, for the metadata a body wikilink cannot carry (STO-8/9).
    # Additive: a note may use body links, this field, or both.
    relations: list[Relation] = Field(default_factory=list)
    body: str = ""

    @field_validator("calc_refs")
    @classmethod
    def _calc_ref_shape(cls, values: list[str]) -> list[str]:
        """Reject anything that is not a `calc_type@version:input_hash:params_hash` key."""
        for value in values:
            if not _CALC_REF.fullmatch(value):
                raise ValueError(
                    f"{value!r} is not a calculation key (expected "
                    "'calc_type@version:input_hash:params_hash', as CalculationKey.as_str() writes)"
                )
        return values

    @field_validator("artifact_refs")
    @classmethod
    def _artifact_ref_shape(cls, values: list[str]) -> list[str]:
        """Reject anything that is not a `<calculation key>#<artifact name>` reference."""
        for value in values:
            key, separator, name = value.rpartition("#")
            if not separator or not name or not _CALC_REF.fullmatch(key):
                raise ValueError(
                    f"{value!r} is not an artifact reference (expected "
                    "'<calculation key>#<name>', as ArtifactRef.as_str() writes)"
                )
        return values

    @model_validator(mode="after")
    def _valid_interval(self) -> "Note":
        """A bi-temporal note's validity window must not end before it starts (plan F10-G2).

        `valid_from`/`valid_to` answer "what did we know at time T"; a `valid_to` earlier than
        `valid_from` is a nonsensical window that would silently break any time-scoped query, so
        it is rejected here at the schema boundary (surfaced by the parser and `kg-validate`).
        Either bound may be absent (open-ended); the check applies only when both are set.
        """
        if (
            self.valid_from is not None
            and self.valid_to is not None
            and self.valid_to < self.valid_from
        ):
            raise ValueError(f"valid_to {self.valid_to} is before valid_from {self.valid_from}")
        return self

    def outgoing_links(self) -> list[str]:
        """The ids this note links to, from its body `[[wikilinks]]` and its `relations:`.

        Deduplicated, preserving first-seen order, so a note that references the same target twice
        yields one id. This is the *untyped* view — what `chemclaw.kg.validate` checks for dangling
        targets
        and what the answer verifier resolves — and it deliberately treats both forms alike, so a
        frontmatter relation to a note that does not exist fails validation exactly as a body link
        would.
        """
        ordered: dict[str, None] = dict.fromkeys(cited_ids(self.body))
        for relation in self.relations:
            ordered.setdefault(relation.to, None)
        return list(ordered)

    def outgoing_relations(self) -> list[Relation]:
        """Every typed edge this note asserts, from both forms, body links first.

        A body `[[rel:target]]` becomes a `Relation` with no confidence or validity — that is all
        the syntax can express, and inventing values for the rest would be a lie about what the
        author wrote. A frontmatter entry is taken as given.

        Deduplicated by `(rel, to)` with the body form winning, so writing an edge both ways is
        harmless rather than a doubled edge; a frontmatter entry that adds metadata to a link also
        written in the body should therefore be the *only* place that pair appears.
        """
        seen: dict[tuple[str, str], Relation] = {}
        for rel, target in cited_links(self.body):
            seen.setdefault((rel, target), Relation(rel=rel, to=target))
        for relation in self.relations:
            seen.setdefault((relation.rel, relation.to), relation)
        return list(seen.values())

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

    This is the one error boundary for per-file failures: an unreadable file
    (non-UTF-8 bytes, vanished mid-scan), malformed YAML frontmatter — including
    non-string keys like bare dates, which surface as TypeError — or valid
    frontmatter that fails the schema all raise `NoteError` with the path, so one
    bad file can never crash a whole-tree consumer (`load_notes`, `kg-validate`)
    that catches only `NoteError` (G4). A plain Markdown file with no frontmatter
    (e.g. a README) is not a note and returns None.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise NoteError(f"{path}: unreadable: {exc}") from exc
    try:
        post = frontmatter.loads(text)
    except (yaml.YAMLError, TypeError) as exc:
        raise NoteError(f"{path}: malformed frontmatter: {exc}") from exc
    if not post.metadata:
        return None
    # The Markdown body is authoritative; a stray `body:` frontmatter key must not
    # collide with the body kwarg (which would be an uncaught TypeError).
    metadata = {key: value for key, value in post.metadata.items() if key != "body"}
    try:
        return Note(body=post.content, **metadata)
    except (ValidationError, TypeError) as exc:
        raise NoteError(f"{path}: invalid note: {exc}") from exc


def parse_note(path: Path) -> Note:
    """Parse a file that must be a note, raising `NoteError` otherwise."""
    note = read_note(path)
    if note is None:
        raise NoteError(f"{path}: no frontmatter — not a note")
    return note
