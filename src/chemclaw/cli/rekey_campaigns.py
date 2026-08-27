"""Re-key recorded BO campaigns after a change to how a campaign id is derived.

**Why a migration cannot do this.** A campaign id is a hash of its decision space
(`science/bo/campaign_record.campaign_id_for`), so changing that derivation changes every id — and
the derivation lives in Python, over pydantic models, with rules SQL cannot express. What makes the
re-key possible at all is that `bo_campaigns.problem` holds the whole `OptimizationProblem`
(migration 031, "so the row reconstructs the space that was searched"): the new id is computable
from what is already stored.

**Why it has to happen.** `D-2026-08-21-a-geometry-is-an-address-not-a-payload` folds case and
whitespace in the identity, because the caller is a model re-emitting a space it just read back and
`THF` versus `thf` was two campaigns with two empty histories. Without this, that fix would *cause*
the failure it prevents — every campaign recorded before it becomes unreachable, and a chemist
resuming one is told their campaign is new.

**Safe to re-run and safe to interrupt.** A campaign whose stored problem already hashes to its
current id is left alone, so a second run is a read. A re-key is one transaction per campaign —
insert under the new id, move the suggestions, delete the old row — so an interruption leaves each
campaign either wholly moved or wholly untouched, never split.

**A collision merges rather than overwrites**, which is the whole point of the change: two rows that
differed only in casing *are* one campaign, and their suggestions belong in one history. The
surviving row keeps the earlier `created_at` and the later `last_asked_at`, so "when was this
framed" and "is it under active work" both stay true.

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

# Insert-or-merge under the new id. `DO UPDATE` rather than `DO NOTHING` because a merge has to
# widen the row's window in both directions: the earliest framing and the latest activity are
# properties of the *campaign*, and the two source rows each hold one of them.
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
            # A row whose `problem` is `{}` predates migration 037's snapshot and cannot be
            # re-derived from anything. Left where it is and named, because the alternative —
            # guessing — would attach real suggestions to an id nobody can reproduce.
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

    **Preview by default**, which is the opposite of what this used to do. `--dry-run` was opt-in,
    so a bare invocation issued `_UPSERT`, `_MOVE_SUGGESTIONS` and `DELETE FROM bo_campaigns` and
    committed per campaign, with no confirmation and no default preview. The two other
    data-touching commands in this package take this default and say why — `erase_actor` ("Dry run
    by default because this is the one irreversible operation an operator performs on live data")
    and `backfill_corpus` ("Run this first") — and `make user-erase` goes out of its way to refuse
    an `APPLY` that is not exactly `1`. An operator reaching for this one to see what it would do,
    which is the habit the other two teach, committed a merge instead.

    That the operation is idempotent and interrupt-safe — which `rekey`'s docstring argues, and
    which is true — is a different property from being *reviewable before it runs*. The merge is
    deliberately lossy at the row level (two rows become one), so a wrong `campaign_id_for`
    derivation collapses distinct campaigns irreversibly.
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
    logger.info("%d campaign(s) examined, %d %s", examined, verb, moved)
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    sys.exit(main())
