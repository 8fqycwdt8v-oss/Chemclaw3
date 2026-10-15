"""The queue-wait bounds and the best-effort note publish never let one failed write end a job.

How an activity is retried is `tests/test_retry.py`'s.
"""

import asyncio
import logging
import types
from datetime import timedelta
from typing import Any

import pytest
from temporalio.exceptions import ActivityError, ApplicationError

import chemclaw.durable.publish as publish_module
from chemclaw.core.config import settings
from chemclaw.durable.publish import (
    connector_queue_wait_timeout,
    publish_note_best_effort,
    queue_wait_timeout,
)
from chemclaw.durable.registry import ActivityClass, activity_in_class


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


@activity_in_class(ActivityClass.NOTE_WRITE)
async def _note_activity() -> str:
    """Stands in for a note-publish activity: the helper reads its class to pick the retry."""
    return "note/some-id"


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

    asyncio.run(publish_note_best_effort(_note_activity, [], label="qm:compute"))

    assert METRICS.value("chemclaw_notes_publish_failures_total") == before + 1


def test_a_replayed_failure_is_not_counted_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replay re-executes workflow code; counting there would inflate the metric on every replay."""
    from chemclaw.core.metrics import METRICS

    monkeypatch.setattr(publish_module, "workflow", _fake_workflow(raises=True, replaying=True))
    before = METRICS.value("chemclaw_notes_publish_failures_total")

    asyncio.run(publish_note_best_effort(_note_activity, [], label="qm:compute"))

    assert METRICS.value("chemclaw_notes_publish_failures_total") == before


def test_a_successful_publish_counts_no_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard is on the failure path only — a working remote must not move the counter."""
    from chemclaw.core.metrics import METRICS

    monkeypatch.setattr(publish_module, "workflow", _fake_workflow(raises=False))
    before = METRICS.value("chemclaw_notes_publish_failures_total")

    asyncio.run(publish_note_best_effort(_note_activity, [], label="qm:compute"))

    assert METRICS.value("chemclaw_notes_publish_failures_total") == before
