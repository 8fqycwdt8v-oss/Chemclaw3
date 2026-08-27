"""Server-backed test for the durable development-report workflow (plan 5b.5/5b.6).

Runs the real `DevelopmentReportWorkflow` on Temporal's time-skipping server (CI; skips
offline), proving the durable path drafts a sectioned, cited report and PR-gates it, with
retrievers and submitter swapped via the module factories (no database or git).
"""

import asyncio
from typing import Any
from unittest import mock

import pytest
from temporalio.client import Client
from temporalio.worker import Worker

import chemclaw.durable.report_workflow as report_workflow
from chemclaw.agent.durable_tools import _report_id
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
    Report,
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
            requested_by="chemist@corp",
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
            requested_by="chemist@corp",
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


def test_a_report_run_is_not_shared_across_entitlements() -> None:
    """Two chemists with different roles must not share one report run.

    `_report_id` keyed on title+sections only, which was sound while a report read the same corpus
    for everyone — and stopped being sound the moment `retrieve_section` began reading
    entitlement-gated sources as the requester. Alice holding the share role launches a report and
    the gated documents land in the draft; Bob asks for the same title and sections, gets the same
    id back from `WorkflowAlreadyStartedError`, and `job_status()` applies no actor check at all
    (`find_past_jobs` explicitly hands people other chemists' job ids for exactly that call). Bob
    collects a report built from a corpus his AD group excludes him from.

    So this is access control living in an id, not idempotency. Two chemists with the *same*
    entitlement still share a run, which is where the idempotency argument was true all along.
    """
    sections = [ReportSection(heading="Scope", query="what is known", memory_layer="evidence")]

    def _request(actor: str, roles: list[str]) -> ReportRequest:
        return ReportRequest(
            title="Route scouting", sections=sections, requested_by=actor, requested_roles=roles
        )

    entitled = _report_id(_request("alice@corp", ["chemclaw.sharedrive.reader"]))
    unentitled = _report_id(_request("bob@corp", []))
    assert entitled != unentitled, "a chemist without the share role must not join an entitled run"
    assert entitled == _report_id(_request("alice@corp", ["chemclaw.sharedrive.reader"])), (
        "the same request from the same person must still be idempotent"
    )
    assert _report_id(_request("carol@corp", ["a", "b"])) == _report_id(
        _request("carol@corp", ["b", "a"])
    ), "role order is not a different entitlement"


def test_re_asking_for_a_report_rejoins_the_run_when_the_model_rephrases_it() -> None:
    """The idempotency this tool advertises has to survive its actual caller, which is an LLM.

    `_report_id` was byte-exact over model-written text, so "re-requesting the same title and
    sections returns the existing job" held only for a byte-identical request. Measured before the
    fix, against one base request: sections swapped -> different id, title re-cased -> different,
    a heading re-cased -> different, a trailing space on a query -> different. Every one of those
    starts a second unbounded multi-section research run, which is the cost
    `CORE_EXPENSIVE_ACTIONS` gates this tool to avoid.
    """
    base = ReportRequest(
        title="Route X",
        sections=[
            ReportSection(heading="Scope", query="what is known", memory_layer="evidence"),
            ReportSection(heading="Cost", query="what does it cost", memory_layer="episodic"),
        ],
        requested_by="alice@corp",
        requested_roles=["r"],
    )
    rephrased = ReportRequest(
        # Re-cased title, sections swapped, a heading re-cased, a query with stray whitespace.
        title="route  x ",
        sections=[
            ReportSection(heading="cost", query="what does it cost", memory_layer="episodic"),
            ReportSection(heading="Scope", query=" what is  known", memory_layer="evidence"),
        ],
        requested_by="alice@corp",
        requested_roles=["r"],
    )
    assert _report_id(base) == _report_id(rephrased)


def test_canonicalising_a_report_id_does_not_reach_the_entitlement_key() -> None:
    """The canonicalisation must stop at the free text, or it undoes the test above it.

    Folding case over `requested_by` or the roles would merge two spellings of a principal or a
    role name into one run — which is exactly the cross-user merge that putting them in the id
    prevents. `memory_layer` is a closed set and is left exact for the same reason: a fold there
    could only ever collapse two layers, never rescue a typo.
    """
    sections = [ReportSection(heading="Scope", query="what is known", memory_layer="evidence")]

    def _request(actor: str, roles: list[str]) -> ReportRequest:
        return ReportRequest(
            title="Route X", sections=sections, requested_by=actor, requested_roles=roles
        )

    assert _report_id(_request("alice@corp", ["r"])) != _report_id(_request("Alice@Corp", ["r"]))
    assert _report_id(_request("alice@corp", ["r"])) != _report_id(_request("alice@corp", ["R"]))
    layers = [
        _report_id(
            ReportRequest(
                title="Route X",
                sections=[ReportSection(heading="Scope", query="q", memory_layer=layer)],
                requested_by="alice@corp",
                requested_roles=["r"],
            )
        )
        for layer in ("evidence", "episodic", "semantic")
    ]
    assert len(set(layers)) == 3, "two memory layers are two different reports"


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


def test_a_section_with_no_requester_stamps_no_identity() -> None:
    """Absent means absent — the fan-out payload must not acquire a synthetic actor.

    The counterweight to the test above: stamping a requester's roles widens what the run can read,
    so it must happen only when there *is* a requester.

    Scoped to `SectionRequest`, not `ReportRequest`, and the distinction matters. An earlier version
    of this test claimed to cover "a scheduled report" — there is no scheduled-report launcher, and
    `require_actor()` never returns `""`, so that branch was unreachable and the test proved nothing
    about production. `ReportRequest.requested_by` is now `min_length=1`. What remains true is that
    the activity must not invent an identity when handed a payload without one.
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


def test_a_dropped_fan_out_child_still_appears_in_the_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gap is shown, never silently missing — whatever exception the child happened to raise.

    `ReportSectionWorkflow` degrades gracefully for the *one* failure it catches, `ActivityError`.
    Every other way a child can end — its `execution_timeout` at `fan_out_child_timeout_seconds`,
    a cancellation, a failure raised outside the `execute_activity` call — is dropped by `fan_out`,
    which is its documented contract ("a child that fails after its retries is logged and
    omitted") and returns a *shorter* list. The assembled draft then omitted the section entirely
    while the summary said "Drafted 'X' with N section(s)" for the smaller N — so a reviewer at the
    PR-gate reads a report whose missing section is indistinguishable from one nobody asked for.

    Driven by handing the workflow exactly what `fan_out` hands it — a short list — because that is
    the whole input the reconciliation has to work from.
    """
    requested = [
        ReportSection(heading="Yield", query="yield trend", memory_layer="episodic"),
        ReportSection(heading="Safety", query="hazards", memory_layer="semantic"),
        ReportSection(heading="Cost", query="cost", memory_layer="episodic"),
    ]

    async def _short_fan_out(*args: object, **kwargs: object) -> list[SynthesizedSection]:
        """Two of three children came back — the middle one was dropped."""
        return [
            SynthesizedSection(heading="Yield", memory_layer="episodic", evidence=[]),
            SynthesizedSection(heading="Cost", memory_layer="episodic", evidence=[]),
        ]

    drafted: list[Report] = []

    async def _capture_publish(*args: Any, **kwargs: Any) -> str:
        drafted.append(args[1][0])
        return "pr://note/report-x"

    monkeypatch.setattr(report_workflow, "fan_out", _short_fan_out)
    monkeypatch.setattr(report_workflow, "publish_note", _capture_publish)

    result = asyncio.run(
        report_workflow.DevelopmentReportWorkflow().run(
            ReportRequest(
                title="Widget development", requested_by="chemist@corp", sections=requested
            )
        )
    )

    report = drafted[0]
    assert [s.heading for s in report.sections] == ["Yield", "Safety", "Cost"], (
        "the dropped child's section vanished from the draft rather than being marked"
    )
    assert [s.retrieval_failed for s in report.sections] == [False, True, False]
    # And the count the chemist is told matches the count they asked for.
    assert result.data["sections"] == len(requested)
    assert "with 3 section(s)" in result.summary
