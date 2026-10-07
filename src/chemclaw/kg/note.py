"""Knowledge-graph note: the frontmatter schema and parser.

A note is a Markdown file with a YAML frontmatter header and a body whose `[[wikilinks]]` encode
relations to other notes by id. This module is the single source of the note schema and the only
parser; an invalid note yields a `NoteError` naming the file, never a crash.
"""

import re
from datetime import date
from pathlib import Path
from typing import Literal, Self

import frontmatter
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from chemclaw.core.authorship import UNNAMED_AGENT, Authorship
from chemclaw.core.errors import ChemclawError
from chemclaw.kg import relations

# [[target]] wikilinks in the body. Targets are note ids; `[[ ... ]]` only. Public because
# the report layer strips the same markup from evidence excerpts — one pattern, no drift.
WIKILINK = re.compile(r"\[\[([^\[\]]+)\]\]")


def split_link(target: str) -> tuple[str, str]:
    """Split a wikilink's inside into `(relation, note id)`.

    `[[precursor-of:compound-x]]` is a typed edge; a bare `[[compound-x]]` is a citation and yields
    `DEFAULT_RELATION`. A target with an empty relation (`[[:x]]`) or empty id (`[[rel:]]`) is
    returned as a plain citation of the whole string, so it fails the unknown-note check with the
    text the author wrote.
    """
    relation, separator, note_id = target.partition(":")
    relation, note_id = relation.strip(), note_id.strip()
    if not separator or not relation or not note_id:
        return relations.DEFAULT_RELATION, target.strip()
    return relation, note_id


def strip_links(text: str) -> str:
    """`text` with every `[[wikilink]]` reduced to its target, so it carries no graph edges.

    For interpolating retrieved or external text into a note body: a surviving link would be a real
    outgoing edge the report never cited. Reduces to the target via `split_link`, so a typed edge
    does not leak its relation prefix into prose.
    """
    return WIKILINK.sub(lambda match: split_link(match.group(1))[1], text)


def as_cell(text: str) -> str:
    """`text` as one line that can fill a slot in a note body but cannot add structure to it.

    Strips wikilinks (no new graph edges) and collapses whitespace (no new Markdown, e.g. a newline
    plus `- ` becoming extra uncited bullets). Applies to retrieved and model-authored text alike.
    The text is preserved, not truncated.
    """
    return " ".join(strip_links(text).split())


def cited_links(text: str) -> list[tuple[str, str]]:
    """Every `(relation, note id)` a body cites, deduplicated by pair in first-seen order.

    By pair, not id: a note may stand in two relations to one target.
    """
    ordered: dict[tuple[str, str], None] = {}
    for match in WIKILINK.findall(text):
        relation, note_id = split_link(match)
        if note_id:
            ordered.setdefault((relation, note_id), None)
    return list(ordered)


#: Separator between source and entry id in a qualified `reaction-` citation. `.` is already a
#: legal slug, ref and filename character. Splits on the first occurrence, so entry ids may contain
#: dots but source names may not.
_SOURCE_SEPARATOR = "."


def note_id_for_reaction(record_id: str, source: str = "") -> str:
    """The `reaction` note id for a fingerprint-index record id, qualified by its source.

    The one spelling of this id, so a search hit can be expanded directly. The qualified form names
    one source's row exactly, since the index is keyed on `(source, id)`. The bare form stays valid
    for existing citations and resolves across all sources, refusing when two sources hold the id.

    Args:
        record_id: The ELN's own entry id, as the store and the index key it.
        source: The registry source name that transcribed it (`Match.source`, or the ingest's
            `source`). Empty produces the bare, unqualified form.

    Returns:
        `reaction-<source>.<id>`, or `reaction-<id>` when no source is given.

    Raises:
        ValueError: `source` contains the separator, so the id could not be split back apart.
    """
    if not source:
        return f"reaction-{record_id}"
    if _SOURCE_SEPARATOR in source:
        raise ValueError(
            f"ingest source {source!r} contains {_SOURCE_SEPARATOR!r}, so a "
            "`reaction-<source>.<id>` citation could not be split back into its two halves"
        )
    return f"reaction-{source}{_SOURCE_SEPARATOR}{record_id}"


# Id namespaces that resolve outside the markdown graph. An ELN transcription is a row in
# `reaction_records`, not a file, but campaign and optimization notes cite runs as
# `[[reaction-<id>]]`. Offline validation can check only the shape of these ids; existence is
# checked against the store by `kg.validate` when a database is available.
EXTERNAL_ID_PREFIXES = ("reaction-",)


def resolves_outside_graph(note_id: str) -> bool:
    """Whether `note_id` names a record in a store rather than a note in the graph.

    One predicate so `kg.graph.dangling_links` and `agent.graph_tools.expand_note` agree.
    """
    return note_id.startswith(EXTERNAL_ID_PREFIXES)


def external_record_ref(note_id: str) -> tuple[str, str]:
    """The `(source, record_id)` an external citation names, `("", id)` for the bare form.

    The inverse of `note_id_for_reaction`. Splits on the first separator, which is exact because
    source names cannot contain it. An empty source means "ask across all sources".
    """
    for prefix in EXTERNAL_ID_PREFIXES:
        if note_id.startswith(prefix):
            stripped = note_id[len(prefix) :]
            source, separator, record_id = stripped.partition(_SOURCE_SEPARATOR)
            return (source, record_id) if separator and record_id else ("", stripped)
    return "", note_id


def external_record_id(note_id: str) -> str:
    """The store-side id behind an external citation, with the prefix and any source stripped.

    Driven by `EXTERNAL_ID_PREFIXES`. Used only for an existence check
    (`kg.validate.unresolved_citations`), which is deliberately weaker than a resolve: a qualified
    citation whose own source lacks the id passes if another source has it, and is refused at read
    time by `records.read`.
    """
    return external_record_ref(note_id)[1]


def note_relative_path(note_type: str, note_id: str) -> str:
    """Where a note lives inside the knowledge directory: `<type>/<id>.md`.

    The one filename shape the system depends on; readers derive a note id from `path.stem`.
    """
    return f"{note_type}/{note_id}.md"


def cited_ids(text: str) -> list[str]:
    """Extract the note ids a body of text cites via `[[wikilinks]]`, stripped and deduped.

    The one extraction every citation reader shares. Targets are stripped (`[[ id ]]` resolves to
    `id`), empties dropped, first-seen order kept. Typed links contribute their target.
    """
    ordered: dict[str, None] = {}
    for _, note_id in cited_links(text):
        ordered.setdefault(note_id, None)
    return list(ordered)


# How this system serializes a note id into a tool result: the `id="..."` attribute of the
# `<retrieved-note-...>` envelope, and the `id` / `note_id` / `source_note_id` JSON fields.
# `tests/test_note.py` pins these against real tool output. Keys are enumerated rather than matched
# as `*_id`, which would capture `job_id`, `session_id` and the like. A plain `"id":` field on a
# non-note object is still captured; that grounds nothing unless the answer also cites it. The
# optional `\\` before each quote matches the envelope when it arrives JSON-escaped inside a
# `gather_evidence` result.
_SERIALIZED_ID = re.compile(
    r"""\\?["']?\b(?:source_note_id|note_id|id)\\?["']?\s*[=:]\s*"""
    r"""\\?["']([A-Za-z0-9][A-Za-z0-9_.-]*)"""
)


def mentioned_ids(text: str) -> list[str]:
    """Every note id a tool result put in front of the model, deduped, in first-seen order.

    Unlike `cited_ids` (what an author claims), this reads what a payload contains, however
    serialized, including wikilinks inside returned note bodies: anything in the context window is
    traceable. Grounding checks must read the full result, not a truncated UI preview.
    """
    ordered: dict[str, None] = {}
    for note_id in _SERIALIZED_ID.findall(text):
        ordered.setdefault(note_id, None)
    for note_id in cited_ids(text):
        ordered.setdefault(note_id, None)
    return list(ordered)


# `id` and `type` become path segments (`knowledge/<type>/<id>.md`) and git pathspecs, and ELN ids
# come from external JSON, so they are constrained to a plain slug: no `/`, no leading `.`. `_` is
# allowed because BO note ids embed objective names.
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def is_note_slug(value: str) -> bool:
    """Whether `value` could name a note: the predicate half of `require_note_slug`.

    For callers that filter rather than validate, e.g. `agent.compaction.cited_note_ids`, since an
    `EvidenceChunk.source_note_id` is not always a note id (share documents, warehouse keys,
    vendored rows).
    """
    return ".." not in value and not value.endswith((".", ".lock")) and bool(_SLUG.fullmatch(value))


def require_note_slug(value: str) -> str:
    """Return `value` if it is a safe note slug, else raise `ValueError` naming the rule.

    Shared with `ingest.eln.records.ReactionRecord`, whose ids reach committed note bodies as
    `reaction-<id>` citations. Also refuses `..`, a trailing `.` and a `.lock` suffix, which git
    rejects in a ref.
    """
    if not is_note_slug(value):
        raise ValueError(
            f"{value!r} is not a safe note slug (allowed: {_SLUG.pattern}; "
            "no '..', trailing '.', or '.lock' suffix)"
        )
    return value


# `CalculationKey.as_str()`: `calc_type@calc_version:input_hash:params_hash`. The segment patterns
# restate `CalculationKey`'s own, because `kg` may not import `science`;
# `tests/test_note.py::test_every_calculation_key_the_store_accepts_can_be_cited` keeps them in
# step. The version is free to contain `:` (calibrated versions do); the parse stays unambiguous
# because `calc_type` bars `@` and the two hashes bar `:`.
_CALC_TYPE = r"[^\s@:]+"
_CALC_VERSION = r"\S+"
_CALC_HASH = r"[^\s:]+"
_CALC_REF = re.compile(rf"^{_CALC_TYPE}@{_CALC_VERSION}:{_CALC_HASH}:{_CALC_HASH}$")


def _reject_unencodable(value: str, field: str) -> str:
    r"""Refuse a string UTF-8 cannot encode: a lone surrogate is not text a note can hold.

    JSON can carry an unpaired surrogate, and such a value would fail every later writer (the note
    file, Postgres, the vector index). A note is by definition written to a UTF-8 file, so the value
    is invalid input and is rejected at the boundary.
    """
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"{field} contains a character UTF-8 cannot encode (a lone surrogate at position "
            f"{exc.start}); a note is stored as a UTF-8 file, so this value cannot be written"
        ) from exc
    return value


def _walk_encodable(model: BaseModel, prefix: str) -> None:
    """Reject any unencodable string on `model`, recursing into nested models and lists.

    Field names are joined dotted (`conditions.major_impurity`) so the error names the field to fix.
    """
    for name in type(model).model_fields:
        value = getattr(model, name)
        path = f"{prefix}{name}"
        if isinstance(value, str):
            _reject_unencodable(value, path)
        elif isinstance(value, BaseModel):
            _walk_encodable(value, f"{path}.")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, str):
                    _reject_unencodable(item, f"{path}[{index}]")
                elif isinstance(item, BaseModel):
                    _walk_encodable(item, f"{path}[{index}].")


# Every note type this system mints, with what it means. A typo'd type would make a note invisible
# to every type-keyed filter. Enforced by `kg-validate` over the corpus rather than by the schema,
# so a deployment extending its vocabulary does not fail the agent's write at the tool.
KNOWN_NOTE_TYPES: frozenset[str] = frozenset(
    {
        "reaction",  # one ELN experiment (eln/note.py)
        "compound",  # one molecule as a graph citizen
        "campaign",  # an episodic chain of linked reactions (memory/campaign.py)
        "optimization-campaign",  # repeated runs of one transformation (memory/optimization.py)
        "playbook",  # a transferable rule distilled across projects (memory/playbook.py)
        "interaction",  # a chemist-confirmed answer (memory/interaction.py)
        "report",  # a drafted development report (report/harness.py)
        # A calculation result written up as a graph citizen, via core's `record_knowledge_note`. A
        # type a connector bundle mints belongs in that bundle's manifest instead (e.g.
        # `bo-candidate`).
        "job-result",
        # The agent's reasoned proposal for the next run in a series, argued from the record
        # rather than from a surrogate model; the non-BO sibling of `bo-candidate`.
        "experiment-proposal",
        "failure-mode",  # a negative result worth not repeating (gap KNW-3)
        # A field of competing explanations ranked by pairwise comparison
        # (`durable/hypothesis_tournament.py`). Unlike `experiment-proposal` (the single next run),
        # it keeps the alternatives and how they placed.
        "hypothesis-field",
        # How a measurement was made: an assay or purity method a chemist ran, as recorded. The
        # legal target of the `measured-by` relation. It records a stated method; this system never
        # devises one.
        "analytical-method",
    }
)

#: The tag a `playbook` carries while it records a recurrence and no rule has been distilled. A tag
#: rather than a note type, so readers asking for playbooks still find it while it says it is not
#: yet a transferable rule (that is the `playbook-distillation` skill's judgment). Defined in `kg`
#: because `memory/jobs.py` stamps it and `kg/analytics.py` counts it, and `kg` cannot import
#: `memory`.
UNDISTILLED_TAG = "undistilled"


def known_note_types() -> frozenset[str]:
    """Core's note types plus those the enabled connector bundles declare.

    A bundle declares `note_types:` in its manifest, so adding a type needs no core edit. The set
    stays closed: an undeclared type still fails `make kg-validate`. The connector registry is
    imported inside the function because `kg` must not depend on the connector layer at import time
    (an allowed lazy edge in `tests/test_layering.py`).
    """
    from chemclaw.connectors.registry import declared_note_types

    return KNOWN_NOTE_TYPES | declared_note_types()


class TemporalWindow(BaseModel):
    """A validity window — `valid_from`/`valid_to`, inclusive at both bounds, either optional.

    Two things in this graph are time-scoped: a note (a *fact* stopped being true) and a relation
    (an *edge* stopped holding, while both notes remain current). They are different statements
    and both are needed, but the window itself is one rule and was written twice — the same
    interval validator and the same `is_current` in `Relation` and in `Note`, arguing the same
    inclusivity semantics in two docstrings. A change to what "current" means had to be made in
    both places or it was made in neither.

    Frozen here rather than in each subclass: every carrier of a window in this package is an
    immutable value object shared out of the note cache (KM-14).

    `extra="forbid"`, because pydantic's default is `ignore` and ignore is silent data loss: a
    note authored with `valid-from:` or `conditons:` parsed clean, validated green, and the
    metadata was simply gone — the note sat outside every bi-temporal query with no error
    anywhere. That is the same failure `KNOWN_NOTE_TYPES` exists to prevent for the *value* of
    `type`, applied to field names. The resilient/strict split is unchanged: `load_notes` still
    skips the now-invalid file so one typo cannot block a query, while `kg-validate` reports it
    with the path and the offending key.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    valid_from: date | None = None
    valid_to: date | None = None

    def _window_owner(self) -> str:
        """How this carrier names itself in an invalid-interval error. Overridden where it helps."""
        return type(self).__name__.lower()

    @model_validator(mode="after")
    def _valid_interval(self) -> Self:
        """A validity window must not end before it starts.

        Such a window matches no query, so it is refused at the schema where the message can name
        the file.
        """
        if (
            self.valid_from is not None
            and self.valid_to is not None
            and self.valid_to < self.valid_from
        ):
            raise ValueError(
                f"{self._window_owner()}: valid_to {self.valid_to} is before "
                f"valid_from {self.valid_from}"
            )
        return self

    def is_current(self, as_of: date) -> bool:
        """Whether this is inside its validity window on `as_of` (bounds inclusive).

        Either bound may be absent (open-ended). Discovery retrieval excludes non-current notes from
        current-evidence sweeps; the note stays in Git and is reachable by explicit id.
        """
        if self.valid_from is not None and as_of < self.valid_from:
            return False
        if self.valid_to is not None and as_of > self.valid_to:
            return False
        return True


class Relation(TemporalWindow):
    """One typed edge to another note, optionally scoped in time and confidence.

    The frontmatter form of a `[[rel:target]]` body link, and the only place per-edge metadata can
    live. Validity on the edge lets a relation stop holding while both notes remain current.
    """

    rel: str = Field(min_length=1)
    to: str = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    def _window_owner(self) -> str:
        """Name the edge, not the class: an error about one of a note's ten edges must say which."""
        return f"relation {self.rel} -> {self.to}"


class ProcessConditions(BaseModel):
    """The setpoints and outcomes a run recorded, as numbers rather than as prose.

    **Why this is frontmatter and not left in the body.** `record_from_ord_reaction` renders these
    into readable bullets and the structure is then gone: `OrdReaction` is never persisted — it
    exists only transiently inside `durable.memory_jobs.read_corpus`, which re-reads and re-maps the
    entire ELN from the beginning of time on every call, on the background worker, behind an ingest
    half the chat pod deliberately does not import. So at turn time the numbers a chemist compares
    exist only as sentences, and anything wanting to compare runs had to re-derive them from prose
    it had just finished rendering.

    Putting them here rather than in a second table keeps one source of truth: the git-markdown
    graph stays authoritative (D-004), `expand_note` already returns frontmatter, `kg-validate`
    already checks it, and there is no migration and no store to keep in step.

    **Exactly the columns the comparative table renders, and no more.** This is not a serialization
    of `OrdReaction` — that would be the second, untyped schema `attributes` argues against. The
    species sets behind "solvent DMF → 2-MeTHF" are deliberately absent: they need the full input
    list, and a turn that wants them reads the prose, which is where the free-text half of a digest
    is looking anyway.

    Every field is optional because every one of them is optional on the record. Absent means "not
    recorded", never "zero" — the distinction `comparison.MISSING` renders and `drop_empty_columns`
    reads.
    """

    temperature_c: float | None = None
    time_h: float | None = Field(default=None, ge=0.0)
    yield_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    purity_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    # `OrdReaction.outcome_class`'s value, so a failed run is not misread as an ordinary one. `None`
    # means the source did not say, not that it worked.
    outcome: Literal["success", "failure", "inconclusive"] | None = None
    # `OrdReaction.major_impurity()`'s answer, by whatever identity the record carries. A process
    # campaign is rarely optimizing yield; it is optimizing the impurity the yield hides.
    major_impurity: str | None = None
    impurity_area_percent: float | None = Field(default=None, ge=0.0, le=100.0)

    # `extra="forbid"`: a typo'd key silently dropped is a number no comparison will render.
    # `allow_inf_nan=False`: this model is written into a `jsonb` column and NaN/Infinity are not
    # JSON; as a `ValidationError` the entry is rejected individually instead of a database error
    # aborting the whole ingest pass. Stated explicitly rather than relying on the field bounds,
    # which do not cover every field.
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Note(TemporalWindow):
    """One knowledge-graph note: its frontmatter metadata plus its Markdown body.

    `created_by` is the provenance line that lets a chemist tell agent-written knowledge (readable
    without prior review) from curated knowledge. `actor` is the person on whose behalf an agent
    wrote it, stamped by `record_note`; absent means not recorded, never guessed. `confidence` and
    `valid_from`/`valid_to` let a query weigh and time-scope evidence.

    Frozen, because the graph indexer shares cached instances with every reader.
    """

    id: str = Field(min_length=1)
    type: str = Field(min_length=1)

    @field_validator("id", "type")
    @classmethod
    def _slug_only(cls, value: str) -> str:
        """Reject path/ref metacharacters — see `require_note_slug`."""
        return require_note_slug(value)

    compound_smiles: str | None = None
    tags: list[str] = Field(default_factory=list)
    created_by: Literal["human", "agent"] = "human"
    # Omitted from the frontmatter while `None`, so older notes re-render byte-identically.
    actor: str | None = Field(default=None, min_length=1)
    source: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    # `conditions`: the run's recorded setpoints and outcomes when the note is about one;
    # `valid_from` already carries the run date. `calc_refs` / `artifact_refs`:
    # `CalculationKey.as_str()` and `ArtifactRef.as_str()` values pointing into the calculation
    # store, so they are frontmatter rather than wikilinks (which would dangle). Shape-validated
    # here; existence needs a database.
    conditions: ProcessConditions | None = None
    calc_refs: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    # Typed edges in structured form, for the metadata a body wikilink cannot carry.
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
    def _text_is_writable(self) -> Self:
        """Every unconstrained string this note carries must survive UTF-8.

        Walks `model_fields` (and nested models) so a new string field is covered automatically.
        Constrained fields (`id`, `type`, relation fields, the ref lists) are already rejected by
        pydantic or their own validators; `tests/test_properties_core.py` checks the split.
        """
        _walk_encodable(self, "")
        return self

    @property
    def authorship(self) -> Authorship:
        """Who wrote this note, in the shape every subsystem answers that question in.

        `created_by: agent` names no specific agent, so it reads as `UNNAMED_AGENT`. A note without
        `actor` reads with the person unrecorded.
        """
        return Authorship(
            actor=self.actor, agent=UNNAMED_AGENT if self.created_by == "agent" else None
        )

    def outgoing_links(self) -> list[str]:
        """The ids this note links to, from its body `[[wikilinks]]` and its `relations:`.

        Deduplicated in first-seen order. The untyped view used by `kg.validate` and the answer
        verifier; a frontmatter relation to a missing note fails validation like a body link.
        """
        ordered: dict[str, None] = dict.fromkeys(cited_ids(self.body))
        for relation in self.relations:
            ordered.setdefault(relation.to, None)
        return list(ordered)

    def outgoing_relations(self) -> list[Relation]:
        """Every typed edge this note asserts, from both forms, in body-link order.

        A body `[[rel:target]]` becomes a `Relation` without confidence or validity. Deduplicated by
        `(rel, to)`, and the frontmatter entry wins, since only it carries that metadata. Order
        follows the body.
        """
        seen: dict[tuple[str, str], Relation] = {}
        for rel, target in cited_links(self.body):
            seen.setdefault((rel, target), Relation(rel=rel, to=target))
        for relation in self.relations:
            seen[(relation.rel, relation.to)] = relation
        return list(seen.values())

    def headline(self, limit: int = 120) -> str:
        """The note's first line of prose, for a surface that can show one line and not a note.

        Derived from the body rather than stored as a title, so it cannot disagree with the note.
        Leading `#` marks are stripped and wikilinks flattened to their target. Empty for a note
        with no body; callers show the id instead.

        Args:
            limit: Longest headline to return; a longer first line is cut on a word boundary and
                given a trailing ellipsis.

        Returns:
            One line, never containing a newline, at most `limit` characters.
        """
        for raw in self.body.splitlines():
            line = strip_links(raw).lstrip("#").strip()
            if line:
                if len(line) <= limit:
                    return line
                head = line[:limit].rsplit(" ", 1)[0] or line[:limit]
                return f"{head}…"
        return ""


class NoteError(ChemclawError):
    """A note file could not be parsed or failed schema validation."""


def read_note(path: Path) -> Note | None:
    """Parse a note file; return None if it has no frontmatter (not a note).

    The one error boundary for per-file failures: an unreadable file, malformed YAML (including
    non-string keys, which surface as TypeError) or a schema violation all raise `NoteError` with
    the path, so whole-tree consumers catching only `NoteError` never crash.
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
