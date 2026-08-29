"""What this system did, read back out of the record it already keeps.

Every table read here has been written since the system was built, and none of them had a reader.
`PostgresAuditStore` exposes `record` and `flush` and nothing else; the grant matrix hands the
runtime principal `SELECT` on every table and no code used it on this one. So the trail proved what
happened and could not answer a question about it — which is why "who else has used this playbook",
"how many hazard flags did the group raise last quarter" and "how much of that note was
agent-written" were all unanswerable from data the system had already stamped.

**This is a projection, not a claim, and that is what makes it ungated.** The same argument
`D-2026-08-25-an-eln-transcription-is-data-not-a-claim` makes one level down: a deterministic
aggregate of rows nobody wrote for this purpose infers nothing, so it hands a reviewer nothing to
decide. Nothing here is proposed, nothing reaches the knowledge graph, and nothing is remembered.

**Three rules the readers below all keep.**

1. **Counts and identifiers only — never a caller's free text.** `audit_events.arguments`,
   `audit_events.detail`, `note_proposals.content` and `job_records.rationale` all hold text a
   caller supplied, and there is one shared corpus with no record-level scoping: an aggregate is
   visible to everyone who can reach the agent. A tool name, a connector name, a note type, an
   outcome and an actor id are bounded vocabularies; a rationale is not. `find_past_jobs` already
   serves the free-text half, through the retrieval path that frames what it returns.
2. **Every reading carries its window.** See `chemclaw.operations.window`.
3. **A row this system never wrote is never inferred.** `authorship` reports the proposals the
   agent opened and what a human decided about them. It does not report what a human wrote, because
   nothing here records that: a note edited in the git host between proposal and merge leaves no
   row, and the honest answer names that boundary rather than reporting a percentage that steps
   over it.
"""

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any

import psycopg
from psycopg.rows import TupleRow
from pydantic import BaseModel, Field

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.operations.window import Window

#: The `audit_events.outcome` vocabulary, in the order a reading reports it. Transcribed rather
#: than imported from `chemclaw.agent.audit`: `operations` is below `agent` in the layering, and
#: the trail holds rows written by every revision that ever ran — including outcomes a current
#: producer no longer mints. A reader of history must therefore not be bounded by today's producer.
OUTCOMES: tuple[str, ...] = ("ok", "refused", "error", "cancelled")


def _connect() -> AbstractAsyncContextManager[psycopg.AsyncConnection[TupleRow]]:
    """The configured connection, with the shared statement timeout (one place, DRY)."""
    return db.connection(settings.session_store_dsn or settings.postgres_dsn)


class Coverage(BaseModel):
    """What a reading actually looked at, so an empty result is legible.

    An operational answer with no rows is ambiguous in a way a scientific one is not, and this is
    the field that resolves it: `rows` zero with `window_days` 90 is "nothing happened", and the
    same zero from a deployment whose retention prunes at 30 is "you asked about a period this
    database no longer holds".
    """

    since: str
    until: str
    described: str
    window_days: int
    #: Rows the window contained, before grouping. Zero means the tables are empty for this span.
    rows: int = 0

    @classmethod
    def of(cls, window: Window, rows: int) -> "Coverage":
        """The coverage of `window`, having scanned `rows`."""
        return cls(
            since=window.since.isoformat(),
            until=window.until.isoformat(),
            described=window.described,
            window_days=window.days,
            rows=rows,
        )


class ToolUse(BaseModel):
    """One tool's use over a window, split by outcome."""

    tool: str
    calls: int = 0
    ok: int = 0
    refused: int = 0
    error: int = 0
    cancelled: int = 0
    #: Distinct actors who invoked it. A count, never the ids: "who has used this" is answered by
    #: how many, because naming colleagues in an aggregate is a different disclosure from naming
    #: the actor on a row that person can already see.
    distinct_actors: int = 0
    first_used: str = ""
    last_used: str = ""


class ToolUsage(BaseModel):
    """Tool use over a window, busiest first, with what the reading covered."""

    coverage: Coverage
    tools: list[ToolUse] = Field(default_factory=list)


class JobRun(BaseModel):
    """One connector job's durable runs over a window."""

    connector: str
    job: str
    runs: int = 0
    distinct_requesters: int = 0
    #: Runs that proposed a note through the PR-gate. The join between a computation and the
    #: knowledge it was allowed to suggest.
    proposed_notes: int = 0
    last_completed: str = ""


class JobActivity(BaseModel):
    """Durable-job activity over a window, busiest first."""

    coverage: Coverage
    jobs: list[JobRun] = Field(default_factory=list)


class ProposalOutcome(BaseModel):
    """One note type's proposals over a window, and what humans decided about them."""

    note_type: str
    proposed: int = 0
    merged: int = 0
    rejected: int = 0
    open: int = 0
    failed: int = 0


class Authorship(BaseModel):
    """What the agent proposed for the knowledge graph, and what a human did with it.

    Read this as the *agent-authored* side of the record and nothing more. `boundary` states in
    words what the tables cannot see, so an answer built on this cannot imply a share of a document
    that human edits are missing from.
    """

    coverage: Coverage
    note_types: list[ProposalOutcome] = Field(default_factory=list)
    proposed: int = 0
    merged: int = 0
    rejected: int = 0
    boundary: str = (
        "These are the notes this system proposed and how they were decided. It holds no record "
        "of what a person wrote or edited in the git host, so this is not a share of a document's "
        "authorship and must never be reported as one."
    )


class ActorSpend(BaseModel):
    """One actor's turns over a window. Tokens and wall clock, never words."""

    actor: str
    turns: int = 0
    completed_turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_seconds: float = 0.0
    tool_calls: int = 0
    tool_refusals: int = 0
    jobs_started: int = 0


class Spend(BaseModel):
    """Turn-level spend over a window, heaviest actor first."""

    coverage: Coverage
    actors: list[ActorSpend] = Field(default_factory=list)


async def _rows(sql: str, params: Sequence[Any]) -> list[tuple[Any, ...]]:
    """Every row the query returned, as plain tuples."""
    async with _connect() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, tuple(params))
            return [tuple(row) for row in await cur.fetchall()]


def _stamp(value: Any) -> str:
    """An ISO timestamp, or '' when the column was NULL."""
    return value.isoformat() if value is not None else ""


_TOOL_USAGE = """
    SELECT tool, outcome, count(*), count(DISTINCT actor), min(ts), max(ts)
    FROM audit_events
    WHERE ts >= %s AND ts < %s
    GROUP BY tool, outcome
"""

_TOOL_USAGE_ONE = _TOOL_USAGE.replace(
    "WHERE ts >= %s AND ts < %s", "WHERE ts >= %s AND ts < %s AND tool = %s"
)


async def tool_usage(window: Window, *, tool: str | None = None) -> ToolUsage:
    """How often each tool was called over `window`, and how those calls ended.

    `tool` narrows to one name — the "is this playbook actually being used" question, asked of the
    tool that reads it. The distinct-actor count is the only person-shaped figure returned, and it
    is a count.
    """
    params: list[Any] = [window.since, window.until]
    sql = _TOOL_USAGE
    if tool:
        sql = _TOOL_USAGE_ONE
        params.append(tool)

    per_tool: dict[str, ToolUse] = {}
    actors: dict[str, int] = {}
    scanned = 0
    for name, outcome, calls, distinct_actors, first, last in await _rows(sql, params):
        use = per_tool.setdefault(str(name), ToolUse(tool=str(name)))
        use.calls += int(calls)
        scanned += int(calls)
        if str(outcome) in OUTCOMES:
            setattr(use, str(outcome), getattr(use, str(outcome)) + int(calls))
        # The per-(tool, outcome) distinct count cannot be summed into a per-tool one — the same
        # person appears under two outcomes — so the maximum is taken as the honest lower bound and
        # the field says "distinct actors seen", not "distinct actors".
        actors[str(name)] = max(actors.get(str(name), 0), int(distinct_actors))
        earliest, latest = _stamp(first), _stamp(last)
        if earliest and (not use.first_used or earliest < use.first_used):
            use.first_used = earliest
        use.last_used = max(use.last_used, latest)

    for name, use in per_tool.items():
        use.distinct_actors = actors[name]

    return ToolUsage(
        coverage=Coverage.of(window, scanned),
        tools=sorted(per_tool.values(), key=lambda use: (-use.calls, use.tool)),
    )


_JOB_ACTIVITY = """
    SELECT connector, job, count(*), count(DISTINCT requested_by),
           count(*) FILTER (WHERE note_id <> ''), max(completed_at)
    FROM job_records
    WHERE completed_at >= %s AND completed_at < %s
    GROUP BY connector, job
"""


async def job_activity(window: Window) -> JobActivity:
    """Which durable jobs ran over `window`, how often, and how many proposed a note."""
    jobs = [
        JobRun(
            connector=str(connector),
            job=str(job),
            runs=int(runs),
            distinct_requesters=int(requesters),
            proposed_notes=int(notes),
            last_completed=_stamp(last),
        )
        for connector, job, runs, requesters, notes, last in await _rows(
            _JOB_ACTIVITY, [window.since, window.until]
        )
    ]
    return JobActivity(
        coverage=Coverage.of(window, sum(job.runs for job in jobs)),
        jobs=sorted(jobs, key=lambda job: (-job.runs, job.connector, job.job)),
    )


_AUTHORSHIP = """
    SELECT note_type, state, count(*)
    FROM note_proposals
    WHERE submitted_at >= %s AND submitted_at < %s
    GROUP BY note_type, state
"""


async def authorship(window: Window) -> Authorship:
    """What the agent proposed over `window`, by note type, and how it was decided."""
    per_type: dict[str, ProposalOutcome] = {}
    for note_type, state, count in await _rows(_AUTHORSHIP, [window.since, window.until]):
        outcome = per_type.setdefault(str(note_type), ProposalOutcome(note_type=str(note_type)))
        outcome.proposed += int(count)
        if hasattr(outcome, str(state)):
            setattr(outcome, str(state), getattr(outcome, str(state)) + int(count))

    types = sorted(per_type.values(), key=lambda row: (-row.proposed, row.note_type))
    return Authorship(
        coverage=Coverage.of(window, sum(row.proposed for row in types)),
        note_types=types,
        proposed=sum(row.proposed for row in types),
        merged=sum(row.merged for row in types),
        rejected=sum(row.rejected for row in types),
    )


_SPEND = """
    SELECT actor,
           count(*),
           count(*) FILTER (WHERE completed),
           sum(input_tokens), sum(output_tokens), sum(duration_seconds),
           sum(coalesce(tool_calls, 0)), sum(coalesce(tool_refusals, 0)),
           sum(coalesce(jobs_started, 0))
    FROM turn_costs
    WHERE recorded_at >= %s AND recorded_at < %s
    GROUP BY actor
"""


async def spend(window: Window) -> Spend:
    """Turns, tokens and wall clock per actor over `window`.

    The actor id is returned here where `tool_usage` returns only a count, because this reading
    answers "where did the effort go" and a total with no subject is not an answer. It is still
    only an identifier and a set of integers: no session, no question, no tool argument.
    """
    actors = [
        ActorSpend(
            actor=str(actor) or "(unattributed)",
            turns=int(turns),
            completed_turns=int(completed),
            input_tokens=int(inp or 0),
            output_tokens=int(out or 0),
            duration_seconds=float(duration or 0.0),
            tool_calls=int(calls or 0),
            tool_refusals=int(refusals or 0),
            jobs_started=int(jobs or 0),
        )
        for actor, turns, completed, inp, out, duration, calls, refusals, jobs in await _rows(
            _SPEND, [window.since, window.until]
        )
    ]
    return Spend(
        coverage=Coverage.of(window, sum(actor.turns for actor in actors)),
        actors=sorted(actors, key=lambda row: (-row.turns, row.actor)),
    )
