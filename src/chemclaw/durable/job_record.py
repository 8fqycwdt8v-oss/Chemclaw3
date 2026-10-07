"""The durable record of one finished connector job — what ran, on what, and **why** (D-157).

Temporal's history expires with the namespace's retention window, taking a job's result with it.
This record keeps what ran, what it produced and why it was started, for every connector job.
It is written by core's `ConnectorJobWorkflow`, the one wrapper every job runs inside, so no
connector can forget it.

The sink is durable by default (`default_job_record_sink`), so a forgotten argument cannot
silently downgrade it; without Postgres it falls back to the null sink.
"""

import logging
from datetime import date, datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, computed_field
from temporalio import activity

from chemclaw.core.config import settings
from chemclaw.core.logging import log_event
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.durable.registry import durable_activity
from chemclaw.kg.note import Note

logger = logging.getLogger(__name__)


class JobRecord(BaseModel):
    """One finished connector job, in full: its arguments, its result, and its reason.

    Self-contained on purpose — reading a row back reconstructs the run without Temporal, without
    the launching conversation and without the knowledge graph. `payload` is the validated launch
    arguments (for a campaign, the entire decision space, objective, seed and round count) and
    `result` is the job's own `ConnectorJobResult.data`, so nothing about the run is left in a
    store that expires.

    `completed_at` is unset on the way in and filled by the database's own `now()`: a workflow
    cannot read a clock without breaking replay determinism, and the row's timestamp should come
    from the same clock that orders the rows anyway.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str = Field(min_length=1)
    connector: str = Field(min_length=1)
    job: str = Field(min_length=1)
    # Why this run was started, in the requester's terms. Empty means a declared procedure launched
    # by
    # name (a template, whose own `summary` says what it is for), never a forgotten field. Connector
    # jobs require a rationale at the launcher (`connectors/jobs.py`), not here.
    rationale: str = ""
    requested_by: str = Field(min_length=1)
    session_id: str = ""
    correlation_id: str = ""
    # The plan step and plan revision this run served, so a surface can tell a step waiting on a job
    # from a stalled plan. Empty means not launched from a plan step.
    plan_step: str = ""
    plan_hash: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    result: dict[str, Any] = Field(default_factory=dict)
    # The note this run produced, or "" — a join to the graph, and not proof the write landed:
    # it is copied off the result envelope, and the graph write that follows is best-effort.
    note_id: str = ""
    # The calculation keys the run rested on, from its envelope. A fact about the run, so kept
    # beside
    # `result` rather than inside it.
    calc_refs: list[str] = Field(default_factory=list)
    # Wall-clock seconds the run took, measured by the wrapper across the child workflow. Not
    # node-hours: no launcher reports parallelism back.
    runtime_seconds: float = Field(default=0.0, ge=0)
    # The name of the model `result` was dumped from, off the envelope's `payload_kind`, so the
    # backfill can route a composite. Empty means the run did not say; the projector infers.
    payload_kind: str = ""
    # How the run ended: `completed`, `failed` or `cancelled`. Failed and cancelled runs write a row
    # too. Defaults to `completed` because every older row is one, not because a caller may omit it.
    state: str = "completed"
    # The application's own account of why it failed (`connector_job.py::failure_reason`), written
    # for the chemist. Empty for a run that succeeded.
    failure_reason: str = ""
    completed_at: datetime | None = None


class JobRecordSummary(BaseModel):
    """A past run as a *listing* shows it: enough to recognise and recall it, no result blob.

    A second model rather than a trimmed `JobRecord`, because the two are read in different
    situations and the difference is the point: a search may match dozens of runs, and a campaign's
    `result` is its entire evaluation history. Handing that to the model for every hit would spend
    a context window to answer "which campaigns have we run?". The full record is one lookup away
    by `job_id` once a run is worth opening.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str
    connector: str
    job: str
    rationale: str
    summary: str
    note_id: str = ""
    # The plan step the run served, in the listing so "which step was this for" needs no second
    # lookup. Empty when the run was not launched from a plan step.
    plan_step: str = ""
    # How the run ended, so a failed run in `find_past_jobs` says it failed; the reason is in the
    # full
    # record.
    state: str = "completed"
    completed_at: datetime | None = None


class JobRecordSearch(BaseModel):
    """One search over the past runs: the hits, **and whether they are all of them**.

    Why this is not a bare `list`, which it was: the search is capped
    (`job_record_search_limit`), and a capped list that merely ended looks exactly like the
    complete answer. Measured against this table with 50 matching rows and the shipped cap of 20,
    `search_job_records("Suzuki")` returned 20 with no total, no flag and no cursor — so the
    21st-oldest matching campaign was invisible, on the one tool whose stated purpose is not paying
    twice for a run that already happened. "Have we optimized this coupling before?" came back
    "no" because a page had ended.

    Deliberately the same shape as `science.fingerprints.store.FingerprintSearch`, down to
    `hits_truncated` and a `computed_field` verdict, rather than a second answer to the same
    question: both are "have we seen this before?" tools, and both have an empty result that means
    two different things. `records_kept` is this seam's `index_empty` — a deployment that keeps no
    durable records answers every query with an empty list, which is not evidence that nothing was
    ever run.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    hits: list[JobRecordSummary] = Field(default_factory=list)
    # True when more rows matched than this page holds, so the count is a floor. Established by
    # fetching one extra row, not inferred from a full page.
    hits_truncated: bool = False
    # False when this deployment keeps no durable job records (`session_store != postgres`), so an
    # empty list says nothing about what has been run.
    records_kept: bool = True

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verdict(self) -> str:
        """The one sentence a reader must take from this result before drawing a conclusion.

        A `computed_field` so it is serialized with the hits; a plain property would be dropped by
        `model_dump()`.
        """
        if not self.records_kept:
            return (
                "This deployment keeps no durable job records, so this is not evidence that the "
                "run has not happened — nothing is recorded either way."
            )
        if not self.hits:
            return "No past run matches this query, out of every run this system has recorded."
        if self.hits_truncated:
            return (
                f"{len(self.hits)} past run(s) shown, the most recent first — more matched than "
                "this list holds, so the count is a floor and an older matching run may not "
                "appear. Narrow the query (or the connector) before concluding there is no "
                "precedent."
            )
        return f"{len(self.hits)} past run(s) matched, and that is all of them."


class JobRecordSink(Protocol):
    """Where a finished job's record goes. One method, so a test can be a list."""

    async def record(self, record: JobRecord) -> None:
        """Persist (or replace) the record for `record.job_id`."""
        ...


class NullJobRecordSink:
    """Drops records — the fallback for a deployment with no database configured."""

    async def record(self, record: JobRecord) -> None:
        """Log the run at debug level and keep nothing."""
        logger.debug("job record dropped (no durable store): %s", record.job_id)


def _records_are_durable() -> bool:
    """Whether this deployment keeps durable records at all.

    `session_store="postgres"` is the switch, the same one `default_audit_sink` reads.
    """
    return settings.session_store == "postgres"


def log_record_durability(component: str) -> None:
    """Say once, at worker start, whether this process actually keeps the records it writes.

    `record_job` is best-effort and the null sink drops silently, so without this a deployment on
    the in-memory store would report runs as recorded while keeping nothing. WARNING, since that is
    nearly always a misconfiguration.

    Args:
        component: What this process is, for the log line — as `serve_worker` names it.
    """
    if _records_are_durable():
        return
    log_event(
        logger,
        "job_records.not_kept",
        "%s: job records are dropped (CHEMCLAW_SESSION_STORE=%s); session push-back still writes",
        component,
        settings.session_store,
        level=logging.WARNING,
        component=component,
        session_store=settings.session_store,
    )


def default_job_record_sink() -> JobRecordSink:
    """The durable sink where a database exists, else the null one.

    The store is imported lazily so a memory-store process never pulls psycopg.
    """
    if not _records_are_durable():
        return NullJobRecordSink()
    from chemclaw.durable.job_record_store import PostgresJobRecordSink

    return PostgresJobRecordSink()


async def lookup_job_record(job_id: str) -> JobRecord | None:
    """The stored record for one job, or None when there is none (or no durable store)."""
    if not _records_are_durable():
        return None
    from chemclaw.durable.job_record_store import read_job_record

    return await read_job_record(job_id)


async def search_job_records(
    text: str = "", connector: str = "", limit: int | None = None, after: str = ""
) -> JobRecordSearch:
    """Past runs matching `text` (in the reason, the summary or the job name), newest first.

    Returns empty `hits` rather than raising when no durable store is configured, with
    `records_kept` False so that empty is distinguishable from an empty table.

    Args:
        text: Words to look for in the reason, the summary or the job name. Empty matches all.
        connector: Restrict to one bundle. Empty searches all.
        limit: Page size; `job_record_search_limit` when omitted.
        after: The `job_id` of the last row of the previous page — a keyset anchor, not an
            offset. Empty starts at the newest run.

    Returns:
        The page, carrying whether more matched than it holds.
    """
    if not _records_are_durable():
        return JobRecordSearch(records_kept=False)
    from chemclaw.durable.job_record_store import read_job_record_summaries

    return await read_job_record_summaries(
        text,
        connector,
        limit if limit is not None else settings.job_record_search_limit,
        after=after,
    )


@durable_activity("background")
@activity.defn
async def record_job(record: JobRecord) -> None:
    """Persist one finished job's record through the configured sink, and publish what it consumed.

    On the light background queue: one small write after the heavy work is done. Metrics are booked
    in the activity (a workflow body may replay) and after the write (the activity retries under
    `BAD_DATA_RETRY`), so they mean "a run was recorded". A lost completion report can still
    redeliver a committed write, so the counters read "at least once each"; the upsert on `job_id`
    keeps the row single.
    """
    await default_job_record_sink().record(record)
    # Booked here for the same reason; every run reaches this, failures included, so the `outcome`
    # label gives a success rate.
    record_metric(
        lambda m: m.increment(
            "chemclaw_jobs_finished_total",
            labels={"connector": record.connector, "outcome": record.state},
        )
    )
    if record.runtime_seconds:
        record_metric(
            lambda m: m.increment(
                "chemclaw_job_runtime_seconds_total",
                record.runtime_seconds,
                {"connector": record.connector},
            )
        )
        # A distribution beside the accumulating counter, so the tail (p95) of job cost is visible.
        record_metric(
            lambda m: m.observe(
                "chemclaw_job_duration_seconds",
                record.runtime_seconds,
                {"connector": record.connector},
            )
        )


def note_with_run_provenance(note: Note, record: JobRecord, *, ran_on: date | None = None) -> Note:
    """Return `note` with a footer naming the run that produced it and the reason it was started.

    Applied by core to every connector note, so the reason a job ran travels with its result. The
    footer carries no `[[wikilink]]`: the job id names a database row, not a graph node. `Note` is
    frozen, so this builds a copy.

    `ran_on` dates a note the connector left undated, so standing-query digests see it (an absent
    `valid_from` reads as open-ended, not news). A connector-supplied date is kept. The caller is
    workflow code, so the date comes from `workflow.now()`; `record.completed_at` is not set yet.
    """
    footer = (
        f"\nWhy this ran: {record.rationale}\n\n"
        f"- run: `{record.job_id}` ({record.connector}/{record.job})\n"
        f"- requested by: {record.requested_by}\n"
    )
    update: dict[str, object] = {"body": note.body.rstrip("\n") + "\n" + footer}
    if ran_on is not None and note.valid_from is None:
        update["valid_from"] = ran_on
    return note.model_copy(update=update)
