"""Agent tools that start core-owned durable workflows and report any durable job's status.

Each launcher is a thin adapter: authorize, stamp the ambient actor, derive a deterministic
workflow id, return it immediately. Nothing here stores durable state and the agent never
blocks; completion reaches the chat through the push-back channel. The ids and reuse policies
written here decide who rejoins whose run, so read `_report_id` before changing what goes into
an id.

A new durable capability belongs in a connector bundle as a `jobs:` entry, not here. The report
stays in core because its dependencies are what core already carries for `gather_evidence`.
`get_durable_job_status` is generic over every durable job; decoding a result is
`durable.connector_job.envelope_from_result`'s job.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from langchain.tools import ToolRuntime
from pydantic import BaseModel, ConfigDict, Field
from temporalio.api.enums.v1 import PendingActivityState, TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client, WorkflowExecutionDescription, WorkflowExecutionStatus
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.types import MethodAsyncNoParam

from chemclaw.agent.authz import authorize_trigger, require_actor
from chemclaw.agent.framing import frame_untrusted
from chemclaw.agent.tool_framing import defanged_payload
from chemclaw.connectors.jobs import failed_job_reason
from chemclaw.connectors.queued_workflow import QueuedToolWorkflow
from chemclaw.core.config import settings
from chemclaw.core.errors import SubsystemUnavailableError
from chemclaw.core.identity_context import get_current_correlation_id, get_current_roles
from chemclaw.core.ids import canonical_text, stable_hash
from chemclaw.core.session_context import get_current_session_id
from chemclaw.core.temporal_client import connect
from chemclaw.core.tool_registry import polls_moving_state, tool
from chemclaw.core.turn_signals import record_job_started
from chemclaw.durable.connector_job import envelope_from_result
from chemclaw.durable.hypothesis_tournament import (
    HypothesisTournamentWorkflow,
    TournamentRequest,
)
from chemclaw.durable.job_record import JobRecordSearch, lookup_job_record, search_job_records

# Importing workflow types type-checks `start_workflow` at the site that decides durable identity.
# Allowed only for core workflows; a bundle's workflow is reached by name (enforced by
# `tests/test_layering.py`).
from chemclaw.durable.memory_jobs import (
    CampaignSynthesisWorkflow,
    OptimizationCampaignWorkflow,
    PlaybookDistillationWorkflow,
)
from chemclaw.durable.observation_jobs import ObservationPromotionWorkflow
from chemclaw.durable.report_workflow import EXHIBIT_SESSION, DevelopmentReportWorkflow
from chemclaw.retrieval.harness import ReportRequest, ReportSection
from chemclaw.science.calc.geometry import without_geometry

logger = logging.getLogger(__name__)


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
    # The calculations this run rested on, in the form `record_knowledge_note` takes. Empty for a
    # job that recorded none.
    calc_refs: list[str] = Field(default_factory=list)
    # Why the run was asked for, when the answer came from the durable record. Empty on the
    # live-Temporal path, where the launching turn is still in the conversation.
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
    """A deterministic id for a report request, so re-asking is idempotent.

    Keyed on title, sections, the requester and their roles. The entitlement half is access
    control: `job_status()` applies no owner check, and sections read entitlement-gated sources, so
    an id two principals could derive would let one collect a report built from the other's corpus.
    Idempotency is therefore per actor.

    The model-written half (title, headings, queries) is whitespace-collapsed and casefolded and the
    sections sorted, so trivial rewordings do not launch a second expensive run; the first
    requester's casing is what renders. `requested_by`, `requested_roles` and `memory_layer` stay
    byte-exact, since folding them would merge actors.
    """
    payload = [
        canonical_text(request.title),
        *sorted(
            f"{canonical_text(s.heading)}|{canonical_text(s.query)}|{s.memory_layer}"
            for s in request.sections
        ),
        request.requested_by,
        *sorted(request.requested_roles),
    ]
    return f"report-{stable_hash(payload)}"


@tool
async def request_development_report(title: str, sections: list[ReportSection]) -> str:
    """Start a durable development report and return its job id immediately.

    Drafts a multi-section report by retrieving evidence per section across every internal
    source, then records the assembled draft as a `report` note, readable at once.
    Long-running and resumable — it survives restarts — so this returns a job id rather than the
    report; poll it with `get_durable_job_status`. Re-requesting the same title and sections
    returns the existing job — matched on meaning, not on bytes, so re-ordered sections and
    differences of case or spacing rejoin the run rather than starting a second one.

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
    # `require_actor`: under Entra, refuse durable work with no user. The result travels on the
    # request as `ReportRequest.requested_by`.
    request = ReportRequest(
        title=title,
        sections=sections,
        requested_by=require_actor(),
        requested_roles=sorted(get_current_roles()),
        # Joins the run's logs and draft back to the asking turn; empty (not minted) outside a turn.
        correlation_id=get_current_correlation_id() or "",
        # The conversation the finished draft is shown in, as a document artefact.
        session_id=get_current_session_id() or "",
    )
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
        # Already running or completed: return the existing id, and send no `job_started` signal,
        # since
        # announcing a start would be false.
        return workflow_id
    record_job_started(handle.id, "report")
    return handle.id


# The four corpus-scanning jobs this tool can start, by the word a chemist would use. They write
# knowledge, so they run on demand rather than on a Schedule.
MemoryJobKind = Literal["campaign", "playbook", "optimization", "observation-promotion"]

# Typed as Temporal's no-argument workflow method so `.run` keeps type-checking; a dict of
# heterogeneous classes would degrade to `type[object]`.
_MEMORY_JOBS: dict[MemoryJobKind, MethodAsyncNoParam[Any, list[str]]] = {
    "campaign": CampaignSynthesisWorkflow.run,
    "playbook": PlaybookDistillationWorkflow.run,
    "optimization": OptimizationCampaignWorkflow.run,
    "observation-promotion": ObservationPromotionWorkflow.run,
}


@tool
async def synthesize_memory(  # noqa: D417 - `runtime` is deliberately not in `Args:`; see below
    kind: MemoryJobKind, runtime: ToolRuntime[Any, Any], fresh: bool = False
) -> str:
    """Mine the reaction corpus for a class of knowledge and record what it finds.

    Use this when someone asks what the corpus now supports — "have we accumulated enough on this
    route to write it up", "what campaigns are in the record", "is anything worth distilling" —
    or after a large ELN ingest. Each kind re-reads **every** reaction from the configured ingest
    sources and records notes directly, so nothing decides what becomes
    knowledge; this only decides *when to look*.

    Nothing runs these on a timer (D-2026-08-25). Knowledge arriving unbidden is what that
    decision removed — so the corpus is mined when a person has a reason, and this tool is that
    reason arriving.

    The kinds:

    - `campaign` — narrate chains of experiments where one run's product is the next one's
      reactant, citing every member.
    - `playbook` — distil a transformation that recurs *across projects* into reusable judgment.
    - `optimization` — group same-transformation runs into a screen and read it as a series.
    - `observation-promotion` — write playbook notes for the ungated observations that have
      crossed both support thresholds. The mining that feeds it still runs on a timer; only this
      half puts what it found into the graph.

    Args:
        kind: Which synthesis to run.
        fresh: Force a new run even when one already ran today. The default deduplicates by UTC
            day — two chemists asking the same morning share one scan — but the tool's own
            recommended use ("after a large ELN ingest") is exactly the case where rejoining the
            morning's run silently reports on the *pre-ingest* corpus. Pass true when the corpus
            has changed since the day's first run.

    Returns:
        The job id. Poll it with `get_durable_job_status`; the result is the list of notes
        recorded, which may be empty when the corpus supports nothing new.
    """
    authorize_trigger("synthesize_memory")
    # `require_actor` before anything durable starts: these jobs write attributed notes.
    actor = require_actor()
    client = await connect()
    # The tool call's own id is what is identical on a replay and different between two
    # genuine asks — see `_memory_job_id` for why `fresh` may not read the clock.
    workflow_id = _memory_job_id(kind, fresh=fresh, discriminator=str(runtime.tool_call_id))
    try:
        handle = await client.start_workflow(
            _MEMORY_JOBS[kind],
            id=workflow_id,
            task_queue=settings.background_task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
        )
    except WorkflowAlreadyStartedError:
        # Today's run of this kind already exists (see `_memory_job_id`): return its id, with no
        # `job_started` signal.
        return workflow_id
    logger.info("memory synthesis %s started by %s as %s", kind, actor, handle.id)
    record_job_started(handle.id, "memory-synthesis")
    return handle.id


def _memory_job_id(kind: MemoryJobKind, *, fresh: bool = False, discriminator: str = "") -> str:
    """A deterministic id for one kind's synthesis, keyed on the UTC date.

    The input is the whole corpus, so same-day asks share one scan and do not write a finding twice.
    The cost is that a second ask the same day misses data ingested in between; `fresh` forces a
    real re-mine. `discriminator` makes a fresh id a function of its inputs — the tool call id is
    the
    same on a replay and different between asks — so a resumed call rejoins rather than duplicates.
    Empty falls back to the clock.
    """
    day = datetime.now(UTC).date().isoformat()
    if not fresh:
        return f"memory-{kind}-{day}"
    suffix = discriminator or datetime.now(UTC).strftime("%H%M%S")
    return f"memory-{kind}-{day}-{stable_hash(suffix, chars=10)}"


@tool
@polls_moving_state
async def get_durable_job_status(job_id: str) -> DurableJobStatus:
    """Collect a durable job: its status, and its result once it has completed.

    This is the follow-up for **every** job id this system hands out — a connector job such as
    `compute_reaction_energy`, `sample_conformers` or `start_optimization_campaign`, a development
    report, or a calculation deferred because it was too slow to answer inside the turn. Poll it
    until the status is neither `queued` nor `running`; a completed connector job carries its
    result with it, so there is no second call to make. `queued` means no worker has it yet.

    It answers for **finished** jobs indefinitely, not only while Temporal remembers them: a
    finished run's result is also stored durably (D-157), so an id from months ago — found with
    `find_past_jobs`, or quoted from an old conversation — still returns its result after the
    workflow history has been retained away.

    That was true of connector jobs alone and this sentence claimed it of everything. A template
    run (`run_*`) wrote no `job_records` row at all, because `record_job` had one caller in the
    tree, so its id answered only until Temporal retained the history away and then answered
    `null` — for the nine shipped procedures, one of whose whole product is a written brief.
    `durable/template_job.py` records one now, on the success path and the failure path both.

    Args:
        job_id: The id returned by any durable launcher.

    Returns:
        The status (queued, running, completed, failed, cancelled, terminated, timed_out) and, once
        completed, the one-line `summary`, the structured `result`, and `calc_refs` — the
        calculation keys the run rested on, which `record_knowledge_note` takes so a conclusion
        drawn from this job stays traceable to what computed it. A job still running reports the
        status alone.

        A geometry in the result is reported by its `structure_id` rather than by its coordinates.
        That address is what the next calculation takes: pass it to `optimize_geometry`,
        `compute_thermochemistry` or `scan_coordinate` to carry one chosen conformer forward
        instead of starting again from the molecule.

    Raises:
        ValueError: When the id is unknown, or names a completed workflow whose result is not the
            connector envelope — the id belongs to a workflow no tool advertises, and reporting it
            as completed with an empty result would say a calculation is done while withholding it.
    """
    # Every durable job returns the connector envelope, so a completed status decodes through
    # `completed_job_status`.
    status = await job_status(job_id, wait_seconds=settings.job_status_wait_seconds)
    # Neutralised here at the model's edge, not in `job_status`, which also serves `GET /jobs/{id}`
    # where markup would be noise. `rationale` and `summary` are framed (people and models wrote
    # them,
    # and `find_past_jobs` exposes every id to everyone). `result` is defanged rather than framed:
    # it
    # is structured but carries requester-chosen strings, with no span to wrap.
    return status.model_copy(
        update={
            "summary": _framed_free_text(status.summary or "", job_id) or None,
            "rationale": _framed_free_text(status.rationale, job_id),
            "result": defanged_payload(status.result),
        }
    )


async def job_status(job_id: str, *, wait_seconds: float = 0.0) -> DurableJobStatus:
    """One durable job's status, from Temporal while it remembers and the record afterwards.

    Shared by the tool and `GET /jobs/{id}` so chat and UI cannot disagree about a run.
    `wait_seconds` is a Temporal long-poll, used by the tool because a model poll costs a whole
    turn;
    the HTTP route passes nothing. A RUNNING run that no worker has started reads `queued`, with the
    reason as its summary.
    """
    client = await connect()
    handle = client.get_workflow_handle(job_id)
    try:
        description = await handle.describe()
    except RPCError as exc:
        # NOT_FOUND only: any other RPC status is an outage and must not read as "no such job".
        if exc.status is not RPCStatusCode.NOT_FOUND:
            raise SubsystemUnavailableError(
                f"the durable subsystem did not answer for job {job_id!r} ({exc.status.name})"
            ) from exc
        # An unknown id usually means the history aged out; consult the durable record before saying
        # the
        # job does not exist.
        recorded = await _recorded_status(job_id)
        if recorded is None:
            raise ValueError(f"no durable job with id {job_id!r}") from exc
        return recorded
    status = _TERMINAL.get(description.status, "running") if description.status else "running"
    if status == "running" and wait_seconds > 0:
        try:
            result = await asyncio.wait_for(handle.result(), wait_seconds)
        except TimeoutError:
            return await _still_open(client, job_id)
        except Exception:
            # The run ended unsuccessfully while we waited (`handle.result()` raises); re-describe
            # once and
            # report it via the no-wait mapping.
            refreshed = await handle.describe()
            ended = _TERMINAL.get(refreshed.status, "running") if refreshed.status else "running"
            return DurableJobStatus(job_id=job_id, status=ended)
        return completed_job_status(job_id, result)
    if status == "running":
        # Still running: return before `failed_job_reason`, whose `handle.result()` would be an
        # unbounded
        # long-poll on an open execution.
        return _open_status(job_id, await _not_started_reason(client, description))
    if status != "completed":
        # Render the failure cause, not just the status word, via the shared `failed_job_reason`;
        # safe
        # because open runs returned above.
        return DurableJobStatus(
            job_id=job_id, status=status, summary=await failed_job_reason(handle) or None
        )
    return completed_job_status(job_id, await handle.result())


async def _still_open(client: Client, job_id: str) -> DurableJobStatus:
    """A run the wait did not see finish, described afresh: the first description is stale now."""
    try:
        description = await client.get_workflow_handle(job_id).describe()
    except RPCError:
        return DurableJobStatus(job_id=job_id, status="running")
    return _open_status(job_id, await _not_started_reason(client, description))


def _open_status(job_id: str, waiting: str | None) -> DurableJobStatus:
    """`queued` with its reason when nothing has started the run, `running` otherwise."""
    if waiting is None:
        return DurableJobStatus(job_id=job_id, status="running")
    return DurableJobStatus(job_id=job_id, status="queued", summary=waiting)


async def _not_started_reason(
    client: Client, description: WorkflowExecutionDescription
) -> str | None:
    """Why an open run is not running yet, or None when something has it (or it cannot be told).

    A started activity means running. Otherwise two states are waiting:

    * a queued tool call whose activity is scheduled but not started is waiting for a slot (the
      same reading `connectors/queued.py::_progress` gives the turn's card);
    * a run whose task queue has no poller cannot be advanced by anything.

    If the queue cannot be asked, the answer stays `running`.
    """
    pending = description.raw_description.pending_activities
    started = PendingActivityState.PENDING_ACTIVITY_STATE_STARTED
    if any(activity.state == started for activity in pending):
        return None
    if pending and description.workflow_type == QueuedToolWorkflow.__name__:
        return "waiting for a free slot on its connector; it starts when one opens"
    queue = description.task_queue
    try:
        answer = await client.workflow_service.describe_task_queue(
            DescribeTaskQueueRequest(
                namespace=client.namespace,
                task_queue=TaskQueue(name=queue),
                task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW,
            ),
            timeout=timedelta(seconds=settings.connector_health_timeout_seconds),
        )
    except Exception:
        # Broad by contract (the docstring): every failure here means "could not tell".
        logger.debug("could not ask whether %r is polled", queue, exc_info=True)
        return None
    if answer.pollers:
        return None
    return (
        f"no worker is polling {queue!r}, so nothing has started this job; it runs when a worker "
        "for that queue is up"
    )


async def _recorded_status(job_id: str) -> DurableJobStatus | None:
    """The stored record for `job_id` as a status, or None when nothing was recorded.

    Records are written for failed runs too, so the record's own state is reported, with
    `failure_reason` as the summary of a failed row.
    """
    record = await lookup_job_record(job_id)
    if record is None:
        return None
    return DurableJobStatus(
        job_id=job_id,
        status=record.state,
        summary=record.summary or (record.failure_reason or None),
        calc_refs=record.calc_refs,
        # Projected on the way out too, for old rows that still hold whole geometries; idempotent.
        result=readable_in_this_session(without_geometry(record.result)),
        rationale=record.rationale,
    )


def _framed_free_text(text: str, job_id: str) -> str:
    """One free-text field of a past run, wrapped as data and attributed to the run that wrote it.

    `find_past_jobs` returns other people's text verbatim, months later, so it is framed like a note
    body. Empty stays empty. The envelope's source id is the `job_id`, the id a citation points at.
    """
    return frame_untrusted(text, note_id=job_id) if text else ""


@tool
async def find_past_jobs(text: str = "", connector: str = "") -> JobRecordSearch:
    """Find durable jobs this system has already run, and why each of them was run.

    The retrospective view over every campaign, calculation, report and template run that has
    ended — **runs that failed as well as runs that succeeded**, including ones from other people's
    conversations and from long before this one. A connector job's hit carries the **reason the run
    was started**, so "have we optimized this coupling before, and what were we trying to find
    out?" is answerable without the original chat; a template run's `rationale` is empty by design,
    because its `job` names a declared procedure whose purpose the template itself states. Filter
    `connector="template"` for procedures alone.

    Read `state` before reading `summary`: a failed run has an empty summary, because a summary is
    what a run *produced* and a failed one produced nothing. Take its `job_id` to
    `get_durable_job_status` for the reason it failed.

    Use it before launching an expensive job (the answer may already exist: re-running an identical
    job rejoins the stored result, but a *similar* one is a fresh bill), and when a chemist asks
    what has been tried. Take the `job_id` of a promising hit to `get_durable_job_status` for that
    run's full result — for a BO campaign, every candidate it evaluated, not only the winner.

    Args:
        text: Words to look for in the recorded reason, the result summary or the job name.
            Empty returns the most recent runs.
        connector: Restrict to one capability bundle (e.g. "bo", "calc"). Empty searches all.

    Returns:
        `hits` — the matching runs, newest first: what ran, why, how it ended (`state` is
        `completed` or `failed`), what came out in one line, and the note it proposed (if any) —
        and `verdict`, one sentence saying what the hits are evidence of. **Read it before
        concluding a run has not happened**: the search is capped, so an empty or a full list is
        not proof of absence.
    """
    # Frame `rationale` and `summary` (summaries interpolate model-authored strings); leave
    # `job_id`, `note_id`, `connector`, `job` and `completed_at`, which are restricted charsets or
    # datetimes and must be passable straight back to follow-up tools. Framing happens here, not in
    # `search_job_records`, which also serves the HTTP UI. Page flags pass through untouched.
    found = await search_job_records(text, connector)
    return found.model_copy(
        update={
            "hits": [
                record.model_copy(
                    update={
                        "rationale": _framed_free_text(record.rationale, record.job_id),
                        "summary": _framed_free_text(record.summary, record.job_id),
                    }
                )
                for record in found.hits
            ]
        }
    )


def completed_job_status(job_id: str, raw: Any) -> DurableJobStatus:
    """Decode a finished durable job's raw result into the status this system reports.

    `envelope_from_result` decodes; this wraps it in the agent's status model for both waiters
    (`get_durable_job_status` and `agent.job_results`).

    Args:
        job_id: The job the result belongs to, for the status and for the error message.
        raw: Whatever the workflow returned, undecoded.

    Raises:
        ValueError: When the result is not the connector envelope.
    """
    envelope = envelope_from_result(job_id, raw)
    return DurableJobStatus(
        job_id=job_id,
        status="completed",
        summary=envelope.summary,
        calc_refs=envelope.calc_refs,
        # A `calc` envelope arrives already projected (`CalcJobWorkflow`); this covers every other
        # bundle's, and an in-flight run started by the previous release. Idempotent either way.
        result=readable_in_this_session(without_geometry(envelope.data)),
    )


def readable_in_this_session(result: Any) -> Any:
    """A job result with a report artefact's id only where that artefact can be opened.

    A report run is shared across sessions but its artefact lives in the starting session, so the
    id is kept only when read in that session (`EXHIBIT_SESSION`). The session key itself is always
    dropped, since the result reaches any signed-in caller.
    """
    if not isinstance(result, dict) or EXHIBIT_SESSION not in result:
        return result
    readable = dict(result)
    if readable.pop(EXHIBIT_SESSION) != get_current_session_id():
        readable.pop("exhibit_id", None)
    return readable


async def cancel_job(job_id: str) -> bool:
    """Ask Temporal to cancel a running job; False when the id is unknown to it.

    Cooperative: returns once the request is delivered; poll `job_status` for the outcome. Not an
    agent tool — stopping a person's work is that person's decision.
    """
    client = await connect()
    try:
        await client.get_workflow_handle(job_id).cancel()
    except RPCError as exc:
        # `False` means unknown id (the route's 404); any other status is an outage and is raised.
        if exc.status is not RPCStatusCode.NOT_FOUND:
            raise SubsystemUnavailableError(
                f"the durable subsystem did not answer the cancel for job {job_id!r} "
                f"({exc.status.name})"
            ) from exc
        return False
    return True


def _tournament_id(request: TournamentRequest) -> str:
    """A deterministic workflow id, so re-asking the same question rejoins the run.

    Actor and roles are in the key for `_report_id`'s reason; question and context go through
    `canonical_text`, the entitlement half stays byte-exact.
    """
    payload = [
        canonical_text(request.question),
        canonical_text(request.context),
        request.requested_by,
        *sorted(request.requested_roles),
    ]
    return f"hypotheses-{stable_hash(payload)}"


@tool
async def rank_competing_hypotheses(question: str, context: str = "") -> str:
    """Generate competing explanations, rank them, and say what experiment would settle them.

    For a puzzling result with several possible causes — "the impurity appeared when I changed the
    solvent", "the yield collapsed on scale-up" — where the useful answer is the *field* of
    candidates with the evidence weighed across it. Generators propose hypotheses in parallel, each
    is critiqued, then they are compared in pairs against retrieved evidence and rated on the Elo
    scale.

    Prefer `suggest_next_experiment` for an optimization over bounded numeric variables with runs
    already done: a fitted surrogate is a stronger instrument than a judged comparison. Answer
    directly when only one explanation is really in play — a tournament over a field of one tells
    nobody anything.

    Returns a job id rather than the ranking; poll `get_durable_job_status`. Re-asking the same
    question rejoins the existing run.

    **The rating orders the candidates this run generated. It is not a probability that any of them
    is true.** Read `competing-hypotheses` before reporting one: it carries the rest, including
    that an unseparated field is an answer rather than a failure.

    Args:
        question: The observation or puzzle to explain, in the chemist's own terms.
        context: Optional extra detail — what was already tried, what was ruled out, constraints.

    Returns:
        The job id to poll. Its result carries the ranked field, every objection raised, the
        discriminating check for each hypothesis, and the ids of any `experiment-proposal` notes
        written for checks that need a laboratory.
    """
    authorize_trigger("rank_competing_hypotheses")
    request = TournamentRequest(
        question=question,
        context=context,
        requested_by=require_actor(),
        requested_roles=sorted(get_current_roles()),
        correlation_id=get_current_correlation_id() or "",
        session_id=get_current_session_id() or "",
    )
    client = await connect()
    workflow_id = _tournament_id(request)
    try:
        handle = await client.start_workflow(
            HypothesisTournamentWorkflow.run,
            request,
            id=workflow_id,
            task_queue=settings.background_task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
        )
    except WorkflowAlreadyStartedError:
        # Same question, same actor: hand back the run rather than paying for it twice. No
        # `job_started` signal, matching `request_development_report` — nothing new began.
        return workflow_id
    record_job_started(handle.id, "hypotheses")
    return handle.id
