"""The durable path, end to end against a real Temporal server — the seam's central claim.

A connector owns a durable capability while core keeps the cross-cutting obligations, bound only
by a workflow type name and a task queue from the manifest. Two workers, as a deployment runs:

- a core worker on the background queue with `ConnectorJobWorkflow` and the real note-publishing
  and push-back activities;
- a connector worker on the bundle's own queue with only the bundle's workflow
  (`tests/fixtures/connectors/fixture/workflows.py`).

Activities are registered for real with only their side effects stubbed, so wiring (queue,
timeouts, retries, serialization) is exercised. Skipped when the test server cannot be fetched.
"""

import asyncio
import gc
import inspect
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel
from temporalio import workflow
from temporalio.client import Client, WorkflowFailureError
from temporalio.worker import Worker

from chemclaw.agent.session_events import record_session_event
from chemclaw.connectors.jobs import build_job_tool, job_workflow_id
from chemclaw.connectors.registry import enabled
from chemclaw.core.config import settings
from chemclaw.durable.connector_job import (
    ConnectorJobInput,
    ConnectorJobResult,
    ConnectorJobWorkflow,
    child_execution_timeout,
    wrapper_execution_timeout,
)
from chemclaw.durable.deliver_message import deliver_message_activity
from chemclaw.durable.job_record import JobRecord, record_job
from chemclaw.durable.memory_jobs import publish_memory_note_activity
from chemclaw.durable.notify import record_session_event_activity
from chemclaw.kg.note import Note
from chemclaw.kg.record import record_note
from chemclaw.memory.jobs import SynthesisUnit
from tests.fixtures.connectors.fixture.workflows import FixtureJobWorkflow
from tests.fixtures.foreign_result_workflow import ForeignResultWorkflow
from tests.temporal_env import pydantic_client, start_env_or_skip, start_local_env_or_skip

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "connectors"
_CONNECTOR_QUEUE = "connector-fixture"
_CORE_QUEUE = "background-jobs"
_SESSION = "session-under-test"
_ACTOR = "oid-under-test"
_EXPECTED_ID = job_workflow_id("fixture", "run_fixture_job", {"subject": "benzene"})

# One launch input whose job declared a 20 s ceiling, below the fleet-wide one.
_CEILING_JOB = ConnectorJobInput(
    connector="fixture",
    job="run_fixture_job",
    workflow="FixtureJobWorkflow",
    task_queue=_CONNECTOR_QUEUE,
    payload={"subject": "benzene"},
    rationale="why the tests run it",
    requested_by=_ACTOR,
    timeout_seconds=20.0,
)


class _CeilingInfo:
    """The two `workflow.info()` fields the wrapper reads outside a real execution."""

    workflow_id = _EXPECTED_ID
    run_id = "run-a"


def test_the_wrapper_is_served_by_the_background_worker() -> None:
    """The wrapper is served by the background worker.

    Runs without a server, so the property that would break every connector job (nobody polling the
    queue) is always checked.
    """
    from chemclaw.durable.background_worker import BACKGROUND_WORKFLOWS

    assert ConnectorJobWorkflow in BACKGROUND_WORKFLOWS


def test_the_publish_activity_calls_the_pr_gate_the_way_the_pr_gate_expects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The publish activity calls `record_note` the way it expects.

    `publish_memory_note_activity` is every machine-written note's path to the graph. Called
    directly with the writer stubbed, binding the call against the real signature, so drift fails
    wherever the suite runs.
    """
    seen: dict[str, Any] = {}

    async def _capture(*args: Any, **kwargs: Any) -> str:
        bound = inspect.signature(record_note).bind(*args, **kwargs)
        seen.update(bound.arguments)
        return "pr://note/n"

    monkeypatch.setattr("chemclaw.durable.memory_jobs.record_note", _capture)
    monkeypatch.setattr("chemclaw.durable.memory_jobs.default_writer", lambda: object())

    note = Note(id="n", type="job-result", created_by="agent", body="no links")
    unit = SynthesisUnit(note=note, retirements=[])
    assert asyncio.run(publish_memory_note_activity(unit)) == "pr://note/n"
    assert seen["note"] is note
    # The dependency list is passed, not omitted — a note that links a compound must carry it into
    # the same PR or the link dangles on the branch it is proposed on. The retirements ride the
    # same submission as `superseded`, which is what makes a supersede atomic (one PR, one merge).
    assert seen["dependencies"] == []
    assert seen["superseded"] == []


def test_a_connector_workflow_returns_a_well_formed_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connector workflow returns a well-formed envelope, checked without a server.

    The envelope is the whole cross-process agreement: a summary, structured data, and an optional
    `Note` that already passed the graph's slug/schema validators. `memo_value` is stubbed; the real
    memo is exercised end to end below.
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
    # `created_by="agent"` is the provenance the note lands with, readable beside its citations.
    assert result.note.created_by == "agent"


def _fixture_job_tool(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point the registry at the fixture bundle and return its generated launch tool.

    Built through the registry, so the real manifest → tool path is exercised. `tests/conftest.py`
    clears discovery's cache on entry.
    """
    monkeypatch.setattr("chemclaw.core.config.settings.connectors_dir", str(_FIXTURE_DIR))
    monkeypatch.setattr("chemclaw.core.config.settings.connectors_enabled", "")
    (manifest,) = enabled()
    (job,) = manifest.jobs
    return build_job_tool(manifest.name, job)


def test_a_connector_job_runs_its_own_workflow_and_core_does_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole contract: child on its own queue, note recorded by core, session woken."""
    published: list[Any] = []
    notified: list[tuple[str, str, dict[str, Any]]] = []
    recorded: list[JobRecord] = []

    async def _fake_propose(*args: Any, **kwargs: Any) -> str:
        """Capture the note proposal instead of writing it.

        Bound against the real `record_note` signature, so the stub accepts exactly what the real
        function does.
        """
        bound = inspect.signature(record_note).bind(*args, **kwargs)
        note = bound.arguments["note"]
        published.append((note, bound.arguments.get("dependencies")))
        return f"note/{note.id}"

    async def _fake_record(*args: Any, **kwargs: Any) -> None:
        """Capture the push-back event instead of inserting a `session_events` row.

        Bound against the real signature for the same reason as the stub above.
        """
        bound = inspect.signature(record_session_event).bind(*args, **kwargs)
        notified.append(
            (
                bound.arguments["session_id"],
                bound.arguments["kind"],
                bound.arguments["payload"],
            )
        )

    class _CapturingSink:
        """Keeps the durable job record instead of writing it to Postgres (D-157)."""

        async def record(self, record: JobRecord) -> None:
            recorded.append(record)

    # Stub what the activities *do*, not the activities themselves — see the module docstring.
    monkeypatch.setattr("chemclaw.durable.job_record.default_job_record_sink", _CapturingSink)
    monkeypatch.setattr("chemclaw.durable.memory_jobs.record_note", _fake_propose)
    monkeypatch.setattr("chemclaw.durable.memory_jobs.default_writer", lambda: object())
    monkeypatch.setattr("chemclaw.durable.notify.record_session_event", _fake_record)
    monkeypatch.setattr("chemclaw.core.config.settings.background_task_queue", _CORE_QUEUE)
    tool = _fixture_job_tool(monkeypatch)

    async def _run() -> ConnectorJobResult:
        async with await start_env_or_skip() as env:
            client = pydantic_client(env)

            async def _connect() -> Client:
                return client

            monkeypatch.setattr("chemclaw.connectors.jobs.connect", _connect)
            core = Worker(
                client,
                task_queue=_CORE_QUEUE,
                workflows=[ConnectorJobWorkflow],
                activities=[
                    # Registered because `_finish` now sends the `job-result` copy out of the
                    # building unconditionally, so the worker has to serve it even though delivery
                    # is off and the activity therefore returns `[]` on the first line.
                    deliver_message_activity,
                    publish_memory_note_activity,
                    record_session_event_activity,
                    record_job,
                ],
            )
            # Hosts ONLY the bundle's own workflow: were core's wrapper to need anything from
            # the connector beyond its type name, this worker could not serve the child at all.
            connector = Worker(client, task_queue=_CONNECTOR_QUEUE, workflows=[FixtureJobWorkflow])
            async with core, connector:
                # The session is ambient, never a model-supplied argument (F3-T3), so the tool
                # picks up which chat to wake exactly as it does mid-turn.
                from chemclaw.core.identity_context import (
                    reset_current_identity,
                    set_current_identity,
                )
                from chemclaw.core.session_context import (
                    reset_current_session_id,
                    set_current_session_id,
                )

                token = set_current_session_id(_SESSION)
                identity = set_current_identity(_ACTOR, frozenset())
                try:
                    job_id = await tool(
                        tool.__annotations__["params"](subject="benzene"),
                        "the reviewer asked whether benzene behaves the same way",
                    )
                finally:
                    reset_current_identity(identity)
                    reset_current_session_id(token)
                # The tool returns immediately — the agent never blocks on a durable job — so
                # the id is what comes back, and the result is awaited separately as a poll
                # would.
                assert job_id == _EXPECTED_ID
                # `result_type` is required: without it the converter returns a raw `dict` and
                # attribute reads below fail.
                handle = client.get_workflow_handle(job_id, result_type=ConnectorJobResult)
                result: ConnectorJobResult = await handle.result()
                return result

    result = asyncio.run(_run())

    # The connector's own result crossed back through the envelope unchanged.
    assert result.summary == "fixture job ran on benzene"
    assert result.data["subject"] == "benzene" and result.data["ran"] is True
    # The requesting actor reached the connector's workflow on the run's memo, never as a payload
    # field the model could author; it is what makes a durable job attributable (F4-T3, D-118).
    assert result.data["requested_by"] == _ACTOR
    # So did the session, the key a bundle needs to speak back to the chemist (e.g.
    # `BoCampaignWorkflow._evaluate`'s waiting notices and reminders).
    assert result.data["session_id"] == _SESSION
    # Core recorded the note the connector produced; the connector never touched the graph itself.
    assert [note.id for note, _ in published] == ["fixture-benzene"]
    assert published[0][0].created_by == "agent"  # so a human must sign it off at the gate
    # And it went through the gate as a note *with its dependencies* (D-133): the fixture note
    # links no compound, so the list is empty — but it is a list, which is what proves core passed
    # the argument at all rather than falling back to the single-file submission this replaced.
    assert published[0][1] == []
    # Core woke the launching session, through the one existing push-back channel.
    assert len(notified) == 1
    session_id, kind, payload = notified[0]
    assert session_id == _SESSION
    assert kind == "job_completed"
    assert payload["connector"] == "fixture" and payload["job"] == "run_fixture_job"
    assert payload["summary"] == "fixture job ran on benzene"
    # And core wrote the run's durable record (D-157) — the copy that outlives Temporal's own
    # history retention, carrying the arguments, the whole result envelope, and the reason.
    assert len(recorded) == 1
    record = recorded[0]
    assert record.job_id == _EXPECTED_ID
    assert record.connector == "fixture" and record.job == "run_fixture_job"
    assert record.rationale == "the reviewer asked whether benzene behaves the same way"
    assert record.requested_by == _ACTOR and record.session_id == _SESSION
    assert record.payload == {"subject": "benzene"}
    assert record.result == result.data  # the full envelope, not a summary of it
    assert record.note_id == "fixture-benzene"
    # The measured duration reached the record. No lower bound: the time-skipping server may report
    # both clock reads as one instant. That it is computed is pinned offline by
    # `test_the_wrapper_measures_the_run_rather_than_hardcoding_it`.
    assert isinstance(record.runtime_seconds, float)
    # The note a human is asked to sign says *why* the run happened, stamped by core rather than
    # by the connector — which is what makes it true of every connector, including ones that
    # know nothing about the record.
    assert "the reviewer asked whether benzene behaves the same way" in published[0][0].body
    assert _EXPECTED_ID in published[0][0].body


def test_a_failed_connector_job_wakes_the_session_before_the_failure_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed connector job wakes the session before the failure propagates.

    A job that fails after its turn ended must reach the asker with the reason. The second assertion
    matters most: the innermost failure message survives, since Temporal's outer frames say only
    "Child Workflow execution failed".
    """
    notified: list[tuple[str, str, dict[str, Any]]] = []

    async def _fake_record(*args: Any, **kwargs: Any) -> None:
        bound = inspect.signature(record_session_event).bind(*args, **kwargs)
        notified.append(
            (bound.arguments["session_id"], bound.arguments["kind"], bound.arguments["payload"])
        )

    class _CapturingSink:
        async def record(self, record: JobRecord) -> None:
            pass

    monkeypatch.setattr("chemclaw.durable.job_record.default_job_record_sink", _CapturingSink)
    monkeypatch.setattr("chemclaw.durable.notify.record_session_event", _fake_record)
    monkeypatch.setattr("chemclaw.core.config.settings.background_task_queue", _CORE_QUEUE)
    tool = _fixture_job_tool(monkeypatch)

    async def _run() -> None:
        async with await start_env_or_skip() as env:
            client = pydantic_client(env)

            async def _connect() -> Client:
                return client

            monkeypatch.setattr("chemclaw.connectors.jobs.connect", _connect)
            core = Worker(
                client,
                task_queue=_CORE_QUEUE,
                workflows=[ConnectorJobWorkflow],
                activities=[
                    # Registered because `_finish` now sends the `job-result` copy out of the
                    # building unconditionally, so the worker has to serve it even though delivery
                    # is off and the activity therefore returns `[]` on the first line.
                    deliver_message_activity,
                    publish_memory_note_activity,
                    record_session_event_activity,
                    record_job,
                ],
            )
            connector = Worker(client, task_queue=_CONNECTOR_QUEUE, workflows=[FixtureJobWorkflow])
            async with core, connector:
                from chemclaw.core.identity_context import (
                    reset_current_identity,
                    set_current_identity,
                )
                from chemclaw.core.session_context import (
                    reset_current_session_id,
                    set_current_session_id,
                )

                token = set_current_session_id(_SESSION)
                identity = set_current_identity(_ACTOR, frozenset())
                try:
                    job_id = await tool(
                        tool.__annotations__["params"](subject="boom"),
                        "prove a failed job still reaches the chemist who asked for it",
                    )
                finally:
                    reset_current_identity(identity)
                    reset_current_session_id(token)
                handle = client.get_workflow_handle(job_id, result_type=ConnectorJobResult)
                with pytest.raises(WorkflowFailureError):
                    await handle.result()

    asyncio.run(_run())

    assert len(notified) == 1, "a failed job emitted no session event at all — the original defect"
    session_id, kind, payload = notified[0]
    assert session_id == _SESSION
    assert kind == "job_failed"
    assert "the fixture job was asked to fail" in payload["reason"], (
        "the innermost cause must survive; Temporal's outer frames say only that a child failed"
    )


# --- the ceiling one job actually gets ---------------------------------------------------------
#
# `connector_job_timeout_seconds` is the deployment's maximum; a bundle may declare less for one of
# its jobs, never more (`JobSpec.timeout_seconds`). These run offline, because the asymmetry is the
# safety property.


def test_a_declared_ceiling_may_only_lower_the_deployments_maximum() -> None:
    """A declared ceiling may only lower the deployment's maximum.

    A manifest that could raise its ceiling would grant itself runtime the operator never funded, so
    the lower of the two wins and a higher declaration is clamped.
    """
    ceiling = settings.connector_job_timeout_seconds
    assert child_execution_timeout(20.0) == timedelta(seconds=20)
    # The declaration a bundle must not be able to make: far above the deployment's maximum, and
    # the deployment still wins.
    assert child_execution_timeout(ceiling * 10) == timedelta(seconds=ceiling)
    assert child_execution_timeout(ceiling) == timedelta(seconds=ceiling)


def test_a_job_that_declares_nothing_is_bounded_exactly_as_it_was_before() -> None:
    """A job that declares nothing is bounded exactly as before.

    `None` (old manifests, in-flight histories) must resolve to the setting itself.
    """
    assert ConnectorJobInput.model_validate(_CEILING_JOB.model_dump()).timeout_seconds == 20.0
    unbounded = _CEILING_JOB.model_copy(update={"timeout_seconds": None})
    assert child_execution_timeout(unbounded.timeout_seconds) == timedelta(
        seconds=settings.connector_job_timeout_seconds
    )


def test_the_child_is_started_with_the_resolved_ceiling_and_the_wrapper_still_clears_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The child starts with the resolved ceiling and the wrapper still clears it.

    After the child returns the wrapper records, publishes and pushes back, so its own ceiling must
    stay strictly above the child's or its failure push-back never runs. Pinned because the
    arithmetic lives in two functions.
    """
    starts: list[dict[str, Any]] = []

    async def _child(*_args: Any, **kwargs: Any) -> ConnectorJobResult:
        starts.append(kwargs)
        return ConnectorJobResult(summary="done")

    async def _nothing(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(workflow, "info", lambda: _CeilingInfo())
    monkeypatch.setattr(workflow, "now", lambda: datetime(2026, 8, 27, tzinfo=UTC))
    monkeypatch.setattr(workflow, "execute_child_workflow", _child)
    monkeypatch.setattr(workflow, "execute_activity", _nothing)

    asyncio.run(ConnectorJobWorkflow().run(_CEILING_JOB))
    (start,) = starts

    assert start["execution_timeout"] == timedelta(seconds=20)
    assert wrapper_execution_timeout() > start["execution_timeout"]


def test_the_declared_ceiling_travels_from_the_manifest_to_the_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The declared ceiling travels from the manifest to the launch.

    A workflow cannot read `connector.yaml`, so the launch site copies it into
    `ConnectorJobInput.timeout_seconds`. Driven through the real generated tool, because forgetting
    the copy fails nothing.
    """
    _fixture_job_tool(monkeypatch)
    (manifest,) = enabled()
    (job,) = manifest.jobs
    assert job.timeout_seconds is None, "the fixture bundle declares no ceiling; see below"
    bounded = build_job_tool(manifest.name, job.model_copy(update={"timeout_seconds": 20.0}))

    launched: list[ConnectorJobInput] = []

    class _Client:
        """The one method a launch calls, capturing the input the wrapper will be started with."""

        async def start_workflow(self, _run: Any, arg: ConnectorJobInput, **kwargs: Any) -> Any:
            launched.append(arg)
            return SimpleNamespace(id=str(kwargs["id"]))

    async def _connect() -> _Client:
        return _Client()

    monkeypatch.setattr("chemclaw.connectors.jobs.connect", _connect)
    params: type[BaseModel] = bounded.__annotations__["params"]
    asyncio.run(bounded(params(subject="benzene"), "why the tests run it"))

    (started,) = launched
    assert started.timeout_seconds == 20.0


# A job that suspends on a *person* is the one shape the asymmetry above cannot express, and the
# three tests below are why the ceiling grew a second argument. These run offline, because the
# arithmetic that makes the pair impossible is arithmetic over two shipped settings.


def test_a_measured_campaigns_wait_does_not_fit_under_the_deployments_job_ceiling() -> None:
    """A measured campaign's wait does not fit under the deployment's job ceiling.

    `BoCampaignWorkflow._measure` waits `bo_measurement_deadline_days` on `AwaitAnswerWorkflow`, far
    beyond `connector_job_timeout_seconds`, and `JobSpec.timeout_seconds` can only lower the
    ceiling. Pinned as an inequality over the shipped defaults: it is why `awaits_answer` exists.
    """
    assert settings.bo_measurement_deadline_days * 86_400 > settings.connector_job_timeout_seconds


def test_a_job_that_suspends_on_a_person_is_not_bounded_by_a_compute_ceiling() -> None:
    """A job that suspends on a person is not bounded by a compute ceiling.

    `awaits_answer` removes the wall-clock ceiling for that job only: a suspended job spends wall
    clock without working, and no finite number fits a multi-round campaign. Every other job keeps
    the deployment's ceiling, so the reaper for wedged xTB or CREST jobs remains.
    """
    assert child_execution_timeout(None, awaits_answer=True) is None
    assert child_execution_timeout(None) == timedelta(
        seconds=settings.connector_job_timeout_seconds
    )


def test_the_suspension_declaration_travels_from_the_manifest_to_the_started_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The suspension declaration travels from the manifest to the started child.

    Two hops (manifest to launch input, input to `execution_timeout`), each driven for real, since
    forgetting either fails nothing at runtime.
    """
    _fixture_job_tool(monkeypatch)
    (manifest,) = enabled()
    (job,) = manifest.jobs
    assert not job.awaits_answer, "the fixture bundle's job computes; the copy below makes it wait"
    # Named in the allowlist for the same reason the stdio transport's own tests set
    # `connector_stdio_enabled`: `build_job_tool` refuses an ungated declaration, and this test is
    # about the two hops the declaration takes *after* it is allowed, not about the gate.
    monkeypatch.setattr(settings, "connector_jobs_awaiting_answer", f"{manifest.name}.{job.name}")
    waiting = build_job_tool(manifest.name, job.model_copy(update={"awaits_answer": True}))

    launched: list[ConnectorJobInput] = []

    class _Client:
        """The one method a launch calls, capturing the input the wrapper will be started with."""

        async def start_workflow(self, _run: Any, arg: ConnectorJobInput, **kwargs: Any) -> Any:
            launched.append(arg)
            return SimpleNamespace(id=str(kwargs["id"]))

    async def _connect() -> _Client:
        return _Client()

    monkeypatch.setattr("chemclaw.connectors.jobs.connect", _connect)
    params: type[BaseModel] = waiting.__annotations__["params"]
    asyncio.run(waiting(params(subject="benzene"), "why the tests run it"))

    (started,) = launched
    assert started.awaits_answer is True

    starts: list[dict[str, Any]] = []

    async def _child(*_args: Any, **kwargs: Any) -> ConnectorJobResult:
        starts.append(kwargs)
        return ConnectorJobResult(summary="done")

    async def _nothing(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(workflow, "info", lambda: _CeilingInfo())
    monkeypatch.setattr(workflow, "now", lambda: datetime(2026, 9, 4, tzinfo=UTC))
    monkeypatch.setattr(workflow, "execute_child_workflow", _child)
    monkeypatch.setattr(workflow, "execute_activity", _nothing)

    asyncio.run(ConnectorJobWorkflow().run(started))
    (start,) = starts
    assert start["execution_timeout"] is None


# --- the headroom the wrapper keeps for what it owes *after* its child ---------------------------

# The scaled configuration below. Only the ratio of the wrapper's headroom to what the post-child
# steps may spend matters, so seconds are scaled down. `_LATE_SECONDS` stands in for the expected
# wait for a `background-jobs` slot under load
# (`durable/publish.py::light_write_queue_wait_timeout`).
_SCALED = {
    "connector_job_timeout_seconds": 2.0,
    # The old reservation was `activity_timeout_seconds * 4`, so this fixes the old headroom at 4 s.
    "activity_timeout_seconds": 1.0,
    "job_record_timeout_seconds": 1.0,
    "result_publish_timeout_seconds": 1.0,
    "note_write_timeout_seconds": 1.0,
    # The two queue bounds the post-child steps actually pass, kept well above the old headroom —
    # which is the whole defect: a step permitted 30 s of queue wait inside a 4 s reservation.
    "template_step_timeout_seconds": 30.0,
    "activity_queue_wait_seconds": 30.0,
}
_LATE_SECONDS = 8.0


async def test_a_job_that_fails_records_and_says_so_even_when_the_write_queue_is_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job that fails still records and says so when the write queue is busy.

    The wrapper's headroom after its child must cover the post-child steps' queue waits, or a child
    that hit its ceiling is reaped as `TIMED_OUT` before `_record_run` and the failure push-back run
    (workflow timeouts are not delivered to workflow code). Driven on the real-time dev server, with
    activities hosted by a second worker started `_LATE_SECONDS` in, which is queue pressure from
    the wrapper's side. Asserted: the durable row and the message both survive.
    """
    for name, value in _SCALED.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr("chemclaw.core.config.settings.background_task_queue", _CORE_QUEUE)

    recorded: list[JobRecord] = []
    notified: list[tuple[str, str, dict[str, Any]]] = []

    class _CapturingSink:
        async def record(self, record: JobRecord) -> None:
            recorded.append(record)

    async def _fake_record(*args: Any, **kwargs: Any) -> None:
        bound = inspect.signature(record_session_event).bind(*args, **kwargs)
        notified.append(
            (bound.arguments["session_id"], bound.arguments["kind"], bound.arguments["payload"])
        )

    monkeypatch.setattr("chemclaw.durable.job_record.default_job_record_sink", _CapturingSink)
    monkeypatch.setattr("chemclaw.durable.notify.record_session_event", _fake_record)

    status: list[str] = []

    async with await start_local_env_or_skip() as env:
        client = pydantic_client(env)
        # Workflow tasks only: the writes below have nowhere to run until the late worker is up.
        core = Worker(client, task_queue=_CORE_QUEUE, workflows=[ConnectorJobWorkflow])
        connector = Worker(client, task_queue=_CONNECTOR_QUEUE, workflows=[FixtureJobWorkflow])
        writes = Worker(
            client,
            task_queue=_CORE_QUEUE,
            activities=[record_session_event_activity, record_job],
        )

        async def _start_writes_late() -> None:
            await asyncio.sleep(_LATE_SECONDS)
            async with writes:
                await asyncio.sleep(_LATE_SECONDS)

        async with core, connector:
            late = asyncio.create_task(_start_writes_late())
            handle = await client.start_workflow(
                ConnectorJobWorkflow.run,
                _CEILING_JOB.model_copy(
                    update={
                        "payload": {"subject": "boom"},
                        "session_id": _SESSION,
                        "timeout_seconds": None,
                    }
                ),
                id="wrapper-headroom-under-a-busy-queue",
                task_queue=_CORE_QUEUE,
                execution_timeout=wrapper_execution_timeout(),
            )
            with pytest.raises(WorkflowFailureError):
                await handle.result()
            described = (await handle.describe()).status
            assert described is not None, "a described execution always carries a status"
            status.append(described.name)
            await late

    assert status == ["FAILED"], (
        "the wrapper was reaped by its own execution timeout instead of failing on its child's "
        "failure, so nothing below it ran"
    )
    assert [record.state for record in recorded] == ["failed"], (
        "the failure row is the durable copy; without it the run exists only in Temporal's history"
    )
    assert [kind for _, kind, _ in notified] == ["job_failed"]
    assert "the fixture job was asked to fail" in notified[0][2]["reason"]


# --- what comes back from the child ------------------------------------------------------------


def _foreign_child_run(returns: dict[str, Any], job_id: str) -> tuple[list[Any], list[Any], str]:
    """Drive the wrapper over a child returning `returns`; give back records, events, status."""
    recorded: list[JobRecord] = []
    notified: list[tuple[str, str, dict[str, Any]]] = []

    class _CapturingSink:
        async def record(self, record: JobRecord) -> None:
            recorded.append(record)

    async def _fake_record(*args: Any, **kwargs: Any) -> None:
        bound = inspect.signature(record_session_event).bind(*args, **kwargs)
        notified.append(
            (bound.arguments["session_id"], bound.arguments["kind"], bound.arguments["payload"])
        )

    status: list[str] = []

    async def _run() -> None:
        async with await start_env_or_skip() as env:
            client = pydantic_client(env)
            core = Worker(
                client,
                task_queue=_CORE_QUEUE,
                workflows=[ConnectorJobWorkflow],
                activities=[record_session_event_activity, record_job],
            )
            connector = Worker(
                client, task_queue=_CONNECTOR_QUEUE, workflows=[ForeignResultWorkflow]
            )
            async with core, connector:
                handle = await client.start_workflow(
                    ConnectorJobWorkflow.run,
                    _CEILING_JOB.model_copy(
                        update={
                            "workflow": "ForeignResultWorkflow",
                            "payload": {"returns": returns},
                            "session_id": _SESSION,
                        }
                    ),
                    id=job_id,
                    task_queue=_CORE_QUEUE,
                    execution_timeout=wrapper_execution_timeout(),
                )
                try:
                    await handle.result()
                except WorkflowFailureError:
                    pass
                described = (await handle.describe()).status
                assert described is not None
                status.append(described.name)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("chemclaw.durable.job_record.default_job_record_sink", _CapturingSink)
        patch.setattr("chemclaw.durable.notify.record_session_event", _fake_record)
        patch.setattr("chemclaw.core.config.settings.background_task_queue", _CORE_QUEUE)
        asyncio.run(_run())
    return recorded, notified, status[0]


def test_a_child_that_returns_a_foreign_result_still_records_and_says_so() -> None:
    """A child that returns a foreign result still records and says so.

    With `result_type` on the child call, the decode happens in the SDK's activation-apply phase,
    outside the coroutine, so the wrapper's failure clause must handle it rather than the run dying
    with no row and no event.
    """
    recorded, notified, status = _foreign_child_run({"not": "an envelope"}, "wrapper-foreign-child")

    assert status == "FAILED"
    assert [record.state for record in recorded] == ["failed"], (
        "the decode failure wrote no durable row — it was raised outside workflow code"
    )
    assert [kind for _, kind, _ in notified] == ["job_failed"]
    assert "envelope" in notified[0][2]["reason"], (
        "the reason must name what was wrong with the result, not just that a child failed"
    )


def test_a_newer_bundle_may_add_a_field_to_the_envelope_it_returns() -> None:
    """A newer bundle may add a field to the envelope it returns.

    During a rolling upgrade a bundle image may be newer than core, so `ConnectorJobResult` ignores
    extras instead of failing every in-flight job.
    """
    envelope = ConnectorJobResult(summary="a newer bundle ran").model_dump(mode="json")
    recorded, notified, status = _foreign_child_run(
        {**envelope, "provenance_v2": {"emitted_by": "a newer bundle"}}, "wrapper-newer-bundle"
    )

    assert status == "COMPLETED", "a newer bundle's extra field must not fail an in-flight job"
    assert [record.state for record in recorded] == ["completed"]
    assert [kind for _, kind, _ in notified] == ["job_completed"]


def test_the_launch_side_of_the_wire_still_refuses_what_it_does_not_know() -> None:
    """The launch side of the wire still refuses what it does not know.

    `ConnectorJobInput` is written only by this repository, so an unknown field is a bug and fails
    loudly; `ConnectorJobResult` comes from a separately deployed image and ignores extras.
    """
    assert ConnectorJobInput.model_config["extra"] == "forbid"
    assert ConnectorJobResult.model_config["extra"] == "ignore"


def test_a_workflow_instance_torn_down_mid_job_attempts_nothing_on_the_way_out() -> None:
    """A workflow instance torn down mid-job attempts nothing on the way out.

    An eviction closes the parked `run` coroutine from outside the event loop; `except
    BaseException` must not run its clause there (`workflow.now()` would raise and Python would
    print "Exception ignored"). Driven on the real-time dev server; `sys.unraisablehook` is where
    this shows.
    """
    caught: list[BaseException | None] = []

    async def _run() -> None:
        async with await start_local_env_or_skip() as env:
            client = pydantic_client(env)
            job = _CEILING_JOB.model_copy(
                update={
                    # A queue nobody serves, so the run parks in the child call and is still
                    # parked when the terminate below evicts it.
                    "task_queue": "connector-nobody-serves-this",
                    "session_id": _SESSION,
                    "timeout_seconds": None,
                    "awaits_answer": True,
                }
            )
            async with Worker(client, task_queue=_CORE_QUEUE, workflows=[ConnectorJobWorkflow]):
                handle = await client.start_workflow(
                    ConnectorJobWorkflow.run,
                    job,
                    id="wrapper-evicted-while-parked",
                    task_queue=_CORE_QUEUE,
                )
                await asyncio.sleep(3)
                await handle.terminate()

    previous = sys.unraisablehook
    sys.unraisablehook = lambda hook: caught.append(hook.exc_value)
    try:
        asyncio.run(_run())
        gc.collect()
    finally:
        sys.unraisablehook = previous

    assert [type(exc).__name__ for exc in caught] == [], (
        "the failure clause ran during instance teardown and died there; an eviction must be "
        "re-raised untouched, because nothing the clause does can reach anything from outside "
        "the workflow event loop"
    )
