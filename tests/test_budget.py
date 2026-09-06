"""The runaway-cost guard: turn/token budgets and usage metering (budget #3).

Proves the missing ceiling above the per-turn loop cap — `BudgetTracker` counts turns and meters
tokens per session and per user and refuses a turn past a cap, `graph_usage_tokens` reads a
streamed chunk's usage, and the whole thing is a no-op when `budget_enabled` is off (the default).
"""

from types import SimpleNamespace

import pytest

from chemclaw.api.budget import BudgetExceeded, BudgetTracker
from chemclaw.api.runner_usage import graph_usage_tokens
from chemclaw.core.config import settings


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

    The second half is the load-bearing one and it used to be asserted only in this docstring:
    while budgets are off `check` returns before looking at anything, so whether `record` booked
    is invisible to it, and deleting `record`'s own `if not settings.budget_enabled: return`
    changed nothing any test could see (measured: 77 tests still passed).

    So the disabled period is *re-read* through an enabled tracker. Nothing must have been
    booked, because a deployment that turns budgets on — the chart does, `values.yaml` ships
    `budget_enabled: true` — would otherwise start every already-live session partway through its
    cap and refuse turns nobody had paid for.
    """
    monkeypatch.setattr(settings, "budget_enabled", False)
    monkeypatch.setattr(settings, "budget_max_turns_per_session", 1)
    tracker = BudgetTracker()
    tracker.record("s1", "alice", tokens=10_000_000)
    tracker.record("s1", "alice", tokens=10_000_000)
    tracker.check("s1", "alice")  # no cap enforced while disabled

    monkeypatch.setattr(settings, "budget_enabled", True)
    tracker.check("s1", "alice")  # a cap of 1 turn, and nothing was ever booked against it


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


def test_tokens_accumulate_across_turns_rather_than_replacing_each_other(
    monkeypatch: pytest.MonkeyPatch, _enabled: None
) -> None:
    """A token cap counts a session's *total*, so `_book` must add rather than assign.

    Found by mutation testing (2026-08-04): changing `counter.tokens += max(tokens, 0)` to
    `counter.tokens = max(tokens, 0)` survived the whole budget suite. Nothing here booked two
    turns and then checked a token cap, so every existing test passed with a meter that
    remembered only the last turn — and a token budget that only ever sees the newest turn is a
    budget that is never reached, which is the failure mode of a runaway-cost guard that costs
    money instead of saving it.

    Three turns of 400 against a cap of 1000: assignment leaves the counter at 400 and admits a
    fourth; addition reaches 1200 and refuses.
    """
    monkeypatch.setattr(settings, "budget_max_tokens_per_session", 1_000)
    tracker = BudgetTracker()
    for _ in range(3):
        tracker.record("s1", None, tokens=400)
    with pytest.raises(BudgetExceeded, match="session token budget"):
        tracker.check("s1", None)


def test_a_turn_that_metered_no_tokens_books_none(
    monkeypatch: pytest.MonkeyPatch, _enabled: None
) -> None:
    """Zero is booked as zero — the other half of `max(tokens, 0)`, and also a survivor.

    `max(tokens, 1)` survived too, which says the same thing from the opposite side: no test
    distinguished a free turn from a one-token turn. It matters because a turn whose usage the
    provider did not report meters as zero, and a guard that charged it anyway would make the token
    cap a function of how many turns failed to report rather than of what they cost.
    """
    tracker = BudgetTracker()
    for _ in range(50):
        tracker.record("s-free", None, tokens=0)
    monkeypatch.setattr(settings, "budget_max_tokens_per_session", 1)
    tracker.check("s-free", None)  # fifty free turns must not have spent a single token


def test_graph_usage_does_not_count_a_cached_token_twice() -> None:
    """A cached token is one token, however the gateway chose to report it.

    `langchain_openai` sets `input_tokens = prompt_tokens` — which OpenAI defines as *including*
    the cached share — and then breaks that share out again under `input_token_details`. Reading
    both without adjusting would bill every cached token as both a cheap read and a fresh input,
    overstating the priced input of exactly the deployments that cache best, which is the
    population the split exists to measure (REV-10). The key names are pinned in
    `tests/test_upstream_surface.py`.

    So `input` here is the *residual*: what was neither read from nor written to the cache.
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

    The axis this holds against the test above is absent-vs-present, which is the one that broke a
    reader before: `cache` defaults to `{}` rather than to the mapping, so a `.get` on a missing
    block must not fail the turn or silently zero the input.
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

    The distinction matters because `unreadable` is what would catch an upstream rename — the
    failure that once booked 50 turns of 15,000 real tokens as zero while the budget guard went on
    allowing the next one. A counter that fires on every ordinary chunk would say nothing.
    """
    assert graph_usage_tokens(SimpleNamespace()).total == 0
    assert graph_usage_tokens(SimpleNamespace()).unreadable == 0


def test_a_service_tier_does_not_turn_every_cached_token_into_fresh_input() -> None:
    """The cache split survives a service tier, because the tier renames the keys.

    `_create_usage_metadata` prefixes both cache keys when a tier is in play —
    `priority_cache_read`, `flex_cache_creation` — and `_create_chat_result` reads that tier off
    the **response**, so no request parameter and no setting in this repository has to exist for it
    to happen: a gateway that stamps `service_tier` decides it. `graph_usage_tokens` read the bare
    names, so measured 2026-09-06 the same 1,000-prompt / 400-cached reply booked
    `input 500, cache_read 400, cache_write 100` untiered and `input 1,000, cache_read 0,
    cache_write 0` on `priority` — every cached token priced as fresh input, and
    `chemclaw_cache_read_tokens_total` flat on the one deployment whose caching it is the only way
    to see. The total is unaffected, so the budget still binds and only the price split breaks.

    Driven through `_create_usage_metadata` itself rather than through a hand-written key list,
    because the thing being asserted is that this reader survives *upstream's* renaming, and a
    literal here would only assert that it survives mine.
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
    """The verifier's documented degrade path spent real tokens and booked zero.

    With `method="json_schema"` the reply is validated inside the OpenAI SDK, called from
    `langchain_openai._agenerate` — so a reply missing a required field raises before
    `_agenerate_with_cache` returns, `on_llm_end` never fires, and `_OffStreamMeter` books nothing.
    Measured 2026-09-06 end to end: the same turn twice against a gateway serving 6,600 tokens both
    times booked **6,600** with a valid verdict and **1,100** with one missing `confidence`, with
    `estimated_tokens` at 0 as well — 5,500 billed, nothing recorded anywhere, on the path
    `agent/verifier.py` degrades through on every schema drift.

    Asserted through the callback the meter actually implements, handed the `LLMResult` shape
    `langchain_core._generate_response_from_error` builds — raw HTTP body under
    `response_metadata["body"]` — because that shape is the whole mechanism: it is what makes this
    the provider's measured number rather than an estimate.
    """
    import asyncio

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
