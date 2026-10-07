"""The ingest half: an `ElnAdapter` whose knowledge of the source is a binding, not code.

`fetch_new_entries` runs the binding's queries and bundles each reaction with its child rows;
`map_to_ord` walks the binding to build an `OrdReaction`. Nothing below names a table or column, so
attaching a new warehouse, or a new column, is YAML. Cursor, overlap, dedup, reject-and-continue and
the chunked drain are inherited from `chemclaw.ingest.eln.sync` and `ElnSyncWorkflow`.
"""

import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from chemclaw.core.config import settings
from chemclaw.ingest.eln.adapter import ElnMappingError, RawEntry, parse_iso_utc
from chemclaw.ingest.eln.ord import (
    Component,
    Impurity,
    OrdReaction,
    Role,
    unresolved_peak_name,
)
from chemclaw.ingest.eln.warehouse import sql
from chemclaw.ingest.eln.warehouse.binding import (
    AttributeBinding,
    ComponentBinding,
    FieldBinding,
    IngestBinding,
    WarehouseBinding,
    load_binding,
)
from chemclaw.ingest.eln.warehouse.connect import open_warehouse
from chemclaw.ingest.eln.warehouse.driver import Warehouse
from chemclaw.ingest.eln.warehouse.expr import (
    apply_transforms,
    as_text,
    render_template,
    resolve_path,
)
from chemclaw.ingest.rejections import record_refusals

logger = logging.getLogger(__name__)

# The payload key the entry's own row lands under, so a binding can say `root.COL` and a child block
# can never shadow it (`RelatedBinding` rejects the name).
ROOT = "root"

# How many pages one fetch may take to get past a single watermark value before reporting itself
# truncated. Rows of the block are held in memory, so this bounds that; past it the source cannot
# resume on a timestamp cursor, and the sync workflow's no-advance guard stops it with a warning.
_MAX_TIE_PAGES = 10


class WarehouseElnAdapter:
    """An `ElnAdapter` over a SQL warehouse, configured entirely by its binding.

    Built by the registry from a manifest's `config:` block, so the constructor signature is the
    manifest's schema. `name` is the data source this adapter is (warnings, ledger `source`), and
    the constructor matches `WarehouseVectorRetriever`'s because the registry splats one `config`
    into either half.
    """

    def __init__(self, binding: dict[str, Any], name: str | None = None) -> None:
        """Validate the binding now — at worker startup — rather than on the first row it breaks."""
        self._binding: WarehouseBinding = load_binding(binding)
        if self._binding.ingest is None:
            raise ElnMappingError(
                "this data source declares an ingest half, but its binding has no 'ingest' section"
            )
        self._ingest: IngestBinding = self._binding.ingest
        self._name = name or "warehouse"
        self._warehouse: Warehouse | None = None
        # Whether the last fetch stopped because the page filled rather than because the source ran
        # out; read via `fetch_was_truncated`.
        self._truncated = False
        if self._ingest.entry.fetch_limit < settings.eln_sync_batch_size:
            logger.warning(
                "%s: entry.fetch_limit (%d) is below eln_sync_batch_size (%d); the durable sync "
                "drains in batches of the larger number and will not make progress past the first "
                "chunk",
                self._name,
                self._ingest.entry.fetch_limit,
                settings.eln_sync_batch_size,
            )

    async def _connection(self) -> Warehouse:
        """The warehouse, opened once per adapter and reused across syncs."""
        if self._warehouse is None:
            self._warehouse = open_warehouse(self._binding.connection)
        return self._warehouse

    async def fetch_new_entries(self, since: datetime, limit: int | None = None) -> list[RawEntry]:
        """Every reaction created or amended at or after `since`, oldest first.

        Inclusive on `since` (replaying the boundary is safe; skipping it is not); amendments count
        via `sql.watermark_expression`, so bindings should declare `modified_at` when the source has
        one.

        A page that cannot move the cursor is continued inside the watermark block, by the composite
        keyset `entry_statement` orders on, until a later watermark appears or the source runs out
        (see `_MAX_TIE_PAGES`). A row that cannot become a `RawEntry` is logged, filed in the
        rejection ledger and skipped. The block at the cursor is re-read each run and skipped as
        unchanged; binding a finer watermark column reduces that.

        `limit` bounds the ordinary page (`min(limit, fetch_limit)`), which also bounds each child
        relation's `IN (...)` list. Tie-crossing pages keep the binding's `fetch_limit`, so the
        bound never shrinks the block a fetch can cross.

        Args:
            since: The cursor; rows at or after it, in watermark order.
            limit: At most this many rows on the ordinary page. `None` reads the binding's page.
        """
        warehouse = await self._connection()
        entry = self._ingest.entry
        page_size = min(limit, entry.fetch_limit) if limit is not None else entry.fetch_limit
        rows: list[dict[str, Any]] = []
        after_key = ""
        for _ in range(_MAX_TIE_PAGES):
            # The ordinary page is the caller's bound; a continuation page is crossing a watermark
            # block and takes the binding's full page, for the reason the docstring gives.
            size = entry.fetch_limit if after_key else page_size
            page = await self._page(warehouse, since, after_key, size)
            rows.extend(page)
            # Short of the limit means the source had nothing more to give, so there is nothing
            # waiting and nothing to page past.
            self._truncated = len(page) == size
            if not self._truncated or self._watermark(rows[-1]) > since:
                break
            last_key = rows[-1].get(entry.key)
            if last_key is None:
                # The keyset cannot continue past a row with no key (`as_text` would yield the
                # string `"None"`), so the fetch stops here, still reporting itself truncated.
                logger.warning(
                    "%s: the last row of a page carries no %s, so this fetch cannot page past the "
                    "watermark %s; the declared key must be present on every row",
                    self._name,
                    entry.key,
                    since.isoformat(),
                )
                break
            after_key = as_text(last_key)
        else:
            logger.warning(
                "%s: more than %d pages of %s share the watermark %s, so this fetch cannot get "
                "past it and the sync cursor cannot advance. Bind `created_at`/`modified_at` to a "
                "column with sub-block resolution, or narrow `where:`",
                self._name,
                _MAX_TIE_PAGES,
                entry.relation,
                since.isoformat(),
            )
        if not rows:
            return []

        # entry key -> why it was refused. These rows never become a `RawEntry`, so they are filed
        # here rather than by the sync.
        refused: dict[str, str] = {}

        # Presence, not truthiness: a key of `0` is real. A blank key is refused rather than
        # silently skipped.
        keyed = [row for row in rows if _has_key(row, entry.key)]
        if len(keyed) != len(rows):
            logger.warning(
                "%s: %d of %d rows carried no %s and were skipped",
                self._name,
                len(rows) - len(keyed),
                len(rows),
                entry.key,
            )
            # One row per fetch keyed by the empty column, since these rows have no id; the ledger's
            # `occurrences`/`last_seen` say whether it is still happening.
            refused[f"<no {entry.key}>"] = (
                f"{len(rows) - len(keyed)} of {len(rows)} rows fetched from {entry.relation} "
                f"carried no {entry.key!r} and could not be ingested: the binding's `key` is what "
                "identifies a reaction, and a row without one cannot be cited, amended or "
                "withdrawn. Bind `key` to a column that is always populated, or narrow `where:`"
            )
        bundles = {str(row[entry.key]): {ROOT: row} for row in keyed}
        if len(bundles) != len(keyed):
            # Counted apart from missing keys: a repeated key means the declared `key` is not unique
            # (e.g. a joined view), a different fault with the same symptom.
            logger.warning(
                "%s: %d row(s) shared a %s with another row and only one survived; the declared "
                "key is not unique in %s",
                self._name,
                len(keyed) - len(bundles),
                entry.key,
                entry.relation,
            )
            # Keyed by the shared id, which the survivor is ingested under, so a citation of it can
            # learn it was one of several. Unlike `adapter.refuse_colliding_ids`, the survivor is
            # kept: the two rows may not be in the same page, and the source can still amend it.
            for shared, count in _repeated_keys(keyed, entry.key).items():
                refused[shared] = (
                    f"{count} rows in {entry.relation} share the {entry.key!r} {shared!r}; only "
                    "one of them is ingested under it and the rest are lost, so a citation of it "
                    "does not name one run. The declared `key` is not unique in that relation — "
                    "bind it to the reaction's own key, or point `relation:` at a view with one "
                    "row per reaction"
                )
        await self._attach_related(warehouse, bundles)
        entries: list[RawEntry] = []
        # A row with a NULL, empty or unparseable `created_at` costs only itself: raising here would
        # escape every per-entry handler as a non-retryable error and stop the source on the same
        # page forever.
        for key, bundle in bundles.items():
            try:
                entries.append(self._raw_entry(key, bundle))
            except ElnMappingError as exc:
                logger.warning("%s: skipping entry %s: %s", self._name, key, exc)
                refused[key] = str(exc)
        # Filed here because these rows never reach `IngestSummary.rejected`, which
        # `durable/eln_sync.py` files for every other refusal.
        await record_refusals(self._name, refused)
        return entries

    def fetch_truncated(self) -> bool:
        """Whether the last fetch was cut short by its own `LIMIT` (the `BoundedFetch` contract)."""
        return self._truncated

    async def _page(
        self, warehouse: Warehouse, since: datetime, after_key: str, size: int
    ) -> list[dict[str, Any]]:
        """One page of `size` entry rows, at the cursor or inside a watermark block.

        `size` is passed in because the ordinary page and the tie-crossing page use different
        bounds.
        """
        entry = self._ingest.entry
        statement, params = sql.entry_statement(
            entry, warehouse.placeholder, since, size, after_key
        )
        async with warehouse.cursor() as cursor:
            await cursor.execute(statement, params)
            return await cursor.fetchall()

    def _watermark(self, row: dict[str, Any]) -> datetime:
        """The row's own value of the column the page is ordered on — `COALESCE(modified, created)`.

        Must match `sql.watermark_expression` exactly (not `entry_window`'s `max`), since paging
        follows what the warehouse sorted on. An unreadable timestamp reads as no later than the
        cursor, so paging continues; `fetch_new_entries` then refuses that row by name.
        """
        entry = self._ingest.entry
        if entry.modified_at and row.get(entry.modified_at) is not None:
            return _optional_timestamp(row.get(entry.modified_at)) or datetime.min.replace(
                tzinfo=UTC
            )
        return _optional_timestamp(row.get(entry.created_at)) or datetime.min.replace(tzinfo=UTC)

    async def _attach_related(
        self, warehouse: Warehouse, bundles: dict[str, dict[str, Any]]
    ) -> None:
        """Fetch every declared child table for the whole batch and file its rows by entry key.

        One query per block, not per row. Rows matching no entry in the batch are dropped; the `IN
        (...)` list scopes them. Sliced per `fetch_limit` keys, since a tie-crossing batch can span
        several pages and `fetch_limit` is what keeps the bind list under the warehouse's limit.
        """
        keys = list(bundles)
        page = self._ingest.entry.fetch_limit
        for block in self._ingest.related:
            for bundle in bundles.values():
                bundle.setdefault(block.name, [])
            for start in range(0, len(keys), page):
                statement, params = sql.related_statement(
                    block, warehouse.placeholder, keys[start : start + page]
                )
                async with warehouse.cursor() as cursor:
                    await cursor.execute(statement, params)
                    rows = await cursor.fetchall()
                for row in rows:
                    owner = bundles.get(str(row.get(block.foreign_key, "")))
                    if owner is not None:
                        owner[block.name].append(row)

    def _raw_entry(self, key: str, bundle: dict[str, Any]) -> RawEntry:
        """Wrap one bundled reaction as the `RawEntry` the sync loop passes back to `map_to_ord`."""
        entry = self._ingest.entry
        row = bundle[ROOT]
        return RawEntry(
            entry_id=key,
            created_at=_timestamp(row.get(entry.created_at), entry.created_at, key),
            modified_at=(
                _stated_timestamp(row.get(entry.modified_at), entry.modified_at, key)
                if entry.modified_at
                else None
            ),
            payload=bundle,
            # The site's own withdrawal, when the binding names its column. Absent means the source
            # does not report withdrawals, never "withdrawn"
            # (D-2026-09-13-a-withdrawal-is-a-fact-a-source-reports).
            retracted_at=(
                _stated_timestamp(row.get(entry.retracted_at), entry.retracted_at, key)
                if entry.retracted_at
                else None
            ),
        )

    def map_to_ord(self, raw: RawEntry) -> OrdReaction:
        """Build the canonical reaction this binding says the row describes.

        Every failure is an `ElnMappingError` (or a `TransformError`, which is one), so the row is
        rejected with its reason and the batch continues.
        """
        binding = self._ingest
        # A field the source was silent about is omitted, not passed as `None`, so the model's
        # default applies and a missing `reaction_id` raises "field required". `outcome_class` is
        # optional, so a source without a status column states no outcome
        # (D-2026-08-26-silence-is-not-a-successful-run).
        fields = {
            name: value
            for name, field in sorted(binding.reaction.items())
            if (value := _read(field, raw.payload)) is not None
        }
        inputs, outcomes = self._components(raw.payload)
        provenance = _provenance(binding.provenance, raw.payload, raw.entry_id)
        attributes = _attributes(binding.attributes, raw.payload[ROOT], self._consumed())

        try:
            return OrdReaction(
                **fields,
                inputs=inputs,
                outcomes=outcomes,
                impurities=self._impurities(raw.payload),
                provenance=provenance,
                attributes=attributes,
            )
        except ValidationError as exc:
            raise ElnMappingError(
                f"entry {raw.entry_id!r} does not form a reaction: {exc}"
            ) from exc

    def _components(self, payload: dict[str, Any]) -> tuple[list[Component], list[Component]]:
        """Split every mapped component row into the reaction's inputs and its products.

        By the role the binding produced, not the source table, since products often share the
        charge table.
        """
        inputs: list[Component] = []
        outcomes: list[Component] = []
        for block in self._ingest.components:
            for row in payload.get(block.source, []):
                component = _component(block, row)
                if component is None:
                    continue
                (outcomes if component.role is Role.PRODUCT else inputs).append(component)
        if not inputs or not outcomes:
            raise ElnMappingError(
                f"the binding produced {len(inputs)} input(s) and {len(outcomes)} product(s); "
                "a reaction needs at least one of each. Check the role value_map and whether the "
                "charge table carries the product row"
            )
        return inputs, outcomes

    def _impurities(self, payload: dict[str, Any]) -> list[Impurity]:
        """The impurity profile, skipping rows that identify nothing.

        Skipped rather than rejected: analytics tables carry system peaks and blank rows.
        """
        found: list[Impurity] = []
        for block in self._ingest.impurities:
            for row in payload.get(block.source, []):
                scope = {ROOT: row, **row}
                name = _read(block.name, scope) if block.name else None
                smiles = _read(block.smiles, scope) if block.smiles else None
                area = _read(block.area_percent, scope) if block.area_percent else None
                rrt = _read(block.rrt, scope) if block.rrt else None
                # An RRT-only row is identified by where it eluted, so it is named rather than
                # dropped (`Impurity._identifiable`).
                # Coerced first, since drivers return NUMERIC as `Decimal` and text as `str`; a
                # value that will not coerce is passed on for `Impurity` to refuse by name.
                retention = _rrt(rrt)
                if not name and not smiles and retention is not None and retention > 0:
                    name = unresolved_peak_name(retention)
                if not name and not smiles:
                    continue
                try:
                    found.append(
                        Impurity(
                            name=str(name) if name else None,
                            smiles=str(smiles) if smiles else None,
                            area_percent=area,
                            rrt=rrt if retention is None else retention,
                        )
                    )
                except ValidationError as exc:
                    raise ElnMappingError(
                        f"impurity row {name or smiles!r} is invalid: {exc}"
                    ) from exc
        return found

    def _consumed(self) -> set[str]:
        """Entry columns already carried by a mapped field, so `['*']` does not repeat them.

        Only direct reads of the entry's own columns.
        """
        entry = self._ingest.entry
        consumed = {entry.key, entry.created_at}
        if entry.modified_at:
            consumed.add(entry.modified_at)
        for field in self._ingest.reaction.values():
            consumed.update(_root_columns(field))
        return consumed


def _root_columns(field: FieldBinding) -> set[str]:
    """The entry columns a field binding reads, following its fallback chain."""
    columns: set[str] = set()
    current: FieldBinding | None = field
    while current is not None:
        head, _, tail = current.path.partition(".")
        if head == ROOT and tail and "." not in tail and "[" not in tail:
            columns.add(tail)
        current = current.fallback
    return columns


def _read(field: FieldBinding, scope: dict[str, Any]) -> Any:
    """Resolve one field binding, falling back while the result is still nothing."""
    current: FieldBinding | None = field
    while current is not None:
        value = apply_transforms(resolve_path(current.path, scope), current.transform)
        if value is not None and value != "":
            return value
        current = current.fallback
    return None


def _component(block: ComponentBinding, row: dict[str, Any]) -> Component | None:
    """One charge row as a `Component`, or `None` when it names no structure.

    A row with no structure is skipped (vessels, notes, blank rows); a structure with no usable role
    is an error, a vocabulary the binding missed.
    """
    scope = {ROOT: row, **row}
    smiles = _read(block.smiles, scope)
    if smiles is None or not str(smiles).strip():
        return None
    role = _read(block.role, scope)
    if role is None:
        raise ElnMappingError(
            f"charge row for {str(smiles)[:60]!r} produced no role; "
            "the role binding read nothing and declared no default"
        )
    try:
        return Component(
            smiles=str(smiles).strip(),
            role=Role(str(role)),
            amount_mmol=_read(block.amount_mmol, scope) if block.amount_mmol else None,
            mass_mg=_read(block.mass_mg, scope) if block.mass_mg else None,
            attributes=_named_attributes(block.attributes, row),
        )
    except (ValidationError, ValueError) as exc:
        raise ElnMappingError(f"charge row {str(smiles)[:60]!r} is not a component: {exc}") from exc


def _named_attributes(columns: list[str], row: dict[str, Any]) -> dict[str, str]:
    """The named columns of a row that actually hold something, as strings."""
    return {
        column: as_text(row[column])
        for column in columns
        if row.get(column) is not None and str(row[column]).strip()
    }


def _attributes(
    binding: AttributeBinding, row: dict[str, Any], consumed: set[str]
) -> dict[str, str]:
    """The entry columns carried into the note verbatim, bounded and in column order.

    Under `['*']`, everything no field already took, so a newly added warehouse column appears
    without code changes.
    """
    if binding.include == ["*"]:
        excluded = set(binding.exclude) | consumed
        names = [column for column in row if column not in excluded]
    else:
        names = list(binding.include)

    carried = {
        column: as_text(row[column]).strip()
        for column in names
        if row.get(column) is not None and str(row[column]).strip()
    }
    if len(carried) <= binding.max_fields:
        return carried
    logger.warning(
        "attribute bag truncated to %d of %d columns; raise attributes.max_fields or name the "
        "columns explicitly if the dropped ones matter",
        binding.max_fields,
        len(carried),
    )
    return dict(list(carried.items())[: binding.max_fields])


def _provenance(template: str, payload: dict[str, Any], entry_id: str) -> str:
    """Render the citation, refusing one that resolved to nothing.

    `OrdReaction.provenance` becomes the record's `source`; a template whose references are all
    empty falls back to naming the entry.
    """
    rendered = render_template(template, payload).strip(": ").strip()
    return rendered or f"warehouse:{entry_id}"


def _has_key(row: dict[str, Any], column: str) -> bool:
    """Whether this row carries a usable entry key in `column`.

    A `0` is a key; `NULL` and a blank string are not, and a blank one is refused rather than
    skipped, since it suggests a mis-bound column.
    """
    value = row.get(column)
    return value is not None and str(value).strip() != ""


def _repeated_keys(rows: list[dict[str, Any]], column: str) -> dict[str, int]:
    """How many rows claim each entry key that more than one row claims.

    Counted separately so the `bundles` comprehension stays the one place the surviving row is
    chosen.
    """
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row[column])
        counts[key] = counts.get(key, 0) + 1
    return {key: count for key, count in counts.items() if count > 1}


def _timestamp(value: Any, column: str, entry_id: str) -> datetime:
    """Read a required timestamp column, or reject the row naming what was missing."""
    parsed = _optional_timestamp(value)
    if parsed is None:
        raise ElnMappingError(
            f"entry {entry_id!r} has no usable {column!r}; the sync cursor is a timestamp and "
            "cannot order a row without one"
        )
    return parsed


def _rrt(value: Any) -> float | None:
    """A relative retention time as a float, or `None` when the cell holds nothing readable.

    `float()` rather than `isinstance`, since drivers return NUMERIC as `Decimal` and text as `str`.
    A `bool` is refused. `None` only affects naming; the caller passes the raw value on for
    `Impurity` to validate.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _stated_timestamp(value: Any, column: str, entry_id: str) -> datetime | None:
    """An amendment or withdrawal stamp: `None` only when the column is genuinely empty.

    A present but unparseable value raises `ElnMappingError` (filed per entry, naming the column)
    rather than reading as absent: an absent `modified_at` would keep an amended row out of the
    fetch window forever, and an absent `retracted_at` would leave a withdrawn record live.
    `_watermark` keeps the lenient reader, since paging must continue past an unreadable value.
    """
    if value is None:
        return None
    if not isinstance(value, datetime) and not str(value).strip():
        return None
    parsed = _optional_timestamp(value)
    if parsed is None:
        raise ElnMappingError(
            f"entry {entry_id!r} carries {str(value)[:60]!r} in {column!r}, which is not a "
            "timestamp this ingest can read. The column is bound as an amendment or withdrawal "
            "stamp, and reading an unparseable one as absent would drop the amendment silently. "
            "An entry column takes no `transform:`, so the remedy is at the source: bind a view "
            "column that is NULL where no stamp was made (e.g. `NULLIF(col, '0000-00-00 "
            "00:00:00')` for a MySQL zero-date), or exclude such rows with the entry's `where:`"
        )
    return parsed


def _optional_timestamp(value: Any) -> datetime | None:
    """Read a timestamp from a driver-native value or an ISO string; `None` when absent.

    Lenient, for `_watermark` only; record fields go through `_stated_timestamp` or `_timestamp`,
    which refuse.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return parse_iso_utc(value.isoformat())
    text = str(value).strip()
    if not text:
        return None
    try:
        return parse_iso_utc(text)
    except (ValueError, TypeError):
        return None
