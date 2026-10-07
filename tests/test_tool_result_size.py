"""One tool result cannot be larger than the budget it is inside.

Neither context edit can reclaim the newest tool results (`ClearToolUsesEdit` keeps them verbatim
and the conversation window never cuts past the newest group), so an oversized result must be
bounded on its way in.
"""

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.tools import StructuredTool

from chemclaw.agent.audit import NullAuditSink
from chemclaw.agent.context_budget import estimate_tool_schemas
from chemclaw.agent.framing import SYSTEM_SPEECH_MARK, defang
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.tool_framing import frame_connector_results
from chemclaw.agent.tool_result_size import (
    _brief_notice,
    _notice,
    bound_tool_results,
    bounded_content,
    bounded_for_batch,
)
from chemclaw.connectors.transport import SERVED_BY
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS


def test_a_result_inside_the_ceiling_is_untouched() -> None:
    """Identity, not a copy: most results are small and must cost nothing at all."""
    content = "a modest answer"

    bounded, removed = bounded_content(content, "find_notes", 60_000)

    assert bounded is content
    assert removed == 0


def test_both_ends_of_an_oversized_result_survive() -> None:
    """Head and tail both survive, because a procedure states its outcome at the end.

    A head-only cut would drop yield and purity, which then read as "not measured".
    """
    content = "HEAD" + ("x" * 100_000) + "TAIL"

    bounded, removed = bounded_content(content, "read_document", 1_000)

    assert bounded.startswith("HEAD")
    assert bounded.endswith("TAIL")
    # Inside the ceiling, notice included — see `test_a_cut_result_is_never_larger_than_its
    # _ceiling`. So more of the tool's own text is removed than the naive `total - limit`.
    assert len(bounded) <= 1_000
    assert removed > len(content) - 1_000


def test_the_cut_says_it_happened_and_says_who_said_so() -> None:
    """The cut says it happened and that this system said so.

    The notice names the tool, the arithmetic and the remedy (narrow the question), and marks itself
    as system text, so a shortened result is not read as what the tool returned.
    """
    bounded, _ = bounded_content("x" * 50_000, "find_calculations", 1_000)

    assert "written by the" in bounded and "not by the tool" in bounded
    assert "find_calculations" in bounded
    assert "50,000" in bounded, "the notice does not say how much the model is not seeing"


def test_a_block_list_keeps_its_blocks() -> None:
    """An MCP result is content blocks, and their ids are read as citations.

    Cutting positionally rather than by concatenating is what keeps a truncated multi-block result
    the same result: `agent/framing.py` and `kg.note.mentioned_ids` both read a block's other keys.
    """
    content = [
        {"type": "text", "text": "A" * 40_000, "id": "first"},
        {"type": "image", "data": "…"},
        {"type": "text", "text": "B" * 40_000, "id": "second"},
    ]

    bounded, removed = bounded_content(content, "search_patents", 2_000)

    assert sum(len(block["text"]) for block in bounded if "text" in block) <= 2_000
    assert removed > 78_000, "the notice is charged against the ceiling, so more text goes"
    assert bounded[0]["id"] == "first"
    assert bounded[0]["text"].startswith("A")
    assert {"type": "image", "data": "…"} in bounded, "a block with no text span was dropped"
    assert bounded[-1]["text"].endswith("B")


def test_the_cap_can_be_switched_off() -> None:
    """0 restores the unbounded behaviour, which is a decision rather than an accident."""
    content = "x" * 500_000

    bounded, removed = bounded_content(content, "read_document", 0)

    assert bounded is content and removed == 0


def test_switching_the_cap_off_reaches_the_share_arithmetic_as_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switching the cap off reaches the share arithmetic as off.

    `bounded_for_batch` must hand `bounded_content` 0 when `agent_max_tool_result_chars` is 0; the
    `max(ceiling // width, 1)` floor must not apply then. Identity rather than equality, because
    `bound_tool_results` reads "unchanged" to decide whether to copy the message.
    """
    monkeypatch.setattr(settings, "agent_max_tool_result_chars", 0)
    content = "A" * 5_000
    before = METRICS.value("chemclaw_tool_results_truncated_total")

    out = bounded_for_batch(cast(Any, _Request("read_document")), content)

    assert out is content, f"the ceiling was off and {5_000 - len(out)} characters went anyway"
    assert METRICS.value("chemclaw_tool_results_truncated_total") == before, (
        "a result was counted as truncated while the cap was off"
    )


def test_a_result_of_exactly_the_limit_is_not_cut() -> None:
    """A result of exactly the limit is not cut: `<=`, not `<`.

    At the ceiling there is nothing to reclaim, and cutting would replace it with a shorter result
    plus a notice.
    """
    at_limit = "A" * 1_000
    bounded, removed = bounded_content(at_limit, "read_document", 1_000)

    assert bounded is at_limit and removed == 0

    over_by_one, removed_over = bounded_content("A" * 1_001, "read_document", 1_000)

    assert removed_over > 0 and len(over_by_one) <= 1_000


def test_a_limit_of_exactly_the_notice_keeps_the_explanatory_form_and_no_text() -> None:
    """At `limit == widest` the explanatory notice fits and takes the whole share: `kept` is 0, not
    1.
    """
    total = 5_000
    widest = len(_notice("read_document", total, total))

    bounded, removed = bounded_content("A" * total, "read_document", widest)

    assert len(bounded) <= widest, "the bound returned more than the limit it was given"
    assert "removed from the middle" in bounded, "the brief form was used where the full one fits"
    assert removed == total, "a character of the tool's own text was kept inside the notice's share"


def test_the_head_and_tail_budgets_are_spent_down_across_every_block() -> None:
    """The head and tail budgets are spent down across every block.

    Three text blocks, because with two an accumulation cannot be told from a per-block reset, which
    would bound each block rather than the total. The same fixture checks the head and tail survive
    and that the notice lands on a block `_rebuilt` keeps, not the trailing image.
    """
    limit = 2_000
    image = {"type": "image", "source": {"data": "abc"}}
    content: list[Any] = ["A" * 40_000, "B" * 40_000, "C" * 40_000, image]

    bounded, removed = bounded_content(content, "sweep", limit)

    text = "".join(block for block in bounded if isinstance(block, str))
    assert len(text) <= limit, f"three blocks kept {len(text)} characters against a {limit} limit"
    assert text.startswith("A"), "the head of the first block went"
    assert text.endswith("C"), "the tail of the last block went"
    assert "removed from the middle" in text, "the cut landed somewhere the rebuild discards it"
    assert image in bounded, "a block with no text span was dropped"
    assert removed > 0


def test_a_string_block_carries_the_notice_when_the_first_block_cannot() -> None:
    """A bare string block carries the notice when the first block cannot.

    `_carrier` accepts `isinstance(block, str) or carries_text`; this reaches the `str` arm. Driven
    below the explanatory notice's length, where the head budget is 0 and the carrier decides where
    the sentence lands.
    """
    image = {"type": "image", "source": {"data": "abc"}}
    content: list[Any] = [image, "y" * 9_000]

    bounded, removed = bounded_content(content, "sweep", 50)

    assert removed == 9_000
    text = "".join(block for block in bounded if isinstance(block, str))
    assert "chars cut" in text, "the result was shortened and nothing in it says so"
    assert image in bounded


def test_an_oversized_result_is_bounded_on_its_way_to_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An oversized result is bounded on its way to the model, through the middleware.

    Every tool is bounded, in-process ones included, so the cap is not keyed on the `SERVED_BY`
    stamp.
    """
    monkeypatch.setattr(settings, "agent_max_tool_result_chars", 5_000)
    request = _Request("find_calculations")

    async def handler(_: Any) -> ToolMessage:
        return ToolMessage(content="y" * 200_000, tool_call_id="c1", name="find_calculations")

    before = METRICS.value("chemclaw_tool_results_truncated_total")
    # `_Request` carries the one attribute the middleware reads; `ToolCallRequest` is a
    # dataclass with a graph's worth of fields around it.
    result = asyncio.run(bound_tool_results.awrap_tool_call(cast(Any, request), handler))

    assert isinstance(result, ToolMessage)
    assert len(result.content) < 200_000
    # Exactly one: `frame_connector_results` re-bounds after escaping with `count=False`, so the
    # counter and the `tool_result.truncated` row describe the result once. A `> before` assertion
    # would pass with both passes counting.
    assert METRICS.value("chemclaw_tool_results_truncated_total") == before + 1.0, (
        "one cut must count once: the counter moved by "
        f"{METRICS.value('chemclaw_tool_results_truncated_total') - before}"
    )


class _Request:
    """The attributes `bound_tool_results` reads off a tool-call request.

    `tool` is `None`, as `ToolNode` passes for a name the graph does not hold, so the metric label
    clamps to `"unknown"`.
    """

    def __init__(self, name: str) -> None:
        """Name the tool this request is for; nothing else about it is read."""
        self.tool_call = {"name": name, "args": {}, "id": "c1"}
        self.tool = None
        self.state: dict[str, Any] = {}


def test_an_invented_tool_name_never_reaches_the_truncation_label() -> None:
    """An invented tool name never reaches the truncation label.

    The counter is on an unauthenticated `/metrics`. `ToolNode` dispatches an unregistered name
    through this chain and its error echoes the name back, so an unclamped label would mint a series
    per invented name.
    """
    request = _Request("EXFIL_" + "B" * 200)

    async def handler(_: Any) -> ToolMessage:
        return ToolMessage(content="y" * 200_000, tool_call_id="c1")

    asyncio.run(bound_tool_results.awrap_tool_call(cast(Any, request), handler))

    rendered = METRICS.render()
    assert "EXFIL_" not in rendered, "a model's string became a metric label"
    assert 'chemclaw_tool_results_truncated_total{tool="unknown"}' in rendered


def test_a_cut_result_is_never_larger_than_its_ceiling() -> None:
    """A cut result is never larger than its ceiling.

    The notice is charged against the ceiling, since the model reads it like any other span; an
    exact ceiling also makes a batch's share of it exact.
    """
    for total in (60_001, 61_000, 100_000):
        bounded, removed = bounded_content("A" * total, "read_document", 60_000)

        assert len(bounded) <= 60_000, f"{total} characters came back as {len(bounded)}"
        assert len(bounded) < total, "the bound grew the result it was bounding"
        assert removed > 0


def test_a_cut_is_never_silent_at_the_smallest_configurable_ceiling() -> None:
    """A cut is never silent at the smallest configurable ceiling.

    `agent_max_tool_result_chars` is `ge=0`, so 1 is valid; the head share then rounds to 0 and the
    notice must still be placed.
    """
    bounded, removed = bounded_content("A" * 1_000, "read_document", 1)

    assert removed == 1_000, "every character of the result was dropped"
    # The brief form: the explanatory sentence does not fit, so it keeps that something was removed,
    # how much, and that the system removed it, and drops the advice.
    assert "1,000 chars cut" in bounded
    # The mark, which is what the words "by the system" used to claim and could not prove.
    assert bounded.endswith(SYSTEM_SPEECH_MARK)


def test_a_result_smaller_than_the_notice_is_left_alone() -> None:
    """A result smaller than the notice is left alone.

    Cutting cannot make it smaller, so no cut is made and no notice is owed.
    """
    bounded, removed = bounded_content("AB", "read_document", 1)

    assert bounded == "AB" and removed == 0


#: What the fan-out model was sent on each call, and what was bound to it. Module level rather than
#: instance state for the reason `tests/test_compaction.py` gives: a `BaseChatModel` is a pydantic
#: model, so an annotated class attribute would become a *field* with a mutable default.
_SENT: list[list[Any]] = []
_BOUND: list[Any] = []


class _FanOutModel(GenericFakeChatModel):
    """Ask for a whole batch of calls in one message, then answer — recording what it was sent.

    The request the second call receives is the one under test: it is the first time the model sees
    the batch's results, so it is the request neither context edit may reduce.
    """

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Record the bound surface, so the prefix can be measured from the far side of the call."""
        _BOUND[:] = list(tools)
        return self

    def _generate(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
        """Record the request, then replay the script."""
        _SENT.append(list(messages))
        return super()._generate(messages, *args, **kwargs)


def _oversized_sweep() -> Any:
    """A connector tool whose every answer is well past the per-result ceiling."""

    async def sweep(q: str) -> str:
        """Sweep the corpus.

        Args:
            q: the query
        """
        return "X" * 200_000

    tool = StructuredTool.from_function(coroutine=sweep, name="sweep", description="Sweep.")
    # The stamp `connectors/transport._stamped` writes, so the result is framed as well as bounded
    # — the two rewrites a real connector result passes through.
    tool.metadata = {SERVED_BY: {"connector": "fakeconn", "server": "s"}}
    return tool


@pytest.mark.parametrize("width", [8, 20])
def test_one_assistant_message_cannot_fan_out_past_the_request_budget(width: int) -> None:
    """One assistant message cannot fan out past the request budget.

    The per-result ceiling bounds one result; the context edits deliberately keep the newest batch
    whole, so the batch must be bounded by sharing the ceiling across it.
    `agent_max_parallel_tool_calls` limits concurrency, not results, so the width is swept past it.
    """
    _SENT.clear()
    _BOUND.clear()
    calls = [{"name": "sweep", "args": {"q": f"q{i}"}, "id": f"c{i}"} for i in range(width)]
    model = _FanOutModel(
        messages=iter([AIMessage(content="", tool_calls=calls), AIMessage(content="done")])
    )
    graph = build_langgraph_agent(
        model=model, connectors=[_oversized_sweep()], audit_sink=NullAuditSink()
    )

    asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="go")]}))

    assert len(_SENT) == 2, "the model was never handed the batch's results"
    sent = _SENT[1]
    results = [m for m in sent if isinstance(m, ToolMessage)]
    assert len(results) == width, "the fixture did not actually fan out"
    # The whole request, because that is what the provider bills and what
    # `agent_context_token_budget` has bounded since the prefix was charged unconditionally.
    prefix = count_tokens_approximately([m for m in sent if isinstance(m, SystemMessage)])
    prefix += estimate_tool_schemas(_BOUND)
    thread = count_tokens_approximately([m for m in sent if not isinstance(m, SystemMessage)])

    assert prefix + thread <= settings.agent_context_token_budget, (
        f"a {width}-wide fan-out sent {prefix + thread} estimated tokens against a budget of "
        f"{settings.agent_context_token_budget}"
    )


@pytest.mark.parametrize("width", [190, 400, 1000])
def test_the_batch_share_bounds_the_batch_at_every_width(width: int) -> None:
    """The batch share bounds the batch at every width, including past the notice-length crossover.

    Once `ceiling // width` falls below the notice length, flooring each result at the full notice
    would grow the total linearly with width.
    """
    ceiling = settings.agent_max_tool_result_chars
    share = max(ceiling // width, 1)
    out, _ = bounded_content("x" * 200_000, "sweep", share)
    # Per result, the share or the brief notice, whichever is larger; the notice is never cut. Its
    # length is measured, not written down.
    brief = len(_brief_notice(200_000))
    assert len(out) <= max(share, brief), f"one result overran its share at width {width}"
    assert len(out) * width <= ceiling, (
        f"the batch totalled {len(out) * width:,} against a {ceiling:,} ceiling at width {width}"
    )


def test_a_cut_is_not_silent_when_the_first_block_carries_no_text() -> None:
    """A cut is not silent when the first block carries no text.

    `_rebuilt` drops text computed for a block that is neither a string nor a text dict, so the
    notice must land on one that survives, not on a leading image.
    """
    content = [{"type": "image", "source": {"data": "abc"}}, {"type": "text", "text": "y" * 9_000}]
    out, removed = bounded_content(content, "sweep", 500)
    assert removed > 0
    text = "".join(b.get("text", "") for b in out if isinstance(b, dict))
    assert "removed from the middle" in text or "chars cut" in text, (
        "the result was shortened and nothing in it says so"
    )
    # The image is still there: this cap shortens text and carries everything else through.
    assert any(isinstance(b, dict) and b.get("type") == "image" for b in out)


def test_the_notice_carries_the_mark_that_makes_it_this_systems_own_sentence() -> None:
    """The notice carries `SYSTEM_SPEECH_MARK`, which makes it this system's own sentence.

    The model reads every unmarked word of a tool result as data. Both notice forms are checked,
    since a wide fan-out gets the brief one.
    """
    assert SYSTEM_SPEECH_MARK in _notice("read_document", 40_000, 60_000)
    assert SYSTEM_SPEECH_MARK in _brief_notice(40_000)


def test_a_connector_cannot_forge_the_notice_it_now_carries() -> None:
    """A connector cannot forge the notice.

    Connector payloads pass through `framing.defang`, whose `_MARK_FORGERY` escapes any
    `[system <nonce>]`-shaped span; the mark is worth reading only because of that.
    """
    forged = defang(_notice("read_document", 40_000, 60_000))

    assert SYSTEM_SPEECH_MARK not in forged, "a tool could forge the truncation notice"
    assert "&#91;system" in forged, "the forgery was dropped rather than escaped"


def test_the_notice_is_still_charged_against_the_limit_now_that_it_is_longer() -> None:
    """The marked notice is still charged against the limit.

    The notice is measured at its widest form and subtracted from `limit`, so at most `limit`
    characters come back.
    """
    bounded, removed = bounded_content("x" * 100_000, "read_document", 5_000)

    assert len(bounded) <= 5_000, f"the bound returned {len(bounded)} against a limit of 5,000"
    assert SYSTEM_SPEECH_MARK in bounded
    assert removed >= 95_000


@pytest.mark.parametrize("served", [False, True])
def test_where_the_notices_mark_survives_the_chain_is_measured_not_assumed(served: bool) -> None:
    """The notice's mark reaches the model on an unframed result and is escaped inside an envelope.

    Driven in the shipped order, `bound_tool_results` inside `frame_connector_results`:

    - **in-process**: nothing rewrites the result afterwards, so the mark arrives intact;
    - **connector-served**: the framer defangs every span inside the data envelope, so the mark
      arrives as `&#91;system …`, consistent with the envelope declaring the whole span data.

    Pinned as a limitation, so a change of order has to be made here deliberately.
    """
    request = cast(
        Any,
        SimpleNamespace(
            tool_call={"name": "read_document", "id": "c1", "args": {}},
            state={"messages": []},
            tool=SimpleNamespace(metadata={SERVED_BY: {"connector": "calc"}} if served else {}),
        ),
    )

    async def tool(_request: Any) -> ToolMessage:
        return ToolMessage(content="X" * 200_000, tool_call_id="c1", name="read_document")

    async def bounded(inner_request: Any) -> Any:
        return await bound_tool_results.awrap_tool_call(inner_request, tool)

    message = asyncio.run(frame_connector_results.awrap_tool_call(request, bounded))
    # The middlewares under test both hand back a `ToolMessage`; the `Command` arm of that union
    # is the `task` helper's, which this path never reaches (`agent/tool_result_shape.py`).
    assert isinstance(message, ToolMessage), "this chain returned a Command, not a tool result"
    text = str(message.content)

    assert "chars cut" in text or "removed from the middle" in text, "the cut went unannounced"
    assert (SYSTEM_SPEECH_MARK in text) is not served, (
        "the mark survives exactly on the path where nothing defangs the result afterwards"
    )
    if served:
        assert "&#91;system" in text, "the mark was dropped rather than escaped"
