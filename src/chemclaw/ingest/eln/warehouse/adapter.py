"""The ingest half: an `ElnAdapter` whose knowledge of the source is a binding, not code.

`fetch_new_entries` runs the binding's queries and bundles each reaction with its child rows;
`map_to_ord` walks the binding to build an `OrdReaction`. Nothing below this line names a table or a
column, which is the property the whole package exists for: attaching a warehouse nobody has seen
yet is writing YAML, and a column landing in it next quarter is a line of YAML.

Everything downstream is untouched and inherited. `chemclaw.ingest.eln.sync` supplies the cursor,
the overlap window, the future-timestamp guard, dedup against merged note bodies and reject-and-
continue; `ingest_reaction` writes the fingerprints and proposes the note through the PR-gate;
`ElnSyncWorkflow` drains this source in chunks under its own `sync_cursors` watermark. This adapter
is one of two methods and a mapping, exactly like the two file-drop adapters beside it.
"""

import logging
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from chemclaw.core.config import settings
from chemclaw.ingest.eln.adapter import ElnMappingError, RawEntry, parse_iso_utc
from chemclaw.ingest.eln.ord import Component, Impurity, OrdReaction, Role
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

logger = logging.getLogger(__name__)

# The payload key the entry's own row lands under, so a binding can say `root.COL` and a child block
# can never shadow it (`RelatedBinding` rejects the name).
ROOT = "root"


class WarehouseElnAdapter:
    """An `ElnAdapter` over a SQL warehouse, configured entirely by its binding.

    Built by the data-source registry from a manifest's `config:` block, so its constructor
    signature *is* the manifest's schema. `name` is accepted and unused here — it exists so this
    class and `WarehouseVectorRetriever` take identical keyword arguments, which they must: the
    registry splats one `config` into whichever half it builds, and `make datasource-validate` binds
    that same config against every declared half.
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

    async def fetch_new_entries(self, since: datetime) -> list[RawEntry]:
        """Every reaction created or amended at or after `since`, oldest first.

        Inclusive on `since` because the sync's cursor is the newest timestamp already seen and
        ingestion is idempotent, so replaying the boundary row is safe and skipping it is not.
        Amendments count as new — `sql.watermark_expression` is what makes that true, and it is the
        reason a binding should declare `modified_at` whenever the source has one.
        """
        warehouse = await self._connection()
        entry = self._ingest.entry
        statement, params = sql.entry_statement(
            entry, warehouse.placeholder, since, entry.fetch_limit
        )
        async with warehouse.cursor() as cursor:
            await cursor.execute(statement, params)
            rows = await cursor.fetchall()
        if not rows:
            return []

        keyed = [row for row in rows if row.get(entry.key)]
        if len(keyed) != len(rows):
            logger.warning(
                "%s: %d of %d rows carried no %s and were skipped",
                self._name,
                len(rows) - len(keyed),
                len(rows),
                entry.key,
            )
        bundles = {str(row[entry.key]): {ROOT: row} for row in keyed}
        if len(bundles) != len(keyed):
            # Counted separately from the missing-key case above, because the two are different
            # faults with the same symptom and one message for both would misdiagnose either. A
            # repeated key means the declared `key` is not unique in the declared relation — a
            # binding pointing at a joined view rather than a reaction view — and the reactions it
            # collapses would otherwise vanish with no explanation at all.
            logger.warning(
                "%s: %d row(s) shared a %s with another row and only one survived; the declared "
                "key is not unique in %s",
                self._name,
                len(keyed) - len(bundles),
                entry.key,
                entry.relation,
            )
        await self._attach_related(warehouse, bundles)
        return [self._raw_entry(key, bundle) for key, bundle in bundles.items()]

    async def _attach_related(
        self, warehouse: Warehouse, bundles: dict[str, dict[str, Any]]
    ) -> None:
        """Fetch every declared child table for the whole batch and file its rows by entry key.

        One query per block rather than per row: a hundred reactions across four child tables is
        four round trips, not four hundred. Rows whose foreign key matches no entry in the batch are
        dropped silently — with `order_by` set the warehouse may return them in any order, and the
        `IN (...)` list is what scopes them.
        """
        keys = list(bundles)
        for block in self._ingest.related:
            for bundle in bundles.values():
                bundle.setdefault(block.name, [])
            statement, params = sql.related_statement(block, warehouse.placeholder, keys)
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
                _optional_timestamp(row.get(entry.modified_at)) if entry.modified_at else None
            ),
            payload=bundle,
        )

    def map_to_ord(self, raw: RawEntry) -> OrdReaction:
        """Build the canonical reaction this binding says the row describes.

        Every failure is an `ElnMappingError` (or a `TransformError`, which is one), so a row the
        binding cannot map is rejected with its reason and the batch continues — the behaviour
        `sync_entries` already gives every adapter that raises that type.
        """
        binding = self._ingest
        fields = {
            name: _read(field, raw.payload) for name, field in sorted(binding.reaction.items())
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

        The split is by the role the binding produced, not by which table a row came from: a site
        that keeps products in the same charge table as its reagents is the common case, and one
        that separates them is served by two `components:` blocks reading two tables.
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

        Skipped rather than rejected, for the same reason a structureless charge row is: an
        analytics table carries system peaks, solvent fronts and blank rows, and failing a good
        reaction over one of them would lose the record to a bookkeeping artefact.
        """
        found: list[Impurity] = []
        for block in self._ingest.impurities:
            for row in payload.get(block.source, []):
                scope = {ROOT: row, **row}
                name = _read(block.name, scope) if block.name else None
                smiles = _read(block.smiles, scope) if block.smiles else None
                if not name and not smiles:
                    continue
                area = _read(block.area_percent, scope) if block.area_percent else None
                try:
                    found.append(
                        Impurity(
                            name=str(name) if name else None,
                            smiles=str(smiles) if smiles else None,
                            area_percent=area,
                        )
                    )
                except ValidationError as exc:
                    raise ElnMappingError(
                        f"impurity row {name or smiles!r} is invalid: {exc}"
                    ) from exc
        return found

    def _consumed(self) -> set[str]:
        """Entry columns already carried by a mapped field, so `['*']` does not repeat them.

        Only the entry's own columns, and only paths that read it directly: an attribute bag that
        restated the yield beside the `yield:` bullet would be noise in every note body.
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

    A row with no structure is skipped rather than rejected: a charge table routinely carries lines
    for things that are not species — a vessel, a note, a blank continuation row — and failing the
    whole reaction over one of them would lose a good record to a bookkeeping artefact. A row with a
    structure but no usable role *is* an error, because that is a vocabulary the binding missed.
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

    Under `['*']` this is "everything the row had that no field already took" — which is what makes
    a column added to the warehouse next quarter visible without anyone editing this repository.
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

    `OrdReaction.provenance` is required and becomes the note's `source` — the line a reviewer reads
    to find the original record. A template whose every reference was empty would produce a citation
    pointing nowhere, so it falls back to naming the entry rather than proposing an uncitable note.
    """
    rendered = render_template(template, payload).strip(": ").strip()
    return rendered or f"warehouse:{entry_id}"


def _timestamp(value: Any, column: str, entry_id: str) -> datetime:
    """Read a required timestamp column, or reject the row naming what was missing."""
    parsed = _optional_timestamp(value)
    if parsed is None:
        raise ElnMappingError(
            f"entry {entry_id!r} has no usable {column!r}; the sync cursor is a timestamp and "
            "cannot order a row without one"
        )
    return parsed


def _optional_timestamp(value: Any) -> datetime | None:
    """Read a timestamp from a driver-native value or an ISO string; `None` when absent."""
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
