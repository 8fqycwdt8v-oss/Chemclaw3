"""Durable development-report workflow (plan steps 5b.5, 5b.6) on the background queue.

The report is a graph of sections; here each section is a Temporal activity, so a
long report (hundreds of retrievals over years of data) is resumable and survives worker
restarts — the same fire-and-forget durability as the QM spine (Phase 1). The workflow
retrieves section by section, then a final activity renders the draft and proposes it through
the PR-gate (5b.7). Retriever construction (the production sources) lives in the activities;
the factory is module-level so tests swap it.
"""

from datetime import timedelta

from temporalio import activity, workflow
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from chemclaw.core.config import settings
    from chemclaw.core.identity_context import reset_current_identity, set_current_identity
    from chemclaw.durable.connector_job import ConnectorJobResult
    from chemclaw.durable.registry import durable_activity, durable_workflow
    from chemclaw.ingest.eln.records import default_record_store
    from chemclaw.ingest.sources.registry import active_retrieve_sources
    from chemclaw.kg.git_submitter import default_submitter
    from chemclaw.kg.pr_gate import propose_note
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

from chemclaw.durable.orchestrator import fan_out
from chemclaw.durable.publish import BAD_DATA_RETRY, publish_note, queue_wait_timeout


def default_retrievers() -> list[SourceRetriever]:
    """The production source retrievers: every active text source, plus reaction fingerprint.

    The text half comes from the same config-driven registry `chemclaw.agent.research_tools.
    gather_evidence` fans out over (`settings.data_sources` — `graph` alone by default, or
    `graph,vector,lexical` for hybrid retrieval), not a hardcoded `GraphRetriever()`: a report
    section's query is prose exactly like a conversational turn's, so it needs the same
    fix for the same literal-substring-match limitation, and a deployment that turns on hybrid
    retrieval must not have to remember to do it in two places (D-018). The reaction-fingerprint
    retriever is always appended — harmless on a prose query (it answers only reaction-SMILES
    queries, `[]` otherwise) and needed when a section's query names a reaction to search by
    structure.
    """
    return [
        *active_retrieve_sources(),
        FingerprintReactionRetriever(default_reaction_store(), default_record_store()),
    ]


@durable_activity("background")
@activity.defn
async def retrieve_section(request: SectionRequest) -> SynthesizedSection:
    """Retrieve one report section's evidence across the production sources, as the requester.

    The identity is stamped here because this is where an entitlement is actually checked:
    `ShareDocumentRetriever._entitled()` reads the ambient actor's roles, and with none set it
    correctly declines — returning `[]` without reaching the index. `gather_section` only
    concatenates, so that is indistinguishable from a source with no matches, and `retrieval_failed`
    stays False. The result was a draft that read as a complete sweep of every internal source while
    a gated share had been skipped in silence.

    A report is *authored* by a user but *run* by the service, and stamping the requester's roles
    onto a background run widens what that run can read. That is the right trade here and not a
    general one: the sections are the requester's own question, the draft goes to them, and the
    alternative on offer was not "read less" but "read less and say nothing about it". A scheduled
    report has no requester, stamps nothing, and is bounded exactly as before.
    """
    if not request.requested_by:
        return await gather_section(request.section, default_retrievers())
    token = set_current_identity(request.requested_by, frozenset(request.requested_roles))
    try:
        return await gather_section(request.section, default_retrievers())
    finally:
        reset_current_identity(token)


@durable_activity("background")
@activity.defn
async def propose_report(report: Report, requested_by: str = "") -> str:
    """Render the gathered report as a PR-gated `report` note; return the reference.

    `requested_by` stamps the ambient identity for the gate, for the same reason
    `publish_memory_note_activity` takes one: `propose_note` records a durable `NoteProposal` whose
    actor comes from `ambient_provenance()`, and an activity sets none. Without it the draft is
    recorded with `actor=""`, `list_note_proposals` scopes a non-reviewer's queue to
    `principal.oid`, and the chemist who asked cannot find the PR opened on their behalf.

    This was missed in the first pass: the memory-note path was fixed and this one was not, while a
    comment on `ReportRequest.requested_by` claimed both were.
    """
    if not requested_by:
        return await propose_note(report_note(report), default_submitter())
    token = set_current_identity(requested_by, frozenset())
    try:
        return await propose_note(report_note(report), default_submitter())
    finally:
        reset_current_identity(token)


@durable_workflow("background")
@workflow.defn
class ReportSectionWorkflow:
    """Retrieve one report section durably — the fan-out unit of a report (plan F10-D2).

    Each section is its own child workflow so a long report resumes section by section after a
    worker restart. A section whose retrieval exhausts its retries does not fail (and so is not
    silently dropped) the report: the child degrades to a placeholder section marked
    `retrieval_failed`, so the assembled draft shows the gap explicitly for the chemist at the
    PR-gate. The activity carries the single retry boundary (`BAD_DATA_RETRY`); the fan-out does not
    layer a second child-level retry on top.
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
                schedule_to_start_timeout=queue_wait_timeout(),
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

    **The degradation contract is enforced here because it is claimed here.**
    `ReportSectionWorkflow` degrades gracefully for the one failure it catches, `ActivityError`;
    every other way a child can
    end — its `execution_timeout` at `fan_out_child_timeout_seconds`, a cancellation, a failure
    raised outside the `execute_activity` call — is *dropped* by `fan_out`, which is that helper's
    documented contract and returns a shorter list. The draft then omitted the section while the
    summary reported the smaller count, so a reviewer at the PR-gate could not tell a missing
    section from one nobody asked for. Making the invariant depend on which exception a child
    happened to raise is what made it untrue.

    Matched by heading and consumed in order, so a report that legitimately repeats a heading gets
    one placeholder for each child that did not come back rather than one for all of them.
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
        # Not logged again: `fan_out` already logs and counts every child it drops
        # (`chemclaw_fan_out_children_dropped_total`). What is missing there is the *report*, and
        # that is what this returns.
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
@workflow.defn
class DevelopmentReportWorkflow:
    """Draft a report durably, fanning sections out to child workflows, then PR-gate the draft."""

    @workflow.run
    async def run(self, request: ReportRequest) -> ConnectorJobResult:
        """Fan each section out to a child workflow, then propose the assembled draft note.

        Sections are retrieved as independent child workflows (bounded parallelism). Each child owns
        its own retry (the activity's `BAD_DATA_RETRY`) and degrades a failed section to a visible
        `retrieval_failed` marker, so every requested section appears in the draft in request order:
        a failure is shown, never silently missing (F10-D2). No child-level retry is layered here.

        **It returns the connector envelope, though it is not a connector's workflow** (D-115). The
        envelope is what `get_durable_job_status` reads, so a bare note-ref string made the report
        the one durable job a chemist could poll to `completed` and then have no tool that hands
        over the answer. Adopting the shape closes that, and it is the whole benefit the report
        would have got from moving into a bundle — the isolation half buys nothing, because its
        closure (the graph, the retrievers, the fingerprint store) is what core keeps for
        `gather_evidence` regardless.

        It still publishes its own note rather than returning one for core to gate, and that is
        correct here for the reason it would be wrong in a bundle: the note *reference* is this
        workflow's result, so publishing is the work, not a side effect — and this is core's own
        workflow, on the side of the boundary the PR-gate lives on.
        """
        sections = await fan_out(
            ReportSectionWorkflow,
            [
                SectionRequest(
                    section=section,
                    requested_by=request.requested_by,
                    requested_roles=request.requested_roles,
                )
                for section in request.sections
            ],
            id_prefix="section",
        )
        report = Report(title=request.title, sections=_reconcile(request.sections, sections))
        # The note reference *is* this workflow's result, so the publish is not
        # best-effort — but it shares the bounded-attempts discipline (G4).
        note_ref = await publish_note(propose_report, [report, request.requested_by])
        return ConnectorJobResult(
            summary=(
                # `report.sections`, not the fan-out's return: after reconciliation that is one
                # per requested section, so the count the chemist is told is the count they asked
                # for. Reading the short list is how "Drafted 'X' with 2 section(s)" came to be a
                # true sentence about a report that was missing one.
                f"Drafted {request.title!r} with {len(report.sections)} section(s); "
                f"opened for review as {note_ref}."
            ),
            data={"note_ref": note_ref, "title": request.title, "sections": len(report.sections)},
        )
