"""No metric labelled by `tool` accepts a name the model invented — derived from the registry.

`/metrics` is unauthenticated by design (a Prometheus scrape carries no identity), and a tool name
in a `tool_call` is the *model's* string: `ToolNode` invokes the whole `wrap_tool_call` chain for a
name the graph does not hold, so an injected document that makes the model emit "a tool call named
<secret>" turns a verbatim label into an exfiltration channel — and the per-counter series cap then
blinds the metric permanently once the invented names fill it. `agent/audit.py::metric_tool_name` is
this tree's one answer to that, and `core/metrics.py` documents every `tool` label as "bounded by
the registered tool surface, which is configuration ... rather than anything a caller can name".

**Three of the five were clamped and two were not, each with its own test saying the label was
safe.** Measured on this branch's parent: three identical calls to a 141-character hallucinated name
rendered `chemclaw_repeated_tool_calls_total{tool="totally_made_up_tool_ZZZ…"} 2` verbatim, and
`chemclaw_tool_results_truncated_total` booked `request.tool_call["name"]` unclamped as well (not
independently reachable, because an unregistered call's error result is far under the ceiling — a
coincidence, which is not the bound the declaration claims).

So this file asks the question **once, over the registry** rather than once per metric. Its two
directions:

- Nothing a model authors reaches the exposition, whichever of these metrics fires.
- Every `tool`-labelled metric the registry declares was actually driven here. That is what makes
  the first direction mean something a year from now: a sixth such metric added with no drive fails
  this test rather than quietly sitting outside it, which is exactly how the two above came to have
  a hole and a docstring denying it.
"""

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from langchain.agents.middleware import ModelRequest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from chemclaw.agent.audit import UNKNOWN_TOOL, AuditEvent, make_audit_middleware
from chemclaw.agent.model_calls import RepairInvalidToolCalls
from chemclaw.agent.repeat_guard import (
    RepeatedCallRefusal,
    begin_call_watch,
    end_call_watch,
    refuse_repeated_calls,
)
from chemclaw.agent.tool_result_size import bound_tool_results
from chemclaw.core.config import settings
from chemclaw.core.metrics import _COUNTER_LABELS, _HISTOGRAM_LABELS, METRICS
from tests.middleware import run_middleware, tool_request

# Long, quoted and punctuated: the label has to survive being refused, not merely being short.
HALLUCINATED = "totally_made_up_tool_'; DROP TABLE audit_events; --" + "Z" * 200


class _Sink:
    """An `AuditSink` that keeps what the middleware decided to write."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> None:
        """Keep the event."""
        self.events.append(event)


@tool
def predict_pka(smiles: str) -> str:
    """A registered tool, so the clamp has a surface to resolve against."""
    return smiles


def _drive_audit(name: str) -> None:
    """One tool call through the audit middleware — `tool_calls_total`, `tool_duration_seconds`."""
    middleware = make_audit_middleware(correlation_id="cid-1", actor="alice@corp", sink=_Sink())

    async def _handler(_request: Any) -> Any:
        return "ok"

    # `tool=None` is what `ToolNode` passes for a name the graph does not hold.
    asyncio.run(run_middleware(middleware, tool_request(name, {"q": "x"}), _handler))


def _drive_repeat(name: str) -> None:
    """Past `max_identical_tool_calls` identical calls — `repeated_tool_calls_total`."""

    async def _handler(_request: Any) -> Any:
        return "ok"

    async def _loop() -> None:
        for index in range(settings.max_identical_tool_calls + 2):
            request = tool_request(name, {"q": "x"}, call_id=f"call-{index}")
            try:
                await run_middleware(refuse_repeated_calls, request, _handler)
            except RepeatedCallRefusal:
                pass

    token = begin_call_watch()
    try:
        asyncio.run(_loop())
    finally:
        end_call_watch(token)


def _drive_truncation(name: str) -> None:
    """An oversized result through the size gate — `tool_results_truncated_total`."""
    oversized = "x" * (settings.agent_max_tool_result_chars + 1)

    async def _handler(_request: Any) -> Any:
        return ToolMessage(content=oversized, tool_call_id="call-1")

    asyncio.run(run_middleware(bound_tool_results, tool_request(name, {"q": "x"}), _handler))


def _drive_invalid(name: str) -> None:
    """A tool call whose arguments did not parse — `invalid_tool_calls_total`."""
    broken = AIMessage(
        content="",
        invalid_tool_calls=[
            {"name": name, "args": "{", "id": "call-1", "error": None, "type": "invalid_tool_call"}
        ],
    )

    async def _handler(_request: ModelRequest[Any]) -> Any:
        return ModelResponse(result=[broken])

    # Typed as the union upstream declares rather than as `list[HumanMessage]`: `list` is
    # invariant, so the narrower annotation is what `mypy --strict` rejects here.
    messages: list[AnyMessage] = [HumanMessage("x")]
    request: ModelRequest[Any] = ModelRequest(
        model=None,  # type: ignore[arg-type]
        system_prompt=None,
        messages=messages,
        tool_choice=None,
        # The tool list is what this metric's clamp resolves against, so it has to carry the one
        # registered tool even when the name under test is not in it.
        tools=[predict_pka],
        response_format=None,
        state=cast(Any, {"messages": messages}),
        runtime=None,
    )
    asyncio.run(RepairInvalidToolCalls().awrap_model_call(request, _handler))


def _drive_every_producer(name: str) -> None:
    """Fire every `tool`-labelled metric here, with `name` as the tool the model asked for."""
    _drive_audit(name)
    _drive_repeat(name)
    _drive_truncation(name)
    _drive_invalid(name)


def _tool_labelled_metrics() -> set[str]:
    """Every metric the registry declares with a `tool` label — counters and histograms alike."""
    declared = {**_COUNTER_LABELS, **_HISTOGRAM_LABELS}
    return {name for name, labels in declared.items() if "tool" in labels}


def _metrics_carrying(exposition: str, label_value: str) -> set[str]:
    """The metric names with at least one rendered series whose `tool` label is `label_value`.

    Read off the exposition rather than off the registry's internals, because the exposition is
    what a scrape sees and a histogram's series is spelled `<name>_bucket{...}` there.
    """
    marker = f'tool="{label_value}"'
    found: set[str] = set()
    for line in exposition.splitlines():
        if line.startswith("#") or marker not in line:
            continue
        head = line.split("{", 1)[0]
        for suffix in ("_bucket", "_sum", "_count"):
            head = head.removesuffix(suffix)
        found.add(head)
    return found


def test_no_tool_labelled_metric_accepts_a_name_the_model_invented() -> None:
    """The forward direction: a hallucinated name is one bucket, never one series per string."""
    _drive_every_producer(HALLUCINATED)

    exposition = METRICS.render()
    assert HALLUCINATED not in exposition, (
        "a model-authored tool name reached the unauthenticated /metrics surface"
    )
    assert "totally_made_up_tool_" not in exposition, exposition


def test_every_tool_labelled_metric_declared_is_actually_driven_here() -> None:
    """The backward direction, and the reason the test above keeps its meaning.

    A metric this file does not fire is a metric the assertion above cannot say anything about, and
    a green run would look identical either way. Derived from `_COUNTER_LABELS` and
    `_HISTOGRAM_LABELS` rather than from a list typed out here, so the next `tool`-labelled metric
    arrives with a hole this test names instead of one it silently tolerates.
    """
    _drive_every_producer(HALLUCINATED)

    clamped = _metrics_carrying(METRICS.render(), UNKNOWN_TOOL)
    missing = sorted(_tool_labelled_metrics() - clamped)
    assert not missing, (
        "declared with a `tool` label but never driven by this file, so nothing here proves the "
        f"label is clamped: {missing}. Add a producer to `_drive_every_producer`."
    )


def test_a_registered_tool_still_keeps_its_own_name() -> None:
    """The guard on the guard: clamping everything to one bucket would lose the whole distinction.

    Every assertion above is satisfied by a registry that labels *nothing*, which is why this is
    here. The name a request actually bound must survive — for the audit path that is the
    registered tool object, and for the unparseable-call path it is the request's tool list.
    """
    registered = SimpleNamespace(name="predict_pka", metadata={})

    async def _handler(_request: Any) -> Any:
        return "ok"

    asyncio.run(
        run_middleware(
            make_audit_middleware(correlation_id="c", actor="a", sink=_Sink()),
            tool_request("predict_pka", {"q": "x"}, tool=registered),
            _handler,
        )
    )
    _drive_invalid("predict_pka")

    kept = _metrics_carrying(METRICS.render(), "predict_pka")
    assert "chemclaw_tool_calls_total" in kept
    assert "chemclaw_tool_duration_seconds" in kept
    assert "chemclaw_invalid_tool_calls_total" in kept
