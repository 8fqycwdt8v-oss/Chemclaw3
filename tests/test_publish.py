"""The shared PR-gate publish retry policies fail fast on bad data and bound transient retries.

These guard the fix for the durability hole where an unclassified deterministic failure
(a `KeyError`/`RuntimeError` bug, or a git ref that can never be created) retried forever
because `BAD_DATA_RETRY` had no attempt bound. The policy must (a) be bounded, and (b) mark
every bad-data error type non-retryable by its exact class name (Temporal matches by name).
"""

import importlib
import pkgutil

from chemclaw.config import settings
from chemclaw.errors import ChemclawError
from workflows.publish import BAD_DATA_RETRY, note_publish_retry


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
    first_party = [
        "agents",
        "bo",
        "calc",
        "chemclaw",
        "eln",
        "evals",
        "kg",
        "mcp_servers",
        "memory",
        "report",
        "service",
        "sources",
        "workers",
        "workflows",
    ]
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
