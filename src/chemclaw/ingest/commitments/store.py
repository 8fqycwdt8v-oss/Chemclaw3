"""Postgres backing for the commitment mirror (`infra/sql/074_commitments.sql`).

Two readings and one write. The write is an upsert on `(source, external_id)`, because a portfolio
export is a snapshot rather than a stream: re-reading it must converge on the source's current state
rather than accumulate versions of it.

**Every reading reports `observed_at`, and that is not decoration.** A mirror's characteristic
failure is being *stale* rather than being wrong — the export stops running, the numbers keep
answering, and a manager acts on a picture of last month. The staleness is therefore a field on the
answer rather than something a reader has to think to ask for, the same argument
`chemclaw.operations.activity.Coverage` makes about a window.
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

#: The most rows one `outstanding` call will serve, whatever a caller asks for. A module constant
#: rather than a `Settings` field for the reason `ingest/rejections._MAX_ROWS_PER_SOURCE` is one:
#: it bounds what a portfolio read may put into a prompt, which is not a deployment decision.
#: Reported as `limit_applied` rather than applied silently — a caller asking for 1,000 used to get
#: 200 and had no way to tell that from a programme with 200 commitments (measured: 205 rows
#: mirrored, `outstanding(limit=1000)` returned 200).
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

    One transaction for the batch: a portfolio snapshot is internally consistent, and applying half
    of one would produce a mirror in a state the source was never in.
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

    Returns the freshness alongside the rows rather than expecting a caller to ask: an answer built
    on a mirror that stopped updating in March is wrong in a way no individual row reveals. It
    returns `total_outstanding` for the same reason one field over — an answer built on a page of a
    programme is wrong in a way no individual row reveals either.

    `due_at IS NULL` sorts last, because a commitment with no date is not the most urgent one —
    which is what a plain `ORDER BY due_at` would make it under Postgres' NULLS FIRST for DESC and
    is an easy thing to get backwards.
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

    Separate from `outstanding` because the two answer different questions: an empty outstanding
    list plus a recent refresh means nothing is due, and the same empty list with no refresh at all
    means nobody has ever mirrored anything. Conflating them is how a manager reads "nothing is
    late" out of a sync that never ran.
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
