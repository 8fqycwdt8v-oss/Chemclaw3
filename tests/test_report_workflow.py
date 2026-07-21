"""Server-backed test for the durable development-report workflow (plan 5b.5/5b.6).

Runs the real `DevelopmentReportWorkflow` on Temporal's time-skipping server (CI; skips
offline), proving the durable path drafts a sectioned, cited report and PR-gates it, with
retrievers and submitter swapped via the module factories (no database or git).
"""

import asyncio

import pytest
from temporalio.client import Client
from temporalio.worker import Worker

import workflows.report_workflow as report_workflow
from chemclaw.config import settings
from report.evidence import EvidenceChunk
from report.harness import ReportRequest, ReportSection
from tests.conftest import FakeSubmitter
from tests.temporal_env import pydantic_client, start_env_or_skip
from workflows.report_workflow import (
    DevelopmentReportWorkflow,
    propose_report,
    retrieve_section,
)


class _FakeRetriever:
    name = "fake"

    async def retrieve(self, query: str, filters: dict) -> list[EvidenceChunk]:  # type: ignore[type-arg]
        if "yield" in query:
            return [
                EvidenceChunk(content="Yield 85%.", source_note_id="reaction-a", retriever="fake")
            ]
        return []


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
                workflows=[DevelopmentReportWorkflow],
                activities=[retrieve_section, propose_report],
            ):
                ref = await client.execute_workflow(
                    DevelopmentReportWorkflow.run,
                    request,
                    id="report-test",
                    task_queue=settings.background_task_queue,
                )
        assert ref.startswith("pr://note/report-")
        body = fake.submissions[0].content
        assert "[[reaction-a]]" in body  # the supported section cites its source
        assert "No supporting data found" in body  # the safety section is marked, not invented

    asyncio.run(_run())


def test_background_worker_registers_report_workflow() -> None:
    """The report workflow + activities are wired onto the background worker (regression)."""
    from workers.background_worker import BACKGROUND_ACTIVITIES, BACKGROUND_WORKFLOWS

    assert DevelopmentReportWorkflow in BACKGROUND_WORKFLOWS
    assert retrieve_section in BACKGROUND_ACTIVITIES
    assert propose_report in BACKGROUND_ACTIVITIES
