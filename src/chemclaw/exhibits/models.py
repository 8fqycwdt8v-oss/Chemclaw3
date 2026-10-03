"""What an artefact is: a spec per kind, the shapes the API serves, and the errors a write can meet.

**The spec is validated here and nowhere else**, for both of its writers. The agent's tools take an
untyped `dict` (`D-2026-10-02-an-artefact-is-part-of-the-answer-not-an-effect` measured the typed
union at 1,989 prefix tokens against 961) and the REST routes take a JSON body; both arrive at
`parse_spec`, so a spec the model may not write is one a browser may not write either, and the
refusal is worded the same way for both.

**Shape and caps are two checks, on purpose.** `parse_spec` is the shape — the closed set of kinds,
`extra="forbid"` everywhere, literal values only — and it is what a read runs too, so a stored
revision always comes back typed. `require_writable` is the caps (bytes, rows, structures, points,
atoms) and the RDKit parse of every SMILES, and only a *write* runs it: a deployment that lowers a
cap must not make the artefacts it already holds unreadable, and re-parsing two hundred molecules on
every read would pay for a check whose answer cannot have changed. The one write-time check that is
not here is whether a geometry's `source` is stored — it is a database read, so it is async and
lives in `exhibits.sources` beside the store it asks.
"""

from __future__ import annotations

import json
import math
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
    SerializerFunctionWrapHandler,
    Strict,
    TypeAdapter,
    ValidationError,
    model_serializer,
    model_validator,
)

from chemclaw.core.chem import InvalidSmilesError, require_molecule
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.protocols.diff import FieldChange
from chemclaw.protocols.models import AuthorKind

#: Every kind an artefact can be. The CHECK constraint (`116_exhibit_geometry_kind.sql`) names the
#: same seven.
ExhibitKind = Literal["document", "table", "structures", "chart", "result", "link", "geometry"]

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


#: Every element symbol an XYZ line may name, H to Og, in the spelling RDKit's periodic table uses.
#: A literal set rather than an RDKit lookup: this package parses SMILES through `core.chem` and
#: imports no toolkit of its own, and the table does not change.
ELEMENTS: frozenset[str] = frozenset(
    """
    H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As Se Br
    Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho
    Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es
    Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og
    """.split()
)


def xyz_atom_count(xyz: str) -> int:
    """How many atoms one standard XYZ block holds, having checked every line of it.

    The layout is the one every program writes: an atom count, a comment line (which may be
    empty), then exactly that many `El x y z` lines in ångström. Trailing blank lines are allowed;
    anything else after the atoms — a second frame, a stray line — is refused, because an artefact
    shows one structure and a silently dropped frame is a geometry nobody asked for. The element is
    matched case-insensitively (`CL` is chlorine) against `ELEMENTS`, and every coordinate must be
    a finite number.

    Raises:
        ValueError: naming the line and what is wrong with it.
    """
    lines = xyz.rstrip().splitlines()
    try:
        count = int(lines[0].strip()) if lines else -1
    except ValueError:
        raise ValueError("xyz: the first line must be the atom count") from None
    if count < 1:
        raise ValueError("xyz: the first line must be the atom count, at least 1")
    atoms = lines[2:]
    if len(atoms) != count:
        raise ValueError(
            f"xyz: the count line says {count} atoms and the block has {len(atoms)} atom lines "
            "(one structure: the count, a comment line, then one `El x y z` line per atom)"
        )
    for number, line in enumerate(atoms, start=3):
        fields = line.split()
        if len(fields) != 4:
            raise ValueError(f"xyz line {number}: expected `El x y z`, got {len(fields)} fields")
        symbol = fields[0][:1].upper() + fields[0][1:].lower()
        if symbol not in ELEMENTS:
            raise ValueError(f"xyz line {number}: {fields[0]!r} is not an element symbol")
        try:
            coordinates = [float(value) for value in fields[1:]]
        except ValueError:
            raise ValueError(f"xyz line {number}: a coordinate is not a number") from None
        if not all(math.isfinite(value) for value in coordinates):
            raise ValueError(f"xyz line {number}: a coordinate is not finite")
    return count


class GeometrySource(_Spec):
    """A calculation by-product a geometry is read from: `science.calc.artifacts.ArtifactRef`'s key.

    Only the two fields that address a stored artifact — which calculation, and the file's role —
    so the reference is the one `fetch_artifact` and a note's `artifact_refs` already spell as
    `<calc_key>#<name>`. Whether it exists is checked when it is written (`exhibits.sources`).
    """

    calc_key: str = Field(min_length=1)
    name: str = Field(min_length=1)

    def as_ref(self) -> str:
        """The flat `<calc_key>#<name>` form, as `ArtifactRef.as_str` writes it."""
        return f"{self.calc_key}#{self.name}"


class GeometrySpec(_Spec):
    """One 3D structure, inline as an XYZ block or named as a stored calculation artifact.

    Exactly one of `xyz` and `source`. `highlight_atoms` are **0-based** indices into the atom
    lines, held inside the inline block's atom count (a `source` is not read to check them — its
    bytes are the calc store's, and an eviction may take them). `energy_hartree` is a label the
    viewer shows, not a figure anything here computes.
    """

    kind: Literal["geometry"]
    format: Literal["xyz"] = "xyz"
    xyz: str | None = None
    source: GeometrySource | None = None
    label: str = ""
    energy_hartree: Number | None = None
    highlight_atoms: list[Annotated[int, Strict(), Field(ge=0)]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_structure(self) -> GeometrySpec:
        """Exactly one of `xyz` and `source`, an inline block that parses, highlights inside it."""
        if (self.xyz is None) == (self.source is None):
            raise ValueError("a geometry takes exactly one of `xyz` (inline) or `source`")
        if self.xyz is not None:
            count = xyz_atom_count(self.xyz)
            if outside := sorted({index for index in self.highlight_atoms if index >= count}):
                raise ValueError(
                    f"highlight_atoms {outside} are not atoms of a {count}-atom block (0-based)"
                )
        return self

    @model_serializer(mode="wrap")
    def _without_absent(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """The stored and served object omits an optional field that was not given.

        `xyz?`, `source?` and `energy_hartree?` are *absent* in the wire contract, not `null`, so a
        client testing `"source" in spec` reads the same answer it would from the writer's object.
        """
        dumped: dict[str, Any] = handler(self)
        for name in ("xyz", "source", "energy_hartree"):
            if dumped.get(name) is None:
                dumped.pop(name, None)
        return dumped


Spec = Annotated[
    DocumentSpec | TableSpec | StructuresSpec | ChartSpec | ResultSpec | LinkSpec | GeometrySpec,
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
    """The typed spec for `raw`, refusing anything that is not one of the seven shapes.

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
    if isinstance(spec, GeometrySpec) and spec.xyz is not None:
        atoms = xyz_atom_count(spec.xyz)
        if atoms > settings.exhibit_max_atoms:
            raise InvalidExhibit(
                f"the geometry has {atoms} atoms, over the {settings.exhibit_max_atoms}-atom cap"
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
