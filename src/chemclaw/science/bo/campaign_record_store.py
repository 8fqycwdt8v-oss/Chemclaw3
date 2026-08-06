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

from chemclaw.core import db
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


class PostgresCampaignStore:
    """The durable `CampaignStore`: one short-lived connection per call (the house choice)."""

    async def record(self, campaign: Campaign, suggestion: Suggestion) -> int:
        """Upsert the campaign and append its suggestion **atomically**; return the suggestion id.

        One method, one connection, one transaction — because the two writes were never
        independent. The upsert sets `problem = EXCLUDED.problem`, so a failure between them left
        the campaign row holding the **new** decision space while the surviving suggestions held
        the **old** space's observations, and `read_campaign_thread` would hand a later session
        that mismatched pair. That is exactly the "seeded with observations from a different
        campaign" failure its own docstring says the design prevents.

        This is atomicity, not an abstraction, so the Rule of Three does not gate it: two
        statements that must both land or neither belong in one transaction.
        """
        async with db.bounded() as conn, conn.transaction():
            await conn.execute(
                _UPSERT_CAMPAIGN,
                (
                    campaign.campaign_id,
                    campaign.objective,
                    campaign.direction,
                    db.jsonb(campaign.problem),
                    campaign.opened_by,
                ),
            )
            cursor = await conn.execute(
                _INSERT_SUGGESTION,
                (
                    suggestion.campaign_id,
                    db.jsonb(
                        [candidate.model_dump(mode="json") for candidate in suggestion.candidates]
                    ),
                    db.jsonb(
                        [
                            observation.model_dump(mode="json")
                            for observation in suggestion.observations
                        ]
                    ),
                    suggestion.calc_refs,
                    db.jsonb(suggestion.problem),
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
        return int(suggestion_id)

    async def read_campaign(self, campaign_id: str) -> Campaign | None:
        """One campaign, or None when it has never been asked about."""
        async with db.bounded() as conn:
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
        async with db.bounded() as conn:
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
