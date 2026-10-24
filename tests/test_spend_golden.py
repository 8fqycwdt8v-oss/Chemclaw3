"""The spend decisions, tabulated: one input table, one recorded answer per row.

Spend is enforced in several places that must keep agreeing: the runaway-iteration cap and the
billed-token cap (`before_model` hooks), the metering that feeds the second, the wrap-up call at the
cap, the per-session and per-user turn budget, the durable ledger's statements and the usage
arithmetic. Each decision is driven through its public entry point over a table of inputs and
compared with a recorded answer, so moving code cannot move a decision.

The recorded answers are the contract; changing one is a behaviour change and needs its own record.
"""

import asyncio
import hashlib
import logging
from dataclasses import astuple
from types import SimpleNamespace
from typing import Any, cast

import pytest
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain.agents.middleware.types import ExtendedModelResponse
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from chemclaw.spend import ledger as cost_module
from chemclaw.spend import limits as budget_store
from chemclaw.spend.guard import (
    SPEND_WRAP_UP_NOTE,
    WRAP_UP_NOTE,
    AnswerAtTheCap,
    MeterTurnSpend,
    begin_loop_watch,
    begin_spend_watch,
    end_loop_watch,
    end_spend_watch,
    enforce_loop_cap,
    enforce_spend_cap,
    loop_capped,
    loop_hit_cap,
    spend_capped,
    spend_hit_cap,
    turn_billed_tokens,
)
from chemclaw.spend.ledger import (
    _COLUMNS,
    _UPSERT,
    InFlightPrompts,
    NullTurnCostSink,
    TurnCost,
    TurnUsage,
    _prompt_estimate,
    error_result_usage,
    graph_usage_tokens,
    llm_result_usage,
    metered_turn_tokens,
    record_turn_cost,
    reset_turn_usage,
    set_turn_usage,
)
from chemclaw.spend.limits import (
    BudgetExceeded,
    BudgetTracker,
    ThreadTooLong,
    check_thread_size,
    refused_metric,
)


def _digest(text: str) -> str:
    """A short, stable fingerprint of a string whose exact bytes are the contract."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _thread(assistant_messages: int) -> list[Any]:
    """A turn's thread: the chemist's message and the assistant messages already produced."""
    return [HumanMessage("question"), *[AIMessage(f"step {n}") for n in range(assistant_messages)]]


def _request(state: dict[str, Any], *, tools: bool = False) -> ModelRequest[Any]:
    """A model request carrying `state`, as a graph hands it to `wrap_model_call`."""
    return ModelRequest(
        model=None,  # type: ignore[arg-type]
        system_prompt=None,
        messages=[HumanMessage("question")],
        tool_choice=None,
        tools=cast(Any, ["a tool"] if tools else []),
        response_format=None,
        state=cast(Any, state),
        runtime=None,
    )


# --- the runaway-iteration cap -------------------------------------------------------------------

# (channel, assistant messages already in the thread, wrap-up already granted)
_LOOP_CASES: list[tuple[int, int, bool]] = [
    (0, 0, False),
    (2, 0, False),
    (3, 0, False),
    (3, 0, True),
    (0, 5, False),
    (0, 5, True),
    (5, 0, True),
    (2, 3, False),
]

_LOOP_GOLDEN: list[Any] = [
    ({"model_calls": 1}, False, False),
    ({"model_calls": 3}, False, False),
    ({"loop_capped": True, "loop_wrap_up": True, "model_calls": 4}, False, True),
    ({"jump_to": "end", "loop_capped": True}, False, True),
    ({"loop_capped": True, "loop_wrap_up": True, "model_calls": 6}, False, True),
    ({"jump_to": "end", "loop_capped": True}, False, True),
    ({"jump_to": "end", "loop_capped": True}, False, True),
    ({"loop_capped": True, "loop_wrap_up": True, "model_calls": 4}, False, True),
]

# Each row is one graph run (one watch) of calls given as (channel, thread length, wrap-up).
_FAN_OUT_RUNS: dict[str, list[tuple[int, int, bool]]] = {
    "four branches off the same base": [(0, 0, False)] * 4 + [(1, 0, True)],
    "one branch counting up": [(0, 0, False), (1, 1, False), (2, 2, False), (3, 3, False)],
    "a resumed thread beside a fresh branch": [(0, 2, False), (0, 0, False), (0, 0, False)],
}

_FAN_OUT_GOLDEN: dict[str, Any] = {
    "four branches off the same base": (
        [
            {"model_calls": 1},
            {"model_calls": 1},
            {"model_calls": 1},
            {"loop_capped": True, "loop_wrap_up": True, "model_calls": 1},
            {"jump_to": "end", "loop_capped": True},
        ],
        True,
    ),
    "one branch counting up": (
        [
            {"model_calls": 1},
            {"model_calls": 2},
            {"model_calls": 3},
            {"loop_capped": True, "loop_wrap_up": True, "model_calls": 4},
        ],
        True,
    ),
    "a resumed thread beside a fresh branch": (
        [
            {"model_calls": 3},
            {"loop_capped": True, "loop_wrap_up": True, "model_calls": 1},
            {"loop_capped": True, "loop_wrap_up": True, "model_calls": 1},
        ],
        True,
    ),
}


def _loop_decisions(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Every loop-cap row, with no watch and then under one shared watch per run."""
    monkeypatch.setattr(settings, "harness_max_loop_iterations", 3)
    alone = []
    for channel, made, wrap_up in _LOOP_CASES:
        state = {"model_calls": channel, "messages": _thread(made), "loop_wrap_up": wrap_up}
        decision = enforce_loop_cap.before_model(cast(Any, state), cast(Any, None))
        alone.append((decision, loop_hit_cap(), loop_capped(decision or {})))
    runs: dict[str, Any] = {}
    for name, calls in _FAN_OUT_RUNS.items():
        token = begin_loop_watch()
        try:
            seen = []
            for channel, made, wrap_up in calls:
                state = {"model_calls": channel, "messages": _thread(made)}
                state["loop_wrap_up"] = wrap_up
                seen.append(enforce_loop_cap.before_model(cast(Any, state), cast(Any, None)))
            runs[name] = (seen, loop_hit_cap())
        finally:
            end_loop_watch(token)
    return {"alone": alone, "runs": runs}


def test_the_loop_cap_decides_each_row_as_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The iteration cap authorises, wraps up or ends exactly as recorded, row by row."""
    decided = _loop_decisions(monkeypatch)
    assert decided["alone"] == _LOOP_GOLDEN
    assert decided["runs"] == _FAN_OUT_GOLDEN


# --- the billed-token cap ------------------------------------------------------------------------

# (budget, the channel's billed_tokens, what the turn's ledger holds or None for no ledger)
_SPEND_CASES: list[tuple[int, int, int | None]] = [
    (0, 9_999, None),
    (1_000, 0, None),
    (1_000, 999, None),
    (1_000, 1_000, None),
    (1_000, 10, 1_500),
    (1_000, 1_500, 10),
    (1_000, 10, 999),
    (1_000, 10, 1_000),
]

_SPEND_GOLDEN: list[Any] = [
    (None, False, 0, False),
    (None, False, 0, False),
    (None, False, 0, False),
    (None, False, 0, False),
    (None, False, 0, False),
    (None, False, 0, False),
    ({"spend_capped": True, "loop_wrap_up": True}, True, 1000, True),
    ({"spend_capped": True, "loop_wrap_up": True}, False, 0, True),
    ({"spend_capped": True, "loop_wrap_up": True}, True, 1500, True),
    ({"spend_capped": True, "loop_wrap_up": True}, False, 0, True),
    ({"spend_capped": True, "loop_wrap_up": True}, True, 1500, True),
    ({"spend_capped": True, "loop_wrap_up": True}, False, 0, True),
    (None, False, 0, False),
    (None, False, 0, False),
    ({"spend_capped": True, "loop_wrap_up": True}, True, 1000, True),
    ({"spend_capped": True, "loop_wrap_up": True}, False, 0, True),
]


def _spend_decisions(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Every spend-cap row, once under a watch and once with none."""
    traced = []
    for budget, channel, ledger in _SPEND_CASES:
        monkeypatch.setattr(settings, "agent_max_turn_billed_tokens", budget)
        for watching in (True, False):
            token = begin_spend_watch() if watching else None
            usage_token = set_turn_usage(TurnUsage(total=ledger)) if ledger is not None else None
            try:
                state: dict[str, Any] = {"billed_tokens": channel}
                decision = enforce_spend_cap.before_model(cast(Any, state), cast(Any, None))
                traced.append(
                    (decision, spend_hit_cap(), turn_billed_tokens(), spend_capped(decision or {}))
                )
            finally:
                if usage_token is not None:
                    reset_turn_usage(usage_token)
                if token is not None:
                    end_spend_watch(token)
    return traced


def test_the_spend_cap_decides_each_row_as_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The billed-token cap grants its wrap-up at the larger of its two readings, as recorded."""
    assert _spend_decisions(monkeypatch) == _SPEND_GOLDEN


# --- metering ------------------------------------------------------------------------------------

_METER_GOLDEN: list[Any] = [
    (True, {"billed_tokens": 1334}, 2468, True),
    (True, {"billed_tokens": 15}, 30, True),
    (False, None, 0, True),
    (False, None, 0, True),
    (True, {"billed_tokens": 53}, 6, True),
    (False, None, 0, True),
]


def _billed_response(usage: dict[str, Any] | None) -> ModelResponse[Any]:
    """A model response whose single message reports `usage` the way a provider does."""
    return ModelResponse(result=[AIMessage(content="reply", usage_metadata=cast(Any, usage))])


def _metering() -> list[Any]:
    """What the meter writes to the channel and the watch for each shape of response."""
    shapes: list[tuple[int, Any]] = [
        (100, _billed_response({"input_tokens": 600, "output_tokens": 634, "total_tokens": 1234})),
        (0, _billed_response({"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})),
        (7, _billed_response({"input_tokens": 10, "output_tokens": 5, "total_tokens": 0})),
        (7, _billed_response(None)),
        (
            50,
            AIMessage(
                content="bare",
                usage_metadata={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            ),
        ),
        (0, ModelResponse(result=[])),
    ]
    traced: list[Any] = []
    for prior, response in shapes:
        token = begin_spend_watch()
        try:
            out = MeterTurnSpend().wrap_model_call(
                _request({"billed_tokens": prior}), _sync_handler(response)
            )
            asynced = asyncio.run(
                MeterTurnSpend().awrap_model_call(
                    _request({"billed_tokens": prior}), _async_handler(response)
                )
            )
            wrapped = isinstance(out, ExtendedModelResponse)
            update = out.command.update if wrapped and out.command is not None else None
            traced.append((wrapped, update, turn_billed_tokens(), type(asynced) is type(out)))
        finally:
            end_spend_watch(token)
    return traced


def _sync_handler(response: Any) -> Any:
    """A handler that answers `response`, for the synchronous path."""

    def handler(_request: ModelRequest[Any]) -> Any:
        return response

    return handler


def _async_handler(response: Any) -> Any:
    """An async handler that answers `response`, for the path a turn actually takes."""

    async def handler(_request: ModelRequest[Any]) -> Any:
        return response

    return handler


def test_the_meter_books_each_response_as_recorded() -> None:
    """The channel gets the absolute total, the watch the call's own bill, as recorded."""
    assert _metering() == _METER_GOLDEN


# --- the wrap-up call ----------------------------------------------------------------------------

_WRAP_UP_GOLDEN: list[Any] = [
    (
        False,
        False,
        None,
        1,
        None,
        None,
        "partial answer",
        [{"name": "ls", "args": {}, "id": "c1", "type": "tool_call"}],
        {"tool_calls": [{"id": "c1"}], "kept": "yes"},
        "structured",
    ),
    (
        False,
        True,
        None,
        1,
        None,
        None,
        "partial answer",
        [{"name": "ls", "args": {}, "id": "c1", "type": "tool_call"}],
        {"tool_calls": [{"id": "c1"}], "kept": "yes"},
        "structured",
    ),
    (
        True,
        False,
        None,
        2,
        str(WRAP_UP_NOTE),
        True,
        "partial answer",
        [],
        {"kept": "yes"},
        "structured",
    ),
    (
        True,
        True,
        "none",
        2,
        str(WRAP_UP_NOTE),
        True,
        "partial answer",
        [],
        {"kept": "yes"},
        "structured",
    ),
]
_WRAP_UP_NOTE_DIGEST = "130acc01718ae1dc"
_SPEND_WRAP_UP_NOTE_DIGEST = "ff090ce6169517b4"


def _wrap_up() -> list[Any]:
    """The request the wrap-up call sends and the reply it keeps, for each state and tool set."""
    reply_with_tools = AIMessage(
        content="partial answer",
        tool_calls=[{"name": "ls", "args": {}, "id": "c1"}],
        additional_kwargs={"tool_calls": [{"id": "c1"}], "kept": "yes"},
    )
    traced: list[Any] = []
    for wrap_up in (False, True):
        for tools in (False, True):
            sent: list[ModelRequest[Any]] = []

            def handler(request: ModelRequest[Any], _sent: list[ModelRequest[Any]] = sent) -> Any:
                _sent.append(request)
                return ModelResponse(result=[reply_with_tools], structured_response="structured")

            out = AnswerAtTheCap().wrap_model_call(
                _request({"loop_wrap_up": wrap_up}, tools=tools), handler
            )
            request = sent[0]
            note = request.messages[-1]
            kept = out.result[0]
            traced.append(
                (
                    wrap_up,
                    tools,
                    request.tool_choice,
                    len(request.messages),
                    note.content if len(request.messages) > 1 else None,
                    bool(note.response_metadata) if len(request.messages) > 1 else None,
                    kept.content,
                    list(kept.tool_calls),
                    dict(kept.additional_kwargs),
                    out.structured_response,
                )
            )
    return traced


def test_the_wrap_up_rewrites_request_and_reply_as_recorded() -> None:
    """The wrap-up turns tools off, appends the note and drops any tool call, as recorded."""
    assert _wrap_up() == _WRAP_UP_GOLDEN
    assert _digest(str(WRAP_UP_NOTE)) == _WRAP_UP_NOTE_DIGEST, "the model reads this sentence"
    assert _digest(str(SPEND_WRAP_UP_NOTE)) == _SPEND_WRAP_UP_NOTE_DIGEST, (
        "the model reads this sentence"
    )


# --- the per-session and per-user turn budget ----------------------------------------------------

# One scripted run: ("check" | "record", session, user, tokens). Caps: session 3 turns / 1,000
# tokens, user 4 turns / 2,000 tokens, warning at 80%.
_BUDGET_SCRIPT: list[tuple[str, str, str | None, int]] = [
    ("check", "s1", "alice", 0),
    ("record", "s1", "alice", 500),
    ("check", "s1", "alice", 0),
    ("record", "s1", "alice", 350),
    ("check", "s1", "alice", 0),
    ("record", "s1", "alice", 100),
    ("check", "s1", "alice", 0),
    ("check", "s2", "alice", 0),
    ("record", "s2", "alice", 1_000),
    ("check", "s3", "alice", 0),
    ("check", "s3", "bob", 0),
    ("record", "s3", None, 5),
    ("check", "s3", None, 0),
    ("record", "s4", "bob", -50),
    ("check", "s4", "bob", 0),
]

_BUDGET_GOLDEN: dict[str, Any] = {
    "outcomes": [
        "check s1 alice -> admitted",
        "record s1 alice 500 -> None",
        "check s1 alice -> admitted",
        "record s1 alice 350 -> None",
        "check s1 alice -> admitted",
        "record s1 alice 100 -> None",
        "check s1 alice -> BudgetExceeded: session turn budget exhausted (3 turns)",
        "check s2 alice -> admitted",
        "record s2 alice 1000 -> None",
        "check s3 alice -> BudgetExceeded: user turn budget exhausted (4 turns)",
        "check s3 bob -> admitted",
        "record s3 None 5 -> None",
        "check s3 None -> admitted",
        "record s4 bob -50 -> None",
        "check s4 bob -> admitted",
    ],
    "warnings": [
        "session tokens budget 85% spent (850 of 1000) for s1 — the next turns will be "
        "refused at the cap",
        "user tokens budget 98% spent (1950 of 2000) for alice — the next turns will be "
        "refused at the cap",
    ],
    "warning_count": 2.0,
}


def _budget_run(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> dict[str, Any]:
    """Drive the scripted run on an in-process tracker; report each outcome and every warning."""
    monkeypatch.setattr(settings, "session_store", "memory")
    monkeypatch.setattr(settings, "budget_enabled", True)
    monkeypatch.setattr(settings, "budget_max_turns_per_session", 3)
    monkeypatch.setattr(settings, "budget_max_tokens_per_session", 1_000)
    monkeypatch.setattr(settings, "budget_max_turns_per_user", 4)
    monkeypatch.setattr(settings, "budget_max_tokens_per_user", 2_000)
    monkeypatch.setattr(settings, "budget_warn_fraction", 0.8)
    tracker = BudgetTracker()
    outcomes: list[str] = []
    before = METRICS.value("chemclaw_budget_warnings_total")
    with caplog.at_level(logging.WARNING):
        for action, session, user, tokens in _BUDGET_SCRIPT:
            if action == "record":
                task = tracker.record(session, user, tokens)
                outcomes.append(f"record {session} {user} {tokens} -> {task}")
                continue
            try:
                asyncio.run(tracker.check(session, user))
            except BudgetExceeded as refused:
                outcomes.append(f"check {session} {user} -> {type(refused).__name__}: {refused}")
            else:
                outcomes.append(f"check {session} {user} -> admitted")
    warnings = [r.getMessage() for r in caplog.records if "budget" in r.getMessage()]
    return {
        "outcomes": outcomes,
        "warnings": warnings,
        "warning_count": METRICS.value("chemclaw_budget_warnings_total") - before,
    }


def test_the_budget_admits_and_refuses_as_recorded(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Turn and token budgets refuse, warn and book exactly as recorded over one scripted run."""
    assert _budget_run(monkeypatch, caplog) == _BUDGET_GOLDEN


_EDGES_GOLDEN: list[Any] = [
    "admitted",
    "session token budget exhausted (100 tokens)",
    (3599.0, "user turn budget exhausted (2 turns)"),
    (3600.0, "admitted"),
    (3601.0, "admitted"),
]


def _budget_edges(monkeypatch: pytest.MonkeyPatch, tracker_cls: Any = BudgetTracker) -> list[Any]:
    """Where a negative booking stops counting and where a user window rolls, on a stepped clock."""
    monkeypatch.setattr(settings, "session_store", "memory")
    monkeypatch.setattr(settings, "budget_enabled", True)
    monkeypatch.setattr(settings, "budget_warn_fraction", 0.0)
    monkeypatch.setattr(settings, "budget_max_turns_per_session", 0)
    monkeypatch.setattr(settings, "budget_max_tokens_per_session", 100)
    monkeypatch.setattr(settings, "budget_max_turns_per_user", 2)
    monkeypatch.setattr(settings, "budget_max_tokens_per_user", 0)
    monkeypatch.setattr(settings, "budget_window_hours", 1.0)
    now = [1_000.0]
    monkeypatch.setattr("time.monotonic", lambda: now[0])

    def verdict(tracker: Any, session: str, user: str | None) -> str:
        try:
            asyncio.run(tracker.check(session, user))
        except Exception as refused:  # the refusal's class differs per tracker module
            return str(refused)
        return "admitted"

    traced: list[Any] = []
    tracker = tracker_cls()
    for tokens in (60, -50):
        tracker.record("clamp", None, tokens)
    traced.append(verdict(tracker, "clamp", None))
    tracker.record("clamp", None, 40)
    traced.append(verdict(tracker, "clamp", None))
    for elapsed in (3_599.0, 3_600.0, 3_601.0):
        now[0] = 1_000.0
        tracker = tracker_cls()
        tracker.record("s", "w", 1)
        # The counter stamped itself with the real clock; anchor it to the stepped one.
        tracker._users.get("w").started = now[0]
        now[0] = 1_000.0 + elapsed
        tracker.record("s", "w", 1)
        traced.append((elapsed, verdict(tracker, "other", "w")))
    return traced


def test_a_negative_booking_never_lowers_a_count_and_a_window_rolls_at_its_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A negative token count books as zero; a user window is spent exactly at its length."""
    assert _budget_edges(monkeypatch) == _EDGES_GOLDEN


def test_a_disabled_budget_books_and_refuses_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """With `budget_enabled` off a check never raises and a record returns no task."""
    monkeypatch.setattr(settings, "budget_enabled", False)
    monkeypatch.setattr(settings, "budget_max_turns_per_session", 1)
    tracker = BudgetTracker()
    assert tracker.record("s", "u", 10) is None
    assert tracker.record("s", "u", 10) is None
    asyncio.run(tracker.check("s", "u"))


# --- the thread-size ceiling ---------------------------------------------------------------------

_THREAD_GOLDEN: list[Any] = [
    "admitted",
    (
        "ThreadTooLong",
        (
            "This conversation has reached its size limit (2.0 MiB stored, against 2.0 MiB). "
            "Start a new session to continue — this one's transcript stays readable."
        ),
        "chemclaw_turns_refused_thread_size_total",
    ),
    (
        "ThreadTooLong",
        (
            "This conversation has reached its size limit (3.5 MiB stored, against 2.0 MiB). "
            "Start a new session to continue — this one's transcript stays readable."
        ),
        "chemclaw_turns_refused_thread_size_total",
    ),
    "admitted",
    "admitted",
    ("unbounded", "chemclaw_turns_refused_budget_total", "BudgetExceeded"),
]


def _thread_size(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """The refusal text and the metric it is counted on, at, below and unreadable."""
    monkeypatch.setattr(settings, "session_max_thread_bytes", 2 * 1024**2)
    traced: list[Any] = []
    for stored in (1024**2, 2 * 1024**2, 3 * 1024**2 + 512 * 1024, RuntimeError("down"), None):

        async def read(_session: str, _stored: Any = stored) -> int:
            if isinstance(_stored, Exception):
                raise _stored
            return int(_stored or 0)

        monkeypatch.setattr("chemclaw.agent.checkpointer.stored_thread_bytes", read)
        try:
            asyncio.run(check_thread_size("s1"))
            traced.append("admitted")
        except BudgetExceeded as refused:
            traced.append((type(refused).__name__, str(refused), refused_metric(refused)))
    monkeypatch.setattr(settings, "session_max_thread_bytes", 0)
    asyncio.run(check_thread_size("s1"))
    traced.append(
        ("unbounded", refused_metric(BudgetExceeded("x")), ThreadTooLong.__mro__[1].__name__)
    )
    return traced


def test_the_thread_ceiling_refuses_as_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A conversation at its size ceiling is refused with the recorded sentence and counter."""
    assert _thread_size(monkeypatch) == _THREAD_GOLDEN


# --- the durable windows and the ledger's statements ---------------------------------------------

_WINDOW_GOLDEN: list[Any] = [
    ((0, 0, None, 0.0), False),
    ((3, 400, 1000.0, 7200.0), False),
    ((3, 400, 1000.0, 86399.0), False),
    ((0, 0, 1000.0, 86400.0), True),
    ((0, 0, 1000.0, 324000.0), True),
]
_STATEMENT_GOLDEN: dict[str, Any] = {
    "book": "ca8fa44acda51876",
    "usage": "765de6c29f4a496b",
    "upsert": "b8f45377871a75c9",
    "columns": [
        "turn_id",
        "correlation_id",
        "session_id",
        "actor",
        "profile",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "estimated_tokens",
        "duration_seconds",
        "completed",
        "outcome",
        "error_code",
        "model",
        "tool_calls",
        "tool_failures",
        "tool_refusals",
        "jobs_started",
        "ttft_seconds",
        "compacted",
        "context_unreducible",
        "retrieval_calls",
        "capture_calls",
        "answer_confidence",
        "review_required",
        "notes_cited",
        "skills_loaded",
    ],
}


def _windows(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """How a stored row reads as a window, expired or not, and the budget's `Window` fields."""
    monkeypatch.setattr(settings, "budget_window_hours", 24)
    hour = 3600.0
    rows: list[Any] = [
        None,
        (3, 400, 1_000.0, 2 * hour),
        (3, 400, 1_000.0, 24 * hour - 1),
        (3, 400, 1_000.0, 24 * hour),
        (3, 400, 1_000.0, 90 * hour),
    ]
    traced = []
    for row in rows:
        window = budget_store._from_row(row)
        traced.append((tuple(window), window.expired))
    return traced


def test_a_stored_row_reads_as_the_recorded_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """An expired window reads zero counts and a missing row reads no window at all."""
    assert _windows(monkeypatch) == _WINDOW_GOLDEN


def test_the_ledger_statements_are_the_recorded_ones() -> None:
    """The SQL both durable ledgers send, and the columns the turn ledger writes, are unchanged."""
    assert {
        "book": _digest(budget_store._BOOK),
        "usage": _digest(budget_store._USAGE),
        "upsert": _digest(_UPSERT),
        "columns": list(_COLUMNS),
    } == _STATEMENT_GOLDEN


# --- usage arithmetic ----------------------------------------------------------------------------

_USAGE_GOLDEN: list[Any] = [
    (0, 0, 0, 0, 0, 0),
    (0, 0, 0, 0, 0, 1),
    (100, 20, 0, 0, 120, 0),
    (100, 20, 0, 0, 120, 0),
    (100, 20, 0, 0, 0, 1),
    (60, 20, 30, 10, 120, 0),
    (70, 20, 30, 0, 120, 0),
    (5, 1, 0, 0, 6, 0),
    (11, 22, 33, 44, 110, 3),
    (3, 4, 0, 0, 7, 0),
    (4000, 500, 1000, 0, 5500, 0),
    (0, 0, 0, 0, 0, 0),
    (0, 0, 0, 0, 0, 0),
]


def _usage_rows() -> list[Any]:
    """`graph_usage_tokens` over each shape of `usage_metadata`, then the summing helpers."""
    shapes: list[Any] = [
        None,
        {},
        {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        {"input_tokens": 100, "output_tokens": 20},
        {"input_tokens": 100, "output_tokens": 20, "total_tokens": 0},
        {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "input_token_details": {"cache_read": 30, "cache_creation": 10},
        },
        {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "input_token_details": {"priority_cache_read": 25, "cache_read": 5},
        },
        {"input_tokens": 5, "output_tokens": 1, "total_tokens": 6, "input_token_details": 3},
    ]
    rows: list[Any] = []
    for shape in shapes:
        usage = graph_usage_tokens(SimpleNamespace(usage_metadata=shape))
        rows.append(
            tuple(
                getattr(usage, f)
                for f in ("input", "output", "cache_read", "cache_write", "total", "unreadable")
            )
        )
    a = TurnUsage(input=1, output=2, cache_read=3, cache_write=4, total=10, unreadable=1)
    a.add(TurnUsage(input=10, output=20, cache_read=30, cache_write=40, total=100, unreadable=2))
    generations: list[Any] = [
        [
            ChatGeneration(
                message=AIMessage(
                    "a", usage_metadata={"input_tokens": 3, "output_tokens": 4, "total_tokens": 7}
                )
            )
        ],
        [ChatGeneration(message=AIMessage("b"))],
    ]
    summed = llm_result_usage(LLMResult(generations=generations))
    served = error_result_usage(
        LLMResult(
            generations=[
                [
                    ChatGeneration(
                        message=AIMessage(
                            content="",
                            response_metadata={
                                "body": {
                                    "usage": {
                                        "prompt_tokens": 5000,
                                        "completion_tokens": 500,
                                        "total_tokens": 5500,
                                        "prompt_tokens_details": {"cached_tokens": 1000},
                                    }
                                }
                            },
                        )
                    )
                ]
            ]
        )
    )
    rows += [
        astuple(a),
        astuple(summed),
        astuple(served),
        astuple(error_result_usage(None)),
        astuple(error_result_usage(LLMResult(generations=[]))),
    ]
    return rows


def test_usage_is_split_as_recorded() -> None:
    """Input, output and both cache dimensions are read off a message exactly as recorded."""
    assert _usage_rows() == _USAGE_GOLDEN


_AMBIENT_GOLDEN: list[Any] = [0, 321, 0, 5104, 5000, 0, [15312, 7656, 0]]


def _ambient(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """The ambient ledger's reading and the in-flight prompt estimate."""
    traced: list[Any] = [metered_turn_tokens()]
    ledger = TurnUsage()
    token = set_turn_usage(ledger)
    try:
        ledger.add(TurnUsage(total=321))
        traced.append(metered_turn_tokens())
    finally:
        reset_turn_usage(token)
    traced.append(metered_turn_tokens())

    monkeypatch.setattr("chemclaw.spend.ledger.prefix_tokens", lambda: 5_000)
    monkeypatch.setattr("chemclaw.spend.ledger.estimator_ratio", lambda: 1.5)
    prompt = [[HumanMessage("x" * 400)]]
    traced += [_prompt_estimate(prompt), _prompt_estimate(None), _prompt_estimate(7)]

    in_flight = InFlightPrompts()

    async def run() -> list[int]:
        seen = []
        await in_flight.on_chat_model_start({}, prompt, run_id="a")
        await in_flight.on_chat_model_start({}, prompt, run_id="b")
        seen.append(in_flight.unbilled_tokens)
        await in_flight.on_llm_end(LLMResult(generations=[]), run_id="a")
        seen.append(in_flight.unbilled_tokens)
        await in_flight.on_llm_error(RuntimeError("x"), run_id="b")
        seen.append(in_flight.unbilled_tokens)
        return seen

    traced.append(asyncio.run(run()))
    return traced


def test_the_ambient_ledger_and_in_flight_estimate_read_as_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ambient ledger and the abandoned-call estimate read as recorded."""
    assert _ambient(monkeypatch) == _AMBIENT_GOLDEN


# --- booking a turn's cost -----------------------------------------------------------------------

_COST_GOLDEN: list[Any] = [
    True,
    None,
    None,
    None,
    [
        {
            "correlation_id": "c-1",
            "session_id": "",
            "actor": "",
            "profile": "default",
            "input_tokens": 5,
            "output_tokens": 7,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "estimated_tokens": 0,
            "duration_seconds": 0.0,
            "completed": True,
            "outcome": "answered",
            "error_code": "",
            "model": "",
            "tool_calls": None,
            "tool_failures": None,
            "tool_refusals": None,
            "jobs_started": None,
            "ttft_seconds": None,
            "compacted": False,
            "context_unreducible": False,
            "retrieval_calls": 0,
            "capture_calls": 0,
            "answer_confidence": None,
            "review_required": False,
            "notes_cited": 0,
            "skills_loaded": [],
            "recorded_at": None,
        }
    ],
]


def _booking(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """What `record_turn_cost` hands a sink, and what it does when there is none or it fails."""
    cost = TurnCost(correlation_id="c-1", input_tokens=5, output_tokens=7, outcome="answered")
    traced: list[Any] = []
    monkeypatch.setattr(settings, "session_store", "memory")
    traced.append(isinstance(cost_module.default_turn_cost_sink(), NullTurnCostSink))
    traced.append(asyncio.run(_book_without_loop(cost)))

    landed: list[TurnCost] = []

    class Landing:
        async def record(self, cost: TurnCost) -> None:
            landed.append(cost)

    class Failing:
        async def record(self, cost: TurnCost) -> None:
            raise RuntimeError("ledger down")

    for sink in (Landing(), Failing()):
        monkeypatch.setattr(cost_module, "default_turn_cost_sink", lambda s=sink: s)

        async def book() -> Any:
            task = record_turn_cost(cost)
            assert task is not None
            return await task

        traced.append(asyncio.run(book()))
    traced.append([c.model_dump(exclude={"turn_id"}) for c in landed])
    return traced


async def _book_without_loop(cost: TurnCost) -> Any:
    """`record_turn_cost` with the null sink: nothing to wait for."""
    return record_turn_cost(cost)


def test_a_turns_cost_is_booked_as_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The booking is a task for a real sink, nothing for the null one, and never raises."""
    assert _booking(monkeypatch) == _COST_GOLDEN
