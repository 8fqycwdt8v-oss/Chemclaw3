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
from chemclaw.core.errors import ChemclawError, SubsystemUnavailableError
from chemclaw.durable.publish import (
    BAD_DATA_RETRY,
    agent_step_retry,
    note_publish_retry,
    publish_note_best_effort,
)
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

    from chemclaw.durable.publish import _DECLARED_RETRYABLE

    registered = set(BAD_DATA_RETRY.non_retryable_error_types or []) | _DECLARED_RETRYABLE
    missing = names(ChemclawError) - registered
    assert not missing, f"ChemclawError subclasses not registered in _BAD_DATA_TYPES: {missing}"
    # And an exemption must never also be listed — a name in both sets is a contradiction the
    # policy would resolve silently (the list wins, and the "retryable" claim becomes false).
    assert not _DECLARED_RETRYABLE & set(BAD_DATA_RETRY.non_retryable_error_types or [])


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


def test_no_subsystem_outage_error_is_listed_non_retryable() -> None:
    """The third hierarchy is absent from `_BAD_DATA_TYPES` **on purpose** — do not "fix" this.

    `SubsystemUnavailableError` (`chemclaw.core.errors`) means "the infrastructure this needs is not
    answering", which is the *retryable* failure par excellence: the identical call succeeds once
    the broker is back. Registering it would tell Temporal to fail an activity fast on precisely the
    fault a retry fixes — the QM/BO/report workflows would give up on a broker restart instead of
    riding it out.

    It reads like an omission next to the two walks above, and the walks are exactly what would
    invite someone to close the gap: both fail loudly when a subclass is *missing* from the list.
    So this one fails loudly when a subclass is *present*, and the assertion message says why. The
    completeness sweep and this test cannot both be satisfied by accident — only by reading.
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


def test_the_agent_step_bound_is_narrower_than_the_shared_one() -> None:
    """The agent step is retried less than everything else, because its retry is not free.

    Every other activity recomputes on a retry. An agent step replays the whole turn from the
    prompt — an activity has no checkpointer behind it — so every tool the failed attempt already
    ran runs again with its side effects; measured, one provider 503 produced two PR-gate branches
    and two audit rows for one logical note. Strictly less than `BAD_DATA_RETRY`, because equal
    would mean the narrowing had been quietly undone while both settings still existed.

    It shares the bad-data list, and that is the point of the pairing: the two policies differ in
    *how many* transient attempts, never in *which* failures count as transient.
    """
    policy = agent_step_retry()

    assert policy.maximum_attempts == settings.agent_step_max_attempts
    assert (policy.maximum_attempts or 0) < (BAD_DATA_RETRY.maximum_attempts or 0)
    assert policy.non_retryable_error_types == BAD_DATA_RETRY.non_retryable_error_types


def test_no_provider_transient_name_is_listed_non_retryable() -> None:
    """The other way someone could "fix" the duplicate-note bug, and it would be wrong.

    The pairing to `test_no_subsystem_outage_error_is_listed_non_retryable` above: that one guards
    the hierarchy this repo owns, this one guards the names it does not. Filing an LLM provider's
    503/429/connection error as bad data would stop the duplicate turns — by declaring a failure
    that *does* succeed on retry to be one that never will, which is false in exactly the direction
    this list exists to keep straight, and would make every workflow give up on a provider blip it
    would otherwise ride out. `agent_step_retry`'s bound is the honest lever instead.

    Worse than merely wrong, it would be wrong at a distance. Temporal matches by bare class name,
    `anthropic` and `openai` use the *same* class names for these, and `_BAD_DATA_TYPES` is shared
    by every activity — so one entry here would silently reclassify these failures for the QM, BO
    and report workflows too, none of which involve a model call at all.
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
