"""Postgres backing for the commitment mirror (`infra/sql/074_commitments.sql`).

The write is an upsert on `(source, external_id)` so re-reading a snapshot converges rather than
accumulating versions. Every reading reports `observed_at`, because a mirror's characteristic
failure is being stale, and the staleness belongs on the answer.
"""

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import TupleRow
from pydantic import BaseModel, Field

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.ingest.commitments.models import LIVE_STATES, Commitment

#: The most rows one `outstanding` call will serve, whatever a caller asks for. A constant, not a
#: setting: it bounds what a portfolio read may put into a prompt. Reported as `limit_applied`
#: so a capped page is distinguishable from a small programme.
_MAX_PAGE = 200

_COLUMNS = (
    "source",
    "external_id",
    "kind",
    "title",
    "owner",
    "state",
    "due_at",
    "parent_id",
    "note_ids",
    "job_ids",
    "compounds",
)

_UPDATED = ",\n        ".join(f"{name} = EXCLUDED.{name}" for name in _COLUMNS[2:])

_UPSERT = f"""
    INSERT INTO commitments ({", ".join(_COLUMNS)})
    VALUES ({", ".join(["%s"] * len(_COLUMNS))})
    ON CONFLICT (source, external_id) DO UPDATE SET
        {_UPDATED},
        observed_at = now()
"""

_SELECT = (
    "SELECT source, external_id, kind, title, owner, state, due_at, parent_id, "
    "note_ids, job_ids, compounds, observed_at FROM commitments"
)


def _connect() -> AbstractAsyncContextManager[psycopg.AsyncConnection[TupleRow]]:
    """The configured connection, with the shared statement timeout (one place, DRY)."""
    return db.connection(settings.session_store_dsn or settings.postgres_dsn)


def _row(values: tuple[Any, ...]) -> tuple[Commitment, datetime]:
    """One database row as its model plus when the source last said it."""
    return (
        Commitment(
            source=str(values[0]),
            external_id=str(values[1]),
            kind=str(values[2]),  # type: ignore[arg-type]
            title=str(values[3]),
            owner=str(values[4]),
            state=str(values[5]),  # type: ignore[arg-type]
            due_at=values[6],
            parent_id=str(values[7]),
            note_ids=list(values[8] or []),
            job_ids=list(values[9] or []),
            compounds=list(values[10] or []),
        ),
        values[11],
    )


async def record_commitments(commitments: list[Commitment]) -> int:
    """Upsert a batch, returning how many rows were written.

    One transaction, so the mirror never holds half of an internally consistent snapshot.
    """
    if not commitments:
        return 0
    async with _connect() as conn:
        async with conn.cursor() as cur:
            for commitment in commitments:
                await cur.execute(
                    _UPSERT,
                    (
                        commitment.source,
                        commitment.external_id,
                        commitment.kind,
                        commitment.title,
                        commitment.owner,
                        commitment.state,
                        commitment.due_at,
                        commitment.parent_id,
                        commitment.note_ids,
                        commitment.job_ids,
                        commitment.compounds,
                    ),
                )
        await conn.commit()
    return len(commitments)


class Outstanding(BaseModel):
    """One page of the live book, when the mirror was last refreshed, **and how big the book is**.

    The `(rows, freshness)` tuple this replaced carried one of the mirror's two silences and not
    the other. Staleness was answered — that is what `observed_at` is for — and *size* was not:
    measured against a real database, 40 outstanding commitments answered a `limit=25` read with 25
    rows and nothing anywhere saying so, which a portfolio-risk question turns into "these are the
    programmes at risk" over the 25 soonest deadlines. The tool above it reasoned carefully that an
    empty list has two meanings and distinguished them, and was blind to this one beside it.

    `total_outstanding` is counted over the same predicate in the same transaction as the page, so
    "25 of 40" is one statement about one snapshot rather than two reads of a table a sync rewrites
    wholesale.
    """

    commitments: list[Commitment] = Field(default_factory=list)
    # `max(observed_at)` over the returned rows; `None` when the page is empty — which is why
    # `mirror_freshness` exists and why the tool asks it when this is null.
    mirrored_at: datetime | None = None
    # Everything live under the same filters, before the page bound.
    total_outstanding: int = Field(default=0, ge=0)
    # The bound actually used, which is not the bound asked for once `_MAX_PAGE` bites.
    limit_applied: int = Field(default=_MAX_PAGE, ge=1)

    @property
    def truncated(self) -> bool:
        """Whether live commitments exist that this page does not carry."""
        return self.total_outstanding > len(self.commitments)


async def outstanding(*, owner: str = "", source: str = "", limit: int = 50) -> Outstanding:
    """What is still live, soonest deadline first, with the freshness and the size of the book.

    Freshness and `total_outstanding` ride with the rows because a stale mirror or a truncated page
    is wrong in a way no individual row reveals. `due_at IS NULL` sorts last: an undated commitment
    is not the most urgent one.
    """
    clauses = ["state = ANY(%s)"]
    params: list[Any] = [list(LIVE_STATES)]
    if owner:
        clauses.append("owner = %s")
        params.append(owner)
    if source:
        clauses.append("source = %s")
        params.append(source)
    where = f"WHERE {' AND '.join(clauses)}"
    page = max(1, min(limit, _MAX_PAGE))
    async with _connect() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"{_SELECT} {where} ORDER BY due_at ASC NULLS LAST, external_id LIMIT %s",
                (*params, page),
            )
            rows = [_row(tuple(row)) for row in await cur.fetchall()]
            await cur.execute(f"SELECT count(*) FROM commitments {where}", tuple(params))
            counted = await cur.fetchone()
    return Outstanding(
        commitments=[commitment for commitment, _observed in rows],
        mirrored_at=max((observed for _c, observed in rows), default=None),
        total_outstanding=int(counted[0]) if counted else len(rows),
        limit_applied=page,
    )


async def mirror_freshness(source: str = "") -> datetime | None:
    """When this mirror was last refreshed at all, whatever state its rows are in.

    Separate from `outstanding` so "nothing is due" (recent refresh) is distinguishable from
    "nothing was ever mirrored" (no refresh).
    """
    sql = "SELECT max(observed_at) FROM commitments"
    params: tuple[Any, ...] = ()
    if source:
        sql += " WHERE source = %s"
        params = (source,)
    async with _connect() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            row = await cur.fetchone()
    return row[0] if row else None
