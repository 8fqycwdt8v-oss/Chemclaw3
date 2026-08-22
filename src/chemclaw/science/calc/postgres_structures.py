"""Postgres backend for the geometry store (D-2026-08-21-a-geometry-is-an-address-not-a-payload).

The same two-method `StructureStore` contract as `InMemoryStructureStore`, over the `structures`
table (`infra/sql/047_structures.sql`), so a handle a chemist wrote down in March still resolves in
August and so a geometry computed by one worker is reachable from another. That cross-process reach
is the whole reason a durable backend exists at all: the conformer search runs on the `calc`
bundle's queue and the follow-up optimization is launched from the chat service, so an in-process
map would resolve nothing.

A write is `ON CONFLICT DO NOTHING`, which is not an optimisation but the contract: the key is the
content, so there is never anything to update, and a second calculation arriving at the same
geometry must not disturb the row (or its `created_at`) that a first one wrote.
"""

import json
from collections.abc import Sequence
from typing import Any

from psycopg.types.json import Jsonb

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.science.calc.models import Structure
from chemclaw.science.calc.structures import StructureStore

# `DO NOTHING`, not `DO UPDATE`: `structure_id` *is* the chemistry, so an existing row already
# holds what this write carries. The one field the identity excludes and the payload keeps —
# `origin`, the key of the calculation that produced the geometry — is deliberately left at the
# first writer's value: a geometry reached twice by different routes was produced by whichever ran
# first, and rewriting that would make the lineage depend on read order.
_INSERT = """
    INSERT INTO structures (structure_id, structure)
    VALUES (%s, %s)
    ON CONFLICT (structure_id) DO NOTHING
"""

_SELECT = "SELECT structure FROM structures WHERE structure_id = %s"


class PostgresStructureStore:
    """Durable `StructureStore` backed by Postgres.

    Short-lived connections through `chemclaw.core.db.connection`, for the reason `PostgresStore`
    gives: geometry traffic is coarse-grained against the calculations that produce it, and the
    process-wide pool underneath removes the handshake where it matters.
    """

    def __init__(self, dsn: str | None = None) -> None:
        """Use the given DSN, or the configured one by default."""
        self._dsn = dsn if dsn is not None else settings.postgres_dsn

    async def put(self, structures: Sequence[Structure]) -> None:
        """Persist every geometry under its own content address, in one round trip.

        `executemany` rather than a loop of `execute`, and one connection rather than one each: a
        conformer search returns up to forty-seven geometries and this runs on the cache-hit path
        as well as the miss, so a per-structure checkout would put forty-seven commits in front of
        an answer that cost nothing to produce.

        An empty sequence is a no-op rather than an empty statement, because most calculations
        return no geometry at all and the common case should not touch the database.
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

    The one place that names the production backend, mirroring `postgres_store.default_store` and
    `postgres_artifacts.default_artifact_store` so a tool module does not have to know which one it
    is; tests swap it at the importing module
    (`monkeypatch.setattr(<module>, "default_structure_store", ...)`).

    **No enable switch, deliberately**, where the artifact store has one. That switch exists
    because an artifact is megabytes and a deployment may reasonably decline to keep them; a
    geometry is kilobytes, and declining to keep it does not save storage — the same coordinates
    are already inside the `calculation_results` payload that D-011 refuses to prune. What it would
    do is make every `structure_id` this system reports unresolvable, which is the defect this
    store was added to end.
    """
    return PostgresStructureStore()
