"""What a model call is really about to cost, and what the context policy may therefore spend.

`agent/compaction.py` bounds the *thread* against a number in `settings`. Three things were wrong
with that, and all three are properties of the arithmetic rather than of the edits:

**The unit was not the unit anybody meant.** Both triggers count with
`count_tokens_approximately` — chars/4 — and that estimator is content dependent in one direction.
Measured against a real BPE tokenizer over this repository's own payloads: the static prefix 1.04x,
tool schemas 1.00x, a knowledge-graph note 1.01x, an ELN export 0.80x — and a connector JSON result
**0.45x**, an xyz geometry 0.47x. So it is right about prose and schemas and roughly half of the
truth about structured chemistry, which is precisely the payload class the two triggers exist to
reclaim. Measured end to end: a thread the policy put at 100,077 tokens billed ~224,000.

No constant fixes that, because the error is a property of the content. What does is the number the
provider already returns: `usage_metadata["input_tokens"]` is the billed size of the request this
system just estimated, so the ratio between them is measurable — and it is measurable at exactly
one place, because `RecordContextCompaction` computes the estimate and then awaits the call that
reports the bill. `note_model_call` is that comparison, and `estimator_ratio` is what the triggers
are divided by.

**It only ever tightens.** `estimator_ratio` is clamped at 1.0 from below, so a mismeasurement can
make the policy compact earlier than it needed to; it can never make it believe a request is
smaller than it is. That asymmetry is deliberate: the failure being closed is a hard context-length
error at the provider, which costs the whole turn, and the price of the other direction is one
conversation group dropped early.

**Nothing knew what the model could hold.** There was no context-window number anywhere in the
tree — the ceiling was discovered from a `BadRequestError` after the request had been assembled,
sent and rejected. `llm_context_window_tokens` is that number, 0 when a deployment cannot state it,
and `effective_trigger` caps the budget at `window - output reservation` when it can.

**The prefix has to be measured per request, which is why there is a contextvar here, and it is
charged whether or not a window is declared.** The system message, the skills listing and every
bound tool schema are part of the request the provider bills and are not in the thread — 43,175
estimated tokens on the `default` profile, measured 2026-09-04 — so a budget that does not charge
them bounds nothing the provider sees. Charging it only under a declared window, which is what
D-2026-08-28 shipped, meant charging it against nothing in every real deployment: measured end to
end, a thread the policy cut to its 90,030-token budget left as a 137,301-token request. So
`effective_trigger` subtracts it unconditionally, and `agent_context_token_budget` is therefore a
bound on **request** spend rather than on thread spend. A `ContextEdit` cannot see the prefix:
upstream's protocol hands `apply` a message list and a counter and nothing else. A middleware can,
so `MeasureRequestPrefix` publishes it and the edits read it — the same shape `agent/turn_flags.py`
and `agent/repeat_guard.py` already use for a fact that belongs to the call in flight.

**What that costs, stated rather than discovered.** At a fixed configured budget every deployment's
thread allowance falls by the prefix, and a configured budget *below* the prefix leaves a trigger of
1, which means "reduce on every model call". `agent_tool_result_clear_trigger` shipped at 30,000
against a 43,175-token prefix, which put the default configuration in exactly that state and is why
`_note_floored_trigger` exists — the floor has to be said rather than arrive silently. The same
commit that charged the prefix raised the default to 73,500, so the shipped configuration is no
longer floored; `_note_floored_trigger` now serves the deployment that lowers it, which is the case
it was written for. The live numbers are whatever `tests/test_compaction.py` and
`tests/test_context_floor.py` measure, not these, for the reason
`D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit` gives.

**And the turn's own context record lives here** rather than on the repeat guard's watch, which is
where `peak_reclaimed` sat because compaction had nowhere else to put it. Two per-turn ambients
with two subjects, each started by the callers that bracket a turn (`api/runner.py`,
`durable/template_activities.py`), is what lets `turn_costs` say whether a turn was compacted at
all — the join between the policy and the bill it exists to reduce, which no series could make.
"""

import logging
import threading
from collections.abc import Awaitable, Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
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

    `peak_reclaimed` is the high-water reduction, and it is the reason the metrics are not
    incremented per model call: both edits are non-destructive, so the same standing reduction is
    re-derived on every call of a turn and a 30-step turn would report one compaction thirty times.

    The two booleans are what `turn_costs` records. They are separate because they are separate
    facts and a turn can carry both: an early model call reduced the thread, a later one was over a
    trigger and could not be reduced at all.
    """

    peak_reclaimed: float = 0.0
    compacted: bool = False
    unreducible: bool = False


_turn: ContextVar[TurnContext | None] = ContextVar("chemclaw_turn_context", default=None)
# The estimated size of the current model call's prefix — the system message plus every bound tool
# schema. 0 off the request path, which makes every rule below inert exactly where there is no
# request to bound.
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

    **An EWMA rather than a mean**, because the quantity being tracked genuinely moves: a
    deployment's traffic shifts between prose turns and evidence sweeps, and the ratio for those is
    1.0 against 2.2. A mean over the life of a process would keep answering with last week's mix.

    **A sample floor before it is believed.** One unusual first call must not move a budget, and a
    single sample of a turn that happened to be one geometry is exactly that. Below
    `agent_context_calibration_min_calls` the ratio reads 1.0, which is the uncalibrated behaviour
    this repository shipped.

    Per process rather than per session: it is a property of the *tokenizer*, which is a property
    of the endpoint, and a per-session estimate would spend every session's first turns learning
    what the process next door already knows.
    """

    #: How much of a new sample the average takes. 0.1 gives roughly a twenty-call memory, which is
    #: the same order as the sample floor — fast enough to follow a traffic shift within a session,
    #: slow enough that one outlier moves the budget by a few percent.
    _ALPHA = 0.1
    #: A single call's ratio outside this range is a measurement fault rather than a tokenizer
    #: difference — a provider reporting usage for a different request, a cached read counted
    #: differently — and it is dropped rather than smoothed in.
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
        """The factor to divide a billed-token budget by, clamped so it can only tighten."""
        if not settings.agent_context_calibration_enabled:
            return 1.0
        with self._lock:
            calls, ratio = self._calls, self._ratio
        if calls < settings.agent_context_calibration_min_calls:
            return 1.0
        return min(max(ratio, 1.0), settings.agent_context_calibration_max_factor)

    def reset(self) -> None:
        """Forget every sample — for tests, which must not inherit another test's traffic."""
        with self._lock:
            self._ratio = 1.0
            self._calls = 0


_CALIBRATION = _Calibration()


def note_model_call(estimated: int, billed: int) -> None:
    """Record that a request this system estimated at `estimated` tokens was billed `billed`.

    Args:
        estimated: This system's own estimate of the whole request — prefix and thread together,
            because `input_tokens` counts the whole request and half a comparison is not one.
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


#: `(configured, prefix, window)` triples whose trigger has already floored and been reported.
#: A floor is a *configuration* fault rather than an event: it is constant for a deployment's
#: settings and bound tool surface, so it is said once per distinct triple instead of on every
#: model call of every turn. Capped because a caller passing arbitrary budgets — a test sweep, a
#: future per-profile budget — would otherwise mint a triple per call and grow this without bound.
_REPORTED_FLOORS: set[tuple[int, int, int]] = set()
_FLOOR_LOCK = threading.Lock()
_MAX_REPORTED_FLOORS = 64


def _note_floored_trigger(configured: int, prefix: int, window: int) -> None:
    """Say, once, that a configured budget left the thread nothing and the trigger floored at 1.

    **A trigger of 1 is not a budget, it is "reduce on every model call".** The edit reading it
    compares a thread against 1 estimated token, which every non-empty thread exceeds, so
    `ClearOlderToolResultsEdit` replaces every reclaimable tool result on every call and the
    conversation window cuts back to its newest group. That is a defensible thing for a deployment
    to have asked for and an indefensible thing for it to arrive at silently — which is precisely
    what happens when the prefix is charged unconditionally and a configured budget is smaller than
    the prefix. That was the shipped default's own state — 30,000 against a `default` profile prefix
    measured at 43,175 on 2026-09-04 — until the default rose to 73,500 in the same commit; this now
    fires for a deployment that configures a budget under its own prefix, which is a corner that
    stays reachable because the prefix grows with every bound tool.

    WARNING rather than a counter, and the choice is about what an operator can do with it. The
    condition is static — the same for every turn of a process, decided by two settings and the
    bound tool surface — so a rate carries no information a single line does not, and this
    repository's own `tests/test_deploy_chart.py` obliges every declared series to earn a panel or
    an alert. The line names both numbers and the setting to move, which is the whole remedy.

    Args:
        configured: The configured budget in billed tokens, as passed to `effective_trigger`.
        prefix: This request's measured prefix in estimated tokens, 0 off the request path.
        window: `llm_context_window_tokens`, 0 when the deployment declares none.
    """
    key = (configured, prefix, window)
    with _FLOOR_LOCK:
        if key in _REPORTED_FLOORS or len(_REPORTED_FLOORS) >= _MAX_REPORTED_FLOORS:
            return
        _REPORTED_FLOORS.add(key)
    log_event(
        logger,
        "context.trigger_floored",
        "a configured context budget of %d billed tokens leaves nothing for the thread once this "
        "request's %d-token prefix (and a declared window of %d) is charged against it, so the "
        "trigger floors at 1 and the edit reading it reduces on every model call: raise the "
        "setting above the prefix, or shrink the bound tool surface",
        configured,
        prefix,
        window,
        level=logging.WARNING,
        configured_tokens=configured,
        prefix_tokens=prefix,
        window_tokens=window,
    )


def reset_floor_reports() -> None:
    """Forget which floors have been reported. Tests only — a process says each of these once."""
    with _FLOOR_LOCK:
        _REPORTED_FLOORS.clear()


def effective_trigger(configured: int) -> int:
    """The trigger to compare an *estimated* token count against, given a budget in billed tokens.

    Three corrections, and the middle one may be inert:

    - **The unit.** `configured` is what the deployment is willing to spend in billed tokens; the
      edits count in the estimator's unit; `estimator_ratio` is the measured conversion, and it is
      1.0 until the process has seen enough calls to say otherwise.
    - **The window.** When `llm_context_window_tokens` is declared, the thread may not have more
      room than the model has left after the output reservation. The smaller of the two budgets
      wins, so declaring a large window never *raises* what a deployment asked to spend.
    - **The prefix, and it is charged whether or not a window is declared.** The system message,
      the skills listing and every bound tool schema are part of the request and are not in the
      thread, so a budget that does not charge them is not a bound on anything the provider sees.

    **That last subtraction changes what `agent_context_token_budget` means, deliberately.** It was
    a bound on *thread* spend; it is now a bound on *request* spend, and the difference is the
    prefix. `D-2026-08-28-a-budget-in-the-wrong-unit-is-not-a-budget` charged it only `if window:`,
    and no deployment declares one, so in the shipped configuration the prefix was charged against
    nothing: measured end to end, a thread the policy cut to 90,030 estimated tokens went out as a
    137,301-token request with the overrun indicator flat. The reason this is the right subtraction
    rather than an extra one is that both numbers are in the same request: what the provider counts
    is `prefix + thread`, and only one of the two was ever budgeted.

    **How big the prefix is is not written here, because the figure this paragraph used to carry —
    43,175, "43% of the shipped 100,000" — was measured on a graph compiled with no connector
    bound.** A shipped `default` turn binds eight connector bundles and sends **75,695** estimated
    tokens, so the subtraction was two thirds of what it is, and both compaction defaults were
    derived from the short number. The ratchet is the place a live figure comes from
    (`tests/test_context_floor.py`) and `core/config/agent.py` carries the derivation; a number
    repeated here is a third copy that can go stale on its own.

    The prefix is counted in the estimator's unit and subtracted from a budget in the provider's.
    That is deliberate rather than sloppy: the prefix is instructions, a skills listing and tool
    schemas, and those are the content on which the two units agree — 1.04x measured over the whole
    default prefix. The thread is where they do not, and the thread is exactly what is left after
    the subtraction, so dividing by the ratio afterwards converts the half that needs converting.

    Args:
        configured: The configured budget, in billed tokens (`agent_context_token_budget` or
            `agent_tool_result_clear_trigger`).

    Returns:
        The estimated-token count above which the edit should act. Never below 1: an edit whose
        trigger reached 0 would fire on an empty thread, and raising instead would fail the turn
        from inside a middleware, which is the worse trade. A floor is reported once by
        `_note_floored_trigger` rather than returned silently, because with the prefix charged
        unconditionally the floor is reachable from a plain misconfiguration and not only from the
        window corner it used to need.
    """
    budget = float(configured)
    window = settings.llm_context_window_tokens
    if window:
        budget = min(budget, float(window - settings.llm_max_tokens))
    prefix = prefix_tokens()
    trigger = int((budget - float(prefix)) / estimator_ratio())
    if trigger < 1:
        _note_floored_trigger(configured, prefix, window)
        return 1
    return trigger


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
    """Estimated tokens of the tool schemas as a provider is sent them.

    Through `convert_to_openai_tool`, which is the function LangChain itself calls when binding
    tools to a model — the same choice `tests/test_context_floor.py` made and for the same reason:
    reading `.name`/`.description` off a plain decorated callable finds a repr, an empty string and
    `None`, and measures the whole surface at ~11 tokens per tool.

    Never raises. A tool whose schema cannot be derived contributes nothing rather than costing the
    turn, because this number exists to *bound* a budget and a missing summand only makes the bound
    more generous.
    """
    from langchain_core.utils.function_calling import convert_to_openai_tool

    total = 0
    for tool in tools:
        try:
            total += count_tokens_approximately([_as_message(convert_to_openai_tool(tool))])
        except Exception:
            continue
    return int(total)


def _as_message(schema: Any) -> BaseMessage:
    """One tool schema as a message, so the same counter measures it as measures the thread."""
    import json

    from langchain_core.messages import HumanMessage

    return HumanMessage(json.dumps(schema, default=str))


class MeasureRequestPrefix(AgentMiddleware[Any, Any, Any]):
    """Publish the size of this model call's prefix, so the edits below can subtract it.

    **Outermost of the compaction group**, because a `ContextEdit` runs inside
    `ContextEditingMiddleware` and reads only the message list — the system message and the tool
    schemas are on the request, which only a middleware holds. Publishing it into a contextvar is
    what lets an edit that cannot see the request nevertheless budget against the whole of it.

    **Memoised for the life of the middleware, which is the life of the turn.** A graph is compiled
    per turn (`langgraph_agent`), tools bind at construction, and `convert_to_openai_tool` over ~45
    tools is real work to repeat on every step of a 30-step turn. The memo is keyed by the tool
    names actually bound, so a build that swaps its surface recomputes rather than reporting the
    previous one.

    Both hooks, for the reason `RecordContextCompaction` gives: `create_agent` puts a middleware
    declaring either hook into both chains, so an async-only middleware fails every synchronous
    `graph.invoke()`.
    """

    def __init__(self) -> None:
        """Start with nothing measured; the first model call of the turn fills the memo."""
        super().__init__()
        self._key: tuple[str, ...] | None = None
        self._tokens = 0

    def _measure(self, request: ModelRequest[Any]) -> int:
        """This request's prefix in estimated tokens, computed once per bound tool surface."""
        key = tuple(_tool_name(tool) for tool in request.tools)
        if key != self._key:
            self._key = key
            self._tokens = estimate_tool_schemas(request.tools)
        system = request.system_message
        instructions = int(count_tokens_approximately([system])) if system is not None else 0
        return self._tokens + instructions

    def _publish(self, request: ModelRequest[Any]) -> object | None:
        """Set the ambient prefix, or leave it alone if it cannot be measured."""
        try:
            return _prefix.set(self._measure(request))
        except Exception:
            degraded(
                logger,
                "context_budget",
                "could not measure this model call's prefix; the budget ignores it",
            )
            return None

    def wrap_model_call(
        self, request: ModelRequest[Any], handler: Callable[[ModelRequest[Any]], Any]
    ) -> Any:
        """Publish the prefix, run the call, and put the ambient back (sync path)."""
        token = self._publish(request)
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
        """The path a turn actually takes."""
        token = self._publish(request)
        try:
            return await handler(request)
        finally:
            if token is not None:
                _prefix.reset(token)  # type: ignore[arg-type]
