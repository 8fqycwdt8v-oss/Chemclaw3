"""Changing how a campaign id is derived must move the rows, not orphan them.

`D-2026-08-21-a-geometry-is-an-address-not-a-payload` folds case and whitespace into
`campaign_id_for`, because the caller is a model re-emitting a decision space it just read back and
`THF` against `thf` was two campaigns with two empty histories. That fix changes every id of a space
carrying a capital letter — so without a re-key it would *cause* the failure it prevents, telling
every chemist with a running campaign that their campaign is new.

The re-key is possible at all because migration 031 stores the whole `OptimizationProblem` on the
row ("so the row reconstructs the space that was searched"), which is what a Python-side derivation
needs and what SQL alone could never do.
"""

from __future__ import annotations

import asyncio

import psycopg
import pytest

from chemclaw.cli.rekey_campaigns import rekey
from chemclaw.core.config import settings
from chemclaw.science.bo.campaign_record import campaign_id_for
from chemclaw.science.bo.problem import (
    CategoricalParameter,
    ContinuousParameter,
    Objective,
    OptimizationProblem,
)
from tests.pg import migrated_db_or_skip

_PROBLEM = OptimizationProblem(
    parameters=[
        ContinuousParameter(name="temperature", lower=20.0, upper=120.0),
        CategoricalParameter(name="solvent", categories=["THF", "toluene"]),
    ],
    objectives=[Objective(name="yield", direction="maximize")],
)


async def _seed(conn: psycopg.AsyncConnection[object], campaign_id: str) -> None:
    """One campaign under `campaign_id` with one suggestion against it."""
    await conn.execute(
        "INSERT INTO bo_campaigns (campaign_id, objective, direction, problem) "
        "VALUES (%s, %s, %s, %s)",
        (
            campaign_id,
            "yield",
            "maximize",
            psycopg.types.json.Jsonb(_PROBLEM.model_dump(mode="json")),
        ),
    )
    await conn.execute(
        "INSERT INTO bo_suggestions (campaign_id, candidates, observations) VALUES (%s, %s, %s)",
        (campaign_id, psycopg.types.json.Jsonb([]), psycopg.types.json.Jsonb([])),
    )
    await conn.commit()


def test_a_campaign_recorded_under_the_old_id_moves_with_its_history() -> None:
    """The whole point: the suggestions follow the campaign, and the old row goes."""

    async def _drive() -> None:
        await migrated_db_or_skip()
        stale = "campaign-stale-under-the-old-derivation"
        current = campaign_id_for(_PROBLEM)
        async with await psycopg.AsyncConnection.connect(settings.postgres_dsn) as conn:
            await conn.execute("DELETE FROM bo_campaigns")
            await conn.commit()
            await _seed(conn, stale)

            examined, moved = await rekey(dry_run=True)
            assert (examined, moved) == (1, 1)
            # A dry run changes nothing, which is the only thing that makes it worth having.
            row = await (
                await conn.execute(
                    "SELECT count(*) FROM bo_campaigns WHERE campaign_id = %s", (stale,)
                )
            ).fetchone()
            assert row is not None and row[0] == 1

            examined, moved = await rekey(dry_run=False)
            assert (examined, moved) == (1, 1)

            moved_rows = await (
                await conn.execute(
                    "SELECT count(*) FROM bo_suggestions WHERE campaign_id = %s", (current,)
                )
            ).fetchone()
            assert moved_rows is not None and moved_rows[0] == 1
            gone = await (
                await conn.execute(
                    "SELECT count(*) FROM bo_campaigns WHERE campaign_id = %s", (stale,)
                )
            ).fetchone()
            assert gone is not None and gone[0] == 0

            # Idempotent: a second run is a read, which is what makes it safe to leave in a deploy.
            assert await rekey(dry_run=False) == (1, 0)

            await conn.execute("DELETE FROM bo_campaigns")
            await conn.commit()

    asyncio.run(_drive())


def test_two_rows_that_differed_only_in_casing_merge_into_one_history() -> None:
    """A collision is the point, not a hazard: they *are* one campaign, and so is their history."""

    async def _drive() -> None:
        await migrated_db_or_skip()
        current = campaign_id_for(_PROBLEM)
        async with await psycopg.AsyncConnection.connect(settings.postgres_dsn) as conn:
            await conn.execute("DELETE FROM bo_campaigns")
            await conn.commit()
            await _seed(conn, "campaign-forked-a")
            await _seed(conn, "campaign-forked-b")

            await rekey(dry_run=False)

            rows = await (await conn.execute("SELECT count(*) FROM bo_campaigns")).fetchone()
            assert rows is not None and rows[0] == 1
            merged = await (
                await conn.execute(
                    "SELECT count(*) FROM bo_suggestions WHERE campaign_id = %s", (current,)
                )
            ).fetchone()
            assert merged is not None and merged[0] == 2

            await conn.execute("DELETE FROM bo_campaigns")
            await conn.commit()

    asyncio.run(_drive())


def test_a_row_with_no_stored_problem_is_left_alone(caplog: pytest.LogCaptureFixture) -> None:
    """Predates migration 037's snapshot, so its id cannot be re-derived from anything.

    Guessing would attach real suggestions to an id nobody can reproduce, so it is named and left.
    """

    async def _drive() -> None:
        await migrated_db_or_skip()
        async with await psycopg.AsyncConnection.connect(settings.postgres_dsn) as conn:
            await conn.execute("DELETE FROM bo_campaigns")
            await conn.execute(
                "INSERT INTO bo_campaigns (campaign_id, objective, direction) VALUES (%s,%s,%s)",
                ("campaign-no-problem", "yield", "maximize"),
            )
            await conn.commit()
            with caplog.at_level("WARNING"):
                assert await rekey(dry_run=False) == (1, 0)
            assert "cannot be re-derived" in caplog.text
            await conn.execute("DELETE FROM bo_campaigns")
            await conn.commit()

    asyncio.run(_drive())
