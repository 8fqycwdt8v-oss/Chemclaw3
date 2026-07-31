"""Postgres backing for the BO campaign record (`infra/sql/031_bo_campaigns.sql`).

Kept separate from `chemclaw.science.bo.campaign_record` for the reason
`chemclaw.kg.proposal_store` is kept separate from `proposal`: the module the connector tool
imports carries no database dependency, so a process running without Postgres never pulls psycopg
for a store it will not use.

The campaign upsert refreshes the problem and `last_asked_at` and **never** the opener: whoever
framed a campaign framed it, and a second chemist asking about the same space does not become its
author. Suggestions are a plain insert — the sequence is the campaign's history, and an upsert
would destroy the record of what was proposed before the latest data arrived.
"""

from contextlib import AbstractAsyncContextManager
from typing import Any

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.science.bo.campaign_record import Campaign, Suggestion

_UPSERT_CAMPAIGN = """
    INSERT INTO bo_campaigns (campaign_id, objective, direction, problem, opened_by)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT (campaign_id) DO UPDATE SET
        problem = EXCLUDED.problem,
        last_asked_at = now()
"""

_INSERT_SUGGESTION = """
    INSERT INTO bo_suggestions
        (campaign_id, candidates, observations, calc_refs, actor, session_id, correlation_id)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    RETURNING id
"""

_SELECT_CAMPAIGN = (
    "SELECT campaign_id, objective, direction, problem, opened_by, created_at, last_asked_at "
    "FROM bo_campaigns WHERE campaign_id = %s"
)

_SELECT_SUGGESTIONS = """
    SELECT id, campaign_id, candidates, observations, calc_refs, actor, session_id,
           correlation_id, proposed_at
    FROM bo_suggestions
    WHERE campaign_id = %s
    ORDER BY id DESC
    LIMIT %s
"""


def _connect() -> AbstractAsyncContextManager[psycopg.AsyncConnection[TupleRow]]:
    """The configured connection, with the shared statement timeout (one place, DRY)."""
    return db.connection(
        settings.postgres_dsn, statement_timeout_seconds=settings.pg_statement_timeout_seconds
    )


def _json(value: Any) -> Jsonb:
    """Wrap a value for a `jsonb` column (psycopg needs the explicit adapter)."""
    return Jsonb(value)


class PostgresCampaignStore:
    """The durable `CampaignStore`: one short-lived connection per call (the house choice)."""

    async def upsert_campaign(self, campaign: Campaign) -> None:
        """Record the campaign, refreshing its problem and `last_asked_at` but never its opener."""
        async with _connect() as conn:
            await conn.execute(
                _UPSERT_CAMPAIGN,
                (
                    campaign.campaign_id,
                    campaign.objective,
                    campaign.direction,
                    _json(campaign.problem),
                    campaign.opened_by,
                ),
            )
            await conn.commit()

    async def add_suggestion(self, suggestion: Suggestion) -> int:
        """Append one proposal; return its id."""
        async with _connect() as conn:
            cursor = await conn.execute(
                _INSERT_SUGGESTION,
                (
                    suggestion.campaign_id,
                    _json(
                        [candidate.model_dump(mode="json") for candidate in suggestion.candidates]
                    ),
                    _json(
                        [
                            observation.model_dump(mode="json")
                            for observation in suggestion.observations
                        ]
                    ),
                    suggestion.calc_refs,
                    suggestion.actor,
                    suggestion.session_id,
                    suggestion.correlation_id,
                ),
            )
            row = await cursor.fetchone()
            await conn.commit()
        return int(row[0]) if row is not None else 0

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
                actor=row[5],
                session_id=row[6],
                correlation_id=row[7],
                proposed_at=row[8],
            )
            for row in rows
        ]
