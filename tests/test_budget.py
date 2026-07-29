"""The runaway-cost guard: turn/token budgets and usage metering (budget #3).

Proves the missing ceiling above the per-turn loop cap — `BudgetTracker` counts turns and meters
tokens per session and per user and refuses a turn past a cap, `_usage_tokens` reads MAF's usage
content, and the whole thing is a no-op when `budget_enabled` is off (today's default behavior).
"""

from types import SimpleNamespace

import pytest

from chemclaw.config import settings
from service.budget import BudgetExceeded, BudgetTracker
from service.runner import _usage_tokens


@pytest.fixture
def _enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable budgets with unlimited caps by default; each test tightens the one it exercises."""
    monkeypatch.setattr(settings, "budget_enabled", True)
    for field in (
        "budget_max_turns_per_session",
        "budget_max_tokens_per_session",
        "budget_max_turns_per_user",
        "budget_max_tokens_per_user",
    ):
        monkeypatch.setattr(settings, field, 0)  # 0 == unlimited


def test_disabled_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """With `budget_enabled` off, check never raises and record books nothing (no change)."""
    monkeypatch.setattr(settings, "budget_enabled", False)
    monkeypatch.setattr(settings, "budget_max_turns_per_session", 1)
    tracker = BudgetTracker()
    tracker.record("s1", "alice", tokens=10_000_000)
    tracker.record("s1", "alice", tokens=10_000_000)
    tracker.check("s1", "alice")  # no cap enforced while disabled


def test_session_turn_cap_refuses_the_next_turn(
    monkeypatch: pytest.MonkeyPatch, _enabled: None
) -> None:
    """A session turn cap of N allows N turns and refuses the N+1-th."""
    monkeypatch.setattr(settings, "budget_max_turns_per_session", 2)
    tracker = BudgetTracker()
    tracker.check("s1", "alice")  # turn 1 admitted
    tracker.record("s1", "alice", tokens=0)
    tracker.check("s1", "alice")  # turn 2 admitted
    tracker.record("s1", "alice", tokens=0)
    with pytest.raises(BudgetExceeded, match="session turn budget"):
        tracker.check("s1", "alice")  # turn 3 refused


def test_session_token_cap_refuses_when_spent(
    monkeypatch: pytest.MonkeyPatch, _enabled: None
) -> None:
    """A session token cap refuses once metered tokens reach it."""
    monkeypatch.setattr(settings, "budget_max_tokens_per_session", 1000)
    tracker = BudgetTracker()
    tracker.check("s1", "alice")
    tracker.record("s1", "alice", tokens=1000)
    with pytest.raises(BudgetExceeded, match="session token budget"):
        tracker.check("s1", "alice")


def test_user_cap_spans_sessions(monkeypatch: pytest.MonkeyPatch, _enabled: None) -> None:
    """The per-user cap accumulates across a user's sessions, unlike the per-session cap."""
    monkeypatch.setattr(settings, "budget_max_turns_per_user", 2)
    tracker = BudgetTracker()
    tracker.record("s1", "alice", tokens=0)
    tracker.record("s2", "alice", tokens=0)  # different session, same user
    tracker.check("s3", "bob")  # a different user is unaffected
    with pytest.raises(BudgetExceeded, match="user turn budget"):
        tracker.check("s3", "alice")  # alice's user cap is spent


def test_zero_cap_is_unlimited(monkeypatch: pytest.MonkeyPatch, _enabled: None) -> None:
    """A cap of 0 means unlimited on that dimension (the caps default to 0 in the fixture)."""
    tracker = BudgetTracker()
    for _ in range(1000):
        tracker.record("s1", "alice", tokens=1_000_000)
    tracker.check("s1", "alice")  # never refused — all caps are 0


def test_anonymous_user_only_hits_session_caps(
    monkeypatch: pytest.MonkeyPatch, _enabled: None
) -> None:
    """A None user (unauthenticated dev path) books to no user scope, only to the session."""
    monkeypatch.setattr(settings, "budget_max_turns_per_user", 1)
    tracker = BudgetTracker()
    tracker.record("s1", None, tokens=0)
    tracker.record("s1", None, tokens=0)
    tracker.check("s1", None)  # no user counter to exceed


def test_usage_tokens_reads_maf_usage_content() -> None:
    """`_usage_tokens` sums the usage content's tokens, preferring total, else input+output."""
    total = SimpleNamespace(usage_details={"total_token_count": 42})
    split = SimpleNamespace(usage_details={"input_token_count": 10, "output_token_count": 5})
    plain = SimpleNamespace(name="tool", arguments="{}")  # a non-usage content
    update = SimpleNamespace(contents=[total, split, plain])
    assert _usage_tokens(update) == 42 + 15


def test_usage_tokens_zero_without_usage() -> None:
    """An update with no usage content meters 0 (the fake-agent / no-usage-provider path)."""
    assert _usage_tokens(SimpleNamespace(contents=[SimpleNamespace(text="hi")])) == 0
    assert _usage_tokens(SimpleNamespace()) == 0


def test_session_counters_are_bounded_by_live_session_cap(
    monkeypatch: pytest.MonkeyPatch, _enabled: None
) -> None:
    """The per-session map is LRU-bounded by `service_max_live_sessions`.

    The tracker lives for the pod's lifetime, so unbounded per-scope counters would be a slow
    memory leak.
    """
    monkeypatch.setattr(settings, "service_max_live_sessions", 2)
    tracker = BudgetTracker()
    for sid in ("s1", "s2", "s3"):
        tracker.record(sid, None, tokens=0)
    assert len(tracker._sessions._entries) == 2  # bounded: the LRU session was evicted


def test_user_counters_are_bounded_and_evict_lru(
    monkeypatch: pytest.MonkeyPatch, _enabled: None
) -> None:
    """Past `budget_max_tracked_users` the LRU user's counters are evicted (reset).

    Eviction resets that user's budget — the documented best-effort trade; the durable
    rolling-window quota stays deferred.
    """
    monkeypatch.setattr(settings, "budget_max_tracked_users", 2)
    monkeypatch.setattr(settings, "budget_max_turns_per_user", 1)
    tracker = BudgetTracker()
    tracker.record("s1", "alice", tokens=0)
    tracker.record("s2", "bob", tokens=0)
    tracker.record("s3", "carol", tokens=0)  # evicts alice (LRU)
    with pytest.raises(BudgetExceeded, match="user turn budget"):
        tracker.check("s4", "bob")  # bob's counter survived and binds
    tracker.check("s4", "alice")  # alice was evicted → her budget reset (best-effort trade)


def test_recently_checked_user_survives_eviction(
    monkeypatch: pytest.MonkeyPatch, _enabled: None
) -> None:
    """`check` marks a scope recently active, so a user mid-conversation is not the one evicted."""
    monkeypatch.setattr(settings, "budget_max_tracked_users", 2)
    monkeypatch.setattr(settings, "budget_max_turns_per_user", 1)
    tracker = BudgetTracker()
    tracker.record("s1", "alice", tokens=0)
    tracker.record("s2", "bob", tokens=0)
    with pytest.raises(BudgetExceeded, match="user turn budget"):
        tracker.check("s3", "alice")  # touches alice → bob becomes the LRU
    tracker.record("s4", "carol", tokens=0)  # evicts bob, not alice
    with pytest.raises(BudgetExceeded, match="user turn budget"):
        tracker.check("s5", "alice")  # alice's spent budget still binds
