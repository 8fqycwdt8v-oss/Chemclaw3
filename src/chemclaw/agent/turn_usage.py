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

**A model call made *inside* a graph node is already counted, and that is measured rather than
assumed.** LangChain carries the invocation config in a contextvar, so a chat model a tool body
builds and `ainvoke`s with no config of its own still inherits the graph's callbacks — LangGraph's
`StreamMessagesHandler` among them — and its chunks ride the same `messages` stream
`api/graph_stream` meters. Driven on a compiled graph through the protocol condenser's exact
fan-out shape (`asyncio.gather` under a semaphore, inside `asyncio.timeout`): three inner calls of
55 tokens, 165 metered. So `agent/condense.py` needs nothing here — and adding it would not merely
be redundant, it would *unmeter* that call: an explicit `config={"callbacks": …}` **replaces** the
inherited ones rather than joining them, measured at 55 booked to the ambient ledger and 0 seen by
the stream. On the template path, where the step's meter is the graph's callback and there is no
ambient ledger at all, the same move would lose those tokens outright.

**A call made outside the graph is the one nothing sees**, and there is exactly one: the verifier's
judge, which `api/runner_answer.build_answer_event` runs after the stream is exhausted. The ambient
ledger below is what that call books itself into — task-local for the same reasons
`agent/loop_cap.py` gives its watch (concurrent turns cannot see each other's, and it is simply
absent off the request path, where nothing is metering anyway).
"""

from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult


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


def llm_result_usage(response: LLMResult) -> TurnUsage:
    """What one finished model call reported, summed over the generations the callback hands back.

    The shape is upstream's: `generations` is a list per prompt, each a list of candidates, and a
    chat call's single generation is the degenerate case of that rather than a different thing. A
    candidate whose message carries no usage meters 0, which is `graph_usage_tokens`' duck-typing
    doing its job — a provider reporting nothing must not fail a turn.

    One implementation because two callbacks read the same object: `durable/template_activities.
    _StepMeter`, which meters a template step's whole graph, and `_OffStreamMeter` below, which
    meters the one call that runs outside a graph. They book into different ledgers and agree about
    the arithmetic by construction.

    Args:
        response: The `on_llm_end` payload for one finished model call.

    Returns:
        That call's usage.
    """
    usage = TurnUsage()
    for generation in response.generations:
        for candidate in generation:
            usage.add(graph_usage_tokens(getattr(candidate, "message", None)))
    return usage


# The turn's ledger, made ambient so a model call that no stream carries can still find it. `None`
# off the request path — the CLI, a test, an eval — where nothing is metering and there is nothing
# to book into, which is what makes `off_stream_metering()` safe to pass unconditionally.
_ledger: ContextVar[TurnUsage | None] = ContextVar("chemclaw_turn_usage", default=None)


def set_turn_usage(usage: TurnUsage) -> object:
    """Make `usage` the ledger this turn's off-stream calls book into; returns a reset token."""
    return _ledger.set(usage)


def metered_turn_tokens() -> int:
    """What this turn has been metered so far, or 0 where nothing is metering.

    **The turn's whole bill, which is a wider number than any one reader assembles.** The runner
    hands one `TurnUsage` to both `set_turn_usage` and `api/graph_stream.graph_events`, so this
    object accumulates every chunk the stream carries — including the calls a *tool body* makes
    (`agent/condense.py` fans out one per protocol) and both attempts of a model call that
    `model_calls.RepairInvalidToolCalls` retried. Neither of those passes through
    `wrap_model_call`, so neither is visible to a middleware counting model responses.

    `agent/spend_cap.py` reads it for exactly that reason. It is a *floor* on the turn's spend
    rather than a live-exact figure — the stream accumulates as chunks arrive, so a call still in
    flight is not fully counted — which is the right shape for a guard that already documents
    itself as one call loose.

    Returns:
        The metered total, or 0 off the request path (a CLI turn, a template step, a test), where
        there is no ledger and the caller's own accounting is all there is.
    """
    ledger = _ledger.get()
    return ledger.total if ledger is not None else 0


def reset_turn_usage(token: object) -> None:
    """Tear the turn's ledger down (mirrors every other ambient's reset)."""
    _ledger.reset(token)  # type: ignore[arg-type]


class _OffStreamMeter(AsyncCallbackHandler):
    """Books every model call made under it into the turn's ambient ledger.

    A *callback* rather than a read of the returned value, because the value is not a message:
    `with_structured_output(...).ainvoke(...)` returns the parsed model, and the `usage_metadata`
    lives on the raw response the parser consumed. `include_raw=True` would expose it and would
    also change the caller's error contract — a parse failure stops raising and starts arriving as
    a field — so the metering would be paid for in the one place the verifier's degrade path
    depends on. The callback sees the call regardless of what the chain does with its output, and
    it is the same hook the template path already meters on.

    The ledger is mutated rather than rebound, for the reason `agent/loop_cap.py` gives: a call
    driven from a task of its own still books into the ledger its caller is holding.
    """

    async def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        """Add what one finished off-stream model call reported to this turn's total.

        Args:
            response: The call's result.
            kwargs: `run_id`, `parent_run_id` and the rest of the callback contract, unused here —
                a turn's spend is one number, not a per-call breakdown.
        """
        ledger = _ledger.get()
        if ledger is not None:
            ledger.add(llm_result_usage(response))


def off_stream_metering() -> dict[str, Any]:
    """The invocation config a model call outside the graph passes so its tokens are counted.

    Passed at the call site rather than baked into the client, because it is a property of *where
    the call runs*, not of which model it runs on: the same provider seam builds the judge and the
    condenser, and the condenser's calls are already metered by the stream they ride (see the
    module docstring). **Attaching this to an in-graph call would take that call off the stream**,
    since an explicit `callbacks` list replaces the inherited one instead of joining it — so this
    is not belt-and-braces, and it belongs only where nothing else is watching.

    Returns:
        The `config` mapping to hand `ainvoke`. Harmless off the request path: with no ambient
        ledger the handler books nothing.
    """
    return {"callbacks": [_OffStreamMeter()]}
