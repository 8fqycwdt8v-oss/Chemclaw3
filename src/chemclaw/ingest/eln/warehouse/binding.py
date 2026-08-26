"""The binding document: a site's own schema, declared rather than compiled in.

Every other ELN adapter in this package names its source's fields in Python — `json_adapter` knows
a payload has `reaction_smiles`, `ord_adapter` knows ORD's shape. That works because both formats
were fixed before the adapter was written. A corporate warehouse is the opposite case: the tables
exist before anyone here sees them, they are site-specific, and they grow. Writing an adapter
against them means writing it on the day access arrives and editing it every time a column lands.

So the schema moves into data. A binding says which relation holds reactions, which columns carry
the cursor, which child tables hang off it, and which column becomes which field of `OrdReaction` —
and `chemclaw.ingest.eln.warehouse.adapter` executes it. Attaching the real warehouse becomes
writing YAML; adding a table to it becomes adding a block.

**Where it lives, and why inline.** In the `config:` block of the source's `datasource.yaml`, which
the registry splats into the half's constructor. Not a sibling file: `registry._build_half` hands a
half its config kwargs and nothing else, so an adapter cannot learn its own folder in order to
resolve a relative path — and inline keeps the property the seam was built for, that a deployment
mounts its own manifest directory (`CHEMCLAW_DATA_SOURCES_DIR`, earlier wins) with no path plumbing
and no image rebuild. `DataSourceManifest.config` is `dict[str, Any]` by contract, so the strictness
lives here instead: `extra="forbid"` on every model below, validated when the half is built, which
is worker startup.

**What is validated up front.** Every path parses, every transform is one of the known ones with
options it accepts, every mapped field is a real `OrdReaction` field, every `components:` block
names a `related:` block that exists, and a role vocabulary maps onto real `Role` members. All of it
offline, with no warehouse reachable — which is the point: the binding for a tenant nobody can
connect to yet is still checkable.
"""

import re
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from chemclaw.core.connect import ENV_SUFFIX, check_env_name
from chemclaw.ingest.eln.ord import OrdReaction, Role
from chemclaw.ingest.eln.warehouse.expr import (
    PathSyntaxError,
    template_paths,
    validate_path,
    validate_transform,
)
from chemclaw.science.labels.vocabulary import LabelGroup

# A relation or column name, optionally qualified (`eln_prod.reactions.v_reaction`). Interpolated
# into SQL, so it is checked rather than trusted: everything a binding contributes to a statement
# is either an identifier matching this or a bound parameter. `$` is legal inside a warehouse
# identifier and appears in generated views; it is not legal as the first character.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)*$")

# The fields of `OrdReaction` a `reaction:` entry may not map, for two different reasons.
#
# Most are built by the engine from another part of the binding, because they are *rows* rather than
# values and a field binding reads one value: components come from `components:`, the impurity
# profile from `impurities:`, provenance from `provenance:`, the attribute bag from `attributes:`.
#
# `steps` is the exception, and it is excluded rather than supported: a warehouse records a protocol
# as prose, which lands in `procedure_text` verbatim, and segmenting prose into ordered steps is
# what `json_adapter` already does. A second, YAML-driven segmenter would be that logic twice.
_ENGINE_OWNED = frozenset({"inputs", "outcomes", "impurities", "provenance", "attributes", "steps"})

_MAPPABLE_FIELDS = frozenset(OrdReaction.model_fields) - _ENGINE_OWNED

Identifier = Annotated[str, Field(min_length=1)]


class BindingError(PathSyntaxError):
    """A binding that will not do what it says. Raised when the half is built, never per row."""


def _check_identifier(value: str, what: str) -> str:
    """Raise unless `value` is a bare or dotted SQL identifier safe to interpolate."""
    # `fullmatch` rather than `match`: with a trailing `$` anchor, `match` also accepts one
    # trailing newline, so a function name ending in one passed and reached the statement text.
    # Nothing could follow that newline — the rest of the value would have to match as well — so
    # this was hygiene rather than a hole. But a checker whose whole job is "the value is exactly
    # this shape" should not rest on which of two anchor semantics it happened to get.
    if not _IDENTIFIER.fullmatch(value):
        raise BindingError(
            f"{what} {value!r} is not a plain SQL identifier; a binding may only name relations "
            "and columns, and every value it contributes is a bound parameter"
        )
    return value


class FieldBinding(BaseModel):
    """One value: where to read it, how to reshape it, and what to try if it is not there."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, description="Dotted path into the row bundle, e.g. root.COL.")
    transform: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Transforms applied left to right, each a single-key mapping.",
    )
    fallback: "FieldBinding | None" = Field(
        default=None,
        description=(
            "Tried when this one yields nothing. For the column a site populated for half its "
            "history — a structure in SMILES_STRUCTURE on new rows and CANONICAL_SMILES on old "
            "ones — where the alternative is losing the older half."
        ),
    )

    @model_validator(mode="after")
    def _is_well_formed(self) -> Self:
        """Check the path and every transform now, so a typo fails at startup, not on row 40,000."""
        validate_path(self.path)
        for step in self.transform:
            validate_transform(step)
        return self


class EntryBinding(BaseModel):
    """The root query: which relation holds reactions, and how the sync watermark reads it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    relation: Identifier
    key: Identifier = Field(description="The column carrying the stable per-reaction id.")
    created_at: Identifier
    modified_at: str = Field(
        default="",
        description=(
            "The column set when a row is amended in place. Declare it whenever the source has "
            "one: the sync's cursor filters on the later of the two, and filtering on creation "
            "alone silently drops every correction a chemist ever makes to a recorded run."
        ),
    )
    where: str = Field(
        default="",
        description=(
            "An extra predicate, ANDed with the cursor filter — typically the site's own notion of "
            "'finished' (STATUS = 'COMPLETED'). Inserted literally, so it is as trusted as the "
            "manifest itself; it is authored beside the module:callable that the same file imports."
        ),
    )
    fetch_limit: int = Field(
        default=500,
        ge=1,
        le=5_000,
        description=(
            "Rows read per fetch. Bounds memory on a first sync of a warehouse holding a decade of "
            "history, which would otherwise be one query trying to materialise the decade. Must "
            "exceed `eln_sync_batch_size` for the durable drain to make progress; the adapter "
            "warns when it does not. Capped well below any warehouse's bind limit because every "
            "fetched key becomes a bind parameter in each child table's `IN (...)` list — a "
            "generous fetch would otherwise fail the *related* queries, not this one."
        ),
    )

    @model_validator(mode="after")
    def _identifiers_are_identifiers(self) -> Self:
        """Everything interpolated into the statement is checked; `where` is trusted by design."""
        _check_identifier(self.relation, "entry relation")
        _check_identifier(self.key, "entry key")
        _check_identifier(self.created_at, "entry created_at")
        if self.modified_at:
            _check_identifier(self.modified_at, "entry modified_at")
        return self


class RelatedBinding(BaseModel):
    """One child table, joined to the entry by a foreign key and landing in the payload by name.

    This is the answer to "and many more data tables": a site's charge sheet, its analytics, its
    workup log and whatever it adds next are each one of these blocks, and none of them costs a
    line of Python. They are fetched per batch with a single `IN (...)` per block, not per row.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(
        min_length=1,
        description="The key this table's rows appear under in the payload, e.g. `charges`.",
    )
    relation: Identifier
    foreign_key: Identifier = Field(description="The column in this table pointing at the entry.")
    order_by: str = Field(
        default="",
        description=(
            "Column giving the rows their meaning-carrying order — a charge sequence, a step "
            "number. Without it the warehouse may return them in any order, and a binding that "
            "reads `charges[0]` would be reading a different row on each sync."
        ),
    )

    @model_validator(mode="after")
    def _identifiers_are_identifiers(self) -> Self:
        """`name` is a payload key, not SQL; the rest reach the statement and are checked."""
        if not re.match(r"^[a-z][a-z0-9_]*$", self.name):
            raise BindingError(
                f"related block name {self.name!r} must be lower_snake_case — it is a payload key "
                "read by paths like 'charges[0].COL', not a SQL identifier"
            )
        if self.name == "root":
            raise BindingError("'root' is the entry row's own key and cannot name a related block")
        _check_identifier(self.relation, "related relation")
        _check_identifier(self.foreign_key, "related foreign_key")
        if self.order_by:
            _check_identifier(self.order_by, "related order_by")
        return self


class ComponentBinding(BaseModel):
    """How one child table's rows become `Component`s — the charge sheet, mapped.

    A binding usually has one of these (the charge table) and may have more when a site splits
    materials across tables — solvents in one, reagents in another. Each block is scoped to its own
    table's rows, so its paths are bare column names.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    source: str = Field(
        alias="from",
        min_length=1,
        description="The `related:` block whose rows this maps.",
    )
    smiles: FieldBinding
    role: FieldBinding
    amount_mmol: FieldBinding | None = None
    mass_mg: FieldBinding | None = None
    attributes: list[Identifier] = Field(
        default_factory=list,
        description=(
            "Columns of this row carried onto the component verbatim — a lot number, a supplier, "
            "an equivalents figure. Named explicitly rather than globbed: a charge table is wide, "
            "and every column here is repeated on every component of every note."
        ),
    )

    @model_validator(mode="after")
    def _role_vocabulary_is_real(self) -> Self:
        """A role `value_map` must produce real `Role` members, checked now rather than per row.

        The single most likely mistake in a binding, and the one with the worst failure shape: a
        site vocabulary mapped to `solvant` would reject every row carrying it, and the sync would
        report a rejected batch rather than a typo in one line of YAML.
        """
        known = {role.value for role in Role}
        for step in self.role.transform:
            ((name, options),) = step.items()
            if name != "value_map":
                continue
            produced = set((options or {}).get("map", {}).values())
            if "default" in (options or {}):
                produced.add(options["default"])
            unknown = sorted(str(value) for value in produced if str(value) not in known)
            if unknown:
                raise BindingError(
                    f"role value_map produces {unknown}, which are not roles; "
                    f"valid roles: {sorted(known)}"
                )
        for column in self.attributes:
            _check_identifier(column, "component attribute column")
        return self


class ImpurityBinding(BaseModel):
    """How an analytics table's rows become the impurity profile behind the purity figure.

    Separate from `components:` because an impurity is not a charged species and carries different
    descriptors — a chromatographic name or RRT far more often than a structure. `Impurity` accepts
    a row with only a name for exactly that reason, so a binding that can reach nothing but the peak
    label still carries the profile rather than dropping it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    source: str = Field(alias="from", min_length=1)
    name: FieldBinding | None = None
    smiles: FieldBinding | None = None
    area_percent: FieldBinding | None = None

    @model_validator(mode="after")
    def _identifies_something(self) -> Self:
        """A block reading neither a name nor a structure could only produce unidentified rows."""
        if self.name is None and self.smiles is None:
            raise BindingError(
                "an impurities block must read a 'name' or a 'smiles'; an impurity identified by "
                "neither is not a record of anything"
            )
        return self


class AttributeBinding(BaseModel):
    """Which of the entry's remaining columns are carried into the note as recorded fields.

    The answer to "the warehouse has many more columns than this schema has fields". Everything
    named here lands in `OrdReaction.attributes` as a string and is rendered at the end of the note
    body, so a column nobody has yet decided is worth a typed field is still visible to a reader and
    to retrieval — instead of being dropped on the floor until someone edits the model.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    include: list[str] = Field(
        default_factory=list,
        description="Entry columns to carry, or ['*'] for every column not otherwise consumed.",
    )
    exclude: list[Identifier] = Field(
        default_factory=list,
        description="Columns to drop under ['*'] — the long prose ones, typically.",
    )
    max_fields: int = Field(
        default=40,
        ge=0,
        le=200,
        description=(
            "Ceiling on how many survive, applied in column order. A wide view would otherwise "
            "put a hundred lines of unmodelled key/value pairs into every note body, which is how "
            "an excerpt stops being about the chemistry."
        ),
    )

    @model_validator(mode="after")
    def _include_is_names_or_star(self) -> Self:
        """`['*']` or a list of columns — the two forms, never a mix that reads as both."""
        if "*" in self.include and self.include != ["*"]:
            raise BindingError("attributes.include is either ['*'] or a list of columns, not both")
        if self.include != ["*"]:
            for column in self.include:
                _check_identifier(column, "attribute column")
            if self.exclude:
                raise BindingError(
                    "attributes.exclude only means something under ['*']; with an explicit "
                    "include list, leave the column out instead"
                )
        return self


class IngestBinding(BaseModel):
    """Everything the ingest half needs: the queries, and the mapping onto `OrdReaction`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entry: EntryBinding
    related: list[RelatedBinding] = Field(default_factory=list)
    reaction: dict[str, FieldBinding] = Field(
        default_factory=dict,
        description="Mapped `OrdReaction` fields, by field name.",
    )
    components: list[ComponentBinding] = Field(min_length=1)
    impurities: list[ImpurityBinding] = Field(default_factory=list)
    attributes: AttributeBinding = Field(default_factory=AttributeBinding)
    provenance: str = Field(
        min_length=1,
        description=(
            "The citation each reaction carries, as a template over the entry row — "
            "'eln-databricks:${root.reaction_id}:${root.operator}'. Name the source in it: with "
            "two ELNs live, colliding entry ids collapse onto the same note, and the source name "
            "is what makes that visible."
        ),
    )

    @model_validator(mode="after")
    def _is_coherent(self) -> Self:
        """Cross-check the parts against each other and against `OrdReaction`'s real field set."""
        names = [block.name for block in self.related]
        duplicated = sorted({name for name in names if names.count(name) > 1})
        if duplicated:
            raise BindingError(f"related blocks must have distinct names; repeated: {duplicated}")

        unknown = sorted(set(self.reaction) - _MAPPABLE_FIELDS)
        if unknown:
            engine = sorted(set(unknown) & _ENGINE_OWNED)
            detail = (
                f" ({engine} are built by the engine from other parts of the binding)"
                if engine
                else ""
            )
            raise BindingError(
                f"reaction maps {unknown}, which are not mappable fields of OrdReaction{detail}; "
                f"mappable: {sorted(_MAPPABLE_FIELDS)}"
            )
        if "reaction_id" not in self.reaction:
            raise BindingError("reaction must map 'reaction_id' — it is the note's identity")

        row_blocks: list[ComponentBinding | ImpurityBinding] = [*self.components, *self.impurities]
        for block in row_blocks:
            if block.source not in names:
                raise BindingError(
                    f"a row block reads from {block.source!r}, which is not a related block; "
                    f"declared: {sorted(names)}"
                )
        for path in template_paths(self.provenance):
            validate_path(path)
        return self


class ConnectionBinding(BaseModel):
    """Where the database is and how to reach it — addresses here, secrets named, never carried.

    **This model deliberately declares one field.** Everything else in the block is passed to
    whatever `driver:` names, as keyword arguments: *the driver's signature is the schema*. That is
    the one thing that makes this seam general over databases rather than over one vendor's idea of
    a connection, and it was learned by getting it wrong. The first version of this model enumerated
    `account_env`, `user_env`, `private_key_env`, `warehouse` and `role` — Snowflake's words — so
    the second driver had to redefine three of them to mean something else (`account` a hostname,
    `password` a token, `warehouse` an HTTP path) and *refuse* the two with no analogue, while
    `publish/connect.py` declined to reuse the model at all rather than make one model mean two
    things. A Postgres, a lakehouse, a DuckDB file and a vector database do not share a vocabulary,
    and a shared model can only be their union.

    So this is `extra="allow"`, and the strictness that `extra="forbid"` bought elsewhere comes back
    at the point it actually applies: the driver is built with these keywords on the first
    connection, so a key it does not accept is a `TypeError` naming the keyword, from the callable
    that owns the vocabulary. Waiting for a connection to learn that would be too late, so
    `make datasource-validate` runs the same check offline — it resolves the driver and binds the
    block against its signature without connecting (`cli/validate_datasources._check_connection`),
    which is the rule the result-sink seam already applied to its own `connection:` block.

    Credentials are named, not carried: a key ending `_env` holds the *name* of an environment
    variable, read at connect time. That is the connector seam's `token_env` idiom
    (`connectors/manifest.py`), and it is what lets this document be a file in a repository. The
    names are deliberately not `CHEMCLAW_`-prefixed — they are the database client's own
    credentials, not settings of this application, and a `CHEMCLAW_*` name would have to become a
    field of `Settings` to satisfy the checks that keep `.env.example` honest.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    driver: str = Field(
        min_length=1,
        description=(
            "`module:callable` building a `Warehouse`. Late-bound exactly like the data-source "
            "seam's own halves, so the vendor client is imported only in a process that connects."
        ),
    )

    @model_validator(mode="after")
    def _names_no_secrets(self) -> Self:
        """`*_env` must look like a variable name, so a pasted secret fails loudly not quietly.

        Not a security boundary — a determined author can still paste anything — but it catches the
        realistic mistake, which is someone filling in `password_env: hunter2` because the field
        sits where a password would go in every other tool they have used. Applied to every key
        ending in `_env` rather than to a list of known credential names: the whole point of this
        block is that the credential names are the driver's, and this repository does not know them.
        """
        for key, value in self.options.items():
            if key.endswith(ENV_SUFFIX):
                check_env_name(key, str(value or ""), error=BindingError)
        if ":" not in self.driver:
            raise BindingError(f"connection.driver must be 'module:callable'; got {self.driver!r}")
        return self

    @property
    def options(self) -> dict[str, Any]:
        """The driver's keyword arguments as written, secrets still named rather than read.

        `model_extra` rather than `model_dump()` minus a key, because the two differ on a field this
        model *declares*: `driver` is the seam's, everything else is the driver's.
        """
        return dict(self.model_extra or {})


class VectorBinding(BaseModel):
    """The similarity search, run inside the warehouse rather than over a local index.

    The warehouse already holds an embedding per reaction; copying it into this system's own index
    would mean re-embedding a corpus that is bigger than what gets ingested, and keeping the copy
    fresh forever. Searching it where it lives makes the whole ELN reachable as evidence while only
    the curated part becomes knowledge-graph notes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    relation: Identifier
    key: Identifier
    # Empty when `index:` is set: an index ranks on its own copy of the vectors, so there is no
    # column here to call a similarity function on.
    vector_column: str = ""
    # A Mosaic AI Vector Search index (a three-level Unity Catalog name), when the corpus is too
    # large to scan. Ranking then happens on the index through the configured `VectorStore` and this
    # relation is queried only to *resolve* the winning keys into content — which is the same
    # division `ingest/documents/external_index.py` makes, with Databricks SQL standing in for
    # Postgres as the catalogue. The endpoint serving it is a deployment fact
    # (`CHEMCLAW_VECTOR_STORE_ENDPOINT_NAME`), not a per-source one, so it is not named here.
    index: str = ""
    content_columns: list[Identifier] = Field(
        min_length=1,
        description="Columns rendered into the evidence chunk a chemist reads.",
    )
    metric: str = Field(default="cosine", pattern="^(cosine|l2|inner)$")
    embedding: str = Field(
        default="local",
        pattern="^(local|server)$",
        description=(
            "`local` embeds the query here and binds the vector; `server` calls the warehouse's "
            "own embedding function, for when it owns the model that produced the column."
        ),
    )
    server_embed_function: str = Field(
        default="",
        description="Warehouse function used when embedding is `server`, e.g. a Cortex embedder.",
    )
    server_embed_model: str = Field(
        default="",
        description=(
            "Model name bound as that function's first argument, for embedders that take one. "
            "Left empty for a plain single-argument UDF."
        ),
    )
    filter_columns: dict[str, Identifier] = Field(
        default_factory=dict,
        description=(
            "Maps the evidence filter keys this system honours (`tag`, `since`, `until`) onto the "
            "site's own columns. An unmapped key is ignored rather than guessed at."
        ),
    )
    where: str = Field(default="", description="An extra literal predicate, as on the entry query.")
    suppress_ingested: bool = Field(
        default=True,
        description=(
            "Drop a hit whose reaction already became a note. Without it a curated reaction would "
            "reach the agent twice — once as reviewed, merged knowledge and once as a raw row — "
            "and the duplicate would look like corroboration."
        ),
    )

    @model_validator(mode="after")
    def _is_coherent(self) -> Self:
        """Identifiers are checked; `server` embedding needs the function it will call."""
        _check_identifier(self.relation, "vector relation")
        _check_identifier(self.key, "vector key")
        if self.index:
            # An index-ranked source and a scanned one are two different shapes, and a binding that
            # half-declares both would silently take one of them. Each contradiction is named.
            if self.vector_column:
                raise BindingError(
                    "a `vector:` block naming an `index` must not also name a `vector_column`: "
                    "the index holds the vectors, and the relation is queried only to resolve the "
                    "keys it returns into content"
                )
            if self.metric != "cosine":
                raise BindingError(
                    f"an index-ranked source is scored by `VectorMatch`, which is a cosine, so "
                    f"metric must be 'cosine' rather than {self.metric!r}"
                )
            if self.embedding != "local":
                raise BindingError(
                    "an index-ranked source embeds the query here and sends the vector; there is "
                    "no statement for a warehouse embedding function to appear in"
                )
        else:
            if not self.vector_column:
                raise BindingError(
                    "a `vector:` block needs either a `vector_column` to rank on, or an `index` to "
                    "rank in"
                )
            _check_identifier(self.vector_column, "vector column")
        for column in self.content_columns:
            _check_identifier(column, "vector content column")
        known = {"tag", "since", "until"}
        unknown = sorted(set(self.filter_columns) - known)
        if unknown:
            raise BindingError(
                f"filter_columns maps {unknown}, which are not honoured filter keys; "
                f"honoured: {sorted(known)}"
            )
        for column in self.filter_columns.values():
            _check_identifier(column, "vector filter column")
        if self.embedding == "server" and not self.server_embed_function:
            raise BindingError("embedding 'server' needs a server_embed_function to call")
        if self.embedding == "server":
            # Checked for the same reason every other interpolated name is: `sql.vector_statement`
            # writes this one into the statement text as `f"{fn}({placeholder}, {placeholder})"`,
            # so an unchecked value closes the call and continues the query. It was the single
            # field this validator skipped, which made `sql.py`'s "only checked identifiers are
            # written here" false for exactly one field — and it is also the one field a site
            # author edits rather than a reviewer. A dotted name passes, so a real qualified
            # embedder (`main.ml.embed_text`, a vendor's built-in) is unaffected.
            _check_identifier(self.server_embed_function, "server embed function")
        if self.embedding == "local" and (self.server_embed_function or self.server_embed_model):
            raise BindingError(
                "server_embed_function/server_embed_model are only used when embedding is "
                "'server'; leaving them set under 'local' hides which path actually runs"
            )
        return self


class CorpusBinding(BaseModel):
    """A bulk reaction corpus in the warehouse: one row per reaction, already a reaction SMILES.

    Deliberately far simpler than `IngestBinding`, and the difference is what the two are for. An
    ELN hands us a run and its charge table, so a binding has to reassemble a reaction from rows in
    several relations. A reaction corpus — Pistachio, an HTE export, any bulk extract — hands us the
    reaction *already assembled* as `reactants>agents>products`, plus whatever metadata the vendor
    classified. There is nothing to reassemble, so there is no `related:`, no `components:` and no
    `impurities:` block: the species come from splitting the SMILES, and their refined roles come
    from the labeller, which is the whole point of the label index.

    **Keyset pagination, not a datetime cursor.** A corpus release is a versioned load addressed by
    key; it is not a live feed where "everything since Tuesday" is meaningful. `order_by` names the
    column the drain walks, and each pass resumes strictly after the last key it saw — which also
    means a drain is safe to stop and resume at any point, and that a re-run over an unchanged
    release is a no-op rather than a re-ingest.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    relation: Identifier
    key: Identifier = Field(description="The column carrying the stable per-reaction id.")
    order_by: Identifier = Field(
        default="",
        description=(
            "The column the drain paginates on, if not the key itself. Must be unique and stable "
            "across the release, because 'resume strictly after this value' is the whole cursor."
        ),
    )
    where: str = Field(
        default="",
        description=(
            "An extra predicate, ANDed with the keyset filter — the corpus's own notion of which "
            "rows are usable. Inserted literally, so it is as trusted as the manifest itself; the "
            "same trade `EntryBinding.where` makes and for the same reason."
        ),
    )
    fetch_limit: int = Field(
        default=1_000,
        ge=1,
        le=20_000,
        description=(
            "Rows read per pass. Higher than the ELN's ceiling because there are no child-table "
            "`IN (...)` lists to blow a bind limit — one relation, one query — and a corpus is "
            "millions of rows rather than a decade of one site's runs."
        ),
    )

    smiles: FieldBinding = Field(
        description="The reaction SMILES, `reactants>agents>products`. The one required value."
    )
    citation: FieldBinding = Field(
        description=(
            "What an answer cites for this row — a patent number, a DOI, a document id. Required, "
            "because a precedent a chemist cannot follow back is not a precedent."
        )
    )
    published_on: FieldBinding | None = None
    temperature_c: FieldBinding | None = None
    time_h: FieldBinding | None = None
    yield_percent: FieldBinding | None = None
    workup_text: FieldBinding | None = None

    # The labels a corpus may already carry. Declaring one here is what its `labels: provides:`
    # block in the manifest claims, and `make datasource-validate` checks the two against each
    # other — a `provides` naming a group no column supplies would be a lie the coverage report
    # then repeats to a chemist.
    named_reaction: FieldBinding | None = None
    reaction_class: FieldBinding | None = None
    rxno_id: FieldBinding | None = None
    mapped_smiles: FieldBinding | None = None

    @model_validator(mode="after")
    def _identifiers_are_identifiers(self) -> Self:
        """Everything interpolated into the statement is checked; `where` is trusted by design."""
        _check_identifier(self.relation, "corpus relation")
        _check_identifier(self.key, "corpus key")
        if self.order_by:
            _check_identifier(self.order_by, "corpus order_by")
        return self

    @property
    def cursor_column(self) -> str:
        """The column the keyset walk resumes after — `order_by` when given, else the key."""
        return self.order_by or self.key

    def label_groups(self) -> frozenset[LabelGroup]:
        """Which label groups this binding actually maps a column for.

        The declaration `make datasource-validate` compares the manifest's `labels: provides:`
        against, so a source cannot claim to carry a name it has no column for.
        """
        groups: set[LabelGroup] = set()
        if self.named_reaction is not None or self.rxno_id is not None:
            groups.add(LabelGroup.NAMED_REACTION)
        if self.mapped_smiles is not None:
            groups.add(LabelGroup.ATOM_MAPPING)
        return frozenset(groups)


class WarehouseBinding(BaseModel):
    """One warehouse, and whichever of the three sections this source declares.

    `ingest` reassembles an ELN run from several relations and ends at its `reaction_records`
    transcription; `corpus` walks a bulk reaction table into the label index as cited evidence;
    `vector` is similarity search over an embedding the warehouse already holds. A source may
    declare any combination, and most declare one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    connection: ConnectionBinding
    ingest: IngestBinding | None = None
    corpus: CorpusBinding | None = None
    vector: VectorBinding | None = None

    @model_validator(mode="after")
    def _declares_something(self) -> Self:
        """A binding with neither half configures a connection that nothing would ever open."""
        if self.ingest is None and self.corpus is None and self.vector is None:
            raise BindingError(
                "a warehouse binding must declare an 'ingest', a 'corpus' or a 'vector' section"
            )
        return self


def load_binding(raw: Any) -> WarehouseBinding:
    """Validate a raw `config: {binding: ...}` block, raising `BindingError` with the reason.

    The one entry point both halves use, so a malformed binding fails identically whichever half a
    process happens to build — and fails at construction, which is worker startup, rather than on
    the first row that reaches the bad line.
    """
    if not isinstance(raw, dict):
        raise BindingError(f"binding must be a mapping, got {type(raw).__name__}")
    try:
        return WarehouseBinding.model_validate(raw)
    except BindingError:
        raise
    except ValueError as exc:
        raise BindingError(f"invalid warehouse binding: {exc}") from exc
