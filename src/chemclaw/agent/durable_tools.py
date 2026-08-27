"""Agent tools that start the two durable subsystems nothing could reach (gaps RCH-1, RCH-2).

`DevelopmentReportWorkflow` (all of Phase 5b) was built, tested, and registered on the
background worker with **no caller anywhere** — no agent tool, no HTTP route, no Schedule — so
the only way to start it in a running deployment was the Temporal CLI. This is the missing
adapter, in the thin shape the QM launcher established (D-002): authorize → stamp the ambient
actor → deterministic workflow id → return the id immediately. Nothing here *stores* durable state
and the agent never blocks; completion reaches the chat through the existing push-back channel
(F3-T3).

It does, however, **define** durable identity, and that is not a lesser thing than storing it: the
workflow ids for three workflows and the reuse policy that decides whether a repeat re-executes or
rejoins are all written here, so "who gets whose run" is settled in this module. `_report_id` is
where that is subtle enough to argue about; read it before changing what goes into an id.

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
connector-owned or not, and it is the only *status tool* — the DFT job was the last one with
one of its own; D-118 made it a connector job, `agents/job_status.py` is gone, and so is the
envelope-shaped exception this tool made for it.

It is **not** the only place a finished job's result is collected, and the sentence that said so
was wrong in a way that showed: three call sites collect one — this tool,
`chemclaw.agent.job_results` (the mid-turn resume), and the in-turn wait in
`chemclaw.connectors.jobs`, which is a different subsystem and cannot route through an agent
tool. What is genuinely single is the *decode*:
`chemclaw.durable.connector_job.envelope_from_result` is the one place raw becomes an envelope or
an error, and all three go through it.
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from temporalio.client import WorkflowExecutionStatus
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.types import MethodAsyncNoParam

from chemclaw.agent.authz import authorize_trigger, require_actor
from chemclaw.agent.framing import frame_untrusted
from chemclaw.core.config import settings
from chemclaw.core.errors import SubsystemUnavailableError
from chemclaw.core.identity_context import get_current_roles
from chemclaw.core.ids import canonical_text, stable_hash
from chemclaw.core.temporal_client import connect
from chemclaw.core.tool_registry import tool
from chemclaw.core.turn_signals import record_job_started
from chemclaw.durable.connector_job import envelope_from_result
from chemclaw.durable.job_record import JobRecordSummary, lookup_job_record, search_job_records

# Importing the workflow *types* to launch them is deliberate and bounded
# (D-2026-08-17-a-workflow-type-is-a-launch-contract-not-a-durability-leak): it is what makes
# `start_workflow`'s argument type-checked at the one site that decides durable identity, and it
# costs 10 modules and no third-party package here, because both closures are what core already
# carries for `gather_evidence`. It is allowed only while that stays true — a *bundle's* workflow
# is reached by name across its own queue instead, and
# `tests/test_layering.py::test_the_agent_layer_imports_no_bundle_workflow` is what keeps the two
# cases apart.
from chemclaw.durable.memory_jobs import (
    CampaignSynthesisWorkflow,
    OptimizationCampaignWorkflow,
    PlaybookDistillationWorkflow,
)
from chemclaw.durable.note_index import NoteReindexWorkflow
from chemclaw.durable.observation_jobs import ObservationPromotionWorkflow
from chemclaw.durable.report_workflow import DevelopmentReportWorkflow
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
    # The calculations this run rested on, as `propose_knowledge_note` takes them (D-2026-08-21).
    # That tool's docstring has said "get them from a job's result envelope" since D-133 against an
    # envelope that carried none, so a note drafted from a calculation the agent had just run could
    # not cite it. Empty for a job that recorded none — a report, or a run from before the refs
    # were captured — which is the honest reading either way.
    calc_refs: list[str] = Field(default_factory=list)
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

    Keyed on the title, the section specs **and the requester's entitlement**. The last part is not
    idempotency, it is access control, and leaving it out was a cross-user data exposure the moment
    `retrieve_section` began reading entitlement-gated sources as the requester.

    Sharing one run across chemists is only sound while the run reads the same corpus for everyone.
    It no longer does. Alice holding `chemclaw.sharedrive.reader` launches a report and the gated
    share's documents land in the draft; Bob asks for the same title and sections, gets the same id
    from `WorkflowAlreadyStartedError`, and `job_status()` — which applies no actor check, and which
    `find_past_jobs` explicitly points people at with other people's job ids — hands him a completed
    report built from a corpus his AD group excludes him from. The mirror case is the defect this
    was all meant to fix: Bob first, and Alice silently receives the narrowed sweep.

    The roles are what the corpus actually depends on; the actor is in the key as well, because it
    is what the draft is attributed to. So idempotency is **per actor**: the same chemist asking
    twice gets one run, and two chemists with identical entitlements get two. An earlier version of
    this paragraph claimed the second pair still share a run — measured false, since `requested_by`
    is in the payload below. Sharing across actors would be the cheaper answer and it is not
    available: the id is what `job_status()` hands a report out by, and that call applies no actor
    check, so an id two principals can both derive is an id either can collect.

    **The model-written half is canonicalised; the entitlement half is not.** The requester of this
    tool is an LLM emitting a section list, and it reorders and re-cases freely, so a byte-exact
    key made "re-asking is idempotent" true only for a byte-identical request: measured, swapping
    two sections, re-casing the title, re-casing a heading and a trailing space on a query each
    produced a *different* id and therefore a second unbounded multi-section research run — the
    cost `CORE_EXPENSIVE_ACTIONS` gates this tool to avoid. Title, headings and queries are
    therefore whitespace-collapsed and casefolded, and the section list is sorted.

    What that costs is real and small: two requests differing only in casing or section order share
    one run, so the *first* requester's casing and ordering are what the draft renders. The second
    is not misled — `get_durable_job_status` reports the run's own summary, which names the title
    actually drafted — and a PR-gated draft is edited by a human before it becomes knowledge.

    `requested_by`, `requested_roles` and `memory_layer` are deliberately left byte-exact, and that
    is the same argument as the paragraph above rather than a separate one: they are not free text
    a model composes. Folding two spellings of a principal or a role together is precisely the
    cross-actor merge this key exists to prevent, and `memory_layer` is a closed set.
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
    source, then opens the assembled draft as a PR-gated `report` note for human review.
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
    # `require_actor` is the core rule (F4-T3): under Entra, refuse durable work with no user. Its
    # result travels on the request rather than being discarded — see `ReportRequest.requested_by`.
    request = ReportRequest(
        title=title,
        sections=sections,
        requested_by=require_actor(),
        requested_roles=sorted(get_current_roles()),
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
        # Same report already running or completed: hand back the existing id rather than
        # redrafting it (the QM tool's idempotency contract, applied here). Deliberately no
        # `job_started` signal: this run already existed (and may already be finished), so
        # announcing a start would be false. The generated connector-job launcher skips the
        # announcement on a duplicate for exactly the same reason.
        return workflow_id
    record_job_started(handle.id, "report")
    return handle.id


# The four corpus-scanning jobs this tool can start, by the word a chemist would use.
#
# **They exist here because D-2026-08-25 took their Schedules away and left nothing behind.** That
# decision was right — each of these opens pull requests, and knowledge arriving on a timer is
# knowledge nobody asked for — but the change removed the trigger without adding one, so all four
# became unreachable code whose docstrings claimed they were "started on demand". That is the
# defect this module's own header was written about: a durable workflow registered on the worker
# with no caller anywhere. This is the adapter, in the same thin shape.
MemoryJobKind = Literal["campaign", "playbook", "optimization", "observation-promotion"]

# Typed as the no-argument workflow method Temporal's own overload takes, rather than as the four
# classes: a bare dict of heterogeneous workflow types degrades to `type[object]` and `.run` stops
# type-checking, which is how the launcher would silently accept something that is not a workflow.
_MEMORY_JOBS: dict[MemoryJobKind, MethodAsyncNoParam[Any, list[str]]] = {
    "campaign": CampaignSynthesisWorkflow.run,
    "playbook": PlaybookDistillationWorkflow.run,
    "optimization": OptimizationCampaignWorkflow.run,
    "observation-promotion": ObservationPromotionWorkflow.run,
}


@tool
async def synthesize_memory(kind: MemoryJobKind, fresh: bool = False) -> str:
    """Mine the reaction corpus for a class of knowledge and propose what it finds for review.

    Use this when someone asks what the corpus now supports — "have we accumulated enough on this
    route to write it up", "what campaigns are in the record", "is anything worth distilling" —
    or after a large ELN ingest. Each kind re-reads **every** reaction from the configured ingest
    sources and proposes notes through the PR-gate, so a human still decides what becomes
    knowledge; this only decides *when to look*.

    Nothing runs these on a timer (D-2026-08-25). A pull request nobody asked for is knowledge
    arriving unbidden, which is the thing that decision removed — so the corpus is mined when a
    person has a reason, and this tool is that reason arriving.

    The kinds:

    - `campaign` — narrate chains of experiments where one run's product is the next one's
      reactant, citing every member.
    - `playbook` — distil a transformation that recurs *across projects* into reusable judgment.
    - `optimization` — group same-transformation runs into a screen and read it as a series.
    - `observation-promotion` — propose playbook notes for the ungated observations that have
      crossed both support thresholds. The mining that feeds it still runs on a timer, because it
      writes rows nobody reviews; only this half opens pull requests.

    Args:
        kind: Which synthesis to run.
        fresh: Force a new run even when one already ran today. The default deduplicates by UTC
            day — two chemists asking the same morning share one scan — but the tool's own
            recommended use ("after a large ELN ingest") is exactly the case where rejoining the
            morning's run silently reports on the *pre-ingest* corpus. Pass true when the corpus
            has changed since the day's first run.

    Returns:
        The job id. Poll it with `get_durable_job_status`; the result is the list of pull requests
        opened, which may be empty when the corpus supports nothing new.
    """
    authorize_trigger("synthesize_memory")
    # `require_actor` before anything durable starts, the core rule (F4-T3): these open pull
    # requests in the knowledge repository, and a PR with no author behind it is exactly what the
    # gate exists to prevent.
    actor = require_actor()
    client = await connect()
    workflow_id = _memory_job_id(kind, fresh=fresh)
    try:
        handle = await client.start_workflow(
            _MEMORY_JOBS[kind],
            id=workflow_id,
            task_queue=settings.background_task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
        )
    except WorkflowAlreadyStartedError:
        # Today's run of this kind already exists — see `_memory_job_id` for why a day is the unit.
        # Hand back its id rather than starting a second full-corpus scan, and deliberately no
        # `job_started` signal: this run already existed and may already have finished, so
        # announcing a start would be false. Same contract as the report launcher above.
        return workflow_id
    logger.info("memory synthesis %s started by %s as %s", kind, actor, handle.id)
    record_job_started(handle.id, "memory-synthesis")
    return handle.id


def _memory_job_id(kind: MemoryJobKind, *, fresh: bool = False) -> str:
    """A deterministic id for one kind's synthesis, keyed on the **UTC date**.

    There is no request to key on: the input is the whole corpus as it stands, so two chemists
    asking the same morning want the same answer and must not each pay a full re-scan — nor open
    two pull requests for one finding, which is what `memory.ids.with_id`'s anchor can produce when
    a cluster grows between two runs.

    A day is the unit because a day is what the retired Schedule used
    (`memory_synthesis_schedule_minutes` defaulted to 1440), so the cadence a deployment already
    reasoned about is preserved and only the *trigger* moved from a clock to a person.

    The cost of the daily unit is stated rather than hidden: a second ask on the same day
    rejoins the first run, so an ingest landing between the two is not picked up. `fresh` is the
    escape hatch for exactly that — it suffixes the id with the current time, so the run really
    re-mines. The caller opts in, because the default has to stay the shared scan: "mine after
    this afternoon's import" was the tool's own recommended use, and it silently returned the
    morning run's id.
    """
    day = datetime.now(UTC).date().isoformat()
    if fresh:
        return f"memory-{kind}-{day}-{datetime.now(UTC).strftime('%H%M%S')}"
    return f"memory-{kind}-{day}"


@tool
async def get_durable_job_status(job_id: str) -> DurableJobStatus:
    """Collect a durable job: its status, and its result once it has completed.

    This is the follow-up for **every** job id this system hands out — a connector job such as
    `compute_reaction_energy`, `sample_conformers` or `start_optimization_campaign`, a development
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
        completed, the one-line `summary`, the structured `result`, and `calc_refs` — the
        calculation keys the run rested on, which `propose_knowledge_note` takes so a conclusion
        drawn from this job stays traceable to what computed it. A job still running reports the
        status alone.

        A geometry in the result is reported by its `structure_id` rather than by its coordinates.
        That address is what the next calculation takes: pass it to `optimize_geometry`,
        `compute_thermochemistry` or `scan_coordinate` to carry one chosen conformer forward
        instead of starting again from the molecule.

    Raises:
        ValueError: When the id is unknown to both Temporal and the durable record, or names a
            completed workflow whose result is not the connector envelope. That second case used to
            degrade to a bare status, because the DFT job returned its own typed result and had
            its own status tool (`agents/job_status.py`). D-118 made it a connector job, so
            every durable job this system hands an id for returns the envelope — a result that is
            not one means the id belongs to a workflow no tool advertises, and reporting
            "completed" with an empty result would tell a chemist their calculation is done while
            silently withholding it.
    """
    return await job_status(job_id, wait_seconds=settings.job_status_wait_seconds)


async def job_status(job_id: str, *, wait_seconds: float = 0.0) -> DurableJobStatus:
    """One durable job's status, from Temporal while it remembers and the record afterwards.

    The tool above and the front door's `GET /jobs/{id}` are the same question asked by different
    surfaces, so they are one function: a chemist polling in chat and a chemist refreshing a page
    must not be able to get different answers about the same run.

    `wait_seconds` is where the two surfaces legitimately differ, and it is a parameter for that
    reason rather than a fork: a poll from the *model* costs a whole conversation turn — connector
    open, graph compile, model call — so answering `running` for a job finishing two seconds later
    spends another full turn learning what a short long-poll would have delivered now. The tool
    passes `job_status_wait_seconds`; the HTTP route passes nothing, because a browser's poll is
    cheap and holding its request open is not. The wait is Temporal's own long-poll
    (`handle.result()`), not a sleep loop.
    """
    client = await connect()
    handle = client.get_workflow_handle(job_id)
    try:
        description = await handle.describe()
    except RPCError as exc:
        # **NOT_FOUND only.** The rationale below is sound for "Temporal has never heard of this
        # id" and for nothing else, and `RPCError` carries `.status` while this code never read it:
        # UNAVAILABLE, DEADLINE_EXCEEDED, RESOURCE_EXHAUSTED and PERMISSION_DENIED all arrived here
        # and were reported to a chemist as "no durable job with id …". A broker rolling during a
        # poll told them their running campaign did not exist.
        if exc.status is not RPCStatusCode.NOT_FOUND:
            raise SubsystemUnavailableError(
                f"the durable subsystem did not answer for job {job_id!r} ({exc.status.name})"
            ) from exc
        # Temporal has never heard of this id — which, for a job that genuinely ran, means its
        # history has aged out rather than that it never existed. Ask the durable record before
        # telling a chemist their campaign does not exist.
        recorded = await _recorded_status(job_id)
        if recorded is None:
            raise ValueError(f"no durable job with id {job_id!r}") from exc
        return recorded
    status = _TERMINAL.get(description.status, "running") if description.status else "running"
    if status == "running" and wait_seconds > 0:
        try:
            result = await asyncio.wait_for(handle.result(), wait_seconds)
        except TimeoutError:
            return DurableJobStatus(job_id=job_id, status="running")
        except Exception:
            # The run reached a terminal state that is not success while we waited (failed,
            # cancelled, timed out) — `handle.result()` raises for those. Re-describe once and
            # report the state by the same mapping the no-wait path uses.
            refreshed = await handle.describe()
            ended = _TERMINAL.get(refreshed.status, "running") if refreshed.status else "running"
            return DurableJobStatus(job_id=job_id, status=ended)
        return completed_job_status(job_id, result)
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
        calc_refs=record.calc_refs,
        # Projected on the way out as well as on the way in, and the difference is *old rows*: a
        # record written before D-2026-08-21 holds the whole geometry, so a months-old conformer
        # search collected here would still spend a context window on coordinates. The projection
        # is idempotent, so applying it to a record already written without them costs a walk.
        result=without_geometry(record.result),
        rationale=record.rationale,
    )


def _framed_free_text(text: str, job_id: str) -> str:
    """One free-text field of a past run, wrapped as data and attributed to the run that wrote it.

    `find_past_jobs` is the one tool whose whole purpose is to return **other people's** text, and
    a job record is never PR-gated: chemist A types a rationale into a launcher and it reaches
    chemist B's model turn verbatim, months later, as tool output. That is the stored, cross-user
    form of the indirect prompt-injection vector `expand_note` and `gather_evidence` already frame a
    note body against, so it gets the same envelope rather than a second mechanism.

    Empty stays empty: an envelope around nothing is context spent to say nothing. `rationale` is
    `min_length=1` where records are written, but a summary is optional and both are read back out
    of a database column, so the guard is applied to whatever is passed rather than to one field.

    The envelope's source id is the `job_id` — the run *is* the source here, and it is the id a
    citation of a past run should point at (and the one `get_durable_job_status` takes).
    """
    return frame_untrusted(text, note_id=job_id) if text else ""


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
        connector: Restrict to one capability bundle (e.g. "bo", "calc"). Empty searches all.

    Returns:
        The matching runs, newest first: what ran, why, what came out in one line, and the note it
        proposed (if any).
    """
    # Two fields are framed and four are not, and both halves of that are deliberate.
    #
    # `rationale` is prose a person (or their model) wrote to justify a run — untrusted by
    # construction. `summary` is the subtler one: the sentence is composed by first-party connector
    # code, which reads as trusted until you look at what it interpolates — `spec.objective_name`,
    # `request.title`, `' + '.join(spec.reactants)` — all model-authored strings echoed verbatim. A
    # first-party template is not a first-party string, so framing the reason and not the summary
    # would leave the vector open through the neighbouring field. (This is the inconsistency
    # BACKLOG notes at `gather_evidence`, which frames a chunk's `content` and not its `source`;
    # the point of naming it here is to not repeat it.)
    #
    # The other four carry no free text and framing them would cost more than it buys. `job_id` and
    # `note_id` exist to be handed straight back to `get_durable_job_status` and `expand_note`, so
    # wrapping them would break the follow-up call this tool's whole docstring points at — and they
    # are generated (`<connector>-<job>-<hash>`) or slug-validated (`Note.id` is
    # `^[A-Za-z0-9][A-Za-z0-9_.-]*$`), a charset with no `<` in it. `connector` and `job` are
    # manifest names matched against `^[a-z][a-z0-9_-]*$`, and `completed_at` is a `datetime`.
    # Framing the structured half of a hit would spend the model's ability to read it on nothing.
    #
    # Nothing is said about the envelope in the docstring above, which is the model-facing text:
    # the system prompt is the single place that vouches for the delimiter, and a per-tool
    # restatement is exactly the drift `test_instructions_name_the_exact_delimiter_framing_uses`
    # exists to catch. Framing is also applied *here* rather than in `search_job_records`, because
    # the front door's `GET /jobs` reads that same function for a human UI, where an envelope is
    # noise; the envelope belongs to the model's context, so it belongs to the agent layer.
    return [
        record.model_copy(
            update={
                "rationale": _framed_free_text(record.rationale, record.job_id),
                "summary": _framed_free_text(record.summary, record.job_id),
            }
        )
        for record in await search_job_records(text, connector)
    ]


def completed_job_status(job_id: str, raw: Any) -> DurableJobStatus:
    """Decode a finished durable job's raw result into the status this system reports.

    This is the agent layer's share of that job: `envelope_from_result` does the decode — the
    same one `chemclaw.connectors.jobs` needs for its in-turn wait — and this wraps it in the
    status model only the agent reports. The two waiters here are `get_durable_job_status`, which
    polls, and `chemclaw.agent.job_results`, the mid-turn resume that waits on the handle instead;
    what they must do with the answer is identical.

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
        result=without_geometry(envelope.data),
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


async def cancel_job(job_id: str) -> bool:
    """Ask Temporal to cancel a running job; False when the id is unknown to it.

    Cancellation is cooperative — Temporal delivers it to the workflow, which unwinds through its
    own teardown — so this returns once the request is *delivered*, not once the run has stopped.
    Poll `job_status` for the outcome.

    Deliberately **not** an agent tool. Stopping work a person asked for is a decision about that
    person's work, and the agent already has every incentive to tidy up after itself; the same
    reasoning keeps the plan gate and the proposal sign-off off the tool surface (D-005).
    """
    client = await connect()
    try:
        await client.get_workflow_handle(job_id).cancel()
    except RPCError as exc:
        # `False` means "Temporal does not know this id", and the route turns it into a 404. Any
        # other status is an outage, and reporting it as a nonexistent job is the worst available
        # answer: an operator cancelling a runaway DFT run during a broker roll was told the run
        # did not exist, stopped trying, and the cluster kept burning.
        if exc.status is not RPCStatusCode.NOT_FOUND:
            raise SubsystemUnavailableError(
                f"the durable subsystem did not answer the cancel for job {job_id!r} "
                f"({exc.status.name})"
            ) from exc
        return False
    return True
