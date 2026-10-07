"""Server-backed tests for the durable development-report workflow.

Runs the real `DevelopmentReportWorkflow` on Temporal's time-skipping server (skips offline),
with retrievers and the writer swapped via the module factories.
"""

import asyncio
import inspect
from typing import Any
from unittest import mock

import pytest
from temporalio.client import Client
from temporalio.worker import Worker

import chemclaw.durable.report_workflow as report_workflow
from chemclaw.agent.durable_tools import _report_id
from chemclaw.core.config import settings
from chemclaw.core.identity_context import get_current_actor, get_current_roles
from chemclaw.durable.interceptor import activity_context
from chemclaw.durable.orchestrator import resolve_fan_out_limit
from chemclaw.durable.report_workflow import (
    DevelopmentReportWorkflow,
    ReportSectionWorkflow,
    propose_report,
    record_report_note,
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
from tests.conftest import FakeWriter
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
    """`default_retrievers` honours `settings.data_sources`, not a hardcoded `GraphRetriever`.

    A report section's query needs the same source registry as `gather_evidence`.
    """
    sentinel = _FakeRetriever()
    monkeypatch.setattr(report_workflow, "active_retrieve_sources", lambda: [sentinel])
    retrievers = report_workflow.default_retrievers()
    assert sentinel in retrievers
    assert any(r.name == "reaction-fingerprint" for r in retrievers)


async def test_report_workflow_drafts_and_pr_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """The workflow retrieves each section durably and proposes one cited report note."""
    fake = FakeWriter()
    monkeypatch.setattr(report_workflow, "default_retrievers", lambda: [_FakeRetriever()])
    monkeypatch.setattr(report_workflow, "default_writer", lambda: fake)

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
            activities=[
                retrieve_section,
                record_report_note,
                propose_report,
                resolve_fan_out_limit,
            ],
        ):
            result = await client.execute_workflow(
                DevelopmentReportWorkflow.run,
                request,
                id="report-test",
                task_queue=settings.background_task_queue,
            )
    # The envelope, so `get_durable_job_status` can hand the finished report back in one call.
    assert result.data["note_ref"].startswith("commit://1")
    assert result.data["sections"] == 2
    assert "Widget development" in result.summary
    body = fake.writes[0].files[0].content
    assert "[[reaction-a]]" in body  # the supported section cites its source
    assert "No supporting data found" in body  # the safety section is marked, not invented


async def test_failed_section_is_marked_not_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A section whose retrieval errors is shown as failed in the draft, never silently missing."""
    fake = FakeWriter()
    monkeypatch.setattr(report_workflow, "default_retrievers", lambda: [_FailingRetriever()])
    monkeypatch.setattr(report_workflow, "default_writer", lambda: fake)

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
            activities=[
                retrieve_section,
                record_report_note,
                propose_report,
                resolve_fan_out_limit,
            ],
        ):
            await client.execute_workflow(
                DevelopmentReportWorkflow.run,
                request,
                id="report-fail-test",
                task_queue=settings.background_task_queue,
            )
    body = fake.writes[0].files[0].content
    assert "## Yield" in body  # the section still appears (not dropped)
    assert "Retrieval failed" in body  # and is explicitly marked incomplete


def test_background_worker_registers_report_workflow() -> None:
    """The report workflow + activities are wired onto the background worker (regression)."""
    from chemclaw.durable.background_worker import BACKGROUND_ACTIVITIES, BACKGROUND_WORKFLOWS

    assert DevelopmentReportWorkflow in BACKGROUND_WORKFLOWS
    assert ReportSectionWorkflow in BACKGROUND_WORKFLOWS  # the fan-out child must be registered too
    assert retrieve_section in BACKGROUND_ACTIVITIES
    assert propose_report in BACKGROUND_ACTIVITIES


def test_a_report_run_is_not_shared_across_entitlements() -> None:
    """Two chemists with different roles must not share one report run.

    Sections read entitlement-gated sources as the requester and `job_status()` has no actor check,
    so the run id carries the entitlement. Chemists with the same entitlement still share a run.
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
    """Re-asking for a report rejoins the run when the model rephrases it.

    The caller is an LLM, so reordering sections, re-casing or trailing spaces must not start a
    second expensive run.
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
    """Canonicalisation stops at the free text and never folds the requester or roles.

    Folding those would merge two principals into one run; `memory_layer` is a closed set and
    stays exact too.
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


async def test_a_report_carries_its_requester_into_retrieval() -> None:
    """A report carries its requester into retrieval.

    Activities have no ambient identity, and a gated retriever declines silently without one, so
    the draft would read as a complete sweep. Asserted at the activity, the only place the identity
    has to be true.
    """
    seen: list[tuple[str, frozenset[str]]] = []

    async def _record(section: ReportSection, retrievers: object) -> SynthesizedSection:
        seen.append((get_current_actor() or "", get_current_roles()))
        return SynthesizedSection(
            heading=section.heading, memory_layer=section.memory_layer, evidence=[]
        )

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

    # The actor crosses into the activity but the roles do not: a workflow payload is relayed data,
    # not a verified claim, so roles bind to the empty set (fail closed). Role-scoped durable
    # retrieval would need a signed payload.
    assert seen == [("alice@corp", frozenset())]


async def test_a_section_with_no_requester_stamps_no_identity() -> None:
    """A section with no requester stamps no identity; the activity must not invent one."""
    seen: list[str] = []

    async def _record(section: ReportSection, retrievers: object) -> SynthesizedSection:
        seen.append(get_current_actor() or "<none>")
        return SynthesizedSection(
            heading=section.heading, memory_layer=section.memory_layer, evidence=[]
        )

    with mock.patch.object(report_workflow, "gather_section", _record):
        await report_workflow.retrieve_section(
            SectionRequest(
                section=ReportSection(
                    heading="Scope", query="what is known", memory_layer="evidence"
                )
            )
        )

    assert seen == ["<none>"]


def test_a_dropped_fan_out_child_still_appears_in_the_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dropped fan-out child still appears in the draft as a gap.

    `fan_out` omits a child that ends in anything other than a caught `ActivityError` and returns a
    shorter list; the draft must show the missing section rather than read as complete. Driven by
    handing the workflow that short list.
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
        return "commit://1"

    async def _no_delivery(_message: Any) -> list[str]:
        """Outbound delivery is not this test's subject; the workflow calls it unconditionally."""
        return []

    monkeypatch.setattr(report_workflow, "fan_out", _short_fan_out)
    monkeypatch.setattr(report_workflow, "publish_note", _capture_publish)
    monkeypatch.setattr(report_workflow, "deliver_best_effort", _no_delivery)

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


async def test_forged_payload_roles_do_not_reach_the_gate() -> None:
    """A privileged role named in the workflow payload does not satisfy authorization.

    Binders leave the ambient roles empty regardless of the payload, so enqueueing a workflow cannot
    claim a role.
    """
    from chemclaw.core.identity_context import get_current_roles

    seen: list[frozenset[str]] = []

    async def _record(section: ReportSection, retrievers: object) -> SynthesizedSection:
        seen.append(get_current_roles())
        return SynthesizedSection(
            heading=section.heading, memory_layer=section.memory_layer, evidence=[]
        )

    with mock.patch.object(report_workflow, "gather_section", _record):
        await report_workflow.retrieve_section(
            SectionRequest(
                section=ReportSection(heading="H", query="q", memory_layer="evidence"),
                requested_by="mallory@evil.example",
                requested_roles=["Chemclaw.Admin", "Chemclaw.Privileged"],
            )
        )

    assert seen == [frozenset()], "a payload-declared privileged role reached the gate"


def test_a_report_run_carries_the_turn_that_asked_for_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The correlation id reaches both activity boundaries and the outbound report message.

    `durable/interceptor.py` binds ids from an activity's own arguments, so this asserts the id is
    on each child's payload and on the writer's arguments, read through `activity_context`, which is
    what the interceptor uses.
    """
    launched: list[SectionRequest] = []

    async def _capture_fan_out(_workflow: object, requests: Any, **_: object) -> list[Any]:
        launched.extend(requests)
        return [
            SynthesizedSection(heading=r.section.heading, memory_layer="evidence", evidence=[])
            for r in requests
        ]

    published: list[list[Any]] = []

    async def _capture_publish(*args: Any, **kwargs: Any) -> str:
        published.append(list(args[1]))
        return "commit://1"

    sent: list[Any] = []

    async def _capture_delivery(message: Any) -> list[str]:
        sent.append(message)
        return []

    monkeypatch.setattr(report_workflow, "fan_out", _capture_fan_out)
    monkeypatch.setattr(report_workflow, "publish_note", _capture_publish)
    monkeypatch.setattr(report_workflow, "deliver_best_effort", _capture_delivery)

    asyncio.run(
        report_workflow.DevelopmentReportWorkflow().run(
            ReportRequest(
                title="Widget development",
                requested_by="chemist@corp",
                correlation_id="corr-42",
                sections=[
                    ReportSection(heading="Yield", query="yield trend", memory_layer="evidence")
                ],
            )
        )
    )

    # The child's payload — a model, so the interceptor reads it by field name.
    assert activity_context(list(launched), fn=retrieve_section).correlation_id == "corr-42"
    # The draft's activity — bare strings beside a model-authored payload, so the interceptor reads
    # it by parameter name off the signature. Positional, which is how Temporal invokes it.
    assert activity_context(published[0], fn=propose_report).correlation_id == "corr-42"
    assert activity_context(published[0], fn=propose_report).actor == "chemist@corp"
    # And the copy that leaves the building, addressed to the chemist who asked.
    assert [(m.kind, m.recipient, m.correlation_id) for m in sent] == [
        ("report", "chemist@corp", "corr-42")
    ]


def test_a_report_launched_outside_a_turn_stays_unjoined() -> None:
    """A report launched outside a turn stays unjoined; a fabricated id would look joined."""
    request = SectionRequest(
        section=ReportSection(heading="Scope", query="what is known", memory_layer="evidence")
    )
    assert activity_context([request], fn=retrieve_section).correlation_id == ""


def test_the_old_activity_name_is_still_registered_and_still_writes() -> None:
    """The old activity name is still registered and still writes.

    In-flight histories have already scheduled `propose_report`; dropping it would retry them
    forever, so a rename takes two releases. Both registration and the write are asserted.
    """
    from chemclaw.durable.background_worker import BACKGROUND_ACTIVITIES
    from chemclaw.durable.registry import temporal_name

    registered = {temporal_name(activity) for activity in BACKGROUND_ACTIVITIES}
    assert {"propose_report", "record_report_note"} <= registered, (
        "a worker on `background-jobs` must offer both names for one deployment cycle; it offers "
        f"{sorted(name for name in registered if 'report' in name)}"
    )

    # The alias delegates rather than duplicating, so a replayed old task writes the same note.
    assert inspect.signature(propose_report) == inspect.signature(record_report_note), (
        "the alias's signature differs from the activity's; `durable/interceptor.py` binds an "
        "activity's ids by parameter name off the signature, so a replayed old task would be the "
        "one unattributed write on this path"
    )
