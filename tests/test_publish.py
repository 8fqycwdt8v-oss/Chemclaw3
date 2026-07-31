"""The shared PR-gate publish retry policies fail fast on bad data and bound transient retries.

These guard the fix for the durability hole where an unclassified deterministic failure
(a `KeyError`/`RuntimeError` bug, or a git ref that can never be created) retried forever
because `BAD_DATA_RETRY` had no attempt bound. The policy must (a) be bounded, and (b) mark
every bad-data error type non-retryable by its exact class name (Temporal matches by name).
"""

import asyncio
import importlib
import logging
import pkgutil
import types
from typing import Any

import pytest
from temporalio.exceptions import ActivityError, ApplicationError

import chemclaw.durable.publish as publish_module
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.durable.publish import BAD_DATA_RETRY, note_publish_retry, publish_note_best_effort


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
    """Temporal matches non-retryable types by exact class name, not isinstance.

    So subclassing `ChemclawError` alone does NOT make a new bad-data error fail fast
    across an activity boundary — its concrete name must be in `_BAD_DATA_TYPES`. This
    walks every first-party module so all subclasses are defined, then asserts none is
    missing from the policy (the drift this base class was created to eliminate).
    """
    # One package since D-148; `walk_packages` reaches every module under it, so a new
    # subclass anywhere in the tree is still defined by the time the assertion runs.
    first_party = ["chemclaw"]
    for package_name in first_party:
        package = importlib.import_module(package_name)
        for module_info in pkgutil.walk_packages(package.__path__, prefix=f"{package_name}."):
            importlib.import_module(module_info.name)

    def names(cls: type) -> set[str]:
        return {cls.__name__}.union(*(names(sub) for sub in cls.__subclasses__()), set())

    missing = names(ChemclawError) - set(BAD_DATA_RETRY.non_retryable_error_types or [])
    assert not missing, f"ChemclawError subclasses not registered in _BAD_DATA_TYPES: {missing}"


def test_note_publish_retry_shares_the_bad_data_types() -> None:
    """A bad note fails fast rather than burning the bounded note-write retries."""
    policy = note_publish_retry()
    assert policy.maximum_attempts == settings.note_write_max_attempts
    assert "NoteError" in (policy.non_retryable_error_types or [])


def _fake_workflow(*, raises: bool, replaying: bool = False) -> types.SimpleNamespace:
    """A stand-in for `temporalio.workflow` inside this module.

    The real workflow API refuses to run outside a workflow event loop, and the time-skipping test
    server is not reachable offline — so the honest way to exercise this function's *own* logic
    (does a failure get counted, is a replay suppressed, is a success left alone) is to substitute
    the handle it calls through. The function under test is the real one, unmodified.
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

    `chemclaw_notes_proposed_total` counts only successes, so with no failure counter "the remote
    is down and every note was lost" and "nobody asked for a note" produce identical exposition.
    """
    from chemclaw.api.metrics import METRICS

    monkeypatch.setattr(publish_module, "workflow", _fake_workflow(raises=True))
    before = METRICS.value("chemclaw_notes_publish_failures_total")

    asyncio.run(publish_note_best_effort(object(), [], label="qm:compute"))

    assert METRICS.value("chemclaw_notes_publish_failures_total") == before + 1


def test_a_replayed_failure_is_not_counted_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replay re-executes workflow code; counting there would inflate the metric on every replay."""
    from chemclaw.api.metrics import METRICS

    monkeypatch.setattr(publish_module, "workflow", _fake_workflow(raises=True, replaying=True))
    before = METRICS.value("chemclaw_notes_publish_failures_total")

    asyncio.run(publish_note_best_effort(object(), [], label="qm:compute"))

    assert METRICS.value("chemclaw_notes_publish_failures_total") == before


def test_a_successful_publish_counts_no_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard is on the failure path only — a working remote must not move the counter."""
    from chemclaw.api.metrics import METRICS

    monkeypatch.setattr(publish_module, "workflow", _fake_workflow(raises=False))
    before = METRICS.value("chemclaw_notes_publish_failures_total")

    asyncio.run(publish_note_best_effort(object(), [], label="qm:compute"))

    assert METRICS.value("chemclaw_notes_publish_failures_total") == before
