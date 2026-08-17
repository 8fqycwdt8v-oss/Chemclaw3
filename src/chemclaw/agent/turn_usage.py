"""One turn's model usage, read off a message and split along the dimensions it is priced along.

Separated from the turn lifecycle because it is the turn's *arithmetic*: `graph_usage_tokens` is a
pure read of one message (streamed chunk or finished `AIMessage` — the attribute is the same), and
`TurnUsage` is a running total, so both can be exercised by handing them an object rather than by
driving a whole turn, which is exactly how `tests/test_budget.py` and `tests/test_metrics_bridge.py`
use them.

**It lives in `chemclaw.agent`, not in `chemclaw.api`, because a turn is not only a chat turn.**
It sat in `api/runner_usage.py` while the *only* caller was the front door, and the consequence was
not a naming quibble: a template's `agent` step (`durable/template_activities.run_agent_step`) runs
a real model turn, and `chemclaw.durable → chemclaw.api` is a forbidden edge
(`tests/test_layering.py`), so the one path that could not reach this module was the one that
therefore metered nothing at all — every template run spent tokens no counter and no `turn_costs`
row ever saw. Moving the arithmetic to the layer both callers may depend on is what makes one
implementation serve both, instead of the durable path growing a second one that would drift.

What a *caller* does with the numbers (book them against the budget, publish the counters, write
the cost row) stays with the caller, because that part is the lifecycle.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class TurnUsage:
    """One turn's model usage, split along the dimensions it is *priced* along (REV-10).

    The runner used to accumulate a single int, and `chemclaw_tokens_total` published it. That
    number cannot answer "what is this deployment costing", which is the question AG-11 asks:
    input, output and cache-read carry different prices — a cache read is roughly an order of
    magnitude cheaper than a fresh input token — so a deployment that caches well and one that does
    not report identical totals while their bills differ several-fold.

    Every provider this system has run against has reported all four; nothing read past the sum.

    `total` stays the sum the budget guard meters, so the runaway-cost refusal is unchanged: this
    splits what is *published*, not what is enforced.
    """

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    total: int = 0
    # Usage blocks that were present and yielded no token count — see `graph_usage_tokens`. Not a
    # token quantity, so it is deliberately not summed into `total`.
    unreadable: int = 0

    def add(self, other: "TurnUsage") -> None:
        """Accumulate another update's usage into this turn's running total."""
        self.input += other.input
        self.output += other.output
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write
        self.total += other.total
        self.unreadable += other.unreadable


def _cache_creation(cache: Mapping[str, Any]) -> int:
    """Tokens written to the prompt cache, under whichever of the two keys the provider used.

    **`cache_creation` alone is wrong, and it was wrong on every real cached call.** LangChain's
    Anthropic reader publishes cache writes twice over: once as the flat `cache_creation`, and once
    broken out per TTL as `ephemeral_5m_input_tokens`/`ephemeral_1h_input_tokens` — and when the
    per-TTL breakdown is present it **zeroes the flat key** to avoid double counting. Anthropic
    returns that breakdown, so the flat key is zero exactly when a write happened.

    Measured live on `claude-haiku-4-5`, first call over a 21,325-token cached prefix:
    `{"cache_read": 0, "cache_creation": 0, "ephemeral_5m_input_tokens": 21325}`. Reading only the
    flat key booked all 21,325 of them as full-price `input`, left `cache_write` at 0, and so wrote
    a `turn_costs` row saying this deployment has never written a cache while it was writing one on
    every cold prefix. A write is priced at 1.25x input, so the row understated the call and the
    counter that exists to show caching working showed it never happening.

    `specific or flat` rather than `specific + flat` mirrors upstream's own rule for the same
    quantity — it is how `input_tokens` was computed, so this can never disagree with the total
    `graph_usage_tokens` subtracts it from.

    5-minute and 1-hour writes are summed into one number because `turn_costs` has one
    `cache_write_tokens` column. They are priced differently (1.25x vs 2x), so a deployment that
    ever runs both TTLs at once wants a column per TTL; nothing here sets a TTL other than the
    5-minute default, so that split would be a migration for a distinction no caller can currently
    make.

    Args:
        cache: One chunk's `input_token_details` mapping.

    Returns:
        Tokens written to cache this chunk, or 0.
    """
    per_ttl = sum(
        int(cache.get(key) or 0)
        for key in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")
    )
    return per_ttl or int(cache.get("cache_creation") or 0)


def graph_usage_tokens(chunk: Any) -> TurnUsage:
    """What one streamed message chunk reports about tokens, read off its `usage_metadata` (M8).

    Duck-typed on the mapping so a provider or version that reports no usage — or a scripted model
    in tests — simply meters 0; the turn caps still bind. `total` falls back to input+output when
    the provider omits it.

    **Cache counts are subtracted from `input`.** LangChain reports `input_tokens` *including* the
    cached tokens and then breaks them out again under `input_token_details`, so reading both
    without adjusting would count every cached token twice — once cheap, once expensive — and
    overstate the priced input of exactly the deployments that cache best. That is also why the
    four dimensions are kept apart at all: a cache read is roughly an order of magnitude cheaper
    than a fresh input token, so one undifferentiated total cannot answer what a deployment costs
    (REV-10, D-144). Which key a *write* arrives under is `_cache_creation`'s problem, and it is
    not the obvious one.

    **`unreadable` is the difference between "nobody reported usage" and "usage was reported and we
    could not read it".** Duck-typing on a provider's key names is the right shape — a provider
    that reports nothing must meter 0 rather than fail a turn — but it makes an upstream rename
    indistinguishable from silence, and the consequences are not the same. Measured on the reader
    this replaced, with the keys renamed under it: it returned all zeros, and with
    `budget_enabled=true` (what the chart ships) 50 turns of 15,000 real tokens each were booked as
    zero while `check()` went on allowing the next one. The runaway-cost guard was disarmed, the
    token counters stayed flat while the turn counter climbed, and `turn_costs` filled with
    all-zero rows — a deployment that looks free and is not.

    A chunk with no usage at all meters zero and is *not* counted unreadable: most chunks in a
    stream carry none, and that is the normal case rather than a signal.
    """
    details = getattr(chunk, "usage_metadata", None)
    if not isinstance(details, Mapping):
        return TurnUsage()
    nested = details.get("input_token_details")
    cache = nested if isinstance(nested, Mapping) else {}
    cache_read = int(cache.get("cache_read") or 0)
    cache_write = _cache_creation(cache)
    reported_input = int(details.get("input_tokens") or 0)
    total = details.get("total_tokens")
    if total is None:
        total = reported_input + int(details.get("output_tokens") or 0)
    return TurnUsage(
        input=max(reported_input - cache_read - cache_write, 0),
        output=int(details.get("output_tokens") or 0),
        cache_read=cache_read,
        cache_write=cache_write,
        total=int(total or 0),
        # A usage block that was present and yielded no total means either a genuinely empty
        # chunk, or the keys moved under us — see the docstring for what the second one costs.
        unreadable=0 if total else 1,
    )
