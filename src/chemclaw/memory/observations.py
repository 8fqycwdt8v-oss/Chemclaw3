"""The ungated observations tier: what the agent noticed, kept out of the knowledge graph (D-161).

Knowledge has had one tier and one gate. Anything an agent writes is proposed as a note and a
human merges it before it counts — right for everything asserted as fact, and the reason there has
been no proactive cross-project learning loop: every candidate learning would cost a reviewer a PR,
and most candidates do not earn one.

An observation is explicitly **not** truth. "Both projects that tried this coupling on an
electron-poor aryl chloride got a poor outcome" is worth noticing and is not worth a PR. So the
human gate does not disappear — it moves, from every observation to the few worth **promoting**
into a playbook note, which still passes the same PR-gate as everything else.

Two rules make that safe, and both are enforced rather than documented:

- **An observation's identity is its scope**, so a finding that grows updates one row instead of
  minting a near-duplicate every time the corpus does. See `with_id`.
- **Support is `len(evidence_note_ids)`**, not a counter. A counter can be incremented by something
  that is not a merged note; a derived count cannot. Migration `025` additionally forbids an
  observation id from ever appearing in that column, because the dangerous failure is the agent
  retrieving its own observation, counting it as corroboration, and inflating into a PR — a
  self-confirming loop that looks exactly like cross-project evidence from the outside.
- **An observation never enters the evidence list.** `recall_observations` is its own tool and its
  results are labelled as what they are; nothing fuses them into `gather_evidence`'s ranked chunks.
  An observation may direct what you look for; it may never be the evidence for a claim.

Stored in Postgres rather than Git, which *preserves* "git is the source of truth" precisely
because these are not truth: with no review, Git buys PR noise and repo churn and returns nothing,
while a table gives cheap upsert-accumulation, TTL eviction, and no branch-per-note explosion.
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

# Accumulate rather than replace: a second run that sees the same finding backed by another
# reaction must raise its support, not restate it. Postgres has no array-union operator, so the
# union is spelled out — `array_agg(DISTINCT ...)` over the concatenation, ordered so the stored
# value is stable and a no-op run produces a byte-identical row.
_UPSERT = """
INSERT INTO observations (id, statement, scope, evidence_note_ids, projects_seen, origin)
VALUES (%(id)s, %(statement)s, %(scope)s, %(evidence)s, %(projects)s, %(origin)s)
ON CONFLICT (id) DO UPDATE SET
    -- The statement restates what the accumulated evidence shows, so it is refreshed rather than
    -- kept: a row whose evidence says three projects must not still read "two projects".
    statement = EXCLUDED.statement,
    evidence_note_ids = (
        SELECT array_agg(DISTINCT e ORDER BY e)
          FROM unnest(observations.evidence_note_ids || EXCLUDED.evidence_note_ids) AS e
    ),
    projects_seen = (
        SELECT array_agg(DISTINCT p ORDER BY p)
          FROM unnest(observations.projects_seen || EXCLUDED.projects_seen) AS p
    ),
    last_seen = now()
"""

_COLUMNS = (
    "id, statement, scope, evidence_note_ids, projects_seen, origin, status, first_seen, last_seen"
)

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

_SET_STATUS = "UPDATE observations SET status = %s WHERE id = %s"

# Retire what has stopped being re-observed. `last_seen` is refreshed by every run that still finds
# the finding, so a stale row is one the corpus no longer supports — the evidence was superseded,
# the reactions were re-classified, or it was noise to begin with.
_RETIRE_STALE = """
UPDATE observations SET status = 'retired'
 WHERE status = 'open' AND last_seen < now() - make_interval(days => %s)
"""


class Observation(BaseModel):
    """One thing the agent noticed across the corpus, with what it rests on.

    Not a note and never rendered as one. `evidence_note_ids` are **merged** note ids, so an
    observation always points at knowledge a human already signed off — it adds a reading of that
    knowledge, never a new fact.
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

        The constraint in `025` is the one that cannot be bypassed, and it is the reason this is a
        structural rule rather than a convention. This copy exists so a miner that would violate it
        fails where it is written, with a message naming the rule, instead of at the insert with a
        Postgres constraint name.
        """
        for value in values:
            if value.startswith("observation-"):
                raise ValueError(
                    f"{value!r} is an observation; support counts distinct *merged notes* only, or "
                    "an observation can corroborate itself into a promotion (D-161)"
                )
        return values

    @property
    def support(self) -> int:
        """How many distinct merged notes back this. Derived — never a stored counter."""
        return len(self.evidence_note_ids)

    def with_id(self) -> "Observation":
        """The same observation carrying its scope-derived id.

        **Scope only, never the statement.** The statement names what the evidence currently shows
        — "run in 2 projects … (2 runs)" — so it changes the moment a cluster gains a member, which
        is routine under periodic ELN sync. Hashing it would mint a *new* row for every growth
        step: support would never accumulate, `first_seen` would reset, and the superseded row
        would sit open for the whole retirement window contradicting its own successor in
        `recall_observations`. That is exactly the failure `memory/ids.py` documents for note ids,
        and the fix is the same one — anchor on something stable.

        Scope is that anchor by construction: `transformation:<smallest member id>` for a corpus
        cluster (stable as the cluster grows, since clusters are disjoint partitions) and
        `interaction:<note id>` for an interaction. The two miners cannot collide, because their
        scopes carry different prefixes.
        """
        digest = stable_hash({"scope": self.scope}, chars=12)
        return self.model_copy(update={"id": f"observation-{digest}"})


@asynccontextmanager
async def _connection() -> AsyncIterator[psycopg.AsyncConnection[TupleRow]]:
    """Borrow a connection with the configured per-statement timeout."""
    async with db.connection(
        settings.postgres_dsn, statement_timeout_seconds=settings.pg_statement_timeout_seconds
    ) as conn:
        yield conn


def _observation(row: tuple[Any, ...]) -> Observation:
    """Build an `Observation` from a `_COLUMNS` row.

    Validated through the model rather than constructed around it, so a row whose `status` or
    `origin` no longer matches the schema fails here instead of flowing on as a plausible-looking
    string. The CHECK constraints make that unreachable today; a future migration widening one is
    exactly when it stops being unreachable.
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


async def record(observations: list[Observation]) -> int:
    """Upsert observations, accumulating support onto rows that already exist. Returns the count.

    Accumulating rather than replacing is what makes support mean anything across runs: the same
    finding seen again with a different reaction behind it is more supported, not restated.
    """
    if not observations:
        return 0
    async with _connection() as conn:
        async with conn.cursor() as cur:
            for observation in observations:
                identified = observation.with_id()
                await cur.execute(
                    _UPSERT,
                    {
                        "id": identified.id,
                        "statement": identified.statement,
                        "scope": identified.scope,
                        "evidence": identified.evidence_note_ids,
                        "projects": identified.projects_seen,
                        "origin": identified.origin,
                    },
                )
        await conn.commit()
    return len(observations)


async def open_observations(limit: int | None = None) -> list[Observation]:
    """The best-supported open observations, for the retrieval bucket.

    Ordered by support before recency: an observation backed by six merged notes is worth reading
    ahead of last night's single-note one, and the tool's page is small enough that the ordering
    decides what is seen at all.
    """
    page = limit if limit is not None else settings.observation_max_results
    page = max(1, min(page, settings.observation_max_results))
    async with _connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_SELECT_OPEN, (page,))
            rows = await cur.fetchall()
    return [_observation(row) for row in rows]


async def promotable() -> list[Observation]:
    """Open observations that have crossed both promotion thresholds.

    Two thresholds, not one, because they answer different questions: evidence count says the
    finding is not a coincidence, and project count says it is not one team's local habit. A
    finding with ten notes from a single project is a well-evidenced *episodic* fact, which is what
    the campaign layer is already for.
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


async def set_status(observation_id: str, status: ObservationStatus) -> None:
    """Move one observation to `status` (promoted once its PR is opened, or retired)."""
    async with _connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_SET_STATUS, (status, observation_id))
        await conn.commit()


async def retire_stale() -> int:
    """Retire open observations nothing has re-observed within the configured window.

    Returns how many were retired. A tier that only ever grows is a write-only log; this is the
    half that lets it shrink when the corpus stops supporting a reading.
    """
    if settings.observation_retire_after_days <= 0:
        return 0
    async with _connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_RETIRE_STALE, (settings.observation_retire_after_days,))
            retired = cur.rowcount
        await conn.commit()
    return int(retired)
