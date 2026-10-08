"""The shared activity retry policies fail fast on bad data and bound transient retries.

`BAD_DATA_RETRY` must be bounded, so an unclassified deterministic failure cannot retry forever,
and must mark every bad-data error type non-retryable by its exact class name (Temporal matches by
name).
"""

import asyncio
import importlib
import logging
import pkgutil
import types
from datetime import timedelta
from typing import Any

import pytest
from temporalio import workflow
from temporalio.api.failure.v1 import Failure
from temporalio.converter import DefaultFailureConverter, DefaultPayloadConverter
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

import chemclaw.durable.publish as publish_module
from chemclaw.agent.authz import AuthorizationError
from chemclaw.agent.profile_discovery import ProfileError
from chemclaw.connectors.contract import ContractMismatch
from chemclaw.connectors.registry import ConnectorError
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError, SubsystemUnavailableError
from chemclaw.durable.publish import (
    BAD_DATA_RETRY,
    agent_step_retry,
    calculation_retry,
    connector_queue_wait_timeout,
    note_publish_retry,
    publish_note_best_effort,
    queue_wait_timeout,
)
from chemclaw.ingest.sources.registry import DataSourceError
from chemclaw.templates.registry import TemplateError
from chemclaw.templates.resolve import UnresolvedReference
from tests.temporal_env import pydantic_client, start_local_env_or_skip


def _import_first_party_tree() -> None:
    """Import every module under `chemclaw` so every subclass of an error base is defined.

    Shared by both completeness walks below (`ChemclawError`'s and `AuthorizationError`'s): a
    subclass declared in a module nobody has imported yet is invisible to `__subclasses__()`.
    """
    package = importlib.import_module("chemclaw")
    for module_info in pkgutil.walk_packages(package.__path__, prefix="chemclaw."):
        importlib.import_module(module_info.name)


def test_bad_data_retry_is_bounded() -> None:
    """An unclassified deterministic failure gives up instead of pinning a worker forever."""
    assert BAD_DATA_RETRY.maximum_attempts == settings.activity_max_attempts


def test_bad_data_retry_lists_every_bad_data_type_by_name() -> None:
    """Every bad-data error name crossing an activity boundary is non-retryable.

    Includes pydantic's `ValidationError` (a `ValueError` subclass with its own class name)
    and the ORD/eval format errors, which were previously missing and so retried.
    """
    names = set(BAD_DATA_RETRY.non_retryable_error_types or [])
    assert {
        "ValueError",
        "ValidationError",
        "ChemclawError",
        "OrdFormatError",
        "NoteError",
        "EvalCaseError",
    } <= names


def test_every_chemclaw_error_subclass_is_listed_non_retryable() -> None:
    """Every `ChemclawError` subclass is listed non-retryable by name.

    Temporal matches by exact class name, not `isinstance`, so a subclass is not covered until its
    own name is in `_BAD_DATA_TYPES`. Walks every first-party module so all subclasses are defined.
    """
    # `walk_packages` reaches every module under `chemclaw` (D-148), so a new subclass anywhere
    # in the tree is still defined by the time the assertion runs.
    _import_first_party_tree()

    def names(cls: type) -> set[str]:
        return {cls.__name__}.union(*(names(sub) for sub in cls.__subclasses__()), set())

    from chemclaw.durable.publish import _DECLARED_RETRYABLE

    registered = set(BAD_DATA_RETRY.non_retryable_error_types or []) | _DECLARED_RETRYABLE
    missing = names(ChemclawError) - registered
    assert not missing, f"ChemclawError subclasses not registered in _BAD_DATA_TYPES: {missing}"
    # And an exemption must never also be listed — a name in both sets is a contradiction the
    # policy would resolve silently (the list wins, and the "retryable" claim becomes false).
    assert not _DECLARED_RETRYABLE & set(BAD_DATA_RETRY.non_retryable_error_types or [])
    # Nor may the list name a class twice: a duplicate changes no behaviour, so it survives review,
    # and a classification register with two entries for one class has two answers for it.
    from chemclaw.durable.publish import _BAD_DATA_TYPES

    duplicated = sorted({name for name in _BAD_DATA_TYPES if _BAD_DATA_TYPES.count(name) > 1})
    assert not duplicated, f"_BAD_DATA_TYPES lists {duplicated} more than once"


def test_every_authorization_error_subclass_is_listed_non_retryable() -> None:
    """Every `AuthorizationError` subclass is listed non-retryable.

    `AuthorizationError` is deliberately not a `ChemclawError`, so the walk above never visits it or
    its subclasses (`DryRunRefusal`, `PlanNotApprovedError`).
    """

    def names(cls: type) -> set[str]:
        return {cls.__name__}.union(*(names(sub) for sub in cls.__subclasses__()), set())

    _import_first_party_tree()
    missing = names(AuthorizationError) - set(BAD_DATA_RETRY.non_retryable_error_types or [])
    assert not missing, f"AuthorizationError subclasses not registered: {missing}"


def test_no_subsystem_outage_error_is_listed_non_retryable() -> None:
    """No `SubsystemUnavailableError` is listed non-retryable, on purpose.

    It means the infrastructure is not answering, the failure a retry fixes; registering it would
    make workflows give up on a broker restart. The walks above fail on a missing subclass, so this
    fails on a present one, with the reason in the message.
    """
    _import_first_party_tree()

    def names(cls: type) -> set[str]:
        return {cls.__name__}.union(*(names(sub) for sub in cls.__subclasses__()), set())

    listed = set(BAD_DATA_RETRY.non_retryable_error_types or [])
    registered = names(SubsystemUnavailableError) & listed
    assert not registered, (
        f"{registered} is registered non-retryable, but an unreachable subsystem is retryable by "
        "definition — a retry is the fix, not a wasted attempt. If a genuinely non-retryable error "
        "needs a home, it does not belong under SubsystemUnavailableError."
    )


@pytest.mark.parametrize(
    "error_cls",
    [
        ConnectorError,
        ContractMismatch,
        DataSourceError,
        TemplateError,
        UnresolvedReference,
        ProfileError,
        AuthorizationError,
    ],
)
def test_bad_data_class_crosses_an_activity_boundary_as_non_retryable(
    error_cls: type[Exception],
) -> None:
    """A bad-data class crosses an activity boundary as non-retryable.

    `DefaultFailureConverter` sets `ApplicationError.type` to the exact class name, never an
    ancestor's. `AuthorizationError` is a plain `Exception`, yet registered by name it is classified
    the same way. Drives the real SDK converter (no server needed) rather than inspecting the class
    hierarchy.
    """
    converter = DefaultFailureConverter()
    payload_converter = DefaultPayloadConverter()
    failure = Failure()

    converter.to_failure(error_cls("boom"), payload_converter, failure)

    assert failure.application_failure_info.type == error_cls.__name__
    assert error_cls.__name__ in (BAD_DATA_RETRY.non_retryable_error_types or [])


def test_note_publish_retry_shares_the_bad_data_types() -> None:
    """A bad note fails fast rather than burning the bounded note-write retries."""
    policy = note_publish_retry()
    assert policy.maximum_attempts == settings.note_write_max_attempts
    assert "NoteError" in (policy.non_retryable_error_types or [])


def test_the_agent_step_bound_is_narrower_than_the_shared_one() -> None:
    """The agent-step retry bound is narrower than the shared one.

    An agent step replays the whole turn, re-running every tool the failed attempt already ran, side
    effects included. Strictly less than `BAD_DATA_RETRY`, so equal settings cannot undo the
    narrowing. The bad-data list is shared: the policies differ in how many transient attempts,
    never in which failures are transient.
    """
    policy = agent_step_retry()

    assert policy.maximum_attempts == settings.agent_step_max_attempts
    assert (policy.maximum_attempts or 0) < (BAD_DATA_RETRY.maximum_attempts or 0)
    assert policy.non_retryable_error_types == BAD_DATA_RETRY.non_retryable_error_types


def test_the_calculation_retry_waits_out_a_full_backend_without_spinning_at_it() -> None:
    """The calculation retry waits out a full backend without spinning at it.

    `CalcBusyError` is retryable, but a slot is held for a whole calculation. The cap is one
    `calc_server_timeout_seconds` and the first interval is that cap divided by the doublings the
    attempt budget allows, so the last retry waits one calculation. The type list matches
    `BAD_DATA_RETRY`'s.
    """
    # Called outside a workflow, so the schedule is the nominal one — the jitter is per-run and
    # has no run here. `test_two_runs_refused_together_do_not_come_back_together` drives that half.
    policy = calculation_retry()

    assert policy.maximum_attempts == settings.activity_max_attempts
    assert policy.non_retryable_error_types == BAD_DATA_RETRY.non_retryable_error_types
    assert policy.backoff_coefficient == 2.0

    cap = timedelta(seconds=settings.calc_server_timeout_seconds)
    assert policy.maximum_interval == cap
    # The whole schedule, rebuilt the way Temporal computes it, so the assertion is about elapsed
    # patience rather than about one field. At the shipped defaults: 112.5 s, 225 s, 450 s, 900 s.
    waits = []
    interval = policy.initial_interval or timedelta()
    for _ in range((policy.maximum_attempts or 1) - 1):
        waits.append(min(interval, cap))
        interval *= policy.backoff_coefficient
    assert waits[-1] == cap, "the last retry must wait a whole calculation, not a fraction of one"
    # Nineteen minutes is the measured CREST search this has to outlast.
    total = sum(waits, timedelta())
    assert total > timedelta(minutes=19)
    # The slack is measured against the composite, not one attempt: on the path these retries exist
    # for, a job waits for a slot, is refused at capacity, backs off and then runs, all within the
    # parent ceiling (`D-2026-09-05-a-refusal-for-capacity-is-not-a-refusal-of-the-question`).
    longest, _ = settings.longest_bundle_activity
    composite = timedelta(hours=1.98) + total + timedelta(seconds=longest)
    assert composite < timedelta(seconds=settings.connector_job_timeout_seconds), (
        "a job that queues, is refused for capacity, backs off and then runs must still finish "
        "inside the ceiling its parent gives it"
    )


@workflow.defn
class _RetrySpacingWorkflow:
    """Report the first retry interval this run would use, from inside a real workflow context."""

    @workflow.run
    async def run(self) -> float:
        """The jittered first interval, in seconds — the only thing that differs per run."""
        interval = calculation_retry().initial_interval
        return interval.total_seconds() if interval else 0.0


def test_two_runs_refused_together_do_not_come_back_together() -> None:
    """Two runs refused together do not come back together.

    Temporal applies no jitter of its own, and a burst refused at once would otherwise retry in
    lockstep. Two runs show desynchronisation; the bound is asserted too, since upward jitter would
    be swallowed by `maximum_interval` on the longest wait.
    """
    nominal = settings.calc_server_timeout_seconds / 2 ** max(settings.activity_max_attempts - 2, 0)

    async def _run() -> list[float]:
        async with await start_local_env_or_skip() as env:
            client = pydantic_client(env)
            async with Worker(
                client,
                task_queue="retry-spacing",
                workflows=[_RetrySpacingWorkflow],
                workflow_runner=UnsandboxedWorkflowRunner(),
            ):
                return [
                    await client.execute_workflow(
                        _RetrySpacingWorkflow.run, id=f"spacing-{n}", task_queue="retry-spacing"
                    )
                    for n in range(2)
                ]

    first, second = asyncio.run(_run())

    assert first != second, "two runs drew the same interval; the spread is not per-run"
    for drawn in (first, second):
        assert nominal * 0.75 <= drawn <= nominal


def test_a_bundle_queue_wait_is_bounded_generously_rather_than_by_cores_hour() -> None:
    """A bundle queue wait is bounded generously rather than by core's hour.

    Core's queue wait is an hour, but a bundle's wait is backpressure that at healthy peak exceeds
    an hour; unbounded, only the parent's execution timeout would end it, naming neither queue nor
    reason. Asserted as an ordering of settings, since the numbers are a deployment's.
    """
    bundle = connector_queue_wait_timeout()
    core = queue_wait_timeout()
    longest, _ = settings.longest_bundle_activity
    ceiling = timedelta(seconds=settings.connector_job_timeout_seconds)

    assert bundle > timedelta(hours=1.98), "measured p95 backpressure would fail this bound"
    # The composite, because the wait precedes the work and the ceiling must hold both. Asserted
    # against `longest_bundle_activity` so any bundle's longest activity is covered.
    assert bundle + timedelta(seconds=longest) < ceiling, (
        "the queue wait plus the longest activity it precedes must fit inside the child's own "
        "execution ceiling, or a job that waits and then runs dies as a workflow execution "
        "timeout delivered to no workflow code"
    )
    assert bundle > core, "core's hour is the bound this one exists not to be"


def test_no_provider_transient_name_is_listed_non_retryable() -> None:
    """No LLM provider transient error name is listed non-retryable.

    Those failures succeed on retry; `agent_step_retry`'s bound is the lever for duplicate turns.
    `_BAD_DATA_TYPES` matches bare class names and is shared by every activity, so one entry would
    reclassify these failures across unrelated workflows too.
    """
    provider_transient = {
        "InternalServerError",
        "OverloadedError",
        "APIConnectionError",
        "RateLimitError",
    }

    listed = provider_transient & set(BAD_DATA_RETRY.non_retryable_error_types or [])

    assert not listed, (
        f"{listed} is registered non-retryable, but a provider 503/429/connection failure is "
        "transient by definition — the identical call succeeds once the provider recovers. If this "
        "was added to stop a retried agent step duplicating notes, the lever is "
        "agent_step_max_attempts, not the bad-data list, which every other activity shares."
    )


def _fake_workflow(*, raises: bool, replaying: bool = False) -> types.SimpleNamespace:
    """A stand-in for `temporalio.workflow` inside this module.

    The real API refuses to run outside a workflow loop and the test server is not reachable
    offline, so the handle is substituted while the function under test stays real.
    """

    async def execute_activity(*_args: Any, **_kwargs: Any) -> str:
        if raises:
            raise ActivityError(
                "boom",
                scheduled_event_id=1,
                started_event_id=2,
                identity="test",
                activity_type="publish",
                activity_id="1",
                retry_state=None,
            ).with_traceback(None) from ApplicationError("git remote is dead")
        return "note/some-id"

    return types.SimpleNamespace(
        logger=logging.getLogger("test.workflow"),
        unsafe=types.SimpleNamespace(is_replaying=lambda: replaying),
        execute_activity=execute_activity,
    )


def test_a_lost_knowledge_note_is_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A swallowed publish failure must be visible, or a dead git remote looks like an idle system.

    `chemclaw_notes_recorded_total` counts only successes, so with no failure counter "the remote
    is down and every note was lost" and "nobody asked for a note" produce identical exposition.
    """
    from chemclaw.core.metrics import METRICS

    monkeypatch.setattr(publish_module, "workflow", _fake_workflow(raises=True))
    before = METRICS.value("chemclaw_notes_publish_failures_total")

    asyncio.run(publish_note_best_effort(object(), [], label="qm:compute"))

    assert METRICS.value("chemclaw_notes_publish_failures_total") == before + 1


def test_a_replayed_failure_is_not_counted_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replay re-executes workflow code; counting there would inflate the metric on every replay."""
    from chemclaw.core.metrics import METRICS

    monkeypatch.setattr(publish_module, "workflow", _fake_workflow(raises=True, replaying=True))
    before = METRICS.value("chemclaw_notes_publish_failures_total")

    asyncio.run(publish_note_best_effort(object(), [], label="qm:compute"))

    assert METRICS.value("chemclaw_notes_publish_failures_total") == before


def test_a_successful_publish_counts_no_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard is on the failure path only — a working remote must not move the counter."""
    from chemclaw.core.metrics import METRICS

    monkeypatch.setattr(publish_module, "workflow", _fake_workflow(raises=False))
    before = METRICS.value("chemclaw_notes_publish_failures_total")

    asyncio.run(publish_note_best_effort(object(), [], label="qm:compute"))

    assert METRICS.value("chemclaw_notes_publish_failures_total") == before
