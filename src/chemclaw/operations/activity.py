"""What this system did, read back out of the record it already keeps.

The tables read here have point-lookup readers elsewhere; this module adds the aggregates ("who
else has used this playbook", "how much of that note was agent-written"). A deterministic
aggregate infers nothing, so nothing here is asserted, reaches the knowledge graph, or is
remembered.

Three rules every reader keeps:

1. **Counts and identifiers only, never a caller's free text.** Arguments, details and
   rationales are caller-supplied text in one shared corpus with no record-level scoping; tool,
   connector, note type, outcome and actor id are bounded vocabularies.
2. **Every reading carries its window.** See `chemclaw.operations.window`.
3. **A row this system never wrote is never inferred.** `authorship` reports this agent's
   knowledge writes only; human edits leave no row, and the answer names that boundary.
"""

import re
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any

import psycopg
from psycopg.rows import TupleRow
from pydantic import BaseModel, Field

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.operations.window import Window

#: The `audit_events.outcome` vocabulary, in reporting order. Transcribed rather than imported
#: from `chemclaw.agent.audit`: `operations` sits below `agent`, and history holds outcomes a
#: current producer may no longer mint.
OUTCOMES: tuple[str, ...] = ("ok", "refused", "error", "cancelled", "empty")


def _connect() -> AbstractAsyncContextManager[psycopg.AsyncConnection[TupleRow]]:
    """The configured connection, with the shared statement timeout (one place, DRY)."""
    return db.connection(settings.session_store_dsn or settings.postgres_dsn)


class Coverage(BaseModel):
    """What a reading actually looked at, so an empty result is legible.

    An operational answer with no rows is ambiguous in a way a scientific one is not, and this is
    the field that narrows it: `rows` zero *with the window stated beside it* is "nothing happened
    in these 90 days", which a bare zero is not.

    **It does not distinguish "nothing happened" from "this deployment holds nothing that old", and
    an earlier version of this docstring claimed it did** — on the strength of a retention that does
    not exist. None of the six tables read here is pruned; they are all in `retention._NOT_PRUNED`,
    explicitly refused. So the case that sentence described cannot arise from retention, and the one
    that *can* — a deployment younger than the window — is still not visible here, because nothing
    reports the oldest row held. That is a real gap and it is left open rather than papered over.
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
    """One tool's use over a window, split by outcome.

    `calls` is the sum of the outcome columns, and `other` is what keeps that true: the trail holds
    rows written by every revision that ever ran, `audit_events.outcome` is bare `TEXT` with no
    `CHECK`, and `chemclaw.agent.audit` expects the vocabulary to grow. Without a catch-all a row
    outside `OUTCOMES` was counted in `calls` and in no column at all, so a reader could not tell
    an undercount from a genuine zero — the same argument `KnowledgeWrites` already made one
    reading further down this file, and the same fix.
    """

    tool: str
    calls: int = 0
    ok: int = 0
    refused: int = 0
    error: int = 0
    cancelled: int = 0
    #: Connector calls that succeeded and returned no content (`chemclaw.agent.audit.EMPTY`).
    empty: int = 0
    #: Calls whose outcome is not one of `OUTCOMES` — an older revision's vocabulary, or a newer
    #: one this reader has not learned yet.
    other: int = 0
    #: Distinct actors seen invoking it: a count, never the ids, and a **lower bound**. The SQL
    #: groups by `(tool, outcome)`, so per-group counts cannot be summed and the maximum is taken
    #: instead.
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
    #: Distinct argument-sets seen in the window — see `failed` for why this is not attempts.
    runs: int = 0
    #: Argument-sets whose **latest** run failed. `job_records` is keyed by argument-set and
    #: upserted, so one row is one argument-set carrying only its latest state, not one run; this
    #: answers "which argument-sets are currently failed", not how many attempts failed.
    failed: int = 0
    distinct_requesters: int = 0
    #: Runs that recorded a note (`job_records.note_id` is non-empty). The join between a
    #: computation and the knowledge it produced.
    recorded_notes: int = 0
    last_completed: str = ""


class JobActivity(BaseModel):
    """Durable-job activity over a window, busiest first."""

    coverage: Coverage
    jobs: list[JobRun] = Field(default_factory=list)


class KnowledgeWrites(BaseModel):
    """One knowledge-writing tool's calls over a window, bucketed by how each call ended.

    The buckets are `audit_events.outcome`'s vocabulary rather than a state machine of this
    reading's own, so `attempted` always equals their sum and a new outcome lands in `other`
    rather than nowhere.
    """

    tool: str
    attempted: int = 0
    written: int = 0
    refused: int = 0
    error: int = 0
    other: int = 0


class Authorship(BaseModel):
    """What the agent wrote into the knowledge graph over a window.

    Read this as the *agent-authored* side of the record and nothing more. `boundary` states in
    words what the tables cannot see, so an answer built on this cannot imply a share of a document
    that human edits are missing from.

    **It counts writes, not decisions.** Until 2026-09-05 this reading answered from
    `note_proposals` — every note the agent proposed and what a human merged or rejected. That
    gate is gone (`D-2026-09-05-the-gate-follows-behaviour-not-knowledge`) and nothing writes that
    table any more, so reading it would report a frozen historical count under a present-tense
    name: on any deployment installed since, a truthful-looking `proposed=0`. The live producer is
    the audit trail, which stamps every one of these calls.
    """

    coverage: Coverage
    tools: list[KnowledgeWrites] = Field(default_factory=list)
    attempted: int = 0
    written: int = 0
    boundary: str = (
        "These are the notes this system wrote and how those calls ended. Nobody reviews them "
        "before they are readable, and this holds no record of what a person wrote or edited in "
        "the git host — so it is not a share of a document's authorship and must never be "
        "reported as one."
    )


class ActorSpend(BaseModel):
    """One actor's turns over a window. Tokens and wall clock, never words.

    **All six spend columns, because reading two of them answered 1.9% of the question.** Measured
    2026-09-06: an actor with one cached turn (600 input, 250 output, 400 cache-read, 300
    cache-write) and one abandoned turn (43,506 estimated) was reported as having spent **850**
    tokens against ~45,000. `cache_read_tokens`, `cache_write_tokens` and `estimated_tokens` were
    in the table, in `TurnCost`, on every counter — and in no query — so this reading understated
    exactly the two populations it exists to find: a deployment that caches heavily, and turns
    abandoned late.

    `billed_tokens` is the four measured columns summed, which is the same number
    `chemclaw_tokens_total` publishes and the same number `TurnUsage.total` meters, so a sum here
    and a sum there answer the same question. `estimated_tokens` stays out of it and beside it: it
    is what the gateway billed and never reported, and the rule this record inherits from
    `TurnCost` is that an inferred number never passes for a provider's. **The whole bill is the
    two added together**, and a caller that adds them should say so.
    """

    actor: str
    turns: int = 0
    completed_turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # Cache reads and writes are priced differently from fresh tokens, hence separate fields.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # Inferred, never measured: the estimated prompt of a request in flight when the turn was torn
    # down. Separate so a reader can ask what fraction of an actor's spend is inference.
    estimated_tokens: int = 0
    # The four measured columns, summed — what the provider actually reported for this actor.
    billed_tokens: int = 0
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


#: Longest tool name a reading reports; anything longer or not `snake_case` is bucketed under
#: `_UNRECOGNISED`.
#:
#: `audit_events.tool` is the model's raw string, and model output is attacker-influenceable, so a
#: poisoned document could plant instruction-shaped text in one actor's trail that another's
#: `review_activity` reads back. The pattern matches every served name (`^[a-z_][a-z0-9_]*$`) and
#: the length cap sits just above the longest served name, so sentence-shaped strings become a
#: count. `tests/test_operations.py` holds both ends.
#:
#: This is bucketing, not a boundary: it bounds what a reader sees, not the `GROUP BY`'s
#: cardinality, which is computed before the bucketing runs.
MAX_TOOL_NAME = 40

_SAFE_TOOL_NAME = re.compile(rf"^[a-z_][a-z0-9_]{{0,{MAX_TOOL_NAME - 1}}}$")

#: Where a name that is not identifier-shaped is counted. Counted rather than dropped: a burst of
#: hallucinated calls is a real signal, and its number is safe to report where the strings are not.
_UNRECOGNISED = "(unrecognised)"


def safe_tool_name(name: str) -> str:
    """A tool name bounded to the shape this system actually serves, or `(unrecognised)`.

    Shared with `operations.evidence_pack`: a bound applied to only one reader of the column is not
    a bound.
    """
    return name if _SAFE_TOOL_NAME.match(name) else _UNRECOGNISED


# Two aggregations rather than one: `count(DISTINCT actor)` cannot hash in PostgreSQL, so the
# single-statement form sorts every row in the window (spilling to disk on large windows), and a
# bigger `work_mem` only makes the in-memory sort slower. Pre-aggregating by
# `(tool, outcome, actor)` lets both levels hash. The result is exact: `count(*)` over the inner
# groups is the distinct actor count, and `sum`, `min` and `max` compose.
_TOOL_USAGE = """
    SELECT tool, outcome, sum(calls), count(*), min(first_seen), max(last_seen)
    FROM (
        SELECT tool, outcome, actor,
               count(*) AS calls, min(ts) AS first_seen, max(ts) AS last_seen
        FROM audit_events
        WHERE ts >= %s AND ts < %s
        GROUP BY tool, outcome, actor
    ) per_actor
    GROUP BY tool, outcome
"""

# The narrowed form, built by rewriting the one predicate (inside the inner aggregation) rather
# than keeping a second copy of the statement.
_TOOL_USAGE_ONE = _TOOL_USAGE.replace(
    "WHERE ts >= %s AND ts < %s", "WHERE ts >= %s AND ts < %s AND tool = %s"
)
if _TOOL_USAGE_ONE == _TOOL_USAGE:  # pragma: no cover - import-time guard on a sibling constant
    # Not an `assert`: `python -O` would strip it, and the failure would surface as a bind-count
    # error at query time instead of at import.
    raise RuntimeError(
        "the predicate `_TOOL_USAGE_ONE` narrows has moved: "
        "`WHERE ts >= %s AND ts < %s` no longer occurs in `_TOOL_USAGE`"
    )


async def tool_usage(window: Window, *, tool: str | None = None) -> ToolUsage:
    """How often each tool was called over `window`, and how those calls ended.

    `tool` narrows to one name. The distinct-actor count is the only person-shaped figure returned,
    and it is a count.
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
        safe = safe_tool_name(str(name))
        use = per_tool.setdefault(safe, ToolUse(tool=safe))
        use.calls += int(calls)
        scanned += int(calls)
        # An outcome with no column lands in `other` rather than nowhere, so `calls` stays the sum
        # of the columns — the reading `authorship` already makes, for the reason `OUTCOMES` states.
        bucket = str(outcome) if str(outcome) in OUTCOMES else "other"
        setattr(use, bucket, getattr(use, bucket) + int(calls))
        # Per-(tool, outcome) distinct counts cannot be summed (one person appears under two
        # outcomes), so the maximum is the lower bound.
        actors[safe] = max(actors.get(safe, 0), int(distinct_actors))
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
    SELECT connector, job, count(*), count(*) FILTER (WHERE state = 'failed'),
           count(DISTINCT requested_by),
           count(*) FILTER (WHERE note_id <> ''),
           max(completed_at) FILTER (WHERE state = 'completed')
    FROM job_records
    WHERE completed_at >= %s AND completed_at < %s
    GROUP BY connector, job
"""


async def job_activity(window: Window) -> JobActivity:
    """Which durable jobs ran over `window`, how often, and how many recorded a note."""
    jobs = [
        JobRun(
            connector=str(connector),
            job=str(job),
            runs=int(runs),
            failed=int(failed),
            distinct_requesters=int(requesters),
            recorded_notes=int(notes),
            # The last run that *succeeded*, not the last row written: a `max(completed_at)` over
            # failures too reports a job as recently working when every recent run died.
            last_completed=_stamp(last),
        )
        for connector, job, runs, failed, requesters, notes, last in await _rows(
            _JOB_ACTIVITY, [window.since, window.until]
        )
    ]
    return JobActivity(
        coverage=Coverage.of(window, sum(job.runs for job in jobs)),
        jobs=sorted(jobs, key=lambda job: (-job.runs, job.connector, job.job)),
    )


#: The tools whose successful call puts an agent-authored note into the knowledge graph.
#:
#: Transcribed rather than imported from `chemclaw.agent.authz`, for `OUTCOMES`' reason.
#: Preference tools are deliberately absent: a preference is per-user, not knowledge.
#: `tests/test_operations.py` holds the relationship to the agent's list.
KNOWLEDGE_WRITE_TOOLS: tuple[str, ...] = (
    "record_confirmed_answer",
    "record_failure",
    "record_knowledge_note",
    "synthesize_memory",
)

#: The `audit_events.outcome` values `KnowledgeWrites` names, mapped onto its fields. Anything
#: else lands in `other`, so the arithmetic closes whatever the trail holds.
_WRITE_BUCKETS = {"ok": "written", "refused": "refused", "error": "error"}

_AUTHORSHIP = """
    SELECT tool, outcome, count(*)
    FROM audit_events
    WHERE ts >= %s AND ts < %s AND tool = ANY(%s)
    GROUP BY tool, outcome
"""


async def authorship(window: Window) -> Authorship:
    """What the agent wrote into the graph over `window`, by tool, and how those calls ended."""
    per_tool: dict[str, KnowledgeWrites] = {}
    for tool, outcome, count in await _rows(
        _AUTHORSHIP, [window.since, window.until, list(KNOWLEDGE_WRITE_TOOLS)]
    ):
        row = per_tool.setdefault(str(tool), KnowledgeWrites(tool=str(tool)))
        row.attempted += int(count)
        # An outcome with no bucket lands in `other` rather than nowhere: a reader cannot tell an
        # undercount from a genuine zero, and `attempted` must stay the sum of the buckets.
        bucket = _WRITE_BUCKETS.get(str(outcome), "other")
        setattr(row, bucket, getattr(row, bucket) + int(count))

    tools = sorted(per_tool.values(), key=lambda row: (-row.attempted, row.tool))
    return Authorship(
        coverage=Coverage.of(window, sum(row.attempted for row in tools)),
        tools=tools,
        attempted=sum(row.attempted for row in tools),
        written=sum(row.written for row in tools),
    )


# Every spend column, so cached or abandoned turns are not under-reported. The measured columns
# are summed into `billed_tokens` in SQL so total and parts cannot disagree; `estimated_tokens`
# is selected beside them, not into them.
_SPEND = """
    SELECT actor,
           count(*),
           count(*) FILTER (WHERE completed),
           sum(input_tokens), sum(output_tokens),
           sum(cache_read_tokens), sum(cache_write_tokens),
           sum(estimated_tokens),
           sum(input_tokens + output_tokens + cache_read_tokens + cache_write_tokens),
           sum(duration_seconds),
           sum(coalesce(tool_calls, 0)), sum(coalesce(tool_refusals, 0)),
           sum(coalesce(jobs_started, 0))
    FROM turn_costs
    WHERE recorded_at >= %s AND recorded_at < %s
    GROUP BY actor
"""


async def spend(window: Window) -> Spend:
    """Turns, tokens and wall clock per actor over `window`.

    Returns the actor id (unlike `tool_usage`) because "where did the effort go" needs a subject. It
    is still only an identifier and integers: no session, question or tool argument.
    """
    actors = [
        ActorSpend(
            actor=str(actor) or "(unattributed)",
            turns=int(turns),
            completed_turns=int(completed),
            input_tokens=int(inp or 0),
            output_tokens=int(out or 0),
            cache_read_tokens=int(cache_read or 0),
            cache_write_tokens=int(cache_write or 0),
            estimated_tokens=int(estimated or 0),
            billed_tokens=int(billed or 0),
            duration_seconds=float(duration or 0.0),
            tool_calls=int(calls or 0),
            tool_refusals=int(refusals or 0),
            jobs_started=int(jobs or 0),
        )
        for (
            actor,
            turns,
            completed,
            inp,
            out,
            cache_read,
            cache_write,
            estimated,
            billed,
            duration,
            calls,
            refusals,
            jobs,
        ) in await _rows(_SPEND, [window.since, window.until])
    ]
    return Spend(
        coverage=Coverage.of(window, sum(actor.turns for actor in actors)),
        actors=sorted(actors, key=lambda row: (-row.turns, row.actor)),
    )
