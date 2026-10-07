"""One turn's model usage, read off a message and split along the dimensions it is priced along.

The turn's arithmetic, pure enough to test on an object rather than a turn. In `chemclaw.agent`
so both the front door and a template's `agent` step (`durable`, which may not import `api`) meter
with one implementation; booking the numbers stays with each caller.

A model call made inside a graph node — including from a tool body — inherits the graph's
callbacks via LangChain's config contextvar and is metered by the stream. Passing an explicit
`callbacks` config would replace the inherited ones and unmeter it. The one call outside the graph
is the verifier's judge, which books itself into the task-local ambient ledger below.
"""

import logging
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import SystemMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.outputs import LLMResult

from chemclaw.agent.context_budget import estimator_ratio, prefix_tokens

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TurnUsage:
    """One turn's model usage, split along the dimensions it is priced along.

    Input, output, cache-read and cache-write carry different prices, so a single total cannot say
    what a deployment costs. `total` is the sum the budget guard meters; this splits what is
    published, not what is enforced.
    """

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    total: int = 0
    # Usage blocks present but unreadable — a count, not tokens, so not in `total`.
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
    """What one streamed message chunk reports about tokens, read off its `usage_metadata`.

    Duck-typed so a provider reporting no usage meters 0; `total` falls back to input+output. Cache
    counts are subtracted from `input`, because `langchain_openai` reports `prompt_tokens`
    (including cached ones) as `input_tokens` and breaks the cached share out again. Cache keys are
    read by suffix because a response's service tier prefixes them (`priority_cache_read`).
    Cache-write is usually 0 on an OpenAI-compatible gateway.

    `unreadable` distinguishes "usage reported but unreadable" (an upstream rename, which would
    otherwise silently disarm the budget guard) from "no usage", which is the normal case for most
    chunks and is not flagged.
    """
    details = getattr(chunk, "usage_metadata", None)
    if not isinstance(details, Mapping):
        return TurnUsage()
    nested = details.get("input_token_details")
    cache = nested if isinstance(nested, Mapping) else {}
    cache_read = _cache_detail(cache, "cache_read")
    cache_write = _cache_detail(cache, "cache_creation")
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
        # Present but no total: an empty chunk, or upstream renamed the keys (see the docstring).
        unreadable=0 if total else 1,
    )


def _cache_detail(details: Mapping[str, Any], name: str) -> int:
    """One cache dimension out of `input_token_details`, whatever tier prefix it carries.

    Summed over every key ending in `name`, since the response's service tier may prefix it and the
    shape does not forbid two prefixes.
    """
    return sum(
        int(value or 0) for key, value in details.items() if key == name or key.endswith(f"_{name}")
    )


def error_result_usage(response: Any) -> TurnUsage:
    """What a model call that raised after the gateway answered was billed for.

    With `method="json_schema"` a reply that fails validation raises inside the SDK before
    `on_llm_end` fires — the verifier's normal degrade path — so it would otherwise book nothing.
    `langchain_core` puts the raw HTTP body on `response_metadata["body"]`, whose `usage` block is
    the provider's own count, so this goes to the measured ledger. A refused request has no `usage`,
    so it books zero without classifying the exception.

    Args:
        response: The `LLMResult` `on_llm_error` was handed, or anything else, in which case this
            books nothing rather than raising on an error path.

    Returns:
        What that call reported, or an empty `TurnUsage`.
    """
    usage = TurnUsage()
    try:
        from langchain_core.messages import AIMessage
        from langchain_openai.chat_models.base import _create_usage_metadata

        for generation in getattr(response, "generations", None) or []:
            for candidate in generation:
                metadata = getattr(getattr(candidate, "message", None), "response_metadata", None)
                body = metadata.get("body") if isinstance(metadata, Mapping) else None
                if not isinstance(body, Mapping):
                    continue
                served = body.get("usage")
                if not isinstance(served, Mapping):
                    continue
                tier = body.get("service_tier")
                usage.add(
                    graph_usage_tokens(
                        AIMessage(
                            content="",
                            usage_metadata=_create_usage_metadata(dict(served), tier),
                        )
                    )
                )
    except Exception:
        logger.warning("could not read the usage of a failed model call; it books zero")
        return TurnUsage()
    return usage


def llm_result_usage(response: LLMResult) -> TurnUsage:
    """What one finished model call reported, summed over the generations the callback hands back.

    Shared by `durable/template_activities._StepMeter` and `_OffStreamMeter`, which book into
    different ledgers. A candidate with no usage meters 0.

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


# The turn's ledger, ambient so a call no stream carries can find it. `None` off the request path,
# which makes `off_stream_metering()` safe to pass unconditionally.
_ledger: ContextVar[TurnUsage | None] = ContextVar("chemclaw_turn_usage", default=None)


def set_turn_usage(usage: TurnUsage) -> object:
    """Make `usage` the ledger this turn's off-stream calls book into; returns a reset token."""
    return _ledger.set(usage)


def metered_turn_tokens() -> int:
    """What this turn has been metered so far, or 0 where nothing is metering.

    The runner hands one `TurnUsage` to `set_turn_usage` and the stream, so this includes model
    calls made by tool bodies, which no `wrap_model_call` sees; `agent/spend_cap.py` reads it for
    that. A floor, not live-exact: a call in flight is not fully counted.

    Returns:
        The metered total, or 0 off the request path, where there is no ledger.
    """
    ledger = _ledger.get()
    return ledger.total if ledger is not None else 0


def reset_turn_usage(token: object) -> None:
    """Tear the turn's ledger down (mirrors every other ambient's reset)."""
    _ledger.reset(token)  # type: ignore[arg-type]


class _OffStreamMeter(AsyncCallbackHandler):
    """Books every model call made under it into the turn's ambient ledger.

    A callback, because `with_structured_output(...)` returns the parsed model and the usage is on
    the raw response; `include_raw=True` would change the caller's error contract. The ledger is
    mutated, not rebound, so a call from another task books into its caller's ledger. `on_llm_error`
    covers a reply that fails validation after being served.
    """

    async def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        """Add what one finished off-stream model call reported to this turn's total.

        Args:
            response: The call's result.
            kwargs: The rest of the callback contract, unused.
        """
        ledger = _ledger.get()
        if ledger is not None:
            ledger.add(llm_result_usage(response))

    async def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        """Book what a call that raised after being served was billed for — see the class.

        Args:
            error: The exception, unused: the gateway's body says whether it billed.
            kwargs: The callback contract; `response` carries the `LLMResult` built from the
                failing call's raw HTTP body.
        """
        ledger = _ledger.get()
        if ledger is not None:
            ledger.add(error_result_usage(kwargs.get("response")))


class InFlightPrompts(AsyncCallbackHandler):
    """What the model calls still in flight have already committed this turn to paying.

    A gateway reports usage only on the terminal frame, so a turn cancelled mid-message would meter
    zero and dropping the connection would defeat the budget guard. So each call's prompt is
    estimated when it starts, held while it runs, and dropped when the provider's numbers arrive;
    only what was never reported is left at teardown.

    The estimate covers the whole request, tool schemas included (`prefix_tokens()` minus the system
    message, plus the message list), converted with `estimator_ratio()`. Streamed output is ignored.
    The caller books it against the budget and publishes it separately as `estimated_tokens`.
    """

    def __init__(self) -> None:
        """Start with nothing in flight — one instance per turn, held by that turn's ledger."""
        self._pending: dict[Any, int] = {}

    async def on_chat_model_start(
        self, serialized: Any, messages: Any, *, run_id: Any = None, **kwargs: Any
    ) -> None:
        """Record what the call about to be made will cost if it is never billed to us."""
        self._pending[run_id] = _prompt_estimate(messages)

    async def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        """Forget a call the provider has reported on: its real usage rode the stream."""
        self._pending.pop(kwargs.get("run_id"), None)

    async def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        """Forget a call that raised — the turn will report the failure.

        A rejected request bills nothing, and a mid-response death cannot be told apart from the
        failure already recorded.
        """
        self._pending.pop(kwargs.get("run_id"), None)

    @property
    def unbilled_tokens(self) -> int:
        """Estimated billed tokens for every call started under this turn and never reported."""
        estimated = sum(self._pending.values())
        return int(estimated * estimator_ratio()) if estimated else 0


def _prompt_estimate(messages: Any) -> int:
    """One model call's whole request in estimated tokens: its messages plus its tool schemas.

    `messages` is upstream's list per prompt. Anything unexpected meters 0: this runs on every model
    call and must never end a turn.
    """
    try:
        prompt = list(messages[0]) if messages else []
        system = prompt[:1] if prompt and isinstance(prompt[0], SystemMessage) else []
        schemas = max(prefix_tokens() - int(count_tokens_approximately(system)), 0)
        return int(count_tokens_approximately(prompt)) + schemas
    except Exception:
        logger.warning("could not estimate a model call's prompt; it books zero if abandoned")
        return 0


def off_stream_metering() -> dict[str, Any]:
    """The invocation config a model call outside the graph passes so its tokens are counted.

    Only for calls outside the graph: on an in-graph call, an explicit `callbacks` list replaces the
    inherited ones and takes the call off the stream.

    Returns:
        The `config` mapping to hand `ainvoke`. Harmless off the request path: with no ambient
        ledger the handler books nothing.
    """
    return {"callbacks": [_OffStreamMeter()]}
