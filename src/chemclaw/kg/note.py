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
from typing import Literal, Self

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


def strip_links(text: str) -> str:
    """`text` with every `[[wikilink]]` reduced to its target, so it carries no graph edges.

    The one place that rule lives, because three renderers need it and each one that grows its own
    copy is a place the report layer and the graph indexer can come to disagree about what a link
    points at. Reduces to the *target* via `split_link` rather than to the whole bracket contents:
    a typed edge reads `[[precursor-of:compound-x]]`, and substituting the raw group would drop
    `precursor-of:compound-x` into prose a person reads.

    **Retrieved text is what this is for.** A chunk's content becomes a bullet inside a PR-gated
    report, and a `[[link]]` surviving that interpolation is not decoration — it is a real outgoing
    edge on a note a human is about to merge, pointing at something no retriever returned. A share
    or warehouse document is written by whoever wrote it; the report's citations must come from the
    report.
    """
    return WIKILINK.sub(lambda match: split_link(match.group(1))[1], text)


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


# Id namespaces that resolve *outside* the markdown graph (D-2026-08-25).
#
# An ELN transcription is data, not a knowledge claim, so it lives in `reaction_records` rather
# than as a file in `knowledge/` — but `memory.campaign` and `memory.optimization` still cite each
# run as `[[reaction-<id>]]`, which is what makes a campaign narrative traversable. Without this,
# every campaign, playbook and optimization note would fail `kg-validate` the moment reactions
# stopped being files, for links that resolve perfectly well.
#
# The cost is stated rather than hidden: offline validation can check the *shape* of these ids and
# not their existence, because `kg-validate` runs in CI with no database. Existence is checked
# against the store by `kg.validate`, which CI runs with a database (`ReactionRecordStore.known`).
EXTERNAL_ID_PREFIXES = ("reaction-",)


def resolves_outside_graph(note_id: str) -> bool:
    """Whether `note_id` names a record in a store rather than a note in the graph.

    One predicate, because two callers ask it — `kg.graph.dangling_links` (is this link broken?)
    and `agent.graph_tools.expand_note` (where do I look this up?) — and a link the first calls
    fine that the second cannot find is exactly the two-spellings failure `note_id_for_reaction`
    exists to prevent.
    """
    return note_id.startswith(EXTERNAL_ID_PREFIXES)


def note_relative_path(note_type: str, note_id: str) -> str:
    """Where a note lives inside the knowledge directory: `<type>/<id>.md`.

    The one filename shape the whole system depends on, and until this it was an f-string in the
    PR-gate (`chemclaw.kg.pr_gate`) that three other places re-derived by hand — including
    `chemclaw.kg.graph.note_file_fingerprints`, which reads a note's id back out of `path.stem`,
    and the warehouse retriever, which spelled the layout *and* the literal type `"reaction"` into
    a `stat` call. A layout that lives in four places is a layout one of them will get wrong.
    """
    return f"{note_type}/{note_id}.md"


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


# How this system serializes a note's id into a tool result: the `id="..."` attribute of the
# `<retrieved-note-...>` envelope `gather_evidence` wraps chunks in, and the `id` / `note_id` /
# `source_note_id` fields of anything dumped as JSON (`expand_note`, `find_notes`, `EvidenceChunk`,
# every connector returning a note model). These are *our own* output formats rather than guesses
# about arbitrary text, which is what makes scanning for them honest — and `tests/test_note.py`
# pins them against real tool output, so a fourth serialization breaks a test instead of silently
# narrowing the scan.
#
# The three keys are enumerated rather than matched as a `*_id` suffix, because that would also
# swallow `structure_id`, `calc_id`, `job_id` and `session_id` — none of which names a note, and
# every one of which would make an answer look grounded in something it never saw.
#
# The `\\?` before each quote is not defensive padding. A `gather_evidence` result is JSON whose
# `content` field holds the `<retrieved-note ... id="X">` envelope as *text*, so on the wire the
# envelope's quotes arrive escaped — `id=\"X\"` — and a pattern insisting on a bare quote matches
# nothing at all on the one tool this check exists for. Found by pinning the fixture to a real
# result instead of an idealized one.
_SERIALIZED_ID = re.compile(
    r"""\\?["']?\b(?:source_note_id|note_id|id)\\?["']?\s*[=:]\s*"""
    r"""\\?["']([A-Za-z0-9][A-Za-z0-9_.-]*)"""
)


def mentioned_ids(text: str) -> list[str]:
    """Every note id a *tool result* put in front of the model, deduped, in first-seen order.

    The counterpart to `cited_ids`, and deliberately a different question. `cited_ids` reads what
    an author *claims* (`[[wikilinks]]`); this reads what a payload *contains* — the ids of notes
    the turn actually retrieved, however they were serialized. Both live here for the same reason:
    a citation reader and a citation writer that disagree about what an id looks like is how a
    grounding check silently stops working.

    Wikilinks count too. If `expand_note` returns a note whose body cites `[[playbook-degassing]]`,
    that id was in the context window this turn, and an answer repeating it is traceable to
    something the turn saw rather than to the model's memory — which is the only question a
    grounding check is entitled to ask.

    Why this exists at all: the live harness scored citations against `ToolResultEvent.preview`,
    truncated to 200 characters for the browser, while `gather_evidence` returns up to 40 chunks.
    Every id past the first chunk read as fabricated, and a run graded 19 of 36 answers as
    fabrication with nine of nine checked verdicts false — see
    `docs/archive/live-grounded-2026-08-03.md`.
    The preview's budget is right for a UI and wrong for a grounding check, so the two now read
    different fields off one event instead of sharing the wrong one.
    """
    ordered: dict[str, None] = {}
    for note_id in _SERIALIZED_ID.findall(text):
        ordered.setdefault(note_id, None)
    for note_id in cited_ids(text):
        ordered.setdefault(note_id, None)
    return list(ordered)


# `id` and `type` become file-path segments (`knowledge/<type>/<id>.md`) and a git
# branch (`note/<id>`) in the PR-gate, and ELN entry ids flow in from external JSON.
# Constraining them to a plain slug at the model is the traversal/ref-injection
# barrier: no `/`, no leading `.`, nothing git or the filesystem could reinterpret.
# `_` is included because BO note ids embed registry objective names (e.g.
# `bo-reizman_suzuki-<sha>`).
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def require_note_slug(value: str) -> str:
    """Return `value` if it is a safe note slug, else raise `ValueError` naming the rule.

    Extracted from `Note`'s validator because `ingest.eln.records.ReactionRecord` needs the same
    rule and must not restate it. An ELN entry id no longer becomes a filename directly — a
    transcription is a database row (D-2026-08-25) — but it still becomes the `reaction-<id>`
    citation that campaign and playbook notes carry into git, so it reaches a file and a diff one
    indirection later. Dropping the constraint when the storage changed would have been a silent
    widening of what external JSON can put into a committed note body.

    A few git ref rules the character class alone does not cover are refused explicitly (defense in
    depth): `..` (an invalid ref component, e.g. `a..b`), a trailing `.`, and a `.lock` suffix —
    git rejects all three, so an id that passed the schema would otherwise fail later at branch
    creation.
    """
    if ".." in value or value.endswith((".", ".lock")) or not _SLUG.fullmatch(value):
        raise ValueError(
            f"{value!r} is not a safe note slug (allowed: {_SLUG.pattern}; "
            "no '..', trailing '.', or '.lock' suffix)"
        )
    return value


# `CalculationKey.as_str()`: `calc_type@calc_version:input_hash:params_hash`. The version segment
# is the loose one on purpose — it carries a method name and a build string
# (`GFN2-xTB+tblite+0.4.0`), so it is matched as "anything but a colon" rather than enumerated.
# The point of validating the shape at all is that a note citing `"the GFN2 run"` in this field is
# a crosslink nothing can resolve, and it should fail at the PR-gate rather than silently.
_CALC_REF = re.compile(r"^[^\s@:]+@[^\s:]+:[0-9a-f]+:[0-9a-f]+$")


def _reject_unencodable(value: str, field: str) -> str:
    r"""Refuse a string UTF-8 cannot encode — a lone surrogate is not text a note can hold.

    Reachable rather than theoretical. An agent-authored note arrives as JSON, and JSON *can*
    carry an unpaired surrogate (`json.loads('"\ud800"')` returns one happily), so a model that
    emits a truncated escape puts a `str` in this field that no UTF-8 consumer can accept. The
    field itself then looks fine and every write of it fails: `path.write_text` raises
    `UnicodeEncodeError` in the PR-gate's commit, and psycopg and the vector index raise the same
    way on the proposal store and the index refresh.

    So the check belongs on the note, not on the file writer. A `Note` is by definition something
    that gets written to a UTF-8 file in Git; a value that cannot be is not a note field that
    happens to fail late, it is invalid input — and rejecting it here fails one proposal loudly at
    the boundary instead of crashing whichever writer reaches it first. Found by
    `tests/test_properties_core.py`'s round-trip generator, which is exactly the kind of input a
    hand-written example never supplies.
    """
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"{field} contains a character UTF-8 cannot encode (a lone surrogate at position "
            f"{exc.start}); a note is stored as a UTF-8 file, so this value cannot be written"
        ) from exc
    return value


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
        # A calculation written up as a graph citizen. It sat in `connectors/qm/connector.yaml`
        # while the `qm` bundle's `publish_to_graph` job was the thing that minted it; with that
        # bundle removed (`D-2026-08-26-semiempirical-is-the-whole-tier`) no bundle mints one, and
        # the rule that put it there says where it goes instead. A type a bundle *mints* belongs to
        # that bundle; this one is now written only through core's own PR-gate
        # (`propose_knowledge_note`), about results the corpus in `knowledge/job-result/` already
        # holds — so it is core's vocabulary again. `bo-candidate` stays in `connectors/bo/`,
        # because `bo` still mints it.
        "job-result",
        # The agent's reasoned proposal for the next run in a series, argued from the record
        # rather than from a surrogate model (D-162) — the non-BO sibling of `bo-candidate`.
        "experiment-proposal",
        "failure-mode",  # a negative result worth not repeating (gap KNW-3)
    }
)


def known_note_types() -> frozenset[str]:
    """Core's note types plus those the enabled connector bundles declare.

    **The vocabulary belongs to the deployment, not to this file.** `bo-candidate` is minted by
    `connectors/bo/` rather than by core, and used to be added to the frozenset above by hand. That
    made "contribute a note type" the one connector contribution that required editing core, inside
    the seam whose whole claim is that a capability is a folder and nothing else (D-118). A bundle
    now declares `note_types:` in its manifest and this unions them in.

    The set stays *closed*, which is the property worth keeping: a name no manifest and no core
    entry declares still fails `make kg-validate`, so a typo cannot reach the graph and make a note
    invisible to every filter keyed on its type.

    The connector registry is imported **inside the function**, deliberately. `chemclaw.kg` is
    layer 4 and `chemclaw.connectors` is layer 2/3, so a module-scope import would make the graph
    depend on the capability layer at import time — for a set that only two validators ever ask
    for. The same shape, and the same reason, as `core.logging`'s lazy resolution of connector
    token names; both are declared in `tests/test_layering.py::_ALLOWED_LAZY_EDGES`.
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
    """

    model_config = ConfigDict(frozen=True)

    valid_from: date | None = None
    valid_to: date | None = None

    def _window_owner(self) -> str:
        """How this carrier names itself in an invalid-interval error. Overridden where it helps."""
        return type(self).__name__.lower()

    @model_validator(mode="after")
    def _valid_interval(self) -> Self:
        """A validity window must not end before it starts.

        `valid_from`/`valid_to` answer "what did we know at time T"; a `valid_to` earlier than
        `valid_from` describes no interval at all, so every query over it silently returns
        nothing. Refused at the schema boundary, where the message can name the file, rather than
        read back later as an absence.
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

        Either bound may be absent (open-ended). Discovery retrieval excludes non-current notes so
        a not-yet-valid or superseded entry is not served as *current* evidence (freshness —
        audit KM-7); the note is never deleted, it stays in Git and is still reachable by explicit
        id, it is only dropped from current-evidence sweeps.
        """
        if self.valid_from is not None and as_of < self.valid_from:
            return False
        if self.valid_to is not None and as_of > self.valid_to:
            return False
        return True


class Relation(TemporalWindow):
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
    # `OrdReaction.outcome_class`'s value. Carried because a failure that reads as an ordinary run
    # is the one row in a comparison a chemist must not misread — and because `outcome_class`
    # defaults to success, so silence here means "the source did not say", not "it worked".
    outcome: Literal["success", "failure", "inconclusive"] | None = None
    # `OrdReaction.major_impurity()`'s answer, by whatever identity the record carries. A process
    # campaign is rarely optimizing yield; it is optimizing the impurity the yield hides.
    major_impurity: str | None = None
    impurity_area_percent: float | None = Field(default=None, ge=0.0, le=100.0)

    model_config = ConfigDict(frozen=True)


class Note(TemporalWindow):
    """One knowledge-graph note: its frontmatter metadata plus its Markdown body.

    `created_by` is the provenance line: `agent`-authored notes must pass the
    PR-gate before merge (D-005). `confidence` (0–1) and `valid_from`/`valid_to`
    let a later query weigh and time-scope evidence.

    Frozen: a note is an immutable value object. The graph indexer caches parsed notes and
    hands the same instances to every reader (KM-14); immutability makes that sharing safe —
    no caller can mutate a cached note and corrupt it for the next query.
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
    source: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    # Calculations and stored by-products this note's claims rest on (STO-7). These are
    # `CalculationKey.as_str()` and `ArtifactRef.as_str()` values — they point *out* of the graph
    # into the calculation store, which is exactly why they are frontmatter fields and not
    # `[[wikilinks]]`: an edge to something the graph does not contain is a dangling link, and
    # `kg-validate` would fail the very PR that added the note. Shape-validated here; whether the
    # target exists is a question only a database can answer, and `kg-validate` runs without one.
    # The run's recorded setpoints and outcomes, when this note is about one (`ProcessConditions`
    # says why they are frontmatter). Note-type-specific on a shared model exactly as
    # `compound_smiles` above is, and for the same reason: the alternative is a second note class.
    # `valid_from` already carries the date the run was performed (D-162), so it is not repeated
    # here — one fact, one field.
    conditions: ProcessConditions | None = None
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
    def _text_is_writable(self) -> Self:
        """Every *unconstrained* string this note carries must survive UTF-8.

        Walked off `model_fields` rather than spelled out, for the reason `skill_tool_names` reads
        the framework's own constants: a note that grows another string field would otherwise
        gain an unchecked one silently, and the whole point is that any unencodable value breaks
        every writer rather than the one that happened to be tested.

        **Four fields are deliberately not walked, because something already refuses them.**
        Measured: pydantic's own constrained-string validation rejects a surrogate in `id`, `type`
        and `Relation.rel`/`to` (all `min_length=1`) with `string_unicode` before this validator
        runs, and `_calc_ref_shape`/`_artifact_ref_shape` reject the ref lists because a lone
        surrogate is no calculation key. What actually reaches here is the unconstrained set —
        `body`, `source`, `compound_smiles`, `tags` — so walking the rest would be code that cannot
        run, which reads as coverage while proving nothing. `tests/test_properties_core.py` pins
        both halves without pinning *who* rejects what, so the split fails loudly if either of the
        other two checks ever stops covering its part.
        """
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, str):
                _reject_unencodable(value, name)
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    if isinstance(item, str):
                        _reject_unencodable(item, f"{name}[{index}]")
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
        """Every typed edge this note asserts, from both forms, in body-link order.

        A body `[[rel:target]]` becomes a `Relation` with no confidence or validity — that is all
        the syntax can express, and inventing values for the rest would be a lie about what the
        author wrote. A frontmatter entry is taken as given.

        Deduplicated by `(rel, to)`, so writing an edge both ways is harmless rather than a doubled
        edge — **and the frontmatter entry wins**, because the body form can express nothing the
        frontmatter cannot and the frontmatter form can express three things it cannot. The body
        form used to win, which meant an author who wrote the edge in both places silently lost the
        confidence and the validity window they had gone out of their way to declare. Every note in
        the shipped corpus that declares a typed relation also writes the link in its body, so the
        measured effect was that D-134's edge metadata existed in the schema, in the parser and in
        the corpus, and reached no query: `graph.related(..., as_of=)` had no dated edge to filter
        and `Relation.confidence` was `None` everywhere it was read.

        Order still follows the body, so a note's edges read in the order the prose introduces
        them; only the *value* at a duplicated pair changes.
        """
        seen: dict[tuple[str, str], Relation] = {}
        for rel, target in cited_links(self.body):
            seen.setdefault((rel, target), Relation(rel=rel, to=target))
        for relation in self.relations:
            seen[(relation.rel, relation.to)] = relation
        return list(seen.values())


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
