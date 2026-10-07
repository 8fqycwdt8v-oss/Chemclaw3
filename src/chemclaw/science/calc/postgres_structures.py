"""Postgres backend for the geometry store (D-2026-08-21-a-geometry-is-an-address-not-a-payload).

The `StructureStore` contract over the `structures` table (`infra/sql/047_structures.sql`), so a
handle resolves across processes and over time: searches run on the `calc` queue while follow-ups
launch from the chat service. Writes are `ON CONFLICT DO NOTHING`: the key is the content, and a
second arrival must not disturb the first row.
"""

import json
from collections.abc import Sequence
from typing import Any

from psycopg.types.json import Jsonb

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.science.calc.models import Structure
from chemclaw.science.calc.structures import StructureStore

# `DO NOTHING`: `structure_id` is the chemistry. `origin` (excluded from the identity) keeps the
# first writer's value, so lineage does not depend on read order.
_INSERT = """
    INSERT INTO structures (structure_id, structure)
    VALUES (%s, %s)
    ON CONFLICT (structure_id) DO NOTHING
"""

_SELECT = "SELECT structure FROM structures WHERE structure_id = %s"


class PostgresStructureStore:
    """Durable `StructureStore` backed by Postgres.

    Short-lived connections through `chemclaw.core.db.connection`'s pool.
    """

    def __init__(self, dsn: str | None = None) -> None:
        """Use the given DSN, or the configured one by default."""
        self._dsn = dsn if dsn is not None else settings.postgres_dsn

    async def put(self, structures: Sequence[Structure]) -> None:
        """Persist every geometry under its own content address, in one round trip.

        One connection and `executemany`, since this also runs on the cache-hit path. An empty
        sequence touches nothing.
        """
        if not structures:
            return
        rows = [
            (structure.structure_id, Jsonb(structure.model_dump(mode="json")))
            for structure in structures
        ]
        async with db.connection(self._dsn) as conn:
            async with conn.cursor() as cur:
                await cur.executemany(_INSERT, rows)
            await conn.commit()

    async def get(self, structure_id: str) -> Structure | None:
        """Return the geometry stored under `structure_id`, or None."""
        async with db.connection(self._dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT, (structure_id,))
                row = await cur.fetchone()
        if row is None:
            return None
        payload: Any = row[0]
        # JSONB comes back already parsed by psycopg; str only if the driver differs.
        if not isinstance(payload, dict):
            payload = json.loads(payload)
        return Structure.model_validate(payload)


def default_structure_store() -> StructureStore:
    """Return the production geometry store.

    The one place that names the production backend; tests monkeypatch it at the importing module.
    No enable switch: a geometry is small and already inside the result payload, and disabling it
    would make every reported `structure_id` unresolvable.
    """
    return PostgresStructureStore()
