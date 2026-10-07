"""Re-key recorded BO campaigns after a change to how a campaign id is derived.

A campaign id is a Python hash of its decision space (`science/bo/campaign_record.campaign_id_for`),
which SQL cannot recompute; `bo_campaigns.problem` stores the whole `OptimizationProblem`, so the
new id is computable from the row. Without a re-key, a derivation change makes every recorded
campaign unreachable.

Safe to re-run and to interrupt: a campaign already under its current id is left alone, and each
re-key is one transaction (insert under the new id, move suggestions, delete the old row). A
collision merges rather than overwrites — the survivor keeps the earlier `created_at` and the later
`last_asked_at`.

Run: `python -m chemclaw.cli.rekey_campaigns [--apply]` — a preview unless `--apply` is given
"""

import argparse
import asyncio
import logging
import sys

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.logging import configure_logging
from chemclaw.science.bo.campaign_record import campaign_id_for
from chemclaw.science.bo.problem import OptimizationProblem

logger = logging.getLogger(__name__)

_SELECT = "SELECT campaign_id, problem FROM bo_campaigns ORDER BY created_at"

# Insert-or-merge under the new id. `DO UPDATE` because a merge widens the row's window both ways:
# earliest framing and latest activity may come from different source rows.
_UPSERT = """
    INSERT INTO bo_campaigns (campaign_id, objective, direction, problem, opened_by,
                              created_at, last_asked_at)
    SELECT %s, objective, direction, problem, opened_by, created_at, last_asked_at
      FROM bo_campaigns WHERE campaign_id = %s
    ON CONFLICT (campaign_id) DO UPDATE SET
        created_at = LEAST(bo_campaigns.created_at, EXCLUDED.created_at),
        last_asked_at = GREATEST(bo_campaigns.last_asked_at, EXCLUDED.last_asked_at)
"""

_MOVE_SUGGESTIONS = "UPDATE bo_suggestions SET campaign_id = %s WHERE campaign_id = %s"
_DELETE_OLD = "DELETE FROM bo_campaigns WHERE campaign_id = %s"


async def rekey(*, dry_run: bool) -> tuple[int, int]:
    """Re-key every campaign whose stored problem no longer hashes to its recorded id.

    Args:
        dry_run: Report what would move and change nothing.

    Returns:
        `(examined, moved)`.
    """
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(_SELECT)
            rows = await cur.fetchall()

        moved = 0
        for recorded_id, payload in rows:
            # A `problem` of `{}` predates migration 037's snapshot and cannot be re-derived; leave
            # it and name it rather than guess an id.
            if not payload:
                logger.warning(
                    "campaign %s stores no problem, so its id cannot be re-derived; left as is",
                    recorded_id,
                )
                continue
            current_id = campaign_id_for(OptimizationProblem.model_validate(payload))
            if current_id == recorded_id:
                continue
            moved += 1
            logger.info("campaign %s -> %s", recorded_id, current_id)
            if dry_run:
                continue
            async with conn.cursor() as cur:
                await cur.execute(_UPSERT, (current_id, recorded_id))
                await cur.execute(_MOVE_SUGGESTIONS, (current_id, recorded_id))
                await cur.execute(_DELETE_OLD, (recorded_id,))
            await conn.commit()
    return len(rows), moved


def main(argv: list[str] | None = None) -> int:
    """Entry point: report what would move, and write only when `--apply` says so.

    Preview by default, like `erase_actor`: the merge is lossy at the row level, so a wrong
    `campaign_id_for` derivation would collapse distinct campaigns irreversibly. Idempotence is not
    the same as being reviewable before it runs.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit the re-key. Without it the rows examined are real and nothing is written.",
    )
    args = parser.parse_args(argv)
    configure_logging()
    examined, moved = asyncio.run(rekey(dry_run=not args.apply))
    verb = "moved" if args.apply else "would move"
    logger.info("%d campaign(s) examined, %s %d", examined, verb, moved)
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    sys.exit(main())
