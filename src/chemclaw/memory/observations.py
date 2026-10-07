"""The observations tier: what the agent noticed, kept out of the knowledge graph.

An observation is explicitly not truth: a pattern across projects that no single run supports. Notes
in `knowledge/` are cited as evidence, so a hunch lives here until it crosses the promotion
thresholds, and promotion writes an ordinary playbook note. Enforced rules:

- An observation's identity is its scope, so a growing finding updates one row (see `with_id` for
  when the scope moves).
- Support is `len(evidence_note_ids)`, derived rather than counted, holding only what the miners put
  there (reaction records). Migration `025` forbids an observation id in that column, so the agent
  cannot count its own observation as corroboration.
- An observation never enters the evidence list: `recall_observations` is its own labelled tool,
  never fused into `gather_evidence`.

Stored in Postgres rather than Git because these are not truth: a table gives cheap upserts and TTL
eviction without a commit per candidate.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Literal

import psycopg
from psycopg.rows import TupleRow
from pydantic import BaseModel, Field, field_validator

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.ids import stable_hash

logger = logging.getLogger(__name__)

ObservationStatus = Literal["open", "promoted", "retired"]
ObservationOrigin = Literal["corpus-mining", "interaction"]

# Re-observing a retired finding reopens it: the corpus may support it again (a lapsed finding
# returns, a quiet source resumes). Without this the row would stay invisible to every read, which
# filters `status = 'open'`. Only `retired -> open`: a `promoted` row is re-observed by construction
# and must stay promoted, or it would be re-promoted every night.
_REVIVE = """
    status = CASE WHEN observations.status = 'retired' THEN 'open' ELSE observations.status END"""

# A complete pass is authoritative for the rows it names and replaces both arrays, so a member that
# left the cluster (e.g. re-assayed as a success) stops counting toward support. A partial pass
# (`read_corpus` may skip rejected entries) uses `_ACCUMULATE` instead: it may add what it saw but
# never delete what it could not see.
_REPLACE = f"""
INSERT INTO observations (id, statement, scope, evidence_note_ids, projects_seen, origin)
VALUES (%(id)s, %(statement)s, %(scope)s, %(evidence)s, %(projects)s, %(origin)s)
ON CONFLICT (id) DO UPDATE SET
    -- The statement restates what the current evidence shows, so it is refreshed rather than kept:
    -- a row whose evidence says three projects must not still read "two projects".
    statement = EXCLUDED.statement,
    evidence_note_ids = EXCLUDED.evidence_note_ids,
    projects_seen = EXCLUDED.projects_seen,
    last_seen = now(),{_REVIVE}
"""

# Array union spelled out (`array_agg(DISTINCT ...)` over the concatenation), ordered so a no-op run
# writes a byte-identical row. The statement is kept, not refreshed: it came from a pass that saw
# more. `COALESCE` because `array_agg` over zero rows is NULL and the columns are NOT NULL; the
# shipped miners never send empty evidence, but `record()` permits it.
_ACCUMULATE = f"""
INSERT INTO observations (id, statement, scope, evidence_note_ids, projects_seen, origin)
VALUES (%(id)s, %(statement)s, %(scope)s, %(evidence)s, %(projects)s, %(origin)s)
ON CONFLICT (id) DO UPDATE SET
    evidence_note_ids = COALESCE((
        SELECT array_agg(DISTINCT e ORDER BY e)
          FROM unnest(observations.evidence_note_ids || EXCLUDED.evidence_note_ids) AS e
    ), '{{}}'),
    projects_seen = COALESCE((
        SELECT array_agg(DISTINCT p ORDER BY p)
          FROM unnest(observations.projects_seen || EXCLUDED.projects_seen) AS p
    ), '{{}}'),
    last_seen = now(),{_REVIVE}
"""

_COLUMNS = (
    "id, statement, scope, evidence_note_ids, projects_seen, origin, status, first_seen, last_seen"
)

# This ORDER BY must match `observations_open_rank_idx` (migration `062`); change either and the
# read falls back to sorting every open row. `tests/test_observations.py` checks both.
_SELECT_OPEN = f"""
SELECT {_COLUMNS} FROM observations
 WHERE status = 'open' ORDER BY cardinality(evidence_note_ids) DESC, last_seen DESC LIMIT %s
"""

_SELECT_PROMOTABLE = f"""
SELECT {_COLUMNS} FROM observations
 WHERE status = 'open'
   AND cardinality(evidence_note_ids) >= %s
   AND cardinality(projects_seen) >= %s
 ORDER BY id
"""

_SELECT_PROMOTED = f"""
SELECT {_COLUMNS} FROM observations
 WHERE status = 'promoted' ORDER BY cardinality(evidence_note_ids) DESC, id
"""

_SET_STATUS = "UPDATE observations SET status = %s WHERE id = %s"

# Retire what has stopped being re-observed: every run that still finds a finding refreshes
# `last_seen`, so a stale row is one the corpus no longer supports.
_RETIRE_STALE = """
UPDATE observations SET status = 'retired'
 WHERE status = 'open' AND last_seen < now() - make_interval(days => %s)
"""


class Observation(BaseModel):
    """One thing the agent noticed across the corpus, with what it rests on.

    Not a note and never rendered as one. `evidence_note_ids` are the citations the miner
    counted: `reaction-<id>` references into the **ungated** transcription store for the corpus
    miner (an ELN row is data, not a claim — D-2026-08-25 removed the gate from transcriptions,
    and the evidence moved with them), and the `interaction` note id for the interaction
    miner. This field's docstring said "merged note ids, so an observation always points at
    knowledge a human already signed off" for months after that stopped being true of the larger
    half — which made every promotion PR's "supported by N merged notes" a false statement to the
    human at the gate. What is still true, and is the actual invariant: an observation adds a
    *reading* of recorded evidence, never a new fact, and the promotion summary now says which
    kind of evidence it is counting.
    """

    id: str = ""
    statement: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    evidence_note_ids: list[str] = Field(default_factory=list)
    projects_seen: list[str] = Field(default_factory=list)
    origin: ObservationOrigin = "corpus-mining"
    status: ObservationStatus = "open"
    first_seen: datetime | None = None
    last_seen: datetime | None = None

    @field_validator("evidence_note_ids")
    @classmethod
    def _evidence_is_never_an_observation(cls, values: list[str]) -> list[str]:
        """Refuse self-citation here too, not only in the database.

        Migration `025` is the constraint that cannot be bypassed; this copy makes a violating miner
        fail where it is written, with a readable message.
        """
        for value in values:
            if value.startswith("observation-"):
                raise ValueError(
                    f"{value!r} is an observation; support counts distinct *evidence* only, or "
                    "an observation can corroborate itself into a promotion (D-161)"
                )
        return values

    @property
    def support(self) -> int:
        """How many distinct pieces of evidence back this. Derived — never a stored counter."""
        return len(self.evidence_note_ids)

    def with_id(self) -> "Observation":
        """The same observation carrying its scope-derived id.

        Scope only, never the statement: the statement changes as a cluster grows, and hashing it
        would mint a new row each time so support never accumulates. `interaction:<note id>` is
        stable; `transformation:<smallest member id>` moves when a smaller id joins or clusters
        merge. A move leaves the old row open with a subset statement, ranked below the superset by
        support, until `retire_stale` reaps it; promotion supersedes the subset so one finding is
        not promoted twice. A merge-stable key would need persisted cluster identity, not worth it
        for redundancy that expires on its own.
        """
        digest = stable_hash({"scope": self.scope}, chars=12)
        return self.model_copy(update={"id": f"observation-{digest}"})


@asynccontextmanager
async def _connection() -> AsyncIterator[psycopg.AsyncConnection[TupleRow]]:
    """Borrow a connection with the configured per-statement timeout."""
    async with db.connection(settings.postgres_dsn) as conn:
        yield conn


def _observation(row: tuple[Any, ...]) -> Observation:
    """Build an `Observation` from a `_COLUMNS` row.

    Validated through the model, so a row whose `status` or `origin` no longer matches the schema
    fails here.
    """
    return Observation(
        id=row[0],
        statement=row[1],
        scope=row[2],
        evidence_note_ids=list(row[3] or []),
        projects_seen=list(row[4] or []),
        origin=row[5],
        status=row[6],
        first_seen=row[7],
        last_seen=row[8],
    )


async def record(observations: list[Observation], *, complete: bool) -> int:
    """Upsert observations. Returns the count.

    `complete` (required, keyword-only) says whether the producing pass read the whole corpus:

    - `True`: each observation replaces its row, so retracted members stop counting (`_REPLACE`).
    - `False`: the pass may only add what it saw (`_ACCUMULATE`), since an absent member is not
      evidence of a retraction.
    """
    if not observations:
        return 0
    statement = _REPLACE if complete else _ACCUMULATE
    if not complete:
        logger.warning(
            "recording %d observation(s) from a partial corpus read: evidence can only be added "
            "this pass, so a retraction waits for the next complete one",
            len(observations),
        )
    rows = [
        {
            "id": identified.id,
            "statement": identified.statement,
            "scope": identified.scope,
            "evidence": identified.evidence_note_ids,
            "projects": identified.projects_seen,
            "origin": identified.origin,
        }
        for identified in (observation.with_id() for observation in observations)
    ]
    async with _connection() as conn:
        async with conn.cursor() as cur:
            # One batched statement rather than a round trip per row.
            await cur.executemany(statement, rows)
        await conn.commit()
    return len(observations)


async def open_observations(limit: int | None = None) -> list[Observation]:
    """The best-supported open observations, for the retrieval bucket.

    Ordered by support, then recency; the page is small, so the order decides what is seen.
    """
    page = limit if limit is not None else settings.observation_max_results
    page = max(1, min(page, settings.observation_max_results))
    async with _connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_SELECT_OPEN, (page,))
            rows = await cur.fetchall()
    return [_observation(row) for row in rows]


async def count_open_observations() -> int:
    """How many observations are open at all: the population `open_observations` pages.

    A separate query, so `open_observations` rows stay page-free; the tier is written by a nightly
    batch, so the two reads rarely disagree. Used by `recall_observations` to tell a full page from
    a truncated one.
    """
    async with _connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM observations WHERE status = 'open'")
            row = await cur.fetchone()
    return int(row[0]) if row else 0


async def promotable() -> list[Observation]:
    """Open observations that have crossed both promotion thresholds.

    Evidence count says the finding is not a coincidence; project count says it is not one team's
    habit (a single-project finding belongs to the campaign layer).
    """
    async with _connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                _SELECT_PROMOTABLE,
                (
                    settings.observation_promote_min_evidence,
                    settings.observation_promote_min_projects,
                ),
            )
            rows = await cur.fetchall()
    return [_observation(row) for row in rows]


async def promoted_observations() -> list[Observation]:
    """Every promoted observation, best-supported first: what the promotion guard checks against.

    Reading promoted rows from the store makes duplicate-promotion protection hold across passes and
    activity retries.
    """
    async with _connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_SELECT_PROMOTED)
            rows = await cur.fetchall()
    return [_observation(row) for row in rows]


async def set_status(observation_id: str, status: ObservationStatus) -> None:
    """Move one observation to `status` (promoted once its playbook note is written, or retired)."""
    async with _connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_SET_STATUS, (status, observation_id))
        await conn.commit()


async def retire_stale() -> int:
    """Retire open observations nothing has re-observed within the configured window.

    Returns how many were retired.
    """
    if settings.observation_retire_after_days <= 0:
        return 0
    async with _connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_RETIRE_STALE, (settings.observation_retire_after_days,))
            retired = cur.rowcount
        await conn.commit()
    return int(retired)
