"""Postgres backing for the BO campaign record (`infra/sql/031_bo_campaigns.sql`).

Kept separate from `chemclaw.science.bo.campaign_record` for the reason
`chemclaw.kg.record` is kept separate from `proposal`: the module the connector tool
imports carries no database dependency, so a process running without Postgres never pulls psycopg
for a store it will not use.

The campaign upsert refreshes the problem and `last_asked_at` and **never** the opener: whoever
framed a campaign framed it, and a second chemist asking about the same space does not become its
author. Suggestions are a plain insert — the sequence is the campaign's history, and an upsert
would destroy the record of what was proposed before the latest data arrived.
"""

from contextlib import AbstractAsyncContextManager

import psycopg
from psycopg.rows import TupleRow

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.jsonb import json_column
from chemclaw.science.bo.campaign_record import Campaign, Suggestion

# `xmax = 0` is Postgres's own answer to "did this upsert insert, or update?" — on a freshly
# inserted tuple the system column is zero, on one updated by this statement it carries the
# updating transaction's id. It is read here because the alternative is a `SELECT` before the
# `INSERT`, and that read is a race: two turns opening the same decision space concurrently both
# see no row and both report having opened a new campaign. The upsert already serializes on the
# primary key, so asking *it* what happened is the only answer that cannot disagree with what was
# written.
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

# What a retried durable run already wrote. Read only when the insert above hit the partial unique
# index, so the caller still gets the suggestion id it would have got the first time — a retry must
# be invisible, not merely harmless.
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
        """Upsert the campaign and append its suggestion **atomically**.

        Returns the suggestion id and whether this call is what *created* the campaign.

        One method, one connection, one transaction — because the two writes were never
        independent. The upsert sets `problem = EXCLUDED.problem`, so a failure between them left
        the campaign row holding the **new** decision space while the surviving suggestions held
        the **old** space's observations, and `read_campaign_thread` would hand a later session
        that mismatched pair. That is exactly the "seeded with observations from a different
        campaign" failure its own docstring says the design prevents.

        This is atomicity, not an abstraction, so the Rule of Three does not gate it: two
        statements that must both land or neither belong in one transaction.

        The created flag comes out of the upsert rather than from a `SELECT` before it, because
        that read was a race — see `_UPSERT_CAMPAIGN`.
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
            # `BIGSERIAL` + `RETURNING id` cannot yield no row on a successful insert — but
            # `DO NOTHING` inserts no row at all when a retried durable run already wrote this
            # suggestion, and that is the one case where no row is the correct answer rather than
            # an anomaly. Read back the id the first attempt got, so a retry is invisible to the
            # caller instead of merely harmless.
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
