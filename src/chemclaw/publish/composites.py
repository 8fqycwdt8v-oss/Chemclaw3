"""The local record of a tool composite — the one published shape that had none.

**Why this exists.** Publishing is recoverable because every shape it carries has a durable local
record behind it, and a walk over that record. A *primitive* is a `calculation_results` row and
`backfill.backfill_cached` walks it; a *job* composite is a `job_records` row and `backfill_jobs`
walks that. A **tool** composite — `compute_thermochemistry`, `predict_logd` — is in neither by
construction: its key would name its own output, which is precisely why it is not written to the
calculation cache (D-011, `D-2026-08-16-the-physics-leaves-the-cache-stays`), and no durable job
produced it.

So for that one shape the outbox row was the only copy, and `publish/outbox.enqueue` is
best-effort by construction — a completed calculation must not be failed by a queue write, so every
enqueue failure is counted, logged and swallowed. A local database blip during the enqueue lost the
composite permanently: nothing in this deployment could produce it again without re-running the
science. Measured on the shipped code: with the outbox unwritable, `publish_tool_result` returned
0, `backfill_cached` and `backfill_jobs` found nothing, and there was no third walk to run.

**Written whether or not a sink is enabled, and that is a deliberate cost.** Everywhere else this
subsystem costs a deployment with `CHEMCLAW_RESULT_SINKS` empty exactly nothing, and that property
is kept for every other payload — the cache and job hooks are untouched. It is traded here for the
one shape that has no other record, because the alternative makes the recovery source conditional
on the subsystem it recovers *for*: `cli/backfill_publications` exists on the premise that the
corpus computed before a results store was attached is the more valuable half, and without this
that premise is false for tool composites. What it costs is one INSERT per composite tool call —
against `compute_thermochemistry`, which relaxes a geometry and takes a Hessian.

**Never raises.** By the time this runs the tool has computed its answer and the model is waiting
for it, so the polarity is `publish_stored_result`'s and the outbox's: a record that could not be
written is logged and counted, and the tool returns exactly what it would have returned.
"""

import logging
from typing import Any

from psycopg.types.json import Jsonb

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.metrics_bridge import record_metric

logger = logging.getLogger(__name__)

# Keyed by the composite's own `calc_ref`, which `hooks._composite_ref` content-addresses on the
# *result*. So the same question asked twice is one row and the same question after the science
# moved is two — the identity the outbox already uses, which is what lets a re-run of this walk
# re-queue exactly what the hook would have.
#
# `DO NOTHING` rather than an update: the key is a hash of the payload, so a conflicting row holds
# the same payload by construction and there is nothing to overwrite.
_RECORD = """
    INSERT INTO result_composites (calc_ref, calc_type, payload_kind, input_hash, payload)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT (calc_ref) DO NOTHING
"""

# Oldest first with `calc_ref` breaking the tie, for the reason `publish/backfill._CACHED` writes
# down: `created_at` is not unique, and Postgres does not promise a stable relative order for tied
# rows across the separate statements that fetch consecutive pages — so a tied row could land on a
# page boundary and be fetched by neither, silently never queued.
WALK = """
    SELECT calc_ref, calc_type, payload_kind, input_hash, payload, created_at
    FROM result_composites
    ORDER BY created_at, calc_ref
    LIMIT %s OFFSET %s
"""


async def record_composite(
    *, calc_ref: str, calc_type: str, payload_kind: str, input_hash: str, payload: dict[str, Any]
) -> bool:
    """Store one tool composite locally. Returns whether a row was written. Never raises.

    False is the ordinary answer for a composite this deployment already holds — the identity is a
    hash of the payload — and is not a failure.
    """
    try:
        async with db.connection(settings.postgres_dsn, operation="composite_record") as conn:
            cursor = await conn.execute(
                _RECORD, (calc_ref, calc_type, payload_kind, input_hash, Jsonb(payload))
            )
            await conn.commit()
            return cursor.rowcount > 0
    except Exception:
        logger.warning(
            "publish[composite]: could not record %s (%s); it will not be republishable",
            calc_ref,
            payload_kind,
            exc_info=True,
        )
        record_metric(lambda m: m.increment("chemclaw_result_publish_failures_total"))
        return False


__all__ = ["WALK", "record_composite"]
