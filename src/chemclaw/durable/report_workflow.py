"""Durable development-report workflow on the background queue.

Each report section is its own child workflow, so a long report resumes section by section after
a worker restart; a final activity renders the draft and records it as a note. The retriever
factory is module-level so tests swap it.
"""

import asyncio
import hashlib
import logging
from datetime import timedelta

from pydantic import BaseModel, Field
from temporalio import activity, workflow
from temporalio.exceptions import ActivityError
from temporalio.exceptions import CancelledError as TemporalCancelledError

with workflow.unsafe.imports_passed_through():
    from chemclaw.agent.session_events import record_session_event
    from chemclaw.agent.session_members import participant_permits
    from chemclaw.agent.session_store import SessionOwnerStore
    from chemclaw.core.config import settings
    from chemclaw.core.identity_context import reset_current_identity, set_current_identity
    from chemclaw.core.logging import log_event
    from chemclaw.core.metrics_bridge import degraded
    from chemclaw.core.turn_signals import ExhibitSignal
    from chemclaw.durable.connector_job import ConnectorJobResult
    from chemclaw.durable.observation_jobs import workflow_safe_today
    from chemclaw.durable.registry import durable_activity, durable_workflow
    from chemclaw.exhibits.models import (
        PUSH_KIND,
        DocumentSpec,
        ExhibitLimit,
        InvalidExhibit,
        require_writable,
    )
    from chemclaw.exhibits.store import default_exhibit_store
    from chemclaw.exhibits.telemetry import record_refusal, record_write
    from chemclaw.ingest.eln.records import default_record_store
    from chemclaw.ingest.sources.registry import active_retrieve_sources
    from chemclaw.kg.git_writer import default_writer
    from chemclaw.kg.record import record_note
    from chemclaw.retrieval.evidence import SourceRetriever
    from chemclaw.retrieval.harness import (
        Report,
        ReportRequest,
        ReportSection,
        SectionRequest,
        SynthesizedSection,
        gather_section,
        report_note,
    )
    from chemclaw.retrieval.retrievers import FingerprintReactionRetriever
    from chemclaw.science.fingerprints.store import default_reaction_store

from chemclaw.durable.deliver_message import (
    OutboundAttachment,
    OutboundMessage,
    deliver_best_effort,
)
from chemclaw.durable.notify import notify_session_best_effort
from chemclaw.durable.orchestrator import fan_out
from chemclaw.durable.publish import (
    BAD_DATA_RETRY,
    activity_failure_reason,
    fan_out_queue_wait_timeout,
    light_write_queue_wait_timeout,
    publish_note,
)

logger = logging.getLogger(__name__)


def default_retrievers() -> list[SourceRetriever]:
    """The production source retrievers: every active text source, plus reaction fingerprint.

    The text half comes from the same `settings.data_sources` registry `gather_evidence` uses, so a
    deployment that enables hybrid retrieval gets it in reports too. The reaction-fingerprint
    retriever answers only reaction-SMILES queries and returns `[]` otherwise.
    """
    return [
        *active_retrieve_sources(),
        FingerprintReactionRetriever(default_reaction_store(), default_record_store()),
    ]


@durable_activity("background")
@activity.defn
async def retrieve_section(request: SectionRequest) -> SynthesizedSection:
    """Retrieve one report section's evidence across the production sources, as the requester.

    The actor is bound here because entitlement-gated retrievers read the ambient identity and
    decline silently without one, which would read as a source with no matches. A scheduled report
    has no requester and stamps nothing.
    """
    if not request.requested_by:
        return await gather_section(request.section, default_retrievers())
    # Empty roles, never `request.requested_roles`: a workflow payload is relayed data, not a
    # verified claim. The actor is bound for attribution; authorization fails closed on the empty
    # set.
    token = set_current_identity(request.requested_by, frozenset())
    try:
        return await gather_section(request.section, default_retrievers())
    finally:
        reset_current_identity(token)


@durable_activity("background")
@activity.defn
async def record_report_note(
    report: Report, requested_by: str = "", correlation_id: str = ""
) -> str:
    """Render the gathered report as a recorded `report` note; return the reference.

    `correlation_id` is not read in the body: `durable/interceptor.py` binds an activity's ids from
    its parameters by name, so declaring it is the wiring that puts the turn's id on this write's
    log lines. `requested_by` stamps the ambient identity so the log lines tie back to the chemist.
    """
    # `drafted_on` sets `valid_from`; `durable/digest._is_new` treats an undated note as not news.
    # Activity code may read the clock, through `workflow_safe_today`.
    drafted = report_note(report, drafted_on=workflow_safe_today())
    if not requested_by:
        return await record_note(drafted, default_writer())
    token = set_current_identity(requested_by, frozenset())
    try:
        return await record_note(drafted, default_writer())
    finally:
        reset_current_identity(token)


@durable_activity("background")
@activity.defn(name="propose_report")
async def propose_report(report: Report, requested_by: str = "", correlation_id: str = "") -> str:
    """The old Temporal name for `record_report_note`, kept for one deployment cycle.

    A registered activity name is a wire name: an in-flight history that scheduled `propose_report`
    fails forever on a worker that no longer offers it. Delete once `background-jobs` has drained
    (trigger in `docs/planning/DEFERRED.md`). The signature, including `correlation_id`, matches so
    the interceptor binds ids the same way.
    """
    return await record_report_note(report, requested_by, correlation_id)


class ReportExhibitInput(BaseModel):
    """The finished draft as a `document` artefact for the session that asked for the report."""

    session_id: str = Field(min_length=1)
    # `report_exhibit_id(workflow_id)`, derived in workflow code so every attempt names one id.
    exhibit_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    markdown: str
    # The chemist who asked: the artefact's author, `author_kind` agent — the agent wrote the
    # draft on their behalf, exactly as a `create_exhibit` call in their turn is recorded.
    requested_by: str = Field(min_length=1)
    correlation_id: str = ""


#: The result key naming the session a report's artefact lives in. Internal: every reader strips it
#: (`agent/durable_tools.readable_in_this_session`), so no surface publishes a session id.
EXHIBIT_SESSION = "exhibit_session"


def report_exhibit_id(workflow_id: str) -> str:
    """The artefact id a report run writes: `xb-` and the first 16 hex of sha256(workflow id).

    Deterministic, so a retried activity or replayed workflow names the same artefact, and in the
    `EXHIBIT_ID` shape.
    """
    return f"xb-{hashlib.sha256(workflow_id.encode('utf-8')).hexdigest()[:16]}"


@durable_activity("background")
@activity.defn
async def record_report_exhibit(request: ReportExhibitInput) -> str:
    """Show the finished report in the session that asked, as a `document` artefact; return its id.

    Idempotent on retry (the id derives from the workflow). Returns `""` with a log line when the
    artefact cannot be shown: no durable session store, session deleted, requester not a
    participant, session at its artefact cap, or draft over the spec cap. The report note is the
    durable result either way; the event push is best effort.
    """
    if settings.session_store != "postgres":
        log_event(logger, "report.exhibit_skipped", "no durable session store", reason="memory")
        return ""
    found, owner, _ = await SessionOwnerStore().lookup(request.session_id)
    if not found:
        log_event(
            logger,
            "report.exhibit_skipped",
            "session %s no longer exists; the report stays a note",
            request.session_id,
            reason="session_gone",
            session=request.session_id,
        )
        return ""
    # The payload is relayed data, so check that the requester may write into this session as it
    # stands now (owner or member), as the front door would.
    if not await participant_permits(request.session_id, owner, request.requested_by):
        log_event(
            logger,
            "report.exhibit_skipped",
            "%s is not the owner or a member of session %s; the report stays a note",
            request.requested_by,
            request.session_id,
            reason="not_a_participant",
            session=request.session_id,
        )
        return ""
    spec = DocumentSpec(kind="document", markdown=request.markdown)
    title = request.title[: settings.exhibit_max_title_chars]
    try:
        require_writable(spec, title=title, change_note="")
        view = await default_exhibit_store().create(
            request.session_id,
            title=title,
            spec=spec,
            author_kind="agent",
            author=request.requested_by,
            correlation_id=request.correlation_id,
            exhibit_id=request.exhibit_id,
        )
    except (InvalidExhibit, ExhibitLimit) as exc:
        record_refusal("exhibit_limit" if isinstance(exc, ExhibitLimit) else "invalid")
        log_event(
            logger,
            "report.exhibit_skipped",
            "the report cannot be shown as an artefact in session %s: %s",
            request.session_id,
            exc,
            reason=type(exc).__name__,
            session=request.session_id,
        )
        return ""
    # Also counted on a retry that found the existing artefact; create-or-return cannot tell which.
    record_write(view, "created")
    announced = ExhibitSignal(
        exhibit_id=view.exhibit_id,
        revision=view.revision,
        kind=view.kind,
        title=view.title,
        op="created",
        author_kind=view.author_kind,
        author=view.author,
    )
    try:
        await record_session_event(
            request.session_id,
            PUSH_KIND,
            announced.model_dump(),
            dedupe_key=f"report-exhibit:{view.exhibit_id}",
        )
    except Exception:
        degraded(
            logger,
            "exhibits",
            "could not push report artefact %s to session %s",
            view.exhibit_id,
            request.session_id,
        )
    return view.exhibit_id


async def _report_exhibit_best_effort(request: ReportExhibitInput) -> str:
    """Run `record_report_exhibit`, and never fail the report because the artefact did not land.

    A failure is a warning and an empty id; a cancellation is re-raised, never swallowed.
    """
    try:
        return await workflow.execute_activity(
            record_report_exhibit,
            request,
            task_queue=settings.background_task_queue,
            start_to_close_timeout=timedelta(seconds=settings.activity_timeout_seconds),
            schedule_to_start_timeout=light_write_queue_wait_timeout(),
            retry_policy=BAD_DATA_RETRY,
        )
    except ActivityError as exc:
        if isinstance(exc.cause, TemporalCancelledError):
            raise asyncio.CancelledError("the report artefact was cancelled with its run") from exc
        workflow.logger.warning(
            "report artefact for session %s failed: %s",
            request.session_id,
            activity_failure_reason(exc),
        )
        return ""


@durable_workflow("background")
# Any exception fails the workflow instead of parking it: a parked section would hold the
# requester for the whole fan-out timeout, while a failed one is reconciled into a visible
# `retrieval_failed` marker.
@workflow.defn(failure_exception_types=[Exception])
class ReportSectionWorkflow:
    """Retrieve one report section durably — the fan-out unit of a report.

    A section whose retrieval exhausts its retries degrades to a placeholder marked
    `retrieval_failed` rather than failing the report. The activity carries the single retry
    boundary (`BAD_DATA_RETRY`); no child-level retry is layered on top.
    """

    @workflow.run
    async def run(self, request: SectionRequest) -> SynthesizedSection:
        """Retrieve the section; on activity failure, return a visible `retrieval_failed` marker."""
        section = request.section
        try:
            return await workflow.execute_activity(
                retrieve_section,
                request,
                start_to_close_timeout=timedelta(seconds=settings.report_section_timeout_seconds),
                # The fan-out queue wait, which fits inside the child's execution timeout so the
                # SCHEDULE_TO_START expiry the `except` below degrades on is reachable.
                schedule_to_start_timeout=fan_out_queue_wait_timeout(),
                retry_policy=BAD_DATA_RETRY,
            )
        except ActivityError:
            workflow.logger.warning("report section %r retrieval failed; marked", section.heading)
            return SynthesizedSection(
                heading=section.heading,
                memory_layer=section.memory_layer,
                evidence=[],
                retrieval_failed=True,
            )


def _reconcile(
    requested: list[ReportSection], retrieved: list[SynthesizedSection]
) -> list[SynthesizedSection]:
    """One section per requested section, in request order — a gap is marked, never omitted.

    `fan_out` drops any child that did not return (timeout, cancellation, non-activity failure), so
    the degradation contract is enforced here rather than in the child. Matched by heading and
    consumed in order, so a repeated heading gets one placeholder per missing child.
    """
    by_heading: dict[str, list[SynthesizedSection]] = {}
    for synthesized in retrieved:
        by_heading.setdefault(synthesized.heading, []).append(synthesized)
    reconciled: list[SynthesizedSection] = []
    for section in requested:
        got = by_heading.get(section.heading)
        if got:
            reconciled.append(got.pop(0))
            continue
        # Not logged again: `fan_out` already logs and counts every child it drops.
        reconciled.append(
            SynthesizedSection(
                heading=section.heading,
                memory_layer=section.memory_layer,
                evidence=[],
                retrieval_failed=True,
            )
        )
    return reconciled


@durable_workflow("background")
# Any exception fails the workflow instead of parking it: it has no `execution_timeout`, so a
# parked run would poll as `running` forever, and `_reconcile` runs logic outside an activity.
@workflow.defn(failure_exception_types=[Exception])
class DevelopmentReportWorkflow:
    """Draft a report durably, fanning sections out to child workflows, then record the draft."""

    @workflow.run
    async def run(self, request: ReportRequest) -> ConnectorJobResult:
        """Fan each section out to a child workflow, then record the assembled draft note.

        Every requested section appears in request order, a failed one as a `retrieval_failed`
        marker. Returns the connector envelope so `get_durable_job_status` can hand over the result.
        The workflow writes its own note because the note reference is its result.
        """
        sections = await fan_out(
            ReportSectionWorkflow,
            [
                SectionRequest(
                    section=section,
                    requested_by=request.requested_by,
                    requested_roles=request.requested_roles,
                    correlation_id=request.correlation_id,
                )
                for section in request.sections
            ],
            id_prefix="section",
        )
        report = Report(title=request.title, sections=_reconcile(request.sections, sections))
        # Rendered once and used for both the note and the delivery attachment; `report_note` is
        # pure, so calling it in workflow code emits no command.
        drafted = report_note(report)
        # The note reference *is* this workflow's result, so the publish is not
        # best-effort — but it shares the bounded-attempts discipline (G4).
        note_ref = await publish_note(
            record_report_note, [report, request.requested_by, request.correlation_id]
        )
        # Deliver the report out of the building when a deployment configured a channel. Best effort
        # and last: the note is the durable handover, this is the courtesy copy.
        await deliver_best_effort(
            OutboundMessage(
                recipient=request.requested_by,
                subject=f"Report drafted: {request.title}",
                body=(
                    f"{len(report.sections)} section(s), recorded as {note_ref}.\n"
                    "The draft is attached; open it beside its citations in the knowledge graph."
                ),
                kind="report",
                correlation_id=request.correlation_id,
                # Attach the draft itself: the recipient is by construction not looking at the
                # graph. Rendered here rather than returned by the activity, whose durable contract
                # is the note reference.
                attachments=[
                    OutboundAttachment(
                        # Named after the note id, not `note_ref` (a writer's commit sha).
                        # `_report_id` slugs to `[a-z0-9-]` plus a hash, inside `Attachment`'s
                        # pattern by construction.
                        filename=f"{drafted.id}.md",
                        media_type="text/markdown",
                        content=drafted.body.encode("utf-8"),
                    )
                ],
            )
        )
        # `report.sections` after reconciliation, so the count is the count the chemist asked for.
        # The note is readable immediately; no review step exists, so the summary must not claim
        # one.
        summary = (
            f"Drafted {request.title!r} with {len(report.sections)} section(s); "
            f"recorded as {note_ref}."
        )
        data: dict[str, object] = {
            "note_ref": note_ref,
            "title": request.title,
            "sections": len(report.sections),
        }
        # Show the draft as a `document` artefact in the session that asked, and push the completion
        # there. Only for a request naming a session, so older in-flight histories replay unchanged.
        if request.session_id:
            exhibit_id = await _report_exhibit_best_effort(
                ReportExhibitInput(
                    session_id=request.session_id,
                    exhibit_id=report_exhibit_id(workflow.info().workflow_id),
                    title=request.title,
                    markdown=drafted.body,
                    requested_by=request.requested_by,
                    correlation_id=request.correlation_id,
                )
            )
            pushed: dict[str, object] = {
                "job_id": workflow.info().workflow_id,
                "job": "report",
                "summary": summary,
                "note_id": drafted.id,
                "note_ref": note_ref,
            }
            if exhibit_id:
                data["exhibit_id"] = pushed["exhibit_id"] = exhibit_id
                # Which session can open it: runs are shared across sessions asking for the same
                # report, and a status read from another session drops the id rather than hand over
                # a link that 404s there.
                data[EXHIBIT_SESSION] = request.session_id
            await notify_session_best_effort(request.session_id, "job_completed", pushed)
        return ConnectorJobResult(summary=summary, data=data)
