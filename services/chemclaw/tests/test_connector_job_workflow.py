"""The durable path, end to end against a real Temporal server — the seam's central claim.

The claim under test is that a connector can own a durable capability while core keeps every
cross-cutting obligation, and that the only thing binding the two is a workflow *type name* and
a task
queue read from a manifest. So this runs the two workers a deployment runs:

- a **core** worker on the background queue hosting `ConnectorJobWorkflow` and the real PR-gate and
  push-back activities;
- a **connector** worker on the bundle's own queue hosting only the bundle's own workflow
  (`tests/fixtures/connectors/fixture/workflows.py`), which imports nothing from the wrapper.

The generated tool from the fixture manifest launches it, and the assertions are about what core
did on the connector's behalf: the envelope came back intact, the note went through the PR-gate
as an agent-authored proposal, and the launching session was woken.

The two activities are *registered for real* and only their side effects are stubbed — the git push
and the database insert — so the workflow's activity wiring (queue, timeouts, retry policy,
serialization) is genuinely exercised rather than replaced.

Skipped when the test server's binary cannot be fetched (the offline sandbox), like every other
Temporal-backed test here.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from temporalio.client import Client
from temporalio.worker import Worker

from connectors.jobs import build_job_tool, job_workflow_id
from connectors.registry import discovered, enabled
from tests.fixtures.connectors.fixture.workflows import FixtureJobWorkflow
from tests.temporal_env import pydantic_client, start_env_or_skip
from workflows.connector_job import ConnectorJobResult, ConnectorJobWorkflow
from workflows.memory_jobs import publish_memory_note_activity
from workflows.notify import record_session_event_activity

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "connectors"
_CONNECTOR_QUEUE = "connector-fixture"
_CORE_QUEUE = "background-jobs"
_SESSION = "session-under-test"
_ACTOR = "oid-under-test"
_EXPECTED_ID = job_workflow_id("fixture", "run_fixture_job", {"subject": "benzene"})


def test_the_wrapper_is_served_by_the_background_worker() -> None:
    """A deployment must actually host the wrapper, or every connector job would hang unstarted.

    Sandbox-safe on purpose: the end-to-end test below needs a Temporal server and is skipped
    offline, so the one property that would silently break *every* connector job — nobody
    polling the queue the generated tool starts work on — is pinned by a test that always runs.
    """
    from workers.background_worker import BACKGROUND_WORKFLOWS

    assert ConnectorJobWorkflow in BACKGROUND_WORKFLOWS


def test_a_connector_workflow_returns_a_well_formed_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The connector-side half of the contract, checked without a server.

    The envelope is the entire cross-process agreement, so it is worth asserting directly: a
    summary the chat can show, the job's own structured data, and an optional `Note` that has
    already passed the graph's slug/schema validators — which is what stops a malformed proposal
    from reaching the PR-gate and failing later at branch creation.

    `memo_value` is stubbed because there is no run outside a workflow to carry a memo; the real
    read is exercised end to end below, where core stamps it.
    """
    monkeypatch.setattr(
        "tests.fixtures.connectors.fixture.workflows.workflow.memo_value",
        lambda key, default="": default,
    )
    result = asyncio.run(FixtureJobWorkflow().run({"subject": "benzene"}))
    assert isinstance(result, ConnectorJobResult)
    assert result.summary == "fixture job ran on benzene"
    assert result.data["subject"] == "benzene" and result.data["ran"] is True
    assert result.note is not None
    assert result.note.id == "fixture-benzene"
    # `created_by="agent"` is what routes it through the PR-gate rather than straight into the
    # graph.
    assert result.note.created_by == "agent"


def _fixture_job_tool(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point the registry at the fixture bundle and return its generated launch tool.

    Built through the registry rather than hand-constructed, so this exercises the real manifest
    → tool path: a mistake in the fixture's YAML fails here as it would in production.
    """
    monkeypatch.setattr("chemclaw.config.settings.connectors_dir", str(_FIXTURE_DIR))
    monkeypatch.setattr("chemclaw.config.settings.connectors_enabled", "")
    discovered.cache_clear()
    (manifest,) = enabled()
    (job,) = manifest.jobs
    return build_job_tool(manifest.name, job)


def test_a_connector_job_runs_its_own_workflow_and_core_does_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole contract in one run: child on its own queue, note PR-gated, session woken."""
    published: list[Any] = []
    notified: list[tuple[str, str, dict[str, Any]]] = []

    async def _fake_propose(note: Any, _submitter: Any) -> str:
        """Capture the PR-gate proposal instead of pushing a git branch."""
        published.append(note)
        return f"note/{note.id}"

    async def _fake_record(
        session_id: str, kind: str, payload: dict[str, Any], *, dedupe_key: str | None = None
    ) -> None:
        """Capture the push-back event instead of inserting a `session_events` row."""
        notified.append((session_id, kind, payload))

    # Stub what the activities *do*, not the activities themselves — see the module docstring.
    monkeypatch.setattr("workflows.memory_jobs.propose_note", _fake_propose)
    monkeypatch.setattr("workflows.memory_jobs.default_submitter", lambda: object())
    monkeypatch.setattr("workflows.notify.record_session_event", _fake_record)
    monkeypatch.setattr("chemclaw.config.settings.background_task_queue", _CORE_QUEUE)
    tool = _fixture_job_tool(monkeypatch)

    async def _run() -> ConnectorJobResult:
        async with await start_env_or_skip() as env:
            client = pydantic_client(env)

            async def _connect() -> Client:
                return client

            monkeypatch.setattr("connectors.jobs.connect", _connect)
            core = Worker(
                client,
                task_queue=_CORE_QUEUE,
                workflows=[ConnectorJobWorkflow],
                activities=[publish_memory_note_activity, record_session_event_activity],
            )
            # Hosts ONLY the bundle's own workflow: were core's wrapper to need anything from
            # the connector beyond its type name, this worker could not serve the child at all.
            connector = Worker(client, task_queue=_CONNECTOR_QUEUE, workflows=[FixtureJobWorkflow])
            async with core, connector:
                # The session is ambient, never a model-supplied argument (F3-T3), so the tool
                # picks up which chat to wake exactly as it does mid-turn.
                from agents.identity_context import (
                    reset_current_identity,
                    set_current_identity,
                )
                from agents.session_context import reset_current_session_id, set_current_session_id

                token = set_current_session_id(_SESSION)
                identity = set_current_identity(_ACTOR, frozenset())
                try:
                    job_id = await tool(tool.__annotations__["params"](subject="benzene"))
                finally:
                    reset_current_identity(identity)
                    reset_current_session_id(token)
                # The tool returns immediately — the agent never blocks on a durable job — so
                # the id is what comes back, and the result is awaited separately as a poll
                # would.
                assert job_id == _EXPECTED_ID
                # `result_type` is required, not decoration. An untyped handle hands the pydantic
                # converter nothing to decode into, so `.result()` returns the raw `dict` and every
                # attribute read below fails with `'dict' object has no attribute 'summary'` — which
                # is exactly how this test failed the first time CI actually ran it (D-117). The
                # product path was fine throughout: `workflows/connector_job.py:106` already passes
                # `result_type` on the child call, verified against a live server.
                handle = client.get_workflow_handle(job_id, result_type=ConnectorJobResult)
                result: ConnectorJobResult = await handle.result()
                return result

    result = asyncio.run(_run())

    # The connector's own result crossed back through the envelope unchanged.
    assert result.summary == "fixture job ran on benzene"
    assert result.data["subject"] == "benzene" and result.data["ran"] is True
    # And the requesting actor reached the connector's own workflow — on the run's memo, so it
    # never became a payload field the model could author. This is the route the HPC job depends
    # on: its cluster submission runs under a shared service identity, and `requested_by` is the
    # only thing that makes it attributable (F4-T3, D-118).
    assert result.data["requested_by"] == _ACTOR
    # Core PR-gated the note the connector produced; the connector never touched the graph itself.
    assert [note.id for note in published] == ["fixture-benzene"]
    assert published[0].created_by == "agent"  # so a human must sign it off at the gate
    # Core woke the launching session, through the one existing push-back channel.
    assert len(notified) == 1
    session_id, kind, payload = notified[0]
    assert session_id == _SESSION
    assert kind == "job_completed"
    assert payload["connector"] == "fixture" and payload["job"] == "run_fixture_job"
    assert payload["summary"] == "fixture job ran on benzene"
