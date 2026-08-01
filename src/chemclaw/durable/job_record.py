"""The durable record of one finished connector job — what ran, on what, and **why** (D-157).

Why this exists: a durable job's result lived in exactly one place, the Temporal workflow result,
and Temporal is an execution engine rather than an archive. A closed workflow's history expires
with the namespace's retention window, taking the result with it — for a multi-round BO campaign,
the best point *and every intermediate observation*, which is the part that cost real compute. The
knowledge graph held only what a job chose to publish (a recommendation, PR-gated and opt-in), and
`audit_events` held the fact of a tool call. Between them, three questions had no answer once the
history aged out: *what did that campaign actually try*, *how do I find it from a later session*,
and — for every job this system runs, not just BO — *why was it run at all*.

So the record is written by core's `ConnectorJobWorkflow` for **every** connector job, not by each
connector. That placement is the same rule the PR-gate and the actor stamp follow: an obligation
that must hold for every capability belongs to the one wrapper they all run inside, because "each
connector remembers" is precisely the discipline that fails silently.

**The sink is durable by default** (`default_job_record_sink`), for the reason
`chemclaw.agent.audit.default_audit_sink` is: opting *in* to a record, per call site, is the wrong
polarity — a forgotten argument must not quietly downgrade it. A deployment with no Postgres falls
back to the null sink and loses nothing it had before.
"""

import logging
from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field
from temporalio import activity

from chemclaw.core.config import settings
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
    # Why this run was started, in the requester's terms. The one thing no other store holds.
    rationale: str = Field(min_length=1)
    requested_by: str = Field(min_length=1)
    session_id: str = ""
    correlation_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    result: dict[str, Any] = Field(default_factory=dict)
    # The note this run proposed, or "" — a join to the graph, not proof of a merge.
    note_id: str = ""
    # Wall-clock seconds the run took, measured by the wrapper across the child workflow. The row
    # said what ran and why and nothing about what it cost, so a two-second xTB call and a six-hour
    # DFT run were one row shape and one increment of `chemclaw_jobs_started_total` — on the most
    # expensive thing this system does, "how many" was the only number anyone had. Not node-hours:
    # parallelism belongs to the launcher and none reports it back yet. Runtime is the factor
    # node-hours multiplies, and it is measurable today.
    runtime_seconds: float = Field(default=0.0, ge=0)
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
    completed_at: datetime | None = None


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

    `session_store="postgres"` is the deployment's statement that a database exists and durable
    records belong in it — the same switch `default_audit_sink` reads, named here rather than
    spelled out at each of the three entry points below so they cannot drift apart.
    """
    return settings.session_store == "postgres"


def default_job_record_sink() -> JobRecordSink:
    """The durable sink where a database exists, else the null one.

    The store is imported lazily so a memory-store process (the CLI, the tests, a connector
    worker) never pulls psycopg for a store it will not use.
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
    text: str = "", connector: str = "", limit: int | None = None
) -> list[JobRecordSummary]:
    """Past runs matching `text` (in the reason, the summary or the job name), newest first.

    Returns an empty list rather than raising when no durable store is configured: "we have no
    record of past runs" is the honest answer for such a deployment, and it is the same answer the
    caller gets from an empty table.
    """
    if not _records_are_durable():
        return []
    from chemclaw.durable.job_record_store import read_job_record_summaries

    return await read_job_record_summaries(
        text, connector, limit if limit is not None else settings.job_record_search_limit
    )


@durable_activity("background")
@activity.defn
async def record_job(record: JobRecord) -> None:
    """Persist one finished job's record through the configured sink, and publish what it consumed.

    On the light background queue with core's other bookkeeping: it is one small write, and the
    heavy work it describes is already done by the time it runs.

    The metric is booked here, in the activity, rather than in the workflow: a workflow body may be
    replayed, and a replayed increment would count one expensive run several times — the arithmetic
    error a consumption counter must not make.

    **And it is booked after the write, for exactly the reason `chemclaw_notes_proposed_total` is
    (`kg/pr_gate.py`).** "An activity's side effects happen once per successful execution" is the
    guarantee this counter needs, and it is a guarantee only about the code that runs *after* the
    part which can fail: this activity runs under `BAD_DATA_RETRY`, so an increment at the top is
    booked once per *attempt*. The everyday case is not an outage — the upsert commits and the
    activity then overruns `job_record_timeout_seconds`, so Temporal retries a run that is already
    recorded and one run is counted twice. In a sustained outage all five attempts increment, the
    wrapper swallows the resulting `ActivityError`, and the counter reports five times the compute
    for a run with no durable record at all. Counting after the awaited write makes the number mean
    "a run was recorded", which is the only claim it can honestly make; the upsert keys on
    `job_id`, so a retry after a committed-but-timed-out write replaces the row and increments once.
    """
    await default_job_record_sink().record(record)
    if record.runtime_seconds:
        record_metric(
            lambda m: m.increment(
                "chemclaw_job_runtime_seconds_total",
                record.runtime_seconds,
                {"connector": record.connector},
            )
        )


def note_with_run_provenance(note: Note, record: JobRecord) -> Note:
    """Return `note` with a footer naming the run that produced it and the reason it was started.

    **Applied by core to every connector note**, which is the whole point: the reason a job ran is
    the one thing a merged markdown note could never say, and asking each connector to append it
    would guarantee that some connector does not. A reader months later gets *why this was done*
    from the same file that says what came out, with no second store to consult — and a reviewer
    sees the reason on the PR they are being asked to sign.

    The footer carries **no `[[wikilink]]`**, deliberately. A link to a note that does not exist
    fails `chemclaw.kg.validate` on the very PR this note opens, and the job id names a database
    row rather than a graph node; it is rendered as code so it stays a literal.

    `Note` is frozen, so this builds a copy — which also leaves the connector's own object intact
    for the result envelope the launching tool hands back.
    """
    footer = (
        f"\nWhy this ran: {record.rationale}\n\n"
        f"- run: `{record.job_id}` ({record.connector}/{record.job})\n"
        f"- requested by: {record.requested_by}\n"
    )
    return note.model_copy(update={"body": note.body.rstrip("\n") + "\n" + footer})
