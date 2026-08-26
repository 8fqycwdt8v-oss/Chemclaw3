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

- **An observation's identity is its scope**, so a finding that grows normally updates one row
  instead of minting a near-duplicate every time the corpus does. "Normally" is load-bearing and
  `with_id` spells out the exception and what it costs.
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

# Re-observing a finding revives it. Retirement means "the corpus stopped supporting this", and
# the corpus is entitled to change its mind: a finding that lapses for
# `observation_retire_after_days` and returns, or an ingest source quiet for that long, both come
# back through here — two ordinary paths, not edge cases. Without this the row kept its evidence
# replaced and its `last_seen` bumped by every subsequent pass while every read
# (`open_observations`, `promotable`, `recall_observations`) filters `status = 'open'` — so it was
# invisible *permanently*, and it never re-entered `retire_stale`'s count either, which is the
# tier's own instrumentation for whether the miners are producing noise. A dead tier rather than a
# breathing one.
#
# `retired -> open` only. The column holds exactly three values (migration `025` constrains it), and
# `promoted` must survive re-observation untouched: the miners keep re-observing a promoted finding
# by construction, and reopening it would re-promote it on the next sweep and open the same PR every
# night — the failure `test_a_promoted_observation_leaves_the_open_set` exists to prevent.
_REVIVE = """
    status = CASE WHEN observations.status = 'retired' THEN 'open' ELSE observations.status END"""

# **A run is authoritative for the rows it names — when, and only when, it saw the whole corpus.**
#
# The union these replace could only ever grow, so a member that left the cluster stayed forever: a
# reaction re-assayed SUCCESS is dropped by `mine_corpus` before fingerprinting, yet its note id
# remained in `evidence_note_ids` and kept counting toward `support`. Proved end to end — an
# observation crossed the promotion threshold on three notes while its own refreshed statement said
# "failed in 2 runs across 2 projects", so the generated PR body contradicted itself in consecutive
# paragraphs and cited a documented success as evidence of failure. `retire_stale` cannot reach it
# either, because the row is still being re-observed. So a complete pass **replaces** both arrays,
# and support tracks the corpus in both directions.
#
# Replacing on *every* pass, however, reintroduces the same defect one layer down: a pass that read
# only part of the corpus would rewrite evidence *down* and render as an authoritative statement of
# what the record holds. `chemclaw.durable.memory_jobs.read_corpus` cannot promise completeness —
# an entry `map_to_ord` rejects is skipped and the read goes on — so it reports whether it was
# complete, and a partial pass falls back to the union: it may add what it saw and may never delete
# what it could not see. A retraction it cannot distinguish from an invisible member simply waits
# for the next complete pass, which is the only pass entitled to make it.
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

# Postgres has no array-union operator, so the union is spelled out — `array_agg(DISTINCT ...)` over
# the concatenation, ordered so the stored value is stable and a no-op run produces a byte-identical
# row. The statement is *kept*, not refreshed: it was written by a pass that saw more than this one,
# and replacing it would leave a "one project" sentence beside three-project evidence — the exact
# self-contradiction the replacement above exists to remove, arrived at from the other side.
_ACCUMULATE = f"""
INSERT INTO observations (id, statement, scope, evidence_note_ids, projects_seen, origin)
VALUES (%(id)s, %(statement)s, %(scope)s, %(evidence)s, %(projects)s, %(origin)s)
ON CONFLICT (id) DO UPDATE SET
    evidence_note_ids = (
        SELECT array_agg(DISTINCT e ORDER BY e)
          FROM unnest(observations.evidence_note_ids || EXCLUDED.evidence_note_ids) AS e
    ),
    projects_seen = (
        SELECT array_agg(DISTINCT p ORDER BY p)
          FROM unnest(observations.projects_seen || EXCLUDED.projects_seen) AS p
    ),
    last_seen = now(),{_REVIVE}
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
        is routine under periodic ELN sync. Hashing it would mint a *new* row for **every** growth
        step, so support would never accumulate at all and the tier's one threshold would never be
        crossed. That is the failure `memory/ids.py` documents for note ids, and the fix is the
        same one — anchor on something that moves less often than the wording does.

        **Scope is a better anchor, not a stable one, and it is worth saying which.**
        `interaction:<note id>` is genuinely stable: a merged note keeps its id.
        `transformation:<smallest member id>` is not. It moves in two cases — a new reaction whose
        id sorts below the current anchor joins the cluster, and two clusters merge because a new
        reaction bridges them under single linkage (`memory.similarity`), after which the merged
        cluster answers to the smaller of the two anchors. Cluster disjointness prevents neither;
        it buys a *different* property, that two clusters never claim one scope, and the two
        miners' scope prefixes do the same job between them.

        **What an anchor move costs, in full.** The next run mints one row for the superset and
        stops refreshing the old one, which sits `open` with its subset statement until
        `retire_stale` reaps it — at most `observation_retire_after_days`. `open_observations`
        orders by support, and the superset holds the subset's evidence plus the new member, so a
        reader of `recall_observations` sees a weaker restatement ranked below the current finding.
        Redundancy, bounded and self-healing, never a contradiction.

        One further cost is **not** among them, which is part of what makes this acceptable where
        hashing the statement is not: `first_seen` resets on the new row, and nothing reads that
        column — no ranking, promotion or retirement query touches it.

        **The duplicate-PR argument that used to sit here has been withdrawn, and replaced by an
        actual check.** It read: promotion runs on every mining pass, so a row over both thresholds
        is already `promoted` — and out of `_SELECT_PROMOTABLE` — before any later run can move the
        anchor. That was true while one workflow did both. D-2026-08-25 split promotion out so that
        no timer opens a pull request, and the precondition went with it: mining now runs daily
        with no promotion, so a subset row can sit `open` and over-threshold while an anchor move
        mints a superset row that is over-threshold too, and one later promotion opens two PRs for
        one finding. `durable.observation_jobs.promote_observations_activity` now supersedes the
        subset instead of relying on the ordering — a guarantee the code makes rather than one the
        schedule happened to provide.

        **Kept rather than replaced, deliberately.** A merge-stable key would have to survive two
        clusters becoming one, and a single-linkage cluster's identity *is* its membership — the
        one thing a merge changes. Nothing derived from the members can be stable across it, so the
        alternative is a union-find identity persisted between runs: new state, plus a
        reconciliation step of its own, bought against a redundancy that expires inside the
        retirement window and is outranked for as long as it lasts.
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


async def record(observations: list[Observation], *, complete: bool) -> int:
    """Upsert observations. Returns the count.

    `complete` says whether the pass that produced these read the **whole** corpus. It is required
    and keyword-only because it decides whether a row may shrink, and a caller that has not thought
    about it is exactly the caller that must not silently get the authoritative branch:

    - `True` — each observation replaces the row it names, so a member the record has since
      retracted stops counting instead of backing a promotion forever (`_REPLACE`).
    - `False` — the pass may only add what it saw (`_ACCUMULATE`). It read part of the corpus, so
      an absent member is not evidence of a retraction, and deleting on that basis would state a
      partial reading as the complete one.

    Support accumulates across runs either way; only a complete pass may take it back down.
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
    async with _connection() as conn:
        async with conn.cursor() as cur:
            for observation in observations:
                identified = observation.with_id()
                await cur.execute(
                    statement,
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
