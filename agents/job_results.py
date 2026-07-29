"""Wait, within one turn, for durable jobs that turn started (gap AGT-2).

The system's defining interaction — "compute this, then reason about the result" — was split in
two. A tool returned a job id, the turn ended, and the result arrived as a push-back event the
session only picked up on its *next* turn. For a research assistant built on durable execution,
that is the interaction, and it took two exchanges.

Both halves of the machinery already existed: the durable job→session mailbox (F3-T3,
`agents.session_events`) and the harness todo flip (D-058, `agents.harness_todo`). What was missing
was a bounded wait that a *live* turn can perform. This is that wait, and nothing more.

**Why bounded, and why opt-in.** Holding a turn open holds an admission permit and a session's
turn slot. A wait longer than the front door's whole-turn deadline would simply be cut off there,
so the bound here must be the shorter one and the caller stays responsible for the outer deadline.
A job that does not finish in time is not an error — the existing push-back path still delivers it
on the next turn, which is exactly the pre-existing behavior. The feature therefore degrades to
"what it did before" rather than to a failure.
"""

import asyncio
import logging
from typing import Any

from agents.session_events import stream_new_events

logger = logging.getLogger(__name__)


async def await_job_results(
    session_id: str,
    job_ids: list[str],
    *,
    timeout_seconds: float,
) -> dict[str, dict[str, Any]]:
    """Wait up to `timeout_seconds` for `job_ids` to complete; return the results that arrived.

    Returns a partial map on timeout rather than raising: some results are strictly better than
    none, and the ones that did not arrive are still delivered by the ordinary push-back path on
    the session's next turn (the behavior before this existed).

    Events for jobs this turn did not start are ignored *and left unclaimed* is not possible — the
    mailbox claim is destructive by design (at-most-once) — so any such event is returned to the
    caller's map only if it matches; a non-matching one would already have been claimed by the
    front door's own `/sessions/{id}/events` stream, which is the consumer that owns them.
    """
    wanted = set(job_ids)
    collected: dict[str, dict[str, Any]] = {}

    async def _collect() -> None:
        async for pushed in stream_new_events(session_id, kinds=("job_completed",)):
            job_id = str(pushed.payload.get("job_id", ""))
            if job_id in wanted:
                collected[job_id] = dict(pushed.payload)
                wanted.discard(job_id)
            if not wanted:
                return

    try:
        await asyncio.wait_for(_collect(), timeout=timeout_seconds)
    except TimeoutError:
        logger.info(
            "mid-turn resume timed out for session %s; %d/%d job(s) arrived, the rest "
            "will surface on the next turn",
            session_id,
            len(collected),
            len(job_ids),
        )
    except Exception:
        # The mailbox is an optimization here, never the system of record: a database blip must
        # degrade the turn to its pre-AGT-2 behavior, not fail an answer the model already has.
        logger.warning("mid-turn resume could not tail session %s", session_id, exc_info=True)
    return collected
