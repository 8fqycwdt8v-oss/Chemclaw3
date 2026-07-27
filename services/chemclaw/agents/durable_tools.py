"""Agent tools that start the two durable subsystems nothing could reach (gaps RCH-1, RCH-2).

`DevelopmentReportWorkflow` (all of Phase 5b) was built, tested, and registered on the
background worker with **no caller anywhere** — no agent tool, no HTTP route, no Schedule — so
the only way to start it in a running deployment was the Temporal CLI. This is the missing
adapter, deliberately the *same* thin shape as `agents/qm_tools.py` (D-002): authorize → stamp
the ambient actor → deterministic workflow id → return the id immediately. No durable state
lives here; the agent never blocks. Completion reaches the chat through the existing push-back
channel (F3-T3).

**This shape is superseded and this module is shrinking.** The BO campaign that used to be its
second tool now lives in the `bo` connector bundle, declared as one `jobs:` entry over the
generic `ConnectorJobWorkflow` (D-110) — which is where a *new* durable capability goes.

The report deliberately did *not* follow it into a bundle (D-114): its dependency closure — the
graph, the retrievers, the embedding index — is what core keeps for `gather_evidence` anyway, so
the isolation a bundle exists to buy would be zero, and all that would remain is churn. What it
*did* adopt is the `ConnectorJobResult` envelope, because that is what `get_durable_job_status`
reads: without it the report was the one durable job a chemist could poll to `completed` and then
have no tool that hands over the answer.

`get_durable_job_status` stays here for good: it is generic over every durable job,
connector-owned or not, and it is now the one place a finished job's result is collected.
"""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from temporalio.client import WorkflowExecutionStatus
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

from agents.authz import authorize_trigger, require_actor
from agents.dialogue_tools import dry_run_notice, is_dry_run
from agents.tool_registry import tool
from agents.turn_signals import record_job_started
from chemclaw.config import settings
from chemclaw.ids import stable_hash
from chemclaw.temporal_client import connect
from report.harness import ReportRequest, ReportSection
from workflows.connector_job import ConnectorJobResult
from workflows.note_index import NoteReindexWorkflow
from workflows.report_workflow import DevelopmentReportWorkflow


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


# Terminal Temporal statuses map to one word the model can act on, so a tool result never leaks
# SDK enum spelling into the conversation. Mirrors `qm_tools._STATUS`.
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


async def _status_of(workflow_id: str, kind: str) -> str:
    """Map a durable run's Temporal status to one word, or raise a clear error on an unknown id."""
    client = await connect()
    handle = client.get_workflow_handle(workflow_id)
    try:
        description = await handle.describe()
    except RPCError as exc:
        raise ValueError(f"no {kind} with id {workflow_id!r}") from exc
    return _TERMINAL.get(description.status, "running") if description.status else "running"


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
        # announcing a start would be false. Mirrors `submit_qm_job`, which for the same reason
        # does not re-mark an awaiting todo on a duplicate submit.
        return workflow_id
    record_job_started(handle.id, "report")
    return handle.id


@tool
async def get_durable_job_status(job_id: str) -> DurableJobStatus:
    """Collect a durable job: its status, and its result once it has completed.

    This is the follow-up for **every** job id this system hands out — a connector job such as
    `compute_reaction_energy` or `start_optimization_campaign`, a development report, or a
    calculation deferred because it was too slow to answer inside the turn. Poll it until the
    status is no longer `running`; a completed connector job carries its result with it, so there
    is no second call to make.

    Args:
        job_id: The id returned by any durable launcher.

    Returns:
        The status (running, completed, failed, cancelled, terminated, timed_out) and, once
        completed, the one-line `summary` plus the structured `result`. A job still running reports
        the status alone, as does an HPC/DFT job — `submit_qm_job` returns its own richer shape, so
        collect that one with `get_job_status`.
    """
    client = await connect()
    handle = client.get_workflow_handle(job_id)
    try:
        description = await handle.describe()
    except RPCError as exc:
        raise ValueError(f"no durable job with id {job_id!r}") from exc
    status = _TERMINAL.get(description.status, "running") if description.status else "running"
    if status != "completed":
        return DurableJobStatus(job_id=job_id, status=status)
    raw = await handle.result()
    try:
        envelope = ConnectorJobResult.model_validate(raw)
    except ValidationError:
        # A completed job whose result is not the envelope — today only the QM/HPC job, which
        # has its own typed result and its own status tool. The status is still the honest
        # answer; the shape is not one this tool reads, and inventing a summary would be worse.
        return DurableJobStatus(job_id=job_id, status=status)
    return DurableJobStatus(
        job_id=job_id, status=status, summary=envelope.summary, result=envelope.data
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
