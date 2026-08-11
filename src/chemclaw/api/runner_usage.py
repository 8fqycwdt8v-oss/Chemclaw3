"""One turn's model usage, read off the stream and split along the dimensions it is priced along.

Separated from `chemclaw.api.runner` because it is the turn's *arithmetic*, not its lifecycle:
`usage_tokens` is a pure read of a streamed update and `TurnUsage` is a running total, so both can
be exercised by handing them an update object — which is exactly how `tests/test_budget.py` and
`tests/test_metrics_bridge.py` already drive them, rather than through a whole turn.

What the runner does with the numbers (book them against the budget, publish the counters, write
the cost row) stays in the runner, because that part is the lifecycle.
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

    MAF has reported all four since the beginning (`UsageDetails` carries
    `cache_read_input_token_count` and `cache_creation_input_token_count` beside the input/output
    pair). Nothing read past the sum.

    `total` stays the sum the budget guard meters, so the runaway-cost refusal is unchanged: this
    splits what is *published*, not what is enforced.
    """

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    total: int = 0
    # Usage contents that were present and yielded no token count — see `usage_tokens`. Not a
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


def usage_tokens(update: Any) -> TurnUsage:
    """Best-effort usage reported in a streamed update's usage content (all zero if none).

    MAF emits usage as a content carrying a `UsageDetails` mapping. Duck-typed on the mapping so a
    provider or version that reports no usage — or the fake agent in tests — simply meters 0; the
    turn caps still bind.

    `total` falls back to input+output when the provider omits it, exactly as before. The cache
    counts are read separately rather than folded in, because a provider that reports them has
    already excluded cache reads from `input_token_count` — adding them would double-count the
    cheap tokens as expensive ones.

    **`unreadable` is the difference between "nobody reported usage" and "usage was reported and we
    could not read it".** Duck-typing on MAF's key names is the right shape — a provider that
    reports nothing must meter 0 rather than fail a turn — but it makes an upstream rename
    indistinguishable from silence, and the consequences are not the same. Measured: with the keys
    renamed to `input_tokens`/`output_tokens`, this returns `TurnUsage(0, 0, 0, 0, 0)`, and with
    `budget_enabled=true` (what the chart ships) 50 turns of 15,000 real tokens each were booked as
    zero while `check()` went on allowing the next one. The runaway-cost guard was disarmed, the
    token counters stayed flat while the turn counter climbed, and `turn_costs` filled with
    all-zero rows — a deployment that looks free and is not.

    So a usage content that yields no total is counted here rather than silently discarded. The
    fake agent in tests carries no usage content at all and is unaffected, which is exactly the
    distinction that makes this detectable without knowing what the keys will be called next.
    """
    usage = TurnUsage()
    for content in getattr(update, "contents", None) or []:
        details = getattr(content, "usage_details", None)
        if not isinstance(details, Mapping):
            continue
        tokens = details.get("total_token_count")
        if tokens is None:
            tokens = (details.get("input_token_count") or 0) + (
                details.get("output_token_count") or 0
            )
        if not tokens:
            # A usage content was present and told us nothing. Either the provider genuinely
            # reported a zero-token update, or the keys moved — and only the second is a problem,
            # so it is counted rather than raised.
            usage.unreadable += 1
        usage.add(
            TurnUsage(
                input=int(details.get("input_token_count") or 0),
                output=int(details.get("output_token_count") or 0),
                cache_read=int(details.get("cache_read_input_token_count") or 0),
                cache_write=int(details.get("cache_creation_input_token_count") or 0),
                total=int(tokens or 0),
            )
        )
    return usage


def graph_usage_tokens(chunk: Any) -> TurnUsage:
    """The same reading, off a LangChain message chunk's `usage_metadata` (M8).

    The twin of `usage_tokens`, and it exists rather than a branch inside that function because the
    two providers name every field differently while meaning the same thing: LangChain reports
    `input_tokens`/`output_tokens`/`total_tokens` with the cache counts nested under
    `input_token_details`. What must *not* differ is the arithmetic — the same `TurnUsage`, the
    same "cache reads are not folded into input" rule, and the same `unreadable` counter — because
    the budget guard, the cost table and the token counters are all downstream of this and none of
    them may report a different number depending on which adapter reported the usage.

    **Cache counts are subtracted from `input`, which `usage_tokens` does not have to do.**
    Anthropic's own API excludes cache reads from `input_tokens`, and MAF passes that through; the
    LangChain adapter *includes* them and then breaks them out again in `input_token_details`. So
    reading both without adjusting would count every cached token twice — once cheap, once
    expensive — and overstate the priced input of exactly the deployments that cache best.

    A chunk with no usage at all meters zero and is not counted unreadable: most chunks in a stream
    carry none, and that is the normal case rather than a signal.
    """
    details = getattr(chunk, "usage_metadata", None)
    if not isinstance(details, Mapping):
        return TurnUsage()
    nested = details.get("input_token_details")
    cache = nested if isinstance(nested, Mapping) else {}
    cache_read = int(cache.get("cache_read") or 0)
    cache_write = int(cache.get("cache_creation") or 0)
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
        # A usage block that was present and yielded no total is the same signal it is on the other
        # engine: either a genuinely empty update, or the keys moved under us.
        unreadable=0 if total else 1,
    )
