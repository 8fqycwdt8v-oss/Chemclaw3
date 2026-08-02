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
from temporalio.api.failure.v1 import Failure
from temporalio.converter import DefaultFailureConverter, DefaultPayloadConverter
from temporalio.exceptions import ActivityError, ApplicationError

import chemclaw.durable.publish as publish_module
from chemclaw.agent.authz import AuthorizationError
from chemclaw.agent.profile_discovery import ProfileError
from chemclaw.connectors.registry import ConnectorError
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.durable.publish import BAD_DATA_RETRY, note_publish_retry, publish_note_best_effort
from chemclaw.ingest.sources.registry import DataSourceError
from chemclaw.templates.registry import TemplateError
from chemclaw.templates.resolve import UnresolvedReference


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
    """Temporal matches non-retryable types by exact class name, not isinstance.

    So subclassing `ChemclawError` alone does NOT make a new bad-data error fail fast
    across an activity boundary — its concrete name must be in `_BAD_DATA_TYPES`. This
    walks every first-party module so all subclasses are defined, then asserts none is
    missing from the policy (the drift this base class was created to eliminate).
    """
    # `walk_packages` reaches every module under `chemclaw` (D-148), so a new subclass anywhere
    # in the tree is still defined by the time the assertion runs.
    _import_first_party_tree()

    def names(cls: type) -> set[str]:
        return {cls.__name__}.union(*(names(sub) for sub in cls.__subclasses__()), set())

    missing = names(ChemclawError) - set(BAD_DATA_RETRY.non_retryable_error_types or [])
    assert not missing, f"ChemclawError subclasses not registered in _BAD_DATA_TYPES: {missing}"


def test_every_authorization_error_subclass_is_listed_non_retryable() -> None:
    """The same completeness walk, rooted at `AuthorizationError` instead of `ChemclawError`.

    `AuthorizationError` is deliberately not a `ChemclawError` (an authorization refusal is not
    "bad data" — see its docstring in `chemclaw.agent.authz`), so the walk above never visits it or
    its subclasses (`DryRunRefusal`, `PlanNotApprovedError`). Without a walk of its own, a new
    subclass could cross an activity boundary unregistered and retry forever, exactly the drift
    `ChemclawError`'s walk exists to catch.
    """

    def names(cls: type) -> set[str]:
        return {cls.__name__}.union(*(names(sub) for sub in cls.__subclasses__()), set())

    _import_first_party_tree()
    missing = names(AuthorizationError) - set(BAD_DATA_RETRY.non_retryable_error_types or [])
    assert not missing, f"AuthorizationError subclasses not registered: {missing}"


@pytest.mark.parametrize(
    "error_cls",
    [
        ConnectorError,
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
    """The real classification Temporal applies, not the isinstance mismatch (R5).

    Temporal matches `non_retryable_error_types` by the `ApplicationError.type` string its own
    `DefaultFailureConverter` assigns — the exact class name, never an ancestor's. A class that
    derives from `ChemclawError` (hence `ValueError`) but is missing from `_BAD_DATA_TYPES` by its
    *own* name would still retry `activity_max_attempts` times with transient backoff before
    failing, exactly as `ConnectorError` did before it and its three siblings were reparented and
    registered, and exactly as `ProfileError` did before this test covered it.

    `AuthorizationError` is the sharper case: it is not a `ChemclawError`/`ValueError` at all (see
    its docstring for why reparenting it would be wrong), yet the same name-matching mechanism
    still applies — Temporal never looks at `isinstance`, so a plain `Exception` registered by name
    is classified non-retryable exactly like a `ChemclawError` subclass would be. This drives the
    real SDK converter (no server needed) rather than asserting on the class hierarchy, so it
    catches the isinstance/name mismatch the docstring in `template_activities.authorize_job_step`
    used to get wrong.
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
