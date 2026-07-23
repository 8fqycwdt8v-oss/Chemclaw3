"""Durable development-report workflow (plan steps 5b.5, 5b.6) on the background queue.

The report is a MAF-style graph of sections; here each section is a Temporal activity, so a
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
    from chemclaw.config import settings
    from kg.git_submitter import default_submitter
    from kg.pr_gate import propose_note
    from mcp_servers.fpstore import default_reaction_store
    from report.evidence import SourceRetriever
    from report.harness import (
        Report,
        ReportRequest,
        ReportSection,
        SynthesizedSection,
        gather_section,
        report_note,
    )
    from report.retrievers import FingerprintReactionRetriever, GraphRetriever

from workflows.orchestrator import fan_out
from workflows.publish import BAD_DATA_RETRY, publish_note


def default_retrievers() -> list[SourceRetriever]:
    """The production source retrievers (graph + reaction fingerprint). Overridden in tests."""
    return [GraphRetriever(), FingerprintReactionRetriever(default_reaction_store())]


@activity.defn
async def retrieve_section(section: ReportSection) -> SynthesizedSection:
    """Retrieve one report section's evidence across the production sources."""
    return await gather_section(section, default_retrievers())


@activity.defn
async def propose_report(report: Report) -> str:
    """Render the gathered report as a PR-gated `report` note; return the reference."""
    return await propose_note(report_note(report), default_submitter())


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
    async def run(self, section: ReportSection) -> SynthesizedSection:
        """Retrieve the section; on activity failure, return a visible `retrieval_failed` marker."""
        try:
            return await workflow.execute_activity(
                retrieve_section,
                section,
                start_to_close_timeout=timedelta(seconds=settings.report_section_timeout_seconds),
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


@workflow.defn
class DevelopmentReportWorkflow:
    """Draft a report durably, fanning sections out to child workflows, then PR-gate the draft."""

    @workflow.run
    async def run(self, request: ReportRequest) -> str:
        """Fan each section out to a child workflow, then propose the assembled draft note.

        Sections are retrieved as independent child workflows (bounded parallelism). Each child owns
        its own retry (the activity's `BAD_DATA_RETRY`) and degrades a failed section to a visible
        `retrieval_failed` marker, so every requested section appears in the draft in request order:
        a failure is shown, never silently missing (F10-D2). No child-level retry is layered here.
        """
        sections = await fan_out(
            ReportSectionWorkflow,
            request.sections,
            id_prefix="section",
        )
        report = Report(title=request.title, sections=sections)
        # The note reference *is* this workflow's result, so the publish is not
        # best-effort — but it shares the bounded-attempts discipline (G4).
        return await publish_note(propose_report, [report])
