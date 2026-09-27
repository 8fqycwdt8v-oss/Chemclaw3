"""Where a designed arm's outcomes are kept: in memory, or in `experiment_arm_results`.

The same two-backend shape `protocols/store.py` has, and for its reason — an in-memory instance is
a real backend for a deployment without Postgres, not a test double. Split into its own module
rather than added to that one because the two answer different questions: that store holds the
document a chemist revises, this holds the numbers a plate produced, and 073's own header is about
keeping the prescriptive and descriptive tiers apart.

**Append-only in both backends.** Neither offers an update or a delete, and the Postgres role is
granted `INSERT` and nothing else on the table (`infra/sql/grants/app_privileges.sql`), because a
re-measured well is a second observation rather than a correction of the first. Overwriting would
delete the evidence that two assays disagree, which is the one thing the shape exists to keep.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Protocol, cast

import psycopg
from psycopg.rows import TupleRow

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.protocols.models import AuthorKind
from chemclaw.protocols.results import ArmResult, StoredArmResult

_INSERT = """
INSERT INTO experiment_arm_results
    (design_id, revision, arm_id, outcome, value, unit, reaction_id, measured_at,
     author_kind, author, note)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

# Newest first, which is what `results.latest_by_arm` expects: it takes the first of each
# (arm, outcome) key rather than sorting, so the order is part of this module's contract.
_SELECT = """
SELECT result_id, revision, arm_id, outcome, value, unit, reaction_id, measured_at,
       author_kind, author, note, created_at
  FROM experiment_arm_results
 WHERE design_id = %s AND (%s::int IS NULL OR revision = %s::int)
 ORDER BY created_at DESC, result_id DESC
"""


class ArmResultStore(Protocol):
    """Append outcomes for a design revision, and read them back newest first."""

    async def append(
        self,
        design_id: str,
        revision: int,
        results: Sequence[ArmResult],
        *,
        author_kind: AuthorKind,
        author: str = "",
    ) -> int:
        """Store `results` against that revision; returns how many rows landed."""
        ...  # pragma: no cover - protocol

    async def read(self, design_id: str, revision: int | None = None) -> list[StoredArmResult]:
        """Every stored outcome for a design, newest first; `revision` narrows to one."""
        ...  # pragma: no cover - protocol


class InMemoryArmResultStore:
    """A real backend for a deployment without Postgres, not a test double."""

    def __init__(self) -> None:
        """Hold the rows and the id counter; nothing is read at construction."""
        self._rows: list[tuple[str, StoredArmResult]] = []
        self._next_id = 1

    async def append(
        self,
        design_id: str,
        revision: int,
        results: Sequence[ArmResult],
        *,
        author_kind: AuthorKind,
        author: str = "",
    ) -> int:
        """Append every result, newest last in storage and newest first on read."""
        now = datetime.now(UTC)
        for result in results:
            self._rows.append(
                (
                    design_id,
                    StoredArmResult(
                        **result.model_dump(),
                        result_id=self._next_id,
                        revision=revision,
                        author_kind=author_kind,
                        author=author,
                        created_at=now,
                    ),
                )
            )
            self._next_id += 1
        return len(results)

    async def read(self, design_id: str, revision: int | None = None) -> list[StoredArmResult]:
        """Newest first, so `latest_by_arm` can take the first of each key."""
        rows = [
            stored
            for held, stored in self._rows
            if held == design_id and (revision is None or stored.revision == revision)
        ]
        return sorted(rows, key=lambda r: (r.created_at, r.result_id), reverse=True)


class PostgresArmResultStore:
    """The durable store — `experiment_arm_results`, insert and select only."""

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[psycopg.AsyncConnection[TupleRow]]:
        """Borrow a connection with the configured per-statement timeout."""
        async with db.connection(settings.postgres_dsn) as conn:
            yield conn

    async def append(
        self,
        design_id: str,
        revision: int,
        results: Sequence[ArmResult],
        *,
        author_kind: AuthorKind,
        author: str = "",
    ) -> int:
        """Insert every result in one transaction, so a half-attached plate cannot happen.

        `executemany` rather than a loop of `execute`: the plate is the unit a chemist attached, and
        eleven of twenty-four wells landing because the connection dropped is a state nobody can
        tell from a half-run plate.
        """
        payload = [
            (
                design_id,
                revision,
                result.arm_id,
                result.outcome,
                result.value,
                result.unit,
                result.reaction_id or None,
                result.measured_at,
                author_kind,
                author,
                result.note,
            )
            for result in results
        ]
        if not payload:
            return 0
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(_INSERT, payload)
        return len(payload)

    async def read(self, design_id: str, revision: int | None = None) -> list[StoredArmResult]:
        """Every stored outcome, newest first; `revision` narrows to one."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT, (design_id, revision, revision))
                rows = await cur.fetchall()
        return [_stored(row) for row in rows]


def _stored(row: TupleRow) -> StoredArmResult:
    """One database row as a `StoredArmResult`."""
    return StoredArmResult(
        result_id=row[0],
        revision=row[1],
        arm_id=row[2],
        outcome=row[3],
        value=row[4],
        unit=row[5] or "",
        reaction_id=row[6] or "",
        measured_at=row[7],
        author_kind=cast(AuthorKind, row[8]),
        author=row[9] or "",
        note=row[10] or "",
        created_at=row[11],
    )


_IN_MEMORY = InMemoryArmResultStore()


def default_arm_result_store() -> ArmResultStore:
    """The store this deployment uses — the same switch `default_design_store` reads.

    Module-level in-memory instance for that function's reason: a backend that forgot every
    attached result between two calls would be worse than none at all.
    """
    if settings.session_store == "postgres":
        return PostgresArmResultStore()
    return _IN_MEMORY
