"""Agent tools that start the two durable subsystems nothing could reach (gaps RCH-1, RCH-2).

`DevelopmentReportWorkflow` (all of Phase 5b) was built, tested, and registered on the
background worker with **no caller anywhere** — no agent tool, no HTTP route, no Schedule — so
the only way to start it in a running deployment was the Temporal CLI. This is the missing
adapter, in the thin shape the QM launcher established (D-002): authorize → stamp the ambient
actor → deterministic workflow id → return the id immediately. No durable state
lives here; the agent never blocks. Completion reaches the chat through the existing push-back
channel (F3-T3).

**This shape is superseded and this module is shrinking.** The BO campaign that used to be its
second tool now lives in the `bo` connector bundle, declared as one `jobs:` entry over the
generic `ConnectorJobWorkflow` (D-111) — which is where a *new* durable capability goes.

The report deliberately did *not* follow it into a bundle (D-115): its dependency closure — the
graph, the retrievers, the embedding index — is what core keeps for `gather_evidence` anyway, so
the isolation a bundle exists to buy would be zero, and all that would remain is churn. What it
*did* adopt is the `ConnectorJobResult` envelope, because that is what `get_durable_job_status`
reads: without it the report was the one durable job a chemist could poll to `completed` and then
have no tool that hands over the answer.

`get_durable_job_status` stays here for good: it is generic over every durable job,
connector-owned or not, and it is now the **only** place a finished job's result is collected.
The QM/HPC job was the last one with a status tool of its own; it is a `qm` connector job as of
D-118, `agents/job_status.py` is gone, and so is the envelope-shaped exception this tool made
for it.
"""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from temporalio.client import WorkflowExecutionStatus
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

from chemclaw.agent.authz import authorize_trigger, require_actor
from chemclaw.agent.dialogue_tools import dry_run_notice, is_dry_run
from chemclaw.agent.tool_registry import tool
from chemclaw.agent.turn_signals import record_job_started
from chemclaw.core.config import settings
from chemclaw.core.ids import stable_hash
from chemclaw.core.temporal_client import connect
from chemclaw.durable.connector_job import ConnectorJobResult
from chemclaw.durable.job_record import JobRecordSummary, lookup_job_record, search_job_records
from chemclaw.durable.note_index import NoteReindexWorkflow
from chemclaw.durable.report_workflow import DevelopmentReportWorkflow
from chemclaw.retrieval.harness import ReportRequest, ReportSection


class DurableJobStatus(BaseModel):
    """What `get_durable_job_status` reports: where a job is, and what it produced.

    A model rather than the bare status word it used to return, because the connector seam made
    the follow-up question answerable: a job's result now arrives in one envelope
    (`ConnectorJobResult`), so the tool that reports "completed" can hand over the result in the
    same breath instead of leaving the model to ask again with no tool that answers.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str
    status: str
    summary: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    # Why the run was asked for, when the answer came from the durable record (D-157). Empty on the
    # live-Temporal path, which reads the workflow's result rather than the record — the launching
    # turn is right there in the conversation, so restating its own reason back to the model would
    # be noise; months later, when only the record survives, it is the whole point.
    rationale: str = ""


# Terminal Temporal statuses map to one word the model can act on, so a tool result never leaks
# SDK enum spelling into the conversation.
_TERMINAL = {
    WorkflowExecutionStatus.COMPLETED: "completed",
    WorkflowExecutionStatus.FAILED: "failed",
    WorkflowExecutionStatus.CANCELED: "cancelled",
    WorkflowExecutionStatus.TERMINATED: "terminated",
    WorkflowExecutionStatus.TIMED_OUT: "timed_out",
}


def _report_id(request: ReportRequest) -> str:
    """A deterministic id for a report request, so re-asking is idempotent (D-011 discipline).

    Keyed on the title *and* the section specs: two chemists asking for the same report get one
    run, while changing a section's query is genuinely a different report.
    """
    payload = [
        request.title,
        *(f"{s.heading}|{s.query}|{s.memory_layer}" for s in request.sections),
    ]
    return f"report-{stable_hash(payload)}"


@tool
async def request_development_report(title: str, sections: list[ReportSection]) -> str:
    """Start a durable development report and return its job id immediately.

    Drafts a multi-section report by retrieving evidence per section across every internal
    source, then opens the assembled draft as a PR-gated `report` note for human review.
    Long-running and resumable — it survives restarts — so this returns a job id rather than the
    report; poll it with `get_durable_job_status`. Re-requesting the same title and sections
    returns the existing job.

    Each section declares the memory layer it draws on, which keeps evidenced history and
    transferred analogy structurally apart in the draft:
    `evidence` (raw retrieved sources), `episodic` (past campaigns/runs), `semantic` (playbooks).

    Args:
        title: The report's title.
        sections: The sections to research, each a heading + the query it answers + its layer.

    Returns:
        The job id to poll for progress.
    """
    authorize_trigger("request_development_report")
    if is_dry_run():
        return dry_run_notice(
            "draft a development report", f"{title!r} with {len(sections)} section(s)"
        )
    request = ReportRequest(title=title, sections=sections)
    # `require_actor` is the core rule (F4-T3): under Entra, refuse durable work with no user.
    require_actor()
    client = await connect()
    workflow_id = _report_id(request)
    try:
        handle = await client.start_workflow(
            DevelopmentReportWorkflow.run,
            request,
            id=workflow_id,
            task_queue=settings.background_task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
        )
    except WorkflowAlreadyStartedError:
        # Same report already running or completed: hand back the existing id rather than
        # redrafting it (the QM tool's idempotency contract, applied here). Deliberately no
        # `job_started` signal: this run already existed (and may already be finished), so
        # announcing a start would be false. The generated connector-job launcher skips the
        # announcement on a duplicate for exactly the same reason.
        return workflow_id
    record_job_started(handle.id, "report")
    return handle.id


@tool
async def get_durable_job_status(job_id: str) -> DurableJobStatus:
    """Collect a durable job: its status, and its result once it has completed.

    This is the follow-up for **every** job id this system hands out — a connector job such as
    `compute_dft_energy`, `compute_reaction_energy` or `start_optimization_campaign`, a development
    report, or a calculation deferred because it was too slow to answer inside the turn. Poll it
    until the status is no longer `running`; a completed connector job carries its result with it,
    so there is no second call to make.

    It answers for **finished** jobs indefinitely, not only while Temporal remembers them: a
    completed connector job's result is also stored durably (D-157), so an id from months ago —
    found with `find_past_jobs`, or quoted from an old conversation — still returns its result
    after the workflow history has been retained away.

    Args:
        job_id: The id returned by any durable launcher.

    Returns:
        The status (running, completed, failed, cancelled, terminated, timed_out) and, once
        completed, the one-line `summary` plus the structured `result`. A job still running reports
        the status alone.

    Raises:
        ValueError: When the id is unknown to both Temporal and the durable record, or names a
            completed workflow whose result is not the connector envelope. That second case used to
            degrade to a bare status, because the HPC/DFT job returned its own typed result and had
            its own status tool (`agents/job_status.py`). It is a `qm` connector job now (D-118), so
            every durable job this system hands an id for returns the envelope — a result that is
            not one means the id belongs to a workflow no tool advertises, and reporting
            "completed" with an empty result would tell a chemist their calculation is done while
            silently withholding it.
    """
    client = await connect()
    handle = client.get_workflow_handle(job_id)
    try:
        description = await handle.describe()
    except RPCError as exc:
        # Temporal has never heard of this id — which, for a job that genuinely ran, means its
        # history has aged out rather than that it never existed. Ask the durable record before
        # telling a chemist their campaign does not exist.
        recorded = await _recorded_status(job_id)
        if recorded is None:
            raise ValueError(f"no durable job with id {job_id!r}") from exc
        return recorded
    status = _TERMINAL.get(description.status, "running") if description.status else "running"
    if status != "completed":
        return DurableJobStatus(job_id=job_id, status=status)
    return completed_job_status(job_id, await handle.result())


async def _recorded_status(job_id: str) -> DurableJobStatus | None:
    """The stored record for `job_id` as a status, or None when nothing was recorded.

    The record is only ever written for a run that *completed* (the workflow raises before
    reaching the write otherwise), so a row here means "completed", never a status this has to
    reconstruct.
    """
    record = await lookup_job_record(job_id)
    if record is None:
        return None
    return DurableJobStatus(
        job_id=job_id,
        status="completed",
        summary=record.summary,
        result=record.result,
        rationale=record.rationale,
    )


@tool
async def find_past_jobs(text: str = "", connector: str = "") -> list[JobRecordSummary]:
    """Find durable jobs this system has already run, and why each of them was run.

    The retrospective view over every finished campaign, calculation and report job — including
    ones from other people's conversations and from long before this one. Each hit carries the
    **reason the run was started**, so "have we optimized this coupling before, and what were we
    trying to find out?" is answerable without the original chat.

    Use it before launching an expensive job (the answer may already exist: re-running an identical
    job rejoins the stored result, but a *similar* one is a fresh bill), and when a chemist asks
    what has been tried. Take the `job_id` of a promising hit to `get_durable_job_status` for that
    run's full result — for a BO campaign, every candidate it evaluated, not only the winner.

    Args:
        text: Words to look for in the recorded reason, the result summary or the job name.
            Empty returns the most recent runs.
        connector: Restrict to one capability bundle (e.g. "bo", "qm"). Empty searches all.

    Returns:
        The matching runs, newest first: what ran, why, what came out in one line, and the note it
        proposed (if any).
    """
    return await search_job_records(text, connector)


def completed_job_status(job_id: str, raw: Any) -> DurableJobStatus:
    """Decode a finished durable job's raw result into the status this system reports.

    Extracted so that "a finished job's result is collected in exactly one place" — which this
    module's docstring claims and which D-118 made true — survives having a second waiter. The
    other caller is `chemclaw.agent.job_results`, the mid-turn resume: it waits on the workflow
    handle rather than polling a status, but what it must do with the answer is identical, and a
    second copy of this decode is how the two would come to disagree about what "completed" means.

    Args:
        job_id: The job the result belongs to, for the status and for the error message.
        raw: Whatever the workflow returned, undecoded.

    Raises:
        ValueError: When the result is not the connector envelope.
    """
    try:
        envelope = ConnectorJobResult.model_validate(raw)
    except ValidationError as exc:
        # Hard, not a degraded status. The single exception this branch tolerated was the QM/HPC
        # job, which is a connector job now — so every launcher this system exposes produces the
        # envelope, and a result that is not one is a foreign workflow id. Returning "completed"
        # with no result for it would report a finished calculation while withholding the answer,
        # which is the failure mode the envelope was adopted to end (D-118).
        raise ValueError(
            f"durable job {job_id!r} completed but did not return the connector job envelope; "
            "the id does not belong to a job any launcher in this system started"
        ) from exc
    return DurableJobStatus(
        job_id=job_id, status="completed", summary=envelope.summary, result=envelope.data
    )


async def request_note_reindex() -> str:
    """Start a note-index rebuild now, returning the workflow id (gap SCH-6).

    Deliberately **not** an agent tool: this is an operational trigger for a merge webhook, not
    a capability the model should reach for mid-conversation. A deterministic id per calendar
    minute collapses a burst of merge notifications into one rebuild — a git host can deliver
    several within seconds, and rebuilding the whole index once per merge would be pure waste.
    """
    client = await connect()
    # `workflow.now()` is unavailable outside a workflow, and the id must be stable within a
    # short window rather than unique per call, so the minute bucket comes from the
    # Temporal-independent clock here at the (non-durable) entry point.
    bucket = datetime.now(UTC).strftime("%Y%m%d%H%M")
    workflow_id = f"note-reindex-{bucket}"
    try:
        handle = await client.start_workflow(
            NoteReindexWorkflow.run,
            id=workflow_id,
            task_queue=settings.background_task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
        )
    except WorkflowAlreadyStartedError:
        return workflow_id  # a rebuild for this minute is already running or done
    return handle.id
