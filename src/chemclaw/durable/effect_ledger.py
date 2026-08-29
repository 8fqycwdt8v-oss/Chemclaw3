"""Postgres backing for the effect ledger (`infra/sql/075_effects.sql`).

`job_records` says a run happened and what it returned. This says what changed in a system this
deployment does **not** own — and, crucially, says it *before* the change is attempted.

**A row in `attempting` after a crash is the honest state**, not a bug in the ledger. This system
may have filed the deviation and lost the acknowledgement; a ledger that recorded only successes
would answer "nothing happened" for exactly the case an operator most needs to investigate. So
`begin_effect` writes first and `settle_effect` updates, and `unsettled` is the query an incident
starts from.
"""

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import TupleRow
from pydantic import BaseModel, Field

from chemclaw.core import db
from chemclaw.core.config import settings


class EffectRecord(BaseModel):
    """One attempt to change something outside this deployment."""

    effect_id: str
    connector: str
    job: str
    system: str
    reversal: str
    requested_by: str = ""
    session_id: str = ""
    correlation_id: str = ""
    approved_by: str = ""
    state: str = "attempting"
    external_ref: str = ""
    detail: str = ""
    attempted_at: str = ""
    settled_at: str = ""


def _connect() -> AbstractAsyncContextManager[psycopg.AsyncConnection[TupleRow]]:
    """The configured connection, with the shared statement timeout (one place, DRY)."""
    return db.connection(settings.session_store_dsn or settings.postgres_dsn)


_BEGIN = """
    INSERT INTO effects
        (effect_id, connector, job, system, reversal, requested_by, session_id,
         correlation_id, approved_by)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (effect_id) DO UPDATE SET
        approved_by = EXCLUDED.approved_by,
        state = 'attempting',
        attempted_at = now(),
        settled_at = NULL
    WHERE effects.state <> 'applied'
"""

# `WHERE effects.state <> 'applied'` for the same reason `_BEGIN` carries it, and its absence here
# was the more consequential of the two. `ConnectorJobWorkflow` settles `applied` and then calls
# `_finish` **inside the same `try`**, whose `except BaseException` settles `failed` — so a
# `ValidationError` out of the note write, or the cancellation path the workflow documents,
# overwrote a landed irreversible change with `failed` and blanked its `external_ref`. An operator
# reading the one ledger of what this system changed outside itself was told the deviation was not
# filed, and would file it again. `unsettled()` could not surface it either, since that reads
# `attempting`.
#
# `external_ref` is coalesced rather than assigned: a settle that does not carry one must not erase
# the handle an earlier one recorded, which is the only string an operator can undo the far side by.
# **`compensated` is the one transition out of `applied` that must still work**, and the first
# version of this guard blocked it. `075_effects.sql` permits the state, `settle_effect`'s own
# docstring names it, and `ConnectorManifest` models `reversal: compensating` as "undone by another
# declared job" — so applied -> compensated is not an edge case, it is the *only* way that state is
# ever reached. Blocking it left the ledger saying a change was still standing after it had been
# rolled back: the same lie the guard was added to prevent, told in the other direction. The shipped
# test asserted failed -> compensated, which the guard always allowed, so it passed either way.
_SETTLE = """
    UPDATE effects
    SET state = %s,
        external_ref = CASE WHEN %s = '' THEN effects.external_ref ELSE %s END,
        detail = %s,
        settled_at = now()
    WHERE effect_id = %s
      AND (effects.state <> 'applied' OR %s = 'compensated')
"""

_COLUMNS = (
    "effect_id, connector, job, system, reversal, requested_by, session_id, correlation_id, "
    "approved_by, state, external_ref, detail, attempted_at, settled_at"
)


async def begin_effect(record: EffectRecord) -> None:
    """Record that this system is about to change something outside itself.

    Idempotent on `effect_id`, which is the job's deterministic workflow id — so a retried run
    re-opens its own row rather than forking one. **It will not re-open an `applied` row**: an
    effect that has already landed must not be walked back to `attempting` by a replay, or the
    ledger would say the far side's change is in doubt when it is not.
    """
    async with _connect() as conn:
        await conn.execute(
            _BEGIN,
            (
                record.effect_id,
                record.connector,
                record.job,
                record.system,
                record.reversal,
                record.requested_by,
                record.session_id,
                record.correlation_id,
                record.approved_by,
            ),
        )


async def settle_effect(
    effect_id: str, *, state: str, external_ref: str = "", detail: str = ""
) -> None:
    """Record how the attempt ended: `applied`, `failed` or `compensated`.

    `external_ref` is stored even on a failure. It is the far side's own handle — a ticket number,
    a record id — and it is the only thing an operator can undo by hand, so losing it because the
    call failed *after* creating the record is the worst possible time to lose it.
    """
    async with _connect() as conn:
        await conn.execute(_SETTLE, (state, external_ref, external_ref, detail, effect_id, state))


def _row(values: tuple[Any, ...]) -> EffectRecord:
    """One database row as its model."""
    stamps = [value.isoformat() if isinstance(value, datetime) else "" for value in values[12:14]]
    return EffectRecord(
        effect_id=str(values[0]),
        connector=str(values[1]),
        job=str(values[2]),
        system=str(values[3]),
        reversal=str(values[4]),
        requested_by=str(values[5]),
        session_id=str(values[6]),
        correlation_id=str(values[7]),
        approved_by=str(values[8]),
        state=str(values[9]),
        external_ref=str(values[10]),
        detail=str(values[11]),
        attempted_at=stamps[0],
        settled_at=stamps[1],
    )


async def get_effect(effect_id: str) -> EffectRecord | None:
    """One effect by id, whatever state it is in."""
    async with _connect() as conn:
        async with conn.cursor() as cur:
            await cur.execute(f"SELECT {_COLUMNS} FROM effects WHERE effect_id = %s", (effect_id,))
            row = await cur.fetchone()
    return _row(tuple(row)) if row else None


async def unsettled(limit: int = 50) -> list[EffectRecord]:
    """Effects that were begun and never settled — where an incident investigation starts.

    Each row means: this system may have changed something outside itself and cannot prove either
    way. That is a small set in a healthy deployment and the first thing to read in an unhealthy
    one, which is why it has an index of its own.
    """
    async with _connect() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {_COLUMNS} FROM effects WHERE state = 'attempting' "
                "ORDER BY attempted_at LIMIT %s",
                (max(1, min(limit, 200)),),
            )
            return [_row(tuple(row)) for row in await cur.fetchall()]


async def effects_for_session(session_id: str, limit: int = 50) -> list[EffectRecord]:
    """Every effect one conversation caused, newest first — the evidence pack's read."""
    async with _connect() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {_COLUMNS} FROM effects WHERE session_id = %s "
                "ORDER BY attempted_at DESC LIMIT %s",
                (session_id, max(1, min(limit, 200))),
            )
            return [_row(tuple(row)) for row in await cur.fetchall()]


class Unsettled(BaseModel):
    """The unsettled set, with the sentence an operator needs beside it."""

    effects: list[EffectRecord] = Field(default_factory=list)
    meaning: str = (
        "Each of these was begun and never settled: this system may have changed something in "
        "the named system and cannot prove either way. Check the far side by `external_ref` "
        "where one was recorded, and by the job's arguments where none was."
    )
