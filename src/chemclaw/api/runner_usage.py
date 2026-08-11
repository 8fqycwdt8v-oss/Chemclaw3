"""One turn's model usage, read off the stream and split along the dimensions it is priced along.

Separated from `chemclaw.api.runner` because it is the turn's *arithmetic*, not its lifecycle:
`graph_usage_tokens` is a pure read of one streamed chunk and `TurnUsage` is a running total, so
both can be exercised by handing them a chunk object — which is exactly how `tests/test_budget.py`
and `tests/test_metrics_bridge.py` drive them, rather than through a whole turn.

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
    (REV-10, D-144).

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
        # A usage block that was present and yielded no total means either a genuinely empty
        # chunk, or the keys moved under us — see the docstring for what the second one costs.
        unreadable=0 if total else 1,
    )
