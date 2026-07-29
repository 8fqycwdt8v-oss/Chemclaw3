"""Retention windows bound the durable stores (gap SCH-1).

Before this, nothing in the system deleted anything: every Postgres table grew for the life of the
deployment. For a GxP system that is a records gap, not just a disk one — "keep for N years, then
dispose, provably" needs a disposal step.

The Postgres round-trip skips offline (like every other PG test), so these pin the *policy* the job
encodes, which is where the real risk lives: what it prunes, what it refuses to prune and why, and
that a deployment must opt in before anything is deleted.
"""

import pytest

from chemclaw.config import settings
from workflows.retention import _PRUNABLE, _window_days


def test_only_spent_operational_rows_are_prunable() -> None:
    """The prunable set is closed and small — a new table is a deliberate addition, not a sweep."""
    assert set(_PRUNABLE) == {"session_events", "session_messages"}


def test_the_hash_chained_audit_trail_is_never_pruned() -> None:
    """Deleting from a hash chain is indistinguishable from tampering — the thing it detects.

    Safe disposal needs archive-then-reseal (export the prefix, verify it, record an out-of-band
    genesis anchor), which is a GxP design decision for an ADR, not something a cleanup job should
    quietly do. The table must therefore be absent from the prunable set entirely.
    """
    assert "audit_events" not in _PRUNABLE


def test_the_calculation_cache_is_never_pruned_by_age() -> None:
    """Evicting a cached result silently converts a cache hit into a recomputation (D-011).

    That is a cost policy question (LRU by access, or by compute cost), not a retention clock —
    an age cutoff could quietly re-run an expensive HPC job.
    """
    assert "calculation_results" not in _PRUNABLE


def test_retention_is_off_until_a_policy_is_stated(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment must choose its window; inheriting a deletion default from code is wrong."""
    assert settings.retention_session_events_days == 0
    assert settings.retention_session_messages_days == 0
    assert settings.retention_enabled is False
    for table in _PRUNABLE:
        assert _window_days(table) == 0, f"{table} would be pruned on an unstated policy"


def test_a_stated_window_is_read_per_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each table carries its own window — a mailbox row and a conversation age differently."""
    monkeypatch.setattr(settings, "retention_session_events_days", 7)
    monkeypatch.setattr(settings, "retention_session_messages_days", 365)
    assert _window_days("session_events") == 7
    assert _window_days("session_messages") == 365
