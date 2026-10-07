"""What a model call is really about to cost, and what the context policy may therefore spend.

`agent/compaction.py` bounds the thread against budgets in billed tokens. This module supplies
the arithmetic:

- **Unit conversion.** The edits count with `count_tokens_approximately` (chars/4), which is close
  on prose and schemas but undercounts structured chemistry results. `note_model_call` compares
  each request's estimate with the provider's billed `input_tokens`, and `estimator_ratio` (an
  EWMA, clamped at 1.0 from below so it can only tighten) converts budgets into estimator units.
- **Exact prefix.** The system message and tool schemas are counted with a BPE encoding
  (`llm_token_encoding`) where one is baked into the image; the thread stays on the estimator,
  because exact counting there would cost loop time on every call.
- **Prefix charging.** The prefix is billed but is not in the thread, so `MeasureRequestPrefix`
  publishes it in a contextvar and `effective_trigger` subtracts it, up to
  `agent_context_prefix_basis`. `agent_context_token_budget` therefore bounds request spend.
- **Window.** When `llm_context_window_tokens` is declared, the trigger is also capped at what the
  model can hold after the output reservation.

Live figures come from `tests/test_context_floor.py` and `tests/test_compaction.py`; the defaults
are derived in `core/config/agent.py`. `TurnContext` records per turn whether compaction acted,
for `turn_costs`.
"""

import asyncio
import logging
import os
import tempfile
import threading
from collections.abc import Awaitable, Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain_core.messages import BaseMessage
from langchain_core.messages.utils import count_tokens_approximately

from chemclaw.core.config import settings
from chemclaw.core.logging import log_event
from chemclaw.core.metrics import METRICS
from chemclaw.core.metrics_bridge import degraded

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TurnContext:
    """What the context policy did to the turn in flight, for the readers that outlive a model call.

    `peak_reclaimed` is a high-water mark because the edits are non-destructive and re-derive the
    same reduction every call; per-call counting would multiply one compaction. The two booleans are
    separate facts `turn_costs` records: reduced, and over a trigger but irreducible.
    """

    peak_reclaimed: float = 0.0
    compacted: bool = False
    unreducible: bool = False


_turn: ContextVar[TurnContext | None] = ContextVar("chemclaw_turn_context", default=None)
# The estimated size of the current model call's prefix (system message plus bound tool schemas).
# 0 off the request path, which makes the prefix rules inert there.
_prefix: ContextVar[int] = ContextVar("chemclaw_request_prefix_tokens", default=0)


def begin_context_watch() -> object:
    """Start a turn's context record; returns a token for `end_context_watch`."""
    return _turn.set(TurnContext())


def end_context_watch(token: object) -> None:
    """Clear the turn's context record at teardown."""
    _turn.reset(token)  # type: ignore[arg-type]


def current_context() -> TurnContext | None:
    """The turn in flight's context record, or `None` off the request path."""
    return _turn.get()


def prefix_tokens() -> int:
    """Estimated tokens of system message plus tool schemas for the model call in flight."""
    return _prefix.get()


class _Calibration:
    """The process's running estimate of `billed / estimated`, and the lock around it.

    An EWMA because the traffic mix moves. The 1.0 seed is divided back out (standard bias
    correction), so the estimate is the weighted average of real samples from the first one; a seed
    would hold early calls at the unsafe uncalibrated end. One sample suffices because the ratio can
    only tighten. Per process because the ratio is a property of the endpoint's tokenizer.
    """

    #: Weight of a new sample: roughly a twenty-call memory, so one outlier moves the budget by a
    #: few percent.
    _ALPHA = 0.1
    #: A single call's ratio outside this range is a measurement fault, not a tokenizer difference,
    #: and is dropped.
    _SANE = (0.2, 8.0)

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ratio = 1.0
        self._calls = 0

    def note(self, estimated: int, billed: int) -> None:
        """Fold one model call's `billed / estimated` into the running ratio."""
        if estimated <= 0 or billed <= 0:
            return
        sample = billed / estimated
        if not self._SANE[0] <= sample <= self._SANE[1]:
            return
        with self._lock:
            self._calls += 1
            self._ratio = (1 - self._ALPHA) * self._ratio + self._ALPHA * sample

    def ratio(self) -> float:
        """The factor to divide a billed-token budget by, clamped so it can only tighten.

        The seed's `(1 - _ALPHA) ** calls` weight is divided back out before the clamp; `calls >= 1`
        is guaranteed by the sample floor, and a negative numerator clamps to 1.0.
        """
        if not settings.agent_context_calibration_enabled:
            return 1.0
        with self._lock:
            calls, ratio = self._calls, self._ratio
        if calls < settings.agent_context_calibration_min_calls:
            return 1.0
        seed = (1.0 - self._ALPHA) ** calls
        observed = (ratio - seed) / (1.0 - seed)
        return min(max(observed, 1.0), settings.agent_context_calibration_max_factor)

    def reset(self) -> None:
        """Forget every sample — for tests, which must not inherit another test's traffic."""
        with self._lock:
            self._ratio = 1.0
            self._calls = 0


_CALIBRATION = _Calibration()


def note_model_call(estimated: int, billed: int) -> None:
    """Record that a request this system estimated at `estimated` tokens was billed `billed`.

    Args:
        estimated: This system's estimate of the whole request (prefix and thread), matching what
            `input_tokens` counts.
        billed: The provider's `usage_metadata["input_tokens"]` for that call.
    """
    _CALIBRATION.note(estimated, billed)


def estimator_ratio() -> float:
    """How many billed tokens one estimated token has been costing (1.0 until calibrated)."""
    return _CALIBRATION.ratio()


def reset_calibration() -> None:
    """Drop every observation. Tests only — a process learns this once and keeps it."""
    _CALIBRATION.reset()


METRICS.bind_gauge("chemclaw_context_estimator_ratio", estimator_ratio)


#: `(configured, prefix, window)` triples whose floored trigger was already reported: a static
#: configuration fault, said once per triple. Capped so arbitrary budgets cannot grow it unbounded.
_REPORTED_FLOORS: set[tuple[int, int, int]] = set()
_FLOOR_LOCK = threading.Lock()
_MAX_REPORTED_FLOORS = 64


def _note_floored_trigger(configured: int, prefix: int, window: int, ratio: float) -> None:
    """Say, once, that a configured budget left the thread nothing and the trigger floored at 1.

    A trigger of 1 means "reduce on every model call". It is reachable when a configured (or
    calibrated) budget falls below the prefix, so it must be said rather than arrive silently. A
    WARNING rather than a metric: the condition is static per process, and the line names the
    numbers and the setting to move.

    Args:
        configured: The configured budget in billed tokens, as passed to `effective_trigger`.
        prefix: This request's measured prefix in estimated tokens, 0 off the request path.
        window: `llm_context_window_tokens`, 0 when the deployment declares none.
        ratio: `estimator_ratio()` at the moment of the floor; named in the message but not in the
            dedup key, since a float key would mint an entry per call.
    """
    key = (configured, prefix, window)
    with _FLOOR_LOCK:
        if key in _REPORTED_FLOORS or len(_REPORTED_FLOORS) >= _MAX_REPORTED_FLOORS:
            return
        _REPORTED_FLOORS.add(key)
    log_event(
        logger,
        "context.trigger_floored",
        "a configured context budget of %d billed tokens leaves nothing for the thread once it is "
        "converted at the measured %.3f billed tokens per estimated one and this request's "
        "%d-token prefix (with a declared window of %d) is charged against it, so the trigger "
        "floors at 1 and the edit reading it reduces on every model call: raise the setting above "
        "the prefix, or shrink the bound tool surface",
        configured,
        ratio,
        prefix,
        window,
        level=logging.WARNING,
        configured_tokens=configured,
        prefix_tokens=prefix,
        window_tokens=window,
        estimator_ratio=ratio,
    )


#: `(prefix, basis)` pairs already reported as over the basis — once each, for `_REPORTED_FLOORS`'
#: reason, and under the same lock and cap.
_REPORTED_EXCESS: set[tuple[int, int]] = set()


def _note_prefix_over_basis(prefix: int, basis: int) -> None:
    """Say, once per surface, that this request's prefix is larger than the budgets were sized for.

    The excess is paid in spend rather than thread, so nothing the chemist sees degrades; the
    operator who bound the extra bundles is told, with the remedies.

    Args:
        prefix: This request's measured prefix in estimated tokens.
        basis: `agent_context_prefix_basis`.
    """
    key = (prefix, basis)
    with _FLOOR_LOCK:
        if key in _REPORTED_EXCESS or len(_REPORTED_EXCESS) >= _MAX_REPORTED_FLOORS:
            return
        _REPORTED_EXCESS.add(key)
    log_event(
        logger,
        "context.prefix_over_basis",
        "this request's prefix is %d estimated tokens, %d over the %d the context budgets were "
        "derived for (agent_context_prefix_basis), so the excess is charged to spend rather than "
        "to the thread and a request may bill past agent_context_token_budget by about that much: "
        "bind fewer bundles, declare llm_context_window_tokens if the model's window is the "
        "limit, or raise the basis and both budgets together",
        prefix,
        prefix - basis,
        basis,
        level=logging.WARNING,
        prefix_tokens=prefix,
        basis_tokens=basis,
        excess_tokens=prefix - basis,
    )


def reset_floor_reports() -> None:
    """Forget which floors and excesses have been reported. Tests only — each is said once."""
    with _FLOOR_LOCK:
        _REPORTED_FLOORS.clear()
        _REPORTED_EXCESS.clear()


def effective_trigger(configured: int) -> int:
    """The trigger to compare an *estimated* token count against, given a budget in billed tokens.

    `configured / ratio - min(prefix, basis)`, further capped by
    `(window - llm_max_tokens) / ratio - prefix` when a window is declared, floored at 1.

    - The budget is converted once, whole, and the prefix subtracted in estimator units afterwards,
      so `billed = ratio * (prefix + thread) <= budget` holds with no assumption about how prefix
      and thread tokenize differently. The residual lag of a running ratio is bounded in
      `tests/test_compaction._TRACKING_SLACK`.
    - The prefix is charged only up to `agent_context_prefix_basis`, the surface the defaults were
      derived from; a deployment binding more bundles pays the excess in spend, not thread, and is
      told once. A declared window charges the whole prefix, since it is the provider's hard limit.
    - The window never raises what a deployment asked to spend; the smaller trigger wins.

    This can only tighten: the ratio is clamped at 1.0 from below.

    Args:
        configured: The configured budget, in billed tokens (`agent_context_token_budget` or
            `agent_tool_result_clear_trigger`).

    Returns:
        The estimated-token count above which the edit should act. Never below 1 (raising inside a
        middleware would fail the turn); a floor is reported once by `_note_floored_trigger`.
    """
    window = settings.llm_context_window_tokens
    prefix = prefix_tokens()
    ratio = estimator_ratio()
    basis = settings.agent_context_prefix_basis
    if prefix > basis:
        _note_prefix_over_basis(prefix, basis)
    # The budget is charged the prefix up to the basis it was derived for; what a deployment binds
    # beyond that is paid in spend, not thread (see the paragraph above).
    trigger = int(configured / ratio) - min(prefix, basis)
    if window:
        # The window is the provider's limit rather than a spend choice, so it is charged the
        # *whole* prefix: no basis buys room a model does not have.
        trigger = min(trigger, int((window - settings.llm_max_tokens) / ratio) - prefix)
    if trigger < 1:
        _note_floored_trigger(configured, prefix, window, ratio)
        return 1
    return trigger


#: The process's BPE encoding: empty before the one attempt, `[None]` when exact counting is
#: unavailable, `[encoding]` when it is. A list so "not resolved yet" differs from "resolved to
#: nothing", and a failed resolution is not retried every call.
_ENCODING: list[Any] = []
_ENCODING_LOCK = threading.Lock()


def _baked_cache_dir() -> Path | None:
    """The merge-table cache `tiktoken` would read, when a deployment has actually baked one.

    A precondition, not an optimisation: on a cache miss `tiktoken` fetches over HTTPS, and
    production is air-gapped, so without a baked cache it must not be called at all. The resolution
    transcribes `tiktoken.load.read_file_cached` exactly — presence of `TIKTOKEN_CACHE_DIR`, with an
    empty value meaning "caching disabled" — because any looser reading would approve a load that
    then dials. A populated cache lacking the configured encoding still attempts one fetch per
    process (see `_resolve_encoding`).

    Returns:
        The directory, or `None` when there is nothing baked there to read — including the
        deliberate "no cache" that an empty value is.
    """
    if "TIKTOKEN_CACHE_DIR" in os.environ:
        named = os.environ["TIKTOKEN_CACHE_DIR"]
    elif "DATA_GYM_CACHE_DIR" in os.environ:
        named = os.environ["DATA_GYM_CACHE_DIR"]
    else:
        named = str(Path(tempfile.gettempdir()) / "data-gym-cache")
    if named == "":
        return None
    try:
        directory = Path(named)
        return directory if any(directory.iterdir()) else None
    except OSError:
        return None


def _resolve_encoding() -> Any | None:
    """Load the configured encoding once, or say why the budget is counting with chars/4 instead.

    Never raises and never fails a turn: every caller can fall back to the estimator. The name is
    configured because the gateway does not say what model it fronts; against a non-OpenAI vendor
    the count is an approximation and `_Calibration` absorbs the residual.

    `tiktoken`'s fetch has no timeout, so a dropping network could hang rather than raise. That is
    bounded by `_baked_cache_dir` (no baked table, no call) and by `core/netguard.py`, which refuses
    the DNS lookup; it is not bounded with the egress guard disabled or behind a loopback or
    allowlisted proxy.
    """
    name = settings.llm_token_encoding
    if not name:
        return None
    cache = _baked_cache_dir()
    if cache is None:
        log_event(
            logger,
            "context.token_encoding_unavailable",
            "no tiktoken merge-table cache is baked here, so the context budget counts with its "
            "chars/4 estimator and the measured calibration ratio; bake one and set "
            "TIKTOKEN_CACHE_DIR to count the request prefix exactly",
            level=logging.INFO,
            encoding=name,
        )
        return None
    try:
        import tiktoken

        encoding = tiktoken.get_encoding(name)
    except Exception:
        degraded(
            logger,
            "context_budget",
            "could not load the '%s' token encoding from the cache at %s; the context budget "
            "counts with its chars/4 estimator instead",
            name,
            cache,
            level=logging.WARNING,
        )
        return None
    log_event(
        logger,
        "context.token_encoding_loaded",
        "counting this request's prefix with the %s encoding",
        name,
        level=logging.INFO,
        encoding=name,
    )
    return encoding


def _encoding() -> Any | None:
    """The process's encoding, resolved at most once. `None` means "count with the estimator".

    The whole resolution runs under the lock so concurrent cold turns share one load. Releasing it
    would not reduce anyone's wait during a stalled fetch, only multiply the hung sockets; a
    timed-out future would stop the waiting but not the socket or thread.
    """
    with _ENCODING_LOCK:
        if not _ENCODING:
            _ENCODING.append(_resolve_encoding())
        return _ENCODING[0]


def reset_encoding() -> None:
    """Forget the resolved encoding. Tests only — a process resolves this once and keeps it."""
    with _ENCODING_LOCK:
        _ENCODING.clear()


def _text_tokens(content: Any, encoding: Any) -> int | None:
    """Exact tokens of a message's content, or `None` when this content cannot be counted that way.

    Handles block lists as well as strings, because the system message arrives as text blocks.
    Anything else (e.g. an image) returns `None` so the whole message falls back to the estimator.
    Uses `encode_ordinary`, since `encode` raises on special-token spellings in tool output.
    """
    if isinstance(content, str):
        return len(encoding.encode_ordinary(content))
    if not isinstance(content, list):
        return None
    total = 0
    for block in content:
        if isinstance(block, str):
            total += len(encoding.encode_ordinary(block))
            continue
        if not isinstance(block, dict) or block.get("type") != "text":
            return None
        text = block.get("text")
        if not isinstance(text, str):
            return None
        total += len(encoding.encode_ordinary(text))
    return total


def _message_tokens(message: BaseMessage) -> int:
    """One message's size: exactly where the encoding can be had, by chars/4 where it cannot.

    Content is encoded; the per-message envelope (role, name, tool calls, call id) stays the
    estimator's, since no local tokenizer knows the provider's framing. This keeps counts addable,
    which `_clear_older_tool_results` relies on.
    """
    encoding = _encoding()
    content = None if encoding is None else _text_tokens(message.content, encoding)
    if content is None:
        return int(count_tokens_approximately([message]))
    envelope = int(count_tokens_approximately([message.model_copy(update={"content": ""})]))
    return envelope + content


def _tool_name(tool: Any) -> str:
    """The name a provider sees for one bound tool, whether it is an object or a dict schema."""
    name = getattr(tool, "name", None)
    if name:
        return str(name)
    if isinstance(tool, dict):
        function = tool.get("function")
        if isinstance(function, dict) and function.get("name"):
            return str(function["name"])
        if tool.get("name"):
            return str(tool["name"])
    return repr(tool)


def estimate_tool_schemas(tools: Sequence[Any]) -> int:
    """Tokens of the tool schemas as a provider is sent them, counted with the best unit available.

    Uses `convert_to_openai_tool`, as LangChain does when binding, because reading attributes off a
    decorated callable misses the schema. Memoised per surface by `_schema_tokens`. Never raises: an
    underivable schema contributes nothing, which only makes the bound more generous.
    """
    from langchain_core.utils.function_calling import convert_to_openai_tool

    total = 0
    for tool in tools:
        try:
            total += _message_tokens(_as_message(convert_to_openai_tool(tool)))
        except Exception:
            continue
    return int(total)


#: Tool-schema token totals for the life of the process, keyed by the names bound to the call.
#:
#: Process-scoped because `MeasureRequestPrefix` is built per turn, so an instance memo would redo
#: the sweep every turn on the event loop. Entries are bounded by profiles times reachable bundles.
#: Keying by name assumes names determine schemas: a bundle redeployed with changed schemas under
#: unchanged names is measured stale until restart. `tests/test_context_budget.py` asserts one sweep
#: per surface.
_SCHEMA_TOKENS: dict[tuple[str, ...], int] = {}


def _schema_tokens(tools: Sequence[Any]) -> int:
    """`estimate_tool_schemas` over this surface, computed once per process per distinct surface.

    Unlocked: a race computes the same number twice, never a wrong one.
    """
    key = tuple(_tool_name(tool) for tool in tools)
    total = _SCHEMA_TOKENS.get(key)
    if total is None:
        total = _SCHEMA_TOKENS[key] = estimate_tool_schemas(tools)
    return total


def _as_message(schema: Any) -> BaseMessage:
    """One tool schema as a message, so the same counter measures it as measures the thread."""
    import json

    from langchain_core.messages import HumanMessage

    return HumanMessage(json.dumps(schema, default=str))


class MeasureRequestPrefix(AgentMiddleware[Any, Any, Any]):
    """Publish the size of this model call's prefix, so the edits below can subtract it.

    Outermost of the compaction group, because a `ContextEdit` sees only the message list while the
    system message and schemas are on the request. The schema half is memoised per process
    (`_SCHEMA_TOKENS`); the instructions half is counted every call. Implements both sync and async
    hooks, since `create_agent` puts it in both chains.
    """

    def _measure(self, request: ModelRequest[Any]) -> int:
        """This request's prefix, the schema half memoised per bound surface.

        The instructions are encoded exactly because chars/4 over-estimates them, and the clamped
        calibration never refunds an over-estimate; counting exactly is what returns that thread
        budget.
        """
        system = request.system_message
        instructions = _message_tokens(system) if system is not None else 0
        return _schema_tokens(request.tools) + instructions

    def _measured(self, request: ModelRequest[Any]) -> int | None:
        """This request's prefix, or `None` — with the degradation recorded — if it cannot be had.

        Separate from `_publish` because the async path measures in a worker thread, where
        `ContextVar.set` would not reach the turn's context.
        """
        try:
            return self._measure(request)
        except Exception:
            degraded(
                logger,
                "context_budget",
                "could not measure this model call's prefix; the budget ignores it",
            )
            return None

    def _publish(self, tokens: int | None) -> object | None:
        """Set the ambient prefix, or leave it alone when there was nothing to measure."""
        return None if tokens is None else _prefix.set(tokens)

    def wrap_model_call(
        self, request: ModelRequest[Any], handler: Callable[[ModelRequest[Any]], Any]
    ) -> Any:
        """Publish the prefix, run the call, and put the ambient back (sync path)."""
        token = self._publish(self._measured(request))
        try:
            return handler(request)
        finally:
            if token is not None:
                _prefix.reset(token)  # type: ignore[arg-type]

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[Any]],
    ) -> Any:
        """The path a turn actually takes — measured off the loop, published on it.

        A memo miss is pure CPU over every schema, and the front door has one event loop serving
        every stream and probe. A thread does not remove the work (GIL) but keeps the loop
        schedulable, which `tests/test_context_budget.py` asserts.
        """
        token = self._publish(await asyncio.to_thread(self._measured, request))
        try:
            return await handler(request)
        finally:
            if token is not None:
                _prefix.reset(token)  # type: ignore[arg-type]
