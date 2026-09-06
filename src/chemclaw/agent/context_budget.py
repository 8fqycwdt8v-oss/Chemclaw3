"""What a model call is really about to cost, and what the context policy may therefore spend.

`agent/compaction.py` bounds the *thread* against a number in `settings`. Three things were wrong
with that, and all three are properties of the arithmetic rather than of the edits:

**The unit was not the unit anybody meant.** Both triggers count with
`count_tokens_approximately` — chars/4 — and that estimator is content dependent in one direction.
Re-measured 2026-09-06 against real BPE encodings (`o200k_base`, the family a current gateway
serves) over payloads this system actually handles: the observed `default` prefix bills **0.985**
estimated tokens' worth per estimated token, a knowledge-graph note **0.996** — and results called
from `Chemclaw3-mcp`'s own `chem` server bill **1.24x** (`describe_sites`, 8.4 kB) to **1.67x**
(`enumerate_bond_cleavages`), **1.34x** over all of them concatenated. So chars/4 is within 2% on
prose and schemas and undercounts structured chemistry by a quarter to two thirds, which is
precisely the payload class the two triggers exist to reclaim. The figures this paragraph used to
carry — "0.45x", i.e. 2.2x billed per estimated — do not reproduce against the fleet's real
results, and `agent_context_calibration_max_factor` was justified by them; `core/config/agent.py`
carries what that ceiling now rests on.

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
bound tool schema are part of the request the provider bills and are not in the thread — a figure
no line here may hold, because it moves on any tool-schema merge in this repository *or* in
`Chemclaw3-mcp`, and this paragraph shipped carrying 43,175, the connector-less number the
2026-09-05 sweep corrected in five other places and missed here. What a shipped `default` turn
sends is what `tests/test_context_floor.py` measures, bounded by its ceiling — so a budget that
does not charge it bounds nothing the provider sees. Charging it only under a declared window,
which is what D-2026-08-28 shipped, meant charging it against nothing in every real deployment:
measured end to end, a thread the policy cut to its 90,030-token budget left as a 137,301-token
request. So `effective_trigger` subtracts it unconditionally, and `agent_context_token_budget` is
therefore a bound on **request** spend rather than on thread spend. A `ContextEdit` cannot see the
prefix: upstream's protocol hands `apply` a message list and a counter and nothing else. A
middleware can, so `MeasureRequestPrefix` publishes it and the edits read it — the same shape
`agent/turn_flags.py` and `agent/repeat_guard.py` already use for a fact that belongs to the call
in flight.

**What that costs, stated rather than discovered.** At a fixed configured budget every deployment's
thread allowance falls by the prefix, and a configured budget *below* the prefix leaves a trigger of
1, which means "reduce on every model call". `agent_tool_result_clear_trigger` has twice shipped in
exactly that state — once against a prefix measured with no connector bound — and that is why
`_note_floored_trigger` exists — the floor has to be said rather than arrive silently. The same
commit that charged the prefix raised the default above the prefix, and it is **derived** from
`tests/test_context_floor.PREFIX_BOUND` — that file's ratchet ceiling plus the allowance for the
bundles it cannot serve — plus 30,000 of thread, so it moves whenever either half does, and no
figure for it is written down here. The shipped configuration is therefore not floored;
`_note_floored_trigger` serves the deployment that lowers it, which is the case it was written
for. The live numbers are whatever `tests/test_compaction.py` and
`tests/test_context_floor.py` measure, not these, for the reason
`D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit` gives.

**And the turn's own context record lives here** rather than on the repeat guard's watch, which is
where `peak_reclaimed` sat because compaction had nowhere else to put it. Two per-turn ambients
with two subjects, each started by the callers that bracket a turn (`api/runner.py`,
`durable/template_activities.py`), is what lets `turn_costs` say whether a turn was compacted at
all — the join between the policy and the bill it exists to reduce, which no series could make.
"""

import asyncio
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

    **The average is of the samples, not of the samples and a fiction.** An EWMA seeded at 1.0 is
    `(1-a)^n` parts seed after `n` samples, so with `a = 0.1` a process's first twenty calls answer
    with a number that is mostly the initial guess — and that guess is the *unsafe* end, because
    the clamp below means believing a sample can only tighten. Dividing the seed back out
    (`(ewma - (1-a)^n) / (1 - (1-a)^n)`, the standard bias correction) makes this the properly
    weighted average of what has actually been observed, and nothing else: one sample answers with
    that sample, twenty answer with their weighted mean. Measured 2026-09-06 on a compiled graph
    with the connector surface bound, a dense connector-JSON thread and the shipped budget, the
    number of model calls that went out over a 128k model's input ceiling: **20** with the seed
    left in and the sample floor at 20, **19** with the floor alone lowered to 1, **1** with both —
    and that one is the process's very first call, before any sample exists, which no policy can
    bound.

    **The sample floor stays as a setting and its default is 1**, because there is nothing left for
    it to protect against. The fear it was written for — "one unusual first call must not move a
    budget" — is the fear of a *loose* budget, and `ratio` is clamped at 1.0 from below, so a
    single sample can only make the policy compact earlier. Its actual effect was to hold every
    pod's first twenty model calls at the uncalibrated end; measured on that arm, those calls billed
    164,989 against 123,904 of permitted input. `_ALPHA` is the real sample floor: one sample of a
    thread that happened to be one geometry moves the answer by its own weight and then decays.

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
        """The factor to divide a billed-token budget by, clamped so it can only tighten.

        The seed is divided back out before the clamp — see the class docstring: `_ratio` carries
        `(1 - _ALPHA) ** calls` of the 1.0 it started at, and returning that blend is what kept a
        fresh process budgeting at the uncalibrated end for twenty calls. `calls >= 1` is
        guaranteed by the floor above (`agent_context_calibration_min_calls` is `ge=1`), so the
        divisor is never 0; a run of samples below the seed can make the numerator negative, and
        the clamp turns that into 1.0, which is the same answer an over-estimate always gets.
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


def _note_floored_trigger(configured: int, prefix: int, window: int, ratio: float) -> None:
    """Say, once, that a configured budget left the thread nothing and the trigger floored at 1.

    **A trigger of 1 is not a budget, it is "reduce on every model call".** The edit reading it
    compares a thread against 1 estimated token, which every non-empty thread exceeds, so
    `ClearOlderToolResultsEdit` replaces every reclaimable tool result on every call and the
    conversation window cuts back to its newest group. That is a defensible thing for a deployment
    to have asked for and an indefensible thing for it to arrive at silently — which is precisely
    what happens when the prefix is charged unconditionally and a configured budget is smaller than
    the prefix. That was the shipped default's own state twice — the second time because the prefix
    it was derived from had been measured with no connector bound — until it was re-derived from
    `tests/test_context_floor.PREFIX_BOUND` plus 30,000 of thread, which is where a live figure
    comes from and why none is written here. It now fires for a deployment that configures a budget
    under its own prefix, a corner that stays reachable because the prefix grows with every bound
    tool — and, since the conversion moved onto the whole budget, for one whose *calibrated* budget
    falls under it, which is why the line names the ratio too.

    WARNING rather than a counter, and the choice is about what an operator can do with it. The
    condition is static — the same for every turn of a process, decided by two settings and the
    bound tool surface — so a rate carries no information a single line does not, and this
    repository's own `tests/test_deploy_chart.py` obliges every declared series to earn a panel or
    an alert. The line names both numbers and the setting to move, which is the whole remedy.

    Args:
        configured: The configured budget in billed tokens, as passed to `effective_trigger`.
        prefix: This request's measured prefix in estimated tokens, 0 off the request path.
        window: `llm_context_window_tokens`, 0 when the deployment declares none.
        ratio: `estimator_ratio()` at the moment of the floor. It is named in the line but is
            deliberately **not** in the key: since the conversion moved onto the whole budget it is
            a third way to reach the floor (a budget above the prefix still floors once
            `budget / ratio` falls under it), and a float in the key would mint a distinct triple
            per call, which is the growth `_MAX_REPORTED_FLOORS` exists to refuse. Keyed on the
            static three, a calibration-driven floor is said once and the number that caused it is
            in the message.
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

    **How big the prefix is is not written here, and the last two attempts to write it here were
    both wrong.** The first carried 43,175 — a graph compiled with no connector bound. The second
    replaced it with "eight connector bundles and 75,695 estimated tokens", which was a bundle
    count nobody re-derived (`connector_specs(get_profile("default"))` returns **seven**) beside a
    token figure that goes stale on a sibling repository's merge schedule. The ratchet is the place
    a live figure comes from (`tests/test_context_floor.py`, whose ceiling is the only figure worth
    quoting) and `core/config/agent.py` carries the derivation; a number repeated here is a third
    copy that can go stale on its own, which is exactly what both of them did.

    **The order of those last two is the whole arithmetic, and it was wrong.** This function used
    to compute `(budget - prefix) / ratio`: subtract an *estimated* prefix from a *billed* budget,
    then convert only the remainder. Its own docstring defended that as "deliberate rather than
    sloppy", on the ground that the prefix is the content where the two units agree — and that
    ground is the part that was never measured on this prefix. It converts to
    `prefix * 1 + thread * ratio <= budget`, which is true only if the prefix bills at exactly one
    billed token per estimated token. `note_model_call` measures `ratio` over the **whole**
    request, so spending it on the remainder alone credits the prefix's over-estimate against the
    thread's under-estimate and then lets the thread grow into the credit. Measured 2026-09-06 on a
    compiled graph with the connector surface bound, shipped defaults, a 128k window declared and a
    thread of real `chem.enumerate_bond_cleavages` results, billed by `o200k_base` over the whole
    113-tool surface a shipped turn binds: the converged request billed **133,803** against a
    119,000 budget and the 123,904 such a model accepts, with `chemclaw_context_unreducible_total`
    flat on all 40 turns — because the prefix is 63% of that request and bills at 0.985, the thread
    bills at 1.675, and the blend the policy divided by was 1.208.

    So the budget is converted **once, whole**, and the prefix is subtracted in the estimator's own
    unit afterwards. Then `billed = ratio * (prefix + thread) <= ratio * (budget / ratio) = budget`
    identically, with no assumption about how the two populations differ — the ratio is applied to
    exactly the quantity it was measured over, which is the property the split could not have. On
    the same arm: **118,589 billed**, inside the budget by 411. It costs that thread ~9,000
    estimated tokens, which is the trade the budget exists to make.

    **It is a near-identity rather than an identity, and the residue is measured rather than
    assumed away.** The conversion uses the ratio as it stands *before* the call it is about, so a
    request is budgeted against a slightly stale average of a sequence the trigger itself steers.
    Driven to a fixed point that lag shows as a two-state cycle a fraction of a percent over the
    budget — `tests/test_compaction._TRACKING_SLACK` measures it and holds the bound, and the
    criterion that matters (does the request fit the model) clears by thousands of tokens.

    **This can only tighten**, so it needs no second safety argument: `budget/ratio - prefix` is
    below `(budget - prefix)/ratio` for every `ratio >= 1`, and the ratio is clamped at 1.0 from
    below. At `ratio == 1.0` — an uncalibrated process — the two are the same number, which is why
    every unit test of this function passed either way and only an end-to-end drive can see it.

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
    ratio = estimator_ratio()
    trigger = int(budget / ratio) - prefix
    if trigger < 1:
        _note_floored_trigger(configured, prefix, window, ratio)
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


#: Tool-schema token totals for the life of the process, keyed by the names bound to the call.
#:
#: **Process-scoped, because the middleware that reads it is per turn.** `MeasureRequestPrefix` is
#: constructed inside the compaction group of a graph that `langgraph_agent` compiles per turn, so
#: an instance memo is cold at every turn's first model call and the whole `convert_to_openai_tool`
#: sweep ran again on the front door's one event loop.
#:
#: **How long that is, is not stated here any more.** This comment carried "~100 ms for the
#: process's first surface, ~20 ms for every turn after it, 210 ms for 12 concurrent turns", and
#: the sweep re-measured on the same 92-tool `default` surface on 2026-09-06 takes **16 ms** cold
#: and **0.01 ms** warm. Neither figure is *provably* wrong, because the conditions the originals
#: were taken under — loop occupancy rather than wall time, a 1 ms heartbeat, twelve turns racing
#: — are not recoverable from the text, and that is the defect: a duration nothing re-runs is a
#: claim about a machine, not about this code. What is asserted, and is what the memo is for, is
#: the **count**: `tests/test_context_budget.py` drives many turns over one surface and requires
#: exactly one sweep, and drives a cold burst and requires the loop to stay schedulable through it.
#:
#: A process serves one profile set over one connector fleet, so the distinct surfaces it ever sees
#: are its profiles times the bundles that happen to be reachable — a bounded handful of entries,
#: not a cache that grows with traffic.
#:
#: **What the key assumes, stated because it now spans turns.** The bound names determine the
#: schemas. Within a turn that was already assumed; across turns it additionally means a bundle
#: redeployed with changed schemas under unchanged tool names is measured stale until this process
#: restarts. The thing that goes stale is an estimate used to *bound* a budget, and it is charged
#: against a sweep every model call of every turn otherwise paid for.
_SCHEMA_TOKENS: dict[tuple[str, ...], int] = {}


def _schema_tokens(tools: Sequence[Any]) -> int:
    """`estimate_tool_schemas` over this surface, computed once per process per distinct surface.

    Unlocked deliberately: two threads racing a surface neither has seen both compute and both
    store the same number, which costs one redundant sweep and cannot produce a wrong one.
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

    **Outermost of the compaction group**, because a `ContextEdit` runs inside
    `ContextEditingMiddleware` and reads only the message list — the system message and the tool
    schemas are on the request, which only a middleware holds. Publishing it into a contextvar is
    what lets an edit that cannot see the request nevertheless budget against the whole of it.

    **The schema half is memoised for the life of the process, not of the middleware.** A graph is
    compiled per turn (`langgraph_agent`) and this middleware is constructed with it, so an
    instance memo is cold at every turn's first model call and every turn re-ran the whole
    `convert_to_openai_tool` sweep — see `_SCHEMA_TOKENS` for what that measured and what keying it
    by name assumes. The instructions half is per request and is counted every call, which is free.

    Both hooks, for the reason `RecordContextCompaction` gives: `create_agent` puts a middleware
    declaring either hook into both chains, so an async-only middleware fails every synchronous
    `graph.invoke()`.
    """

    def _measure(self, request: ModelRequest[Any]) -> int:
        """This request's prefix in estimated tokens, the schema half memoised per bound surface."""
        system = request.system_message
        instructions = int(count_tokens_approximately([system])) if system is not None else 0
        return _schema_tokens(request.tools) + instructions

    def _measured(self, request: ModelRequest[Any]) -> int | None:
        """This request's prefix, or `None` — with the degradation recorded — if it cannot be had.

        Separate from `_publish` because the async path measures this half in a worker thread, and
        `ContextVar.set` there would set the variable in that thread's context rather than in the
        turn's.
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

        **Off the loop because a memo miss is pure CPU over every bound tool schema and this
        process has one loop.** The front door pins itself to one uvicorn worker, so that loop
        carries every SSE stream, both kubelet probes and the submission side of every token
        validation, and the sweep ran to completion on it.

        **What this line buys and what it does not, stated as a shape rather than as milliseconds**
        — see `_SCHEMA_TOKENS` for why the figures that used to be here are gone. A burst that
        misses together (a pod's first turns, or a bundle whose tools have come back under new
        names) still does the work: `asyncio.to_thread` is a halving rather than an elimination,
        because a thread buys no parallelism against the GIL and CPU-bound threads starve the loop
        thread of it for much of the burst. What is asserted is that the loop stays schedulable
        through such a burst (`tests/test_context_budget.py`), not a duration. `api/runner.py`
        makes the same trade for the graph build.
        """
        token = self._publish(await asyncio.to_thread(self._measured, request))
        try:
            return await handler(request)
        finally:
            if token is not None:
                _prefix.reset(token)  # type: ignore[arg-type]
