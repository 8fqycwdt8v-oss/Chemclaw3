"""Postgres backing for the effect ledger (`infra/sql/075_effects.sql`).

Records what changed in a system this deployment does not own, and records it *before* the
change is attempted: `begin_effect` writes first and `settle_effect` updates, so a row left in
`attempting` after a crash is the honest "may have happened" state an incident starts from.

The per-session view is read by `operations/evidence_pack.assemble`. `unsettled` (what is in
doubt across every session) is served by no route, CLI or tool; an operator runs its query by
hand (see its docstring). `get_effect` and `unsettled` remain as the write path's read-back
under test, and `tests/test_effects.py` fails if an operator surface starts calling them
without this sentence being rewritten.
"""

from contextlib import AbstractAsyncContextManager

import psycopg
from psycopg.rows import TupleRow, class_row
from pydantic import BaseModel, ConfigDict

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.db import IsoStamp


class EffectRecord(BaseModel):
    """One attempt to change something outside this deployment.

    Read back by `class_row`, so every field name here is a column name in `_COLUMNS` and the two
    are one declaration rather than two that agree by inspection.
    """

    #: `extra="forbid"` so a SELECT column with no matching field is an error rather than silently
    #: ignored.
    model_config = ConfigDict(extra="forbid")

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
    attempted_at: IsoStamp = ""
    settled_at: IsoStamp = ""


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

# `WHERE effects.state <> 'applied'` so a later `failed` settle (e.g. from a cleanup path) cannot
# overwrite a landed irreversible change — except `compensated`, the one legal transition out of
# `applied` (`reversal: compensating`). `external_ref` is coalesced so a settle without one never
# erases the handle an operator needs to undo the far side.
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

    Idempotent on `effect_id` (the job's deterministic workflow id), so a retried run re-opens its
    own row. It will not re-open an `applied` row: a landed change must not be walked back to
    `attempting` by a replay.
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

    `external_ref` is stored even on a failure: it is the far side's handle, and the only thing an
    operator can undo by hand.
    """
    async with _connect() as conn:
        await conn.execute(_SETTLE, (state, external_ref, external_ref, detail, effect_id, state))


async def get_effect(effect_id: str) -> EffectRecord | None:
    """One effect by id, whatever state it is in.

    Raises:
        pydantic.ValidationError: `_COLUMNS` and `EffectRecord` no longer describe the same row.
    """
    async with _connect() as conn:
        async with conn.cursor(row_factory=class_row(EffectRecord)) as cur:
            await cur.execute(f"SELECT {_COLUMNS} FROM effects WHERE effect_id = %s", (effect_id,))
            return await cur.fetchone()


async def unsettled(limit: int = 50) -> list[EffectRecord]:
    """Effects that were begun and never settled — where an incident investigation starts.

    Each row means this system may have changed something outside itself and cannot prove either
    way. Backed by the partial index `effects_unsettled_idx`. No route, CLI or tool calls this; an
    operator runs the same query by hand::

        SELECT effect_id, connector, job, system, reversal, requested_by, session_id,
               correlation_id, approved_by, state, external_ref, detail, attempted_at, settled_at
        FROM effects WHERE state = 'attempting' ORDER BY attempted_at LIMIT 50;

    Args:
        limit: Rows to return, clamped to 1..200.
    """
    async with _connect() as conn:
        async with conn.cursor(row_factory=class_row(EffectRecord)) as cur:
            await cur.execute(
                f"SELECT {_COLUMNS} FROM effects WHERE state = 'attempting' "
                "ORDER BY attempted_at LIMIT %s",
                (max(1, min(limit, 200)),),
            )
            return await cur.fetchall()
