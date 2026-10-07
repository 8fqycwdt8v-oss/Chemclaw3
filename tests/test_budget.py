"""The runaway-cost guard: turn/token budgets and usage metering.

`BudgetTracker` counts turns and meters tokens per session and per user and refuses a turn past a
cap; `graph_usage_tokens` reads a streamed chunk's usage; all of it is a no-op with
`budget_enabled` off.
"""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from chemclaw.api.budget import BudgetExceeded, BudgetTracker
from chemclaw.api.runner_usage import graph_usage_tokens
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS


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
    """With `budget_enabled` off, check never raises and record books nothing.

    The disabled period is re-read through an enabled tracker: nothing may be booked, or turning
    budgets on would start live sessions partway to their cap.
    """
    monkeypatch.setattr(settings, "budget_enabled", False)
    monkeypatch.setattr(settings, "budget_max_turns_per_session", 1)
    tracker = BudgetTracker()
    tracker.record("s1", "alice", tokens=10_000_000)
    tracker.record("s1", "alice", tokens=10_000_000)
    asyncio.run(tracker.check("s1", "alice"))  # no cap enforced while disabled

    monkeypatch.setattr(settings, "budget_enabled", True)
    # A cap of 1 turn, and nothing was ever booked against it.
    asyncio.run(tracker.check("s1", "alice"))


def test_session_turn_cap_refuses_the_next_turn(
    monkeypatch: pytest.MonkeyPatch, _enabled: None
) -> None:
    """A session turn cap of N allows N turns and refuses the N+1-th."""
    monkeypatch.setattr(settings, "budget_max_turns_per_session", 2)
    tracker = BudgetTracker()
    asyncio.run(tracker.check("s1", "alice"))  # turn 1 admitted
    tracker.record("s1", "alice", tokens=0)
    asyncio.run(tracker.check("s1", "alice"))  # turn 2 admitted
    tracker.record("s1", "alice", tokens=0)
    with pytest.raises(BudgetExceeded, match="session turn budget"):
        asyncio.run(tracker.check("s1", "alice"))  # turn 3 refused


def test_session_token_cap_refuses_when_spent(
    monkeypatch: pytest.MonkeyPatch, _enabled: None
) -> None:
    """A session token cap refuses once metered tokens reach it."""
    monkeypatch.setattr(settings, "budget_max_tokens_per_session", 1000)
    tracker = BudgetTracker()
    asyncio.run(tracker.check("s1", "alice"))
    tracker.record("s1", "alice", tokens=1000)
    with pytest.raises(BudgetExceeded, match="session token budget"):
        asyncio.run(tracker.check("s1", "alice"))


def test_user_cap_spans_sessions(monkeypatch: pytest.MonkeyPatch, _enabled: None) -> None:
    """The per-user cap accumulates across a user's sessions, unlike the per-session cap."""
    monkeypatch.setattr(settings, "budget_max_turns_per_user", 2)
    tracker = BudgetTracker()
    tracker.record("s1", "alice", tokens=0)
    tracker.record("s2", "alice", tokens=0)  # different session, same user
    asyncio.run(tracker.check("s3", "bob"))  # a different user is unaffected
    with pytest.raises(BudgetExceeded, match="user turn budget"):
        asyncio.run(tracker.check("s3", "alice"))  # alice's user cap is spent


def test_zero_cap_is_unlimited(monkeypatch: pytest.MonkeyPatch, _enabled: None) -> None:
    """A cap of 0 means unlimited on that dimension (the caps default to 0 in the fixture)."""
    tracker = BudgetTracker()
    for _ in range(1000):
        tracker.record("s1", "alice", tokens=1_000_000)
    asyncio.run(tracker.check("s1", "alice"))  # never refused — all caps are 0


def test_anonymous_user_only_hits_session_caps(
    monkeypatch: pytest.MonkeyPatch, _enabled: None
) -> None:
    """A None user (unauthenticated dev path) books to no user scope, only to the session."""
    monkeypatch.setattr(settings, "budget_max_turns_per_user", 1)
    tracker = BudgetTracker()
    tracker.record("s1", None, tokens=0)
    tracker.record("s1", None, tokens=0)
    asyncio.run(tracker.check("s1", None))  # no user counter to exceed


def test_a_reported_total_is_preferred_and_a_missing_one_is_derived() -> None:
    """`graph_usage_tokens` meters `total_tokens`, falling back to input+output when it is absent.

    The budget guard meters `total`, so this is the number that refuses a turn — unchanged by the
    priced split (REV-10), which only changed what is *published*.
    """
    reported = SimpleNamespace(
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 42}
    )
    derived = SimpleNamespace(usage_metadata={"input_tokens": 10, "output_tokens": 5})
    assert graph_usage_tokens(reported).total == 42
    assert graph_usage_tokens(derived).total == 15


def test_a_chunk_that_is_not_a_usage_chunk_meters_zero() -> None:
    """A chunk with no `usage_metadata` at all meters 0 — the scripted-model path in tests."""
    assert graph_usage_tokens(SimpleNamespace(content="hi")).total == 0
    assert graph_usage_tokens(SimpleNamespace()).total == 0


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
        asyncio.run(tracker.check("s4", "bob"))  # bob's counter survived and binds
    # Alice was evicted, so her budget reset — the documented best-effort trade.
    asyncio.run(tracker.check("s4", "alice"))


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
        asyncio.run(tracker.check("s3", "alice"))  # touches alice → bob becomes the LRU
    tracker.record("s4", "carol", tokens=0)  # evicts bob, not alice
    with pytest.raises(BudgetExceeded, match="user turn budget"):
        asyncio.run(tracker.check("s5", "alice"))  # alice's spent budget still binds


def test_tokens_accumulate_across_turns_rather_than_replacing_each_other(
    monkeypatch: pytest.MonkeyPatch, _enabled: None
) -> None:
    """A token cap counts a session's *total*, so `_book` must add rather than assign.

    Three turns of 400 against a cap of 1000: assignment would admit a fourth; addition refuses.
    """
    monkeypatch.setattr(settings, "budget_max_tokens_per_session", 1_000)
    tracker = BudgetTracker()
    for _ in range(3):
        tracker.record("s1", None, tokens=400)
    with pytest.raises(BudgetExceeded, match="session token budget"):
        asyncio.run(tracker.check("s1", None))


def test_a_turn_that_metered_no_tokens_books_none(
    monkeypatch: pytest.MonkeyPatch, _enabled: None
) -> None:
    """Zero is booked as zero — the other half of `max(tokens, 0)`.

    A turn whose usage was not reported meters zero; charging it anyway would tie the cap to
    reporting failures rather than cost.
    """
    tracker = BudgetTracker()
    for _ in range(50):
        tracker.record("s-free", None, tokens=0)
    monkeypatch.setattr(settings, "budget_max_tokens_per_session", 1)
    # Fifty free turns must not have spent a single token.
    asyncio.run(tracker.check("s-free", None))


def test_graph_usage_does_not_count_a_cached_token_twice() -> None:
    """A cached token is one token, however the gateway chose to report it.

    `langchain_openai`'s `input_tokens` includes the cached share and `input_token_details` breaks
    it out again, so `input` here is the residual. Key names are pinned in
    `tests/test_upstream_surface.py`.
    """
    chunk = SimpleNamespace(
        usage_metadata={
            "input_tokens": 1000,
            "output_tokens": 200,
            "total_tokens": 1200,
            "input_token_details": {"cache_read": 700, "cache_creation": 100},
        }
    )
    usage = graph_usage_tokens(chunk)
    assert (usage.input, usage.cache_read, usage.cache_write) == (200, 700, 100)
    assert usage.output == 200
    assert usage.total == 1200
    # The priced dimensions still account for every input token exactly once.
    assert usage.input + usage.cache_read + usage.cache_write == 1000


def test_a_usage_block_with_no_cache_details_meters_full_input() -> None:
    """The ordinary uncached reply: no `input_token_details`, so nothing is subtracted.

    A missing block must neither fail the turn nor zero the input.
    """
    usage = graph_usage_tokens(
        SimpleNamespace(
            usage_metadata={"input_tokens": 500, "output_tokens": 20, "total_tokens": 520}
        )
    )
    assert (usage.cache_read, usage.cache_write) == (0, 0)
    assert usage.input == 500
    assert usage.unreadable == 0


def test_a_chunk_with_no_usage_meters_nothing_and_is_not_called_unreadable() -> None:
    """Most chunks in a stream carry no usage; that is the normal case, not a missing-keys signal.

    `unreadable` is what catches an upstream rename, so it must not fire on ordinary chunks.
    """
    assert graph_usage_tokens(SimpleNamespace()).total == 0
    assert graph_usage_tokens(SimpleNamespace()).unreadable == 0


def test_a_service_tier_does_not_turn_every_cached_token_into_fresh_input() -> None:
    """The cache split survives a service tier, because the tier renames the keys.

    `_create_usage_metadata` prefixes the cache keys (`priority_cache_read`, ...) when the response
    carries a `service_tier`, which a gateway decides. Driven through `_create_usage_metadata`
    itself, so the reader is shown to survive upstream's renaming.
    """
    from langchain_core.messages import AIMessage
    from langchain_openai.chat_models.base import _create_usage_metadata

    served = {
        "prompt_tokens": 1000,
        "completion_tokens": 250,
        "total_tokens": 1250,
        "prompt_tokens_details": {"cached_tokens": 400, "cache_write_tokens": 100},
    }
    split = {
        tier: graph_usage_tokens(
            AIMessage(content="", usage_metadata=_create_usage_metadata(dict(served), tier))
        )
        for tier in (None, "priority", "flex")
    }
    for tier, usage in split.items():
        assert (usage.input, usage.cache_read, usage.cache_write, usage.total) == (
            500,
            400,
            100,
            1250,
        ), f"service_tier={tier!r} changed how a cached token is priced"


def test_a_judge_reply_that_fails_validation_still_books_what_the_gateway_served() -> None:
    """A judge reply that fails validation still books what the gateway served.

    With `method="json_schema"` validation raises before `on_llm_end`, so tokens would go unbooked
    on the verifier's degrade path. Asserted through the callback the meter implements, with the
    `LLMResult` shape `_generate_response_from_error` builds (raw body under
    `response_metadata["body"]`), so the provider's own count is used.
    """
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    from chemclaw.agent.turn_usage import TurnUsage, off_stream_metering, set_turn_usage

    served = LLMResult(
        generations=[
            [
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        response_metadata={
                            "status_code": 200,
                            "body": {
                                "usage": {
                                    "prompt_tokens": 5000,
                                    "completion_tokens": 500,
                                    "total_tokens": 5500,
                                    "prompt_tokens_details": {"cached_tokens": 1000},
                                }
                            },
                        },
                    )
                )
            ]
        ]
    )
    refused = LLMResult(generations=[])

    async def _run() -> tuple[TurnUsage, TurnUsage]:
        billed, unbilled = TurnUsage(), TurnUsage()
        for ledger, response in ((billed, served), (unbilled, refused)):
            token = set_turn_usage(ledger)
            try:
                meter = off_stream_metering()["callbacks"][0]
                await meter.on_llm_error(
                    ValueError("no structured VerificationResult"), response=response, run_id="r"
                )
            finally:
                from chemclaw.agent.turn_usage import reset_turn_usage

                reset_turn_usage(token)
        return billed, unbilled

    billed, unbilled = asyncio.run(_run())
    assert billed.total == 5500, "a reply the gateway served and we could not parse booked nothing"
    assert (billed.input, billed.output, billed.cache_read) == (4000, 500, 1000), (
        "the failed call's usage is booked through the same four-way split as any other"
    )
    # A request the gateway never answered has no usage block, and must book nothing — the test
    # for "were we billed" is the gateway's own block, not a classification of the exception.
    assert unbilled.total == 0 and unbilled.unreadable == 0


def test_the_warning_fires_before_the_cap_rather_than_at_it(
    monkeypatch: pytest.MonkeyPatch, _enabled: None, caplog: pytest.LogCaptureFixture
) -> None:
    """A budget whose first signal is the 429 gives an operator no lead time.

    The turn that reports the problem is the turn that was lost to it, which is the whole reason
    `budget_warn_fraction` exists. 800 of 1,000 is 80%: over the fraction, under the cap.
    """
    monkeypatch.setattr(settings, "budget_max_tokens_per_session", 1_000)
    monkeypatch.setattr(settings, "budget_warn_fraction", 0.8)
    tracker = BudgetTracker()

    before = METRICS.value("chemclaw_budget_warnings_total")
    with caplog.at_level(logging.WARNING, logger="chemclaw.api.budget"):
        tracker.record("s1", None, tokens=800)

    assert METRICS.value("chemclaw_budget_warnings_total") == before + 1
    assert "80% spent" in caplog.text
    # And the turn is still admitted — a warning that refused would just be an earlier cap.
    asyncio.run(tracker.check("s1", None))


def test_the_warning_fires_once_for_a_crossing_rather_than_on_every_turn_in_the_band(
    monkeypatch: pytest.MonkeyPatch, _enabled: None, caplog: pytest.LogCaptureFixture
) -> None:
    """The warning fires once for a crossing rather than on every turn in the band.

    The band between the fraction and the cap can span many turns, and the alert has no `for:`
    clause because a crossing is a step, not a rate.
    """
    monkeypatch.setattr(settings, "budget_max_tokens_per_session", 1_000)
    monkeypatch.setattr(settings, "budget_warn_fraction", 0.8)
    tracker = BudgetTracker()

    before = METRICS.value("chemclaw_budget_warnings_total")
    with caplog.at_level(logging.WARNING, logger="chemclaw.api.budget"):
        tracker.record("s1", None, tokens=800)
        for _ in range(9):
            tracker.record("s1", None, tokens=20)

    assert METRICS.value("chemclaw_budget_warnings_total") == before + 1, (
        "nine further turns inside the same band must not each re-announce the one crossing"
    )


def test_the_warning_names_the_principal_it_is_about(
    monkeypatch: pytest.MonkeyPatch, _enabled: None, caplog: pytest.LogCaptureFixture
) -> None:
    """The series is unlabelled on purpose, so the log line is the only route to *who*.

    Principals cannot be label values, so the warning names the principal, not just the scope kind.
    """
    monkeypatch.setattr(settings, "budget_max_tokens_per_session", 1_000)
    monkeypatch.setattr(settings, "budget_max_tokens_per_user", 1_000)
    monkeypatch.setattr(settings, "budget_warn_fraction", 0.8)
    tracker = BudgetTracker()

    with caplog.at_level(logging.WARNING, logger="chemclaw.api.budget"):
        tracker.record("session-abc-123", "oid-alice-9999", tokens=850)

    assert "session-abc-123" in caplog.text, "the session warning must name the session"
    assert "oid-alice-9999" in caplog.text, "the user warning must name the principal"


def test_the_warning_is_silent_below_the_fraction_and_at_the_cap(
    monkeypatch: pytest.MonkeyPatch, _enabled: None
) -> None:
    """The warning is silent below the fraction and at the cap.

    At the cap the refusal already tells the caller; a warning would repeat it every turn.
    """
    monkeypatch.setattr(settings, "budget_max_tokens_per_session", 1_000)
    monkeypatch.setattr(settings, "budget_warn_fraction", 0.8)

    quiet = BudgetTracker()
    before = METRICS.value("chemclaw_budget_warnings_total")
    quiet.record("s-low", None, tokens=799)
    assert METRICS.value("chemclaw_budget_warnings_total") == before, "warned below the fraction"

    spent = BudgetTracker()
    spent.record("s-cap", None, tokens=1_000)
    assert METRICS.value("chemclaw_budget_warnings_total") == before, "warned at the cap"


def test_checking_a_budget_never_warns(monkeypatch: pytest.MonkeyPatch, _enabled: None) -> None:
    """The warning is booked by `record`, because `check` runs twice for every one turn."""
    monkeypatch.setattr(settings, "budget_max_tokens_per_session", 1_000)
    monkeypatch.setattr(settings, "budget_warn_fraction", 0.8)
    tracker = BudgetTracker()
    tracker.record("s1", None, tokens=800)

    before = METRICS.value("chemclaw_budget_warnings_total")
    asyncio.run(tracker.check("s1", None))
    asyncio.run(tracker.check("s1", None))

    assert METRICS.value("chemclaw_budget_warnings_total") == before, (
        "a warning emitted from `check` doubles every count, because the front door checks twice"
    )


def test_a_zero_fraction_disables_the_warning(
    monkeypatch: pytest.MonkeyPatch, _enabled: None
) -> None:
    """0 is off, on the convention `core/config/agent.py` states for numeric ceilings."""
    monkeypatch.setattr(settings, "budget_max_tokens_per_session", 1_000)
    monkeypatch.setattr(settings, "budget_warn_fraction", 0)
    tracker = BudgetTracker()

    before = METRICS.value("chemclaw_budget_warnings_total")
    tracker.record("s1", None, tokens=999)

    assert METRICS.value("chemclaw_budget_warnings_total") == before
