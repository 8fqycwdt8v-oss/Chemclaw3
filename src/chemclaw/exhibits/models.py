"""What an artefact is: a spec per kind, the shapes the API serves, and the errors a write can meet.

**The spec is validated here and nowhere else**, for both of its writers. The agent's tools take an
untyped `dict` (`D-2026-10-02-an-artefact-is-part-of-the-answer-not-an-effect` measured the typed
union at 1,989 prefix tokens against 961) and the REST routes take a JSON body; both arrive at
`parse_spec`, so a spec the model may not write is one a browser may not write either, and the
refusal is worded the same way for both.

**Shape and caps are two checks, on purpose.** `parse_spec` is the shape — the closed set of kinds,
`extra="forbid"` everywhere, literal values only — and it is what a read runs too, so a stored
revision always comes back typed. `require_writable` is the caps (bytes, rows, structures, points)
and the RDKit parse of every SMILES, and only a *write* runs it: a deployment that lowers a cap must
not make the artefacts it already holds unreadable, and re-parsing two hundred molecules on every
read would pay for a check whose answer cannot have changed.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Iterator
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AllowInfNan,
    BaseModel,
    ConfigDict,
    Field,
    Strict,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from chemclaw.core.chem import InvalidSmilesError, require_molecule
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.protocols.diff import FieldChange
from chemclaw.protocols.models import AuthorKind

#: Every kind an artefact can be. The migration's CHECK constraint names the same six.
ExhibitKind = Literal["document", "table", "structures", "chart", "result", "link"]

#: The `session_events` kind a person's create or revision is pushed under, for
#: `GET /sessions/{id}/events` to claim and render as the turn stream's `exhibit` event.
PUSH_KIND = "exhibit"

#: The shape a minted id takes — `xb-` and sixteen random hex digits (the frozen wire contract).
#: Every id a caller names is held to it (`ExhibitRef`, the routes' path segment), so a malformed
#: one is refused as malformed rather than looked up.
EXHIBIT_ID = re.compile(r"^xb-[0-9a-f]{16}$")

#: A finite JSON number, and never a boolean: `true` is not a yield, and pydantic's lax mode would
#: read it as 1. NaN and the infinities are refused because `jsonb` cannot hold them.
Number = Annotated[float, Strict(), AllowInfNan(False)] | Annotated[int, Strict()]
#: A value one table cell or one structure property may hold.
Cell = Annotated[str, Strict()] | Number


class _Spec(BaseModel):
    """The shared configuration: frozen, and refusing every key the kind does not define."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class DocumentSpec(_Spec):
    """A Markdown document — a plan, a report draft, a comparison write-up."""

    kind: Literal["document"]
    markdown: str


class Column(_Spec):
    """One table column: the key rows use, the header a chemist reads, and its unit."""

    key: str = Field(min_length=1)
    label: str
    unit: str = ""


class TableSpec(_Spec):
    """A table of literal values, keyed by column."""

    kind: Literal["table"]
    columns: list[Column] = Field(min_length=1)
    rows: list[dict[str, Cell | None]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _keys_agree(self) -> TableSpec:
        """Column keys are unique, and a row names no key the columns do not declare."""
        keys = [column.key for column in self.columns]
        if len(set(keys)) != len(keys):
            raise ValueError(f"column keys must be unique; got {keys}")
        declared = set(keys)
        for index, row in enumerate(self.rows):
            if unknown := sorted(set(row) - declared):
                raise ValueError(f"rows[{index}] uses keys no column declares: {unknown}")
        return self


class Structure(_Spec):
    """One molecule in a structures panel, drawn by the client from its SMILES."""

    smiles: str = Field(min_length=1)
    label: str = ""
    props: dict[str, Cell] = Field(default_factory=dict)


class StructuresSpec(_Spec):
    """A set of molecules shown as a grid."""

    kind: Literal["structures"]
    items: list[Structure] = Field(min_length=1)


class Series(_Spec):
    """One plotted series: x and y of equal length."""

    name: str
    x: list[Annotated[str, Strict()] | Number]
    y: list[Number]

    @model_validator(mode="after")
    def _paired(self) -> Series:
        """Every x has its y."""
        if len(self.x) != len(self.y):
            raise ValueError(
                f"series {self.name!r} has {len(self.x)} x values and {len(self.y)} y values"
            )
        return self


class ChartSpec(_Spec):
    """A line, scatter or bar chart over literal points: a small vocabulary, not a plot language."""

    kind: Literal["chart"]
    chart: Literal["line", "scatter", "bar"]
    x_label: str
    y_label: str
    series: list[Series] = Field(min_length=1)

    @model_validator(mode="after")
    def _categories_only_on_bars(self) -> ChartSpec:
        """A text x value is a category, and only a bar chart has categories."""
        if self.chart != "bar":
            for series in self.series:
                if any(isinstance(value, str) for value in series.x):
                    raise ValueError(
                        f"series {series.name!r} has text x values; only a 'bar' chart takes them"
                    )
        return self


class ResultSpec(_Spec):
    """A stored tool result pinned by the chemist; whether the session holds it is the route's."""

    kind: Literal["result"]
    result_ref: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool: str = ""


class LinkSpec(_Spec):
    """A pointer to a protocol, a knowledge note or a durable job, opened in the pane."""

    kind: Literal["link"]
    target: Literal["protocol", "note", "job"]
    id: str = Field(min_length=1)


Spec = Annotated[
    DocumentSpec | TableSpec | StructuresSpec | ChartSpec | ResultSpec | LinkSpec,
    Field(discriminator="kind"),
]
_SPEC: TypeAdapter[Spec] = TypeAdapter(Spec)


class InvalidExhibit(ChemclawError):
    """A spec, title or note this system will not store — the 422 of this package."""


class StaleRevision(ChemclawError):
    """A write whose base revision is not the head: somebody revised the artefact first."""

    def __init__(self, exhibit_id: str, head: int, base: int) -> None:
        """Name the artefact, the head it is at and the base the write was derived from."""
        super().__init__(
            f"{exhibit_id} is at revision {head}; this write was derived from revision {base}"
        )
        self.head = head


class UnknownExhibit(ChemclawError):
    """An artefact id this session does not hold — or a revision it does not have."""


class ExhibitLimit(ChemclawError):
    """The session already holds `exhibit_max_per_session` artefacts."""


def parse_spec(raw: object) -> Spec:
    """The typed spec for `raw`, refusing anything that is not one of the six shapes.

    Raises:
        InvalidExhibit: `raw` is not an object, names no known `kind`, or does not fit its kind —
            with the first few problems named, so the writer can correct them in one attempt.
    """
    try:
        return _SPEC.validate_python(raw)
    except ValidationError as exc:
        raise InvalidExhibit(f"the spec does not fit its kind: {_worded(exc)}") from exc


def _worded(exc: ValidationError) -> str:
    """The first few validation problems as one line, each with where it is."""
    shown = settings.exhibit_diff_max_changes
    problems = [
        f"{'.'.join(str(part) for part in error['loc']) or 'spec'}: {error['msg']}"
        for error in exc.errors()[:shown]
    ]
    more = len(exc.errors()) - len(problems)
    return "; ".join(problems) + (f"; and {more} more" if more > 0 else "")


def spec_json(spec: Spec) -> dict[str, Any]:
    """The spec as the JSON object that is stored, served and diffed."""
    dumped: dict[str, Any] = _SPEC.dump_python(spec, mode="json")
    return dumped


def spec_bytes(spec: Spec) -> int:
    """The spec's size as the compact UTF-8 JSON the byte cap is measured on."""
    return len(json.dumps(spec_json(spec), separators=(",", ":")).encode("utf-8"))


def require_writable(spec: Spec, *, title: str, change_note: str) -> None:
    """Refuse a spec, title or note over a cap, a SMILES RDKit cannot read, or unstorable text.

    The write-time half of validation (see the module docstring for why it is not `parse_spec`).

    Raises:
        InvalidExhibit: naming the cap and the value, or the SMILES and why RDKit refused it.
    """
    if not title.strip():
        raise InvalidExhibit("an artefact needs a title")
    if len(title) > settings.exhibit_max_title_chars:
        raise InvalidExhibit(f"the title is over {settings.exhibit_max_title_chars} characters")
    if len(change_note) > settings.exhibit_max_note_chars:
        raise InvalidExhibit(f"the note is over {settings.exhibit_max_note_chars} characters")
    size = spec_bytes(spec)
    if size > settings.exhibit_max_spec_bytes:
        raise InvalidExhibit(
            f"the spec is {size} bytes, over the {settings.exhibit_max_spec_bytes}-byte cap; "
            "split it into more than one artefact"
        )
    _require_within_counts(spec)
    for label, text in [("title", title), ("note", change_note), *_strings(spec_json(spec))]:
        if _UNSTORABLE.search(text):
            raise InvalidExhibit(
                f"{label} contains a character no text column can store (a NUL, a C0 control "
                "character or an unpaired surrogate)"
            )


def _require_within_counts(spec: Spec) -> None:
    """The per-kind count caps, and the SMILES parse for a structures panel."""
    if isinstance(spec, TableSpec) and len(spec.rows) > settings.exhibit_max_rows:
        raise InvalidExhibit(
            f"the table has {len(spec.rows)} rows, over the {settings.exhibit_max_rows}-row cap"
        )
    if isinstance(spec, StructuresSpec):
        if len(spec.items) > settings.exhibit_max_structures:
            raise InvalidExhibit(
                f"the panel has {len(spec.items)} structures, over the "
                f"{settings.exhibit_max_structures}-structure cap"
            )
        for index, item in enumerate(spec.items):
            try:
                require_molecule(item.smiles)
            except InvalidSmilesError as exc:
                raise InvalidExhibit(f"items[{index}].smiles: {exc}") from exc
    if isinstance(spec, ChartSpec):
        points = sum(len(series.y) for series in spec.series)
        if points > settings.exhibit_max_points:
            raise InvalidExhibit(
                f"the chart has {points} points, over the {settings.exhibit_max_points}-point cap"
            )


#: What no `text` or `jsonb` column can hold: NUL, the C0 controls other than tab and the two line
#: breaks, and an unpaired UTF-16 surrogate. Refused rather than stripped — stripping would store a
#: document that is not the one that was sent (`protocols/store.require_storable` makes the same
#: argument for a design).
_UNSTORABLE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff]")


def _strings(value: Any, path: str = "spec") -> Iterator[tuple[str, str]]:
    """Every string in a dumped spec, keys included, with the path that reaches it."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield f"{path}.<key>", key
            yield from _strings(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f"{path}[{index}]")


def new_exhibit_id() -> str:
    """A fresh random id in the contract's shape."""
    return f"xb-{secrets.token_hex(8)}"


class ExhibitHeader(BaseModel):
    """One artefact as a listing shows it — what it is and who wrote its head."""

    exhibit_id: str
    session_id: str
    kind: ExhibitKind
    title: str
    head_revision: int
    head_author_kind: AuthorKind
    head_author: str
    created_by: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(frozen=True, extra="forbid")


class ExhibitView(ExhibitHeader):
    """One revision of an artefact: its header, the revision's own record, and its spec.

    `unverified_figures` lists the numerals an agent-authored revision states that no tool in this
    session returned — *unchecked*, not wrong: the figure may be the chemist's own, or arithmetic,
    or a value the grounding scan could not see. Empty for a human revision and wherever nothing
    was checked.
    """

    revision: int
    parent_revision: int
    author_kind: AuthorKind
    author: str
    change_note: str
    revision_created_at: datetime
    spec: Spec
    unverified_figures: list[str] = Field(default_factory=list)


class ExhibitRevision(BaseModel):
    """One entry of an artefact's history — enough to choose a revision to open or compare."""

    revision: int
    parent_revision: int
    author_kind: AuthorKind
    author: str
    change_note: str
    created_at: datetime
    byte_size: int

    model_config = ConfigDict(frozen=True, extra="forbid")


class ExhibitDiff(BaseModel):
    """What changed between two revisions — `protocols.diff.DesignDiff`'s shape, so one renderer."""

    from_revision: int
    to_revision: int
    changes: list[FieldChange] = Field(default_factory=list)

    model_config = ConfigDict(frozen=True, extra="forbid")


class ExhibitRef(BaseModel):
    """One artefact a chemist's message points at — an id and a revision (0 for the latest)."""

    exhibit_id: str = Field(pattern=EXHIBIT_ID.pattern)
    revision: int = Field(default=0, ge=0)

    model_config = ConfigDict(frozen=True, extra="forbid")


class ExhibitState(BaseModel):
    """A header plus what the turn note needs to know about it and the wire does not carry.

    `agent_seen_revision` is the highest revision the agent has written, read or been told about,
    and `last_agent_revision` the highest it wrote (0 for an artefact the chemist created and the
    agent never revised). Internal: neither is in the frozen contract.
    """

    header: ExhibitHeader
    agent_seen_revision: int
    last_agent_revision: int

    model_config = ConfigDict(frozen=True, extra="forbid")
