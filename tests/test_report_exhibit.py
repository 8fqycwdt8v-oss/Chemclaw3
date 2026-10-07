"""A development report asked for in a conversation lands there as a `document` artefact.

Against a real database: revision 1 of artefact `xb-` + sha256(workflow id)[:16], authored by
the requester; a retry writes nothing twice; a deleted session is skipped. Through the workflow
on Temporal: `exhibit_id` rides beside `note_id`, the `exhibit` push precedes completion, and a
request naming no session issues none of it.
"""

import hashlib
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from temporalio.client import Client
from temporalio.worker import Worker

import chemclaw.durable.report_workflow as report_workflow
from chemclaw.agent.session_events import claim_unconsumed
from chemclaw.agent.session_store import SessionOwnerStore
from chemclaw.api.routes.streams import _exhibit_event
from chemclaw.core.config import settings
from chemclaw.durable.notify import record_session_event_activity
from chemclaw.durable.orchestrator import resolve_fan_out_limit
from chemclaw.durable.report_workflow import (
    DevelopmentReportWorkflow,
    ReportExhibitInput,
    ReportSectionWorkflow,
    propose_report,
    record_report_exhibit,
    record_report_note,
    report_exhibit_id,
    retrieve_section,
)
from chemclaw.exhibits.models import PUSH_KIND, DocumentSpec
from chemclaw.exhibits.store import default_exhibit_store
from chemclaw.retrieval.harness import ReportRequest, ReportSection
from tests.conftest import FakeWriter
from tests.pg import migrated_db_or_skip
from tests.temporal_env import pydantic_client, start_env_or_skip


@pytest.fixture
def durable_sessions(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The Postgres session layer, as a deployment that keeps artefacts durably runs it."""
    monkeypatch.setattr(settings, "session_store", "postgres")
    yield


async def _session(owner: str = "ana@corp") -> str:
    """A session that exists — the conversation the report was asked from."""
    await migrated_db_or_skip()
    session = uuid4().hex
    await SessionOwnerStore().record(session, owner)
    return session


def _input(session: str, workflow_id: str) -> ReportExhibitInput:
    return ReportExhibitInput(
        session_id=session,
        exhibit_id=report_exhibit_id(workflow_id),
        title="Widget development",
        markdown="# Widget development\n\n## Yield\n\nYield 85% [[reaction-a]].\n",
        requested_by="ana@corp",
        correlation_id="corr-7",
    )


def test_the_id_is_the_workflows_and_in_the_minted_shape() -> None:
    """`xb-` and the first sixteen hex of sha256(workflow id) — the contract's derivation."""
    expected = "xb-" + hashlib.sha256(b"report-abc").hexdigest()[:16]
    assert report_exhibit_id("report-abc") == expected


async def test_a_retry_returns_the_artefact_the_first_attempt_wrote(durable_sessions: None) -> None:
    """Twice, as Temporal retries a committed attempt: one artefact, one revision, one push."""
    session = await _session()
    request = _input(session, f"report-{uuid4().hex}")
    first = await record_report_exhibit(request)
    again = await record_report_exhibit(request)
    assert first == again == request.exhibit_id

    store = default_exhibit_store()
    view = await store.view(session, first)
    assert view is not None
    assert (view.kind, view.title, view.revision, view.author_kind, view.author) == (
        "document",
        "Widget development",
        1,
        "agent",
        "ana@corp",
    )
    assert isinstance(view.spec, DocumentSpec) and view.spec.markdown == request.markdown
    assert [header.exhibit_id for header in await store.headers(session)] == [first]
    pushed = await claim_unconsumed(session)
    assert [(event.kind, event.payload["exhibit_id"], event.payload["op"]) for event in pushed] == [
        (PUSH_KIND, first, "created")
    ]
    announced = _exhibit_event(pushed[0].payload)
    assert announced is not None and announced.call_id == "", "a report is no tool call's write"


async def test_a_session_deleted_since_it_asked_is_skipped(durable_sessions: None) -> None:
    """No session row: `""`, and nothing written for a conversation nobody can open."""
    await migrated_db_or_skip()
    gone = uuid4().hex
    assert await record_report_exhibit(_input(gone, f"report-{uuid4().hex}")) == ""
    assert await default_exhibit_store().headers(gone) == []
    assert await claim_unconsumed(gone) == []


class _Retriever:
    name = "fake"

    async def retrieve(self, query: str, filters: dict[str, Any]) -> list[Any]:
        return []


async def _run_report(monkeypatch: pytest.MonkeyPatch, request: ReportRequest, run_id: str) -> Any:
    """The real workflow on the time-skipping server, with every activity this path schedules."""
    monkeypatch.setattr(report_workflow, "default_retrievers", lambda: [_Retriever()])
    monkeypatch.setattr(report_workflow, "default_writer", lambda: FakeWriter())
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
                record_report_exhibit,
                record_session_event_activity,
            ],
        ):
            return await client.execute_workflow(
                DevelopmentReportWorkflow.run,
                request,
                id=run_id,
                task_queue=settings.background_task_queue,
            )


def _request(session: str = "") -> ReportRequest:
    return ReportRequest(
        title="Widget development",
        requested_by="ana@corp",
        sections=[ReportSection(heading="Yield", query="yield trend", memory_layer="evidence")],
        session_id=session,
    )


async def test_a_report_from_a_session_is_shown_there_and_its_completion_names_it(
    durable_sessions: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`exhibit` then `job_completed`, the latter's summary carrying `exhibit_id` and `note_id`."""
    session = await _session()
    run_id = f"report-{uuid4().hex}"
    result = await _run_report(monkeypatch, _request(session), run_id)
    xid = report_exhibit_id(run_id)
    assert result.data["exhibit_id"] == xid

    pushed = await claim_unconsumed(session)
    assert [event.kind for event in pushed] == [PUSH_KIND, "job_completed"]
    completed = pushed[1].payload
    assert completed["job_id"] == run_id and completed["exhibit_id"] == xid
    assert str(completed["note_id"]).startswith("report-")
    view = await default_exhibit_store().view(session, xid)
    assert view is not None and view.title == "Widget development"
    assert isinstance(view.spec, DocumentSpec) and "## Yield" in view.spec.markdown


async def test_a_report_from_no_session_issues_none_of_it(
    durable_sessions: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No session named — every history started before the field existed — no new command."""
    await migrated_db_or_skip()
    result = await _run_report(monkeypatch, _request(), f"report-{uuid4().hex}")
    assert "exhibit_id" not in result.data


def test_a_second_session_rejoining_the_run_is_not_handed_an_artefact_it_cannot_open() -> None:
    """The run is shared; the artefact is the origin's. Elsewhere the id is dropped, the note kept.

    Read through `completed_job_status` with the reading session bound, and with none; the origin's
    session id never leaves.
    """
    from chemclaw.agent.durable_tools import completed_job_status
    from chemclaw.core.session_context import reset_current_session_id, set_current_session_id
    from chemclaw.durable.connector_job import ConnectorJobResult

    raw = ConnectorJobResult(
        summary="Drafted 'W'",
        data={
            "note_ref": "commit://1",
            "exhibit_id": "xb-00000000000000ab",
            "exhibit_session": "a",
        },
    )

    def _read_in(session: str | None) -> dict[str, Any]:
        token = set_current_session_id(session) if session else None
        try:
            return dict(completed_job_status("report-x", raw.model_dump()).result)
        finally:
            if token is not None:
                reset_current_session_id(token)

    assert _read_in("a") == {"note_ref": "commit://1", "exhibit_id": "xb-00000000000000ab"}
    assert _read_in("b") == {"note_ref": "commit://1"}
    assert _read_in(None) == {"note_ref": "commit://1"}


async def test_a_requester_who_is_not_in_the_session_writes_nothing_there(
    durable_sessions: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A requester who is not the session's owner or an admitted member writes nothing there.

    A workflow payload is not an authenticated request, so the session rule is re-checked.
    """
    from chemclaw.agent.session_members import session_member_store

    monkeypatch.setattr(settings, "entra_required", True)
    session_member_store.cache_clear()
    session = await _session(owner="ana@corp")
    try:
        stranger = _input(session, f"report-{uuid4().hex}").model_copy(
            update={"requested_by": "eve@corp"}
        )
        assert await record_report_exhibit(stranger) == ""
        assert await default_exhibit_store().headers(session) == []
        assert await claim_unconsumed(session) == []

        await session_member_store().add(session, "ben@corp")
        member = _input(session, f"report-{uuid4().hex}").model_copy(
            update={"requested_by": "ben@corp"}
        )
        assert await record_report_exhibit(member) == member.exhibit_id
        owner = _input(session, f"report-{uuid4().hex}")
        assert await record_report_exhibit(owner) == owner.exhibit_id
    finally:
        session_member_store.cache_clear()
