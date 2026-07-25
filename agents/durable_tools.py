"""Agent tools that start the two durable subsystems nothing could reach (gaps RCH-1, RCH-2).

`DevelopmentReportWorkflow` (all of Phase 5b) and `BoCampaignWorkflow` (Phase 1d) were built,
tested, and registered on the background worker — with **no caller anywhere**: no agent tool, no
HTTP route, no Schedule. In a running deployment the only way to start either was the Temporal CLI.
`skills/experiment-design/SKILL.md` even directs the agent at the durable campaign, so the agent was
being pointed at a capability it had no way to invoke.

These are the missing adapters, deliberately the *same* thin shape as `agents/qm_tools.py` (D-002):
authorize → stamp the ambient actor → deterministic workflow id → return the id immediately. No
durable state lives here; the agent never blocks. Completion reaches the chat through the existing
job→session push-back channel (F3-T3), exactly as a QM job does.

Why one module for both: they are the same three-line adapter over two workflows, and splitting them
would duplicate the id-derivation and status-mapping helpers for no gain (DRY, Rule of Three).
"""

from temporalio.client import WorkflowExecutionStatus
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

from agents.authz import authorize_trigger, require_actor
from bo.problem import CampaignSpec, require_rounds_within_ceiling
from chemclaw.config import settings
from chemclaw.ids import stable_hash
from chemclaw.temporal_client import connect
from report.harness import ReportRequest, ReportSection
from workflows.bo_campaign import BoCampaignWorkflow
from workflows.report_workflow import DevelopmentReportWorkflow

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


def _campaign_id(spec: CampaignSpec) -> str:
    """A deterministic id for a campaign spec — a duplicate submit joins the existing run."""
    return (
        f"bo-{stable_hash([spec.objective_name, spec.problem.model_dump_json(), str(spec.seed)])}"
    )


async def _status_of(workflow_id: str, kind: str) -> str:
    """Map a durable run's Temporal status to one word, or raise a clear error on an unknown id."""
    client = await connect()
    handle = client.get_workflow_handle(workflow_id)
    try:
        description = await handle.describe()
    except RPCError as exc:
        raise ValueError(f"no {kind} with id {workflow_id!r}") from exc
    return _TERMINAL.get(description.status, "running") if description.status else "running"


async def request_development_report(title: str, sections: list[ReportSection]) -> str:
    """Start a durable development report and return its job id immediately.

    Drafts a multi-section report by retrieving evidence per section across every internal source,
    then opens the assembled draft as a PR-gated `report` note for human review. Long-running and
    resumable — it survives restarts — so this returns a job id rather than the report; poll it with
    `get_durable_job_status`. Re-requesting the same title and sections returns the existing job.

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
        # redrafting it (the QM tool's idempotency contract, applied here).
        return workflow_id
    return handle.id


async def start_optimization_campaign(spec: CampaignSpec) -> str:
    """Start a durable multi-round Bayesian-optimization campaign; return its job id immediately.

    Use this when the optimization needs *many* rounds of propose→evaluate against a computable
    objective and must survive restarts. For a single "what should I run next?" answer over
    observations you already have, use `suggest_next_experiment` instead — it answers inline.

    The campaign proposes candidates, evaluates them through the named objective, and (when
    `publish_to_graph` is set) opens its recommendation as a PR-gated note. Poll with
    `get_durable_job_status`; an identical spec returns the existing job id.

    Args:
        spec: The campaign: the decision space, the registered objective name, seed/round/batch
            budget, and whether to publish the recommendation to the knowledge graph.

    Returns:
        The job id to poll for progress.
    """
    authorize_trigger("start_optimization_campaign")
    # Enforce the round ceiling at the creation entry point — deliberately not in the model, whose
    # validators must stay replay-stable across a config change (see `CampaignSpec`).
    require_rounds_within_ceiling(spec.n_rounds)
    require_actor()
    client = await connect()
    workflow_id = _campaign_id(spec)
    try:
        handle = await client.start_workflow(
            BoCampaignWorkflow.run,
            spec,
            id=workflow_id,
            task_queue=settings.background_task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
        )
    except WorkflowAlreadyStartedError:
        return workflow_id
    return handle.id


async def get_durable_job_status(job_id: str) -> str:
    """Check a durable report or optimization campaign: `running`, `completed`, `failed`, ….

    Args:
        job_id: The id returned by `request_development_report` or
            `start_optimization_campaign`.

    Returns:
        One word: running, completed, failed, cancelled, terminated, or timed_out.
    """
    return await _status_of(job_id, "durable job")
