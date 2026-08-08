"""Server-backed test for the durable development-report workflow (plan 5b.5/5b.6).

Runs the real `DevelopmentReportWorkflow` on Temporal's time-skipping server (CI; skips
offline), proving the durable path drafts a sectioned, cited report and PR-gates it, with
retrievers and submitter swapped via the module factories (no database or git).
"""

import asyncio
from unittest import mock

import pytest
from temporalio.client import Client
from temporalio.worker import Worker

import chemclaw.durable.report_workflow as report_workflow
from chemclaw.core.config import settings
from chemclaw.core.identity_context import get_current_actor, get_current_roles
from chemclaw.durable.orchestrator import resolve_fan_out_limit
from chemclaw.durable.report_workflow import (
    DevelopmentReportWorkflow,
    ReportSectionWorkflow,
    propose_report,
    retrieve_section,
)
from chemclaw.retrieval.evidence import EvidenceChunk
from chemclaw.retrieval.harness import (
    ReportRequest,
    ReportSection,
    SectionRequest,
    SynthesizedSection,
)
from tests.conftest import FakeSubmitter
from tests.temporal_env import pydantic_client, start_env_or_skip


class _FakeRetriever:
    name = "fake"

    async def retrieve(self, query: str, filters: dict) -> list[EvidenceChunk]:  # type: ignore[type-arg]
        if "yield" in query:
            return [
                EvidenceChunk(content="Yield 85%.", source_note_id="reaction-a", retriever="fake")
            ]
        return []


class _FailingRetriever:
    name = "boom"

    async def retrieve(self, query: str, filters: dict) -> list[EvidenceChunk]:  # type: ignore[type-arg]
        from chemclaw.core.errors import ChemclawError

        raise ChemclawError("retriever exploded")  # non-retryable → activity fails fast


def test_default_retrievers_uses_the_configured_source_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`default_retrievers` must honor `settings.data_sources`, not a hardcoded `GraphRetriever`.

    A report section's query is prose exactly like a conversational turn's, so it needs the
    same source registry `chemclaw.agent.research_tools.gather_evidence` fans out over — a
    deployment
    that turns on hybrid (vector/lexical) retrieval must not have to remember to also flip it
    here (D-018). No Temporal/Postgres needed: this is a direct call, not a workflow run.
    """
    sentinel = _FakeRetriever()
    monkeypatch.setattr(report_workflow, "active_retrieve_sources", lambda: [sentinel])
    retrievers = report_workflow.default_retrievers()
    assert sentinel in retrievers
    assert any(r.name == "reaction-fingerprint" for r in retrievers)


def test_report_workflow_drafts_and_pr_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """The workflow retrieves each section durably and proposes one cited report note."""
    fake = FakeSubmitter()
    monkeypatch.setattr(report_workflow, "default_retrievers", lambda: [_FakeRetriever()])
    monkeypatch.setattr(report_workflow, "default_submitter", lambda: fake)

    async def _run() -> None:
        request = ReportRequest(
            title="Widget development",
            sections=[
                ReportSection(heading="Yield", query="yield trend", memory_layer="episodic"),
                ReportSection(heading="Safety", query="hazard data", memory_layer="evidence"),
            ],
        )
        async with await start_env_or_skip() as env:
            client: Client = pydantic_client(env)
            async with Worker(
                client,
                task_queue=settings.background_task_queue,
                workflows=[DevelopmentReportWorkflow, ReportSectionWorkflow],
                activities=[retrieve_section, propose_report, resolve_fan_out_limit],
            ):
                result = await client.execute_workflow(
                    DevelopmentReportWorkflow.run,
                    request,
                    id="report-test",
                    task_queue=settings.background_task_queue,
                )
        # The envelope, so `get_durable_job_status` can hand the finished report back in one call.
        assert result.data["note_ref"].startswith("pr://note/report-")
        assert result.data["sections"] == 2
        assert "Widget development" in result.summary
        body = fake.submissions[0].files[0].content
        assert "[[reaction-a]]" in body  # the supported section cites its source
        assert "No supporting data found" in body  # the safety section is marked, not invented

    asyncio.run(_run())


def test_failed_section_is_marked_not_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A section whose retrieval errors is shown as failed in the draft, never silently missing."""
    fake = FakeSubmitter()
    monkeypatch.setattr(report_workflow, "default_retrievers", lambda: [_FailingRetriever()])
    monkeypatch.setattr(report_workflow, "default_submitter", lambda: fake)

    async def _run() -> None:
        request = ReportRequest(
            title="Widget development",
            sections=[
                ReportSection(heading="Yield", query="yield trend", memory_layer="episodic"),
            ],
        )
        async with await start_env_or_skip() as env:
            client: Client = pydantic_client(env)
            async with Worker(
                client,
                task_queue=settings.background_task_queue,
                workflows=[DevelopmentReportWorkflow, ReportSectionWorkflow],
                activities=[retrieve_section, propose_report, resolve_fan_out_limit],
            ):
                await client.execute_workflow(
                    DevelopmentReportWorkflow.run,
                    request,
                    id="report-fail-test",
                    task_queue=settings.background_task_queue,
                )
        body = fake.submissions[0].files[0].content
        assert "## Yield" in body  # the section still appears (not dropped)
        assert "Retrieval failed" in body  # and is explicitly marked incomplete

    asyncio.run(_run())


def test_background_worker_registers_report_workflow() -> None:
    """The report workflow + activities are wired onto the background worker (regression)."""
    from chemclaw.durable.background_worker import BACKGROUND_ACTIVITIES, BACKGROUND_WORKFLOWS

    assert DevelopmentReportWorkflow in BACKGROUND_WORKFLOWS
    assert ReportSectionWorkflow in BACKGROUND_WORKFLOWS  # the fan-out child must be registered too
    assert retrieve_section in BACKGROUND_ACTIVITIES
    assert propose_report in BACKGROUND_ACTIVITIES


def test_a_report_carries_its_requester_into_retrieval() -> None:
    """The gap: a gated source contributed nothing to a report, and the draft said so nowhere.

    `retrieve_section` runs in an activity, where no identity contextvar is set unless something
    puts one there. `ShareDocumentRetriever._entitled()` reads the ambient actor's roles and — quite
    correctly — declines when there is no actor, returning `[]` without ever reaching the index.
    `gather_section` only concatenates, so that outcome is indistinguishable from a source with no
    matches, and `retrieval_failed` stays False. The chemist received a draft that read as a
    complete sweep of every internal source while an entitlement-gated share had been skipped in
    silence.

    `ReportRequest` was the one user-launched durable job input with no actor field at all
    (`ConnectorJobInput.requested_by` and `TemplateRunInput.requested_by` are both `min_length=1`),
    and `request_development_report` called `require_actor()` and threw the result away.

    Asserted at the activity, because that is the only place the identity has to be true — a value
    that reaches the workflow and stops there is exactly the defect.
    """
    seen: list[tuple[str, frozenset[str]]] = []

    async def _record(section: ReportSection, retrievers: object) -> SynthesizedSection:
        seen.append((get_current_actor() or "", get_current_roles()))
        return SynthesizedSection(
            heading=section.heading, memory_layer=section.memory_layer, evidence=[]
        )

    async def _run() -> None:
        with mock.patch.object(report_workflow, "gather_section", _record):
            await report_workflow.retrieve_section(
                SectionRequest(
                    section=ReportSection(
                        heading="Scope", query="what is known", memory_layer="evidence"
                    ),
                    requested_by="alice@corp",
                    requested_roles=["chemclaw.sharedrive.reader"],
                )
            )

    asyncio.run(_run())
    assert seen == [("alice@corp", frozenset({"chemclaw.sharedrive.reader"}))]


def test_a_scheduled_report_stamps_no_identity() -> None:
    """Absent means absent — a background run must not acquire a synthetic actor.

    The counterweight to the test above: stamping the requester's roles widens what a background run
    can read, so it must happen only when there is a requester. A scheduled report has none and is
    bounded exactly as it was before.
    """
    seen: list[str] = []

    async def _record(section: ReportSection, retrievers: object) -> SynthesizedSection:
        seen.append(get_current_actor() or "<none>")
        return SynthesizedSection(
            heading=section.heading, memory_layer=section.memory_layer, evidence=[]
        )

    async def _run() -> None:
        with mock.patch.object(report_workflow, "gather_section", _record):
            await report_workflow.retrieve_section(
                SectionRequest(
                    section=ReportSection(
                        heading="Scope", query="what is known", memory_layer="evidence"
                    )
                )
            )

    asyncio.run(_run())
    assert seen == ["<none>"]
