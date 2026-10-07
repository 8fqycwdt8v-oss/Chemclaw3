"""Postgres backing for the BO campaign record (`infra/sql/031_bo_campaigns.sql`).

The upsert refreshes the problem and `last_asked_at`, never the opener; suggestions are append-only
history.
"""

from contextlib import AbstractAsyncContextManager

import psycopg
from psycopg.rows import TupleRow

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.jsonb import json_column
from chemclaw.science.bo.campaign_record import Campaign, Suggestion

# `xmax = 0` tells whether the upsert inserted (zero) or updated. Asking the upsert, which
# serializes on the primary key, avoids the race a prior `SELECT` would have.
_UPSERT_CAMPAIGN = """
    INSERT INTO bo_campaigns (campaign_id, objective, direction, problem, opened_by)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT (campaign_id) DO UPDATE SET
        problem = EXCLUDED.problem,
        last_asked_at = now()
    RETURNING (xmax = 0) AS inserted
"""

_INSERT_SUGGESTION = """
    INSERT INTO bo_suggestions
        (campaign_id, candidates, observations, calc_refs, problem, job_id,
         actor, session_id, correlation_id)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (campaign_id, job_id) WHERE job_id <> '' DO NOTHING
    RETURNING id
"""

# What a retried durable run already wrote; read only when the insert hit the partial unique
# index, so a retry returns the original suggestion id.
_SELECT_SUGGESTION_BY_JOB = "SELECT id FROM bo_suggestions WHERE campaign_id = %s AND job_id = %s"

_SELECT_CAMPAIGN = (
    "SELECT campaign_id, objective, direction, problem, opened_by, created_at, last_asked_at "
    "FROM bo_campaigns WHERE campaign_id = %s"
)

_SELECT_SUGGESTIONS = """
    SELECT id, campaign_id, candidates, observations, calc_refs, problem, job_id, actor,
           session_id, correlation_id, proposed_at
    FROM bo_suggestions
    WHERE campaign_id = %s
    ORDER BY id DESC
    LIMIT %s
"""


def _connect() -> AbstractAsyncContextManager[psycopg.AsyncConnection[TupleRow]]:
    """The configured connection, with the shared statement timeout (one place, DRY)."""
    return db.connection(settings.postgres_dsn)


class PostgresCampaignStore:
    """The durable `CampaignStore`: one short-lived connection per call (the house choice)."""

    async def record(self, campaign: Campaign, suggestion: Suggestion) -> tuple[int, bool]:
        """Upsert the campaign and append its suggestion **atomically**, in one transaction.

        Returns the suggestion id and whether this call created the campaign (see
        `_UPSERT_CAMPAIGN`).
        """
        async with _connect() as conn, conn.transaction():
            cursor = await conn.execute(
                _UPSERT_CAMPAIGN,
                (
                    campaign.campaign_id,
                    campaign.objective,
                    campaign.direction,
                    json_column(campaign.problem),
                    campaign.opened_by,
                ),
            )
            created_row = await cursor.fetchone()
            created = bool(created_row[0]) if created_row else False
            cursor = await conn.execute(
                _INSERT_SUGGESTION,
                (
                    suggestion.campaign_id,
                    json_column(
                        [candidate.model_dump(mode="json") for candidate in suggestion.candidates]
                    ),
                    json_column(
                        [
                            observation.model_dump(mode="json")
                            for observation in suggestion.observations
                        ]
                    ),
                    suggestion.calc_refs,
                    json_column(suggestion.problem),
                    suggestion.job_id,
                    suggestion.actor,
                    suggestion.session_id,
                    suggestion.correlation_id,
                ),
            )
            # `DO NOTHING` yields no row when a retried durable run already wrote this suggestion;
            # read back the original id so the retry is invisible to the caller.
            row = await cursor.fetchone()
            if row is None:
                cursor = await conn.execute(
                    _SELECT_SUGGESTION_BY_JOB, (suggestion.campaign_id, suggestion.job_id)
                )
                row = await cursor.fetchone()
            (suggestion_id,) = row  # type: ignore[misc]
        return int(suggestion_id), created

    async def read_campaign(self, campaign_id: str) -> Campaign | None:
        """One campaign, or None when it has never been asked about."""
        async with _connect() as conn:
            cursor = await conn.execute(_SELECT_CAMPAIGN, (campaign_id,))
            row = await cursor.fetchone()
        if row is None:
            return None
        return Campaign(
            campaign_id=row[0],
            objective=row[1],
            direction=row[2],
            problem=row[3],
            opened_by=row[4],
            created_at=row[5],
            last_asked_at=row[6],
        )

    async def suggestions_for(self, campaign_id: str, limit: int) -> list[Suggestion]:
        """A campaign's proposals, newest first."""
        async with _connect() as conn:
            cursor = await conn.execute(_SELECT_SUGGESTIONS, (campaign_id, limit))
            rows = await cursor.fetchall()
        return [
            Suggestion(
                id=row[0],
                campaign_id=row[1],
                candidates=row[2],
                observations=row[3],
                calc_refs=list(row[4] or []),
                problem=row[5] or {},
                job_id=row[6],
                actor=row[7],
                session_id=row[8],
                correlation_id=row[9],
                proposed_at=row[10],
            )
            for row in rows
        ]
