"""One tool result cannot be larger than the budget it is inside — the bound that did not exist.

`connector_max_request_bytes` capped what this system sends a capability server. Nothing capped
what came back, and the two context edits are structurally unable to: `ClearToolUsesEdit` preserves
the newest `agent_keep_last_tool_groups` results verbatim and the conversation window never cuts
past the newest group, so a single oversized result is the one thing neither can reclaim.

Measured on the shipped defaults, with each result inside its own tool's ceiling: two results at
200,000 characters are 100,077 estimated tokens — one over the budget — and ~224,000 billed, with
both edits running and reclaiming nothing.
"""

import asyncio
from typing import Any, cast

import pytest
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.tools import StructuredTool

from chemclaw.agent.audit import NullAuditSink, metric_tool_name
from chemclaw.agent.authz import AuthorizationError
from chemclaw.agent.context_budget import estimate_tool_schemas
from chemclaw.agent.framing import ENVELOPE_TAG
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.tool_authz import surface_authorization_denials, surface_domain_errors
from chemclaw.agent.tool_result_size import bound_tool_results, bounded_content
from chemclaw.connectors.transport import SERVED_BY
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.metrics import METRICS


def test_a_result_inside_the_ceiling_is_untouched() -> None:
    """Identity, not a copy: most results are small and must cost nothing at all."""
    content = "a modest answer"

    bounded, removed = bounded_content(content, "find_notes", 60_000)

    assert bounded is content
    assert removed == 0


def test_both_ends_of_an_oversized_result_survive() -> None:
    """Head *and* tail, because a procedure states its outcome at the end.

    `agent/condense.py` makes this argument for a protocol and it generalises: a head-truncated
    result returns conditions that look complete with the yield and purity silently absent, which
    reads as "not measured" against neighbours that measured it. Keeping both ends costs nothing
    and leaves the two places a reader's eye actually goes.
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
    """A silently shortened result is a model reporting on a corpus it was never shown all of.

    The notice names the tool, the arithmetic and the remedy that exists — narrowing the question,
    not asking again — and marks itself as system text, for the reason `TOOL_RESULT_PLACEHOLDER`
    does: a model shown a shortened result with no explanation reads it as what the tool returned.
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


def test_an_oversized_result_is_bounded_on_its_way_to_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through the middleware, because the claim is about the chain and not about the arithmetic.

    Every tool, not only an out-of-process one: the two results that measured this defect —
    `read_document` and `find_calculations` — are in-process, so a cap keyed on the `SERVED_BY`
    stamp would have missed exactly the case it exists for.
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
    assert METRICS.value("chemclaw_tool_results_truncated_total") > before


class _Request:
    """The attributes `bound_tool_results` reads off a tool-call request.

    `tool` is `None`, which is both LangChain's documented default for a request built outside a
    graph and what `ToolNode` passes for a name the graph does not hold — so the metric label
    clamps to `"unknown"`, which is the case the clamp exists for.
    """

    def __init__(self, name: str) -> None:
        """Name the tool this request is for; nothing else about it is read."""
        self.tool_call = {"name": name, "args": {}, "id": "c1"}
        self.tool = None
        self.state: dict[str, Any] = {}


def test_an_invented_tool_name_never_reaches_the_truncation_label() -> None:
    """The counter is on an unauthenticated `/metrics`, so its label may not be model-authored.

    `core/metrics.py` declares this counter's label as bounded and says why — "a tool name here is
    one the registry served, never a string a caller invented" — and that was the belief rather
    than the code. `ToolNode` dispatches an unregistered name through this chain deliberately, its
    not-a-valid-tool error **echoes the name back**, and the echo is over the ceiling exactly when
    the name is: measured on a compiled graph, a 90,006-character invented name minted a
    **90,054-character** exposition line, one new series per name, while
    `chemclaw_tool_calls_total` beside it correctly read `tool="unknown"` — the same clamp, two
    middlewares away, already applied.
    """
    request = _Request("EXFIL_" + "B" * 200)

    async def handler(_: Any) -> ToolMessage:
        return ToolMessage(content="y" * 200_000, tool_call_id="c1")

    asyncio.run(bound_tool_results.awrap_tool_call(cast(Any, request), handler))

    rendered = METRICS.render()
    assert "EXFIL_" not in rendered, "a model's string became a metric label"
    assert 'chemclaw_tool_results_truncated_total{tool="unknown"}' in rendered


def test_a_cut_result_is_never_larger_than_its_ceiling() -> None:
    """The notice is charged against the ceiling, because the model reads it like any other span.

    Past the ceiling the function used to keep exactly `limit` characters and *then* add a
    313-character notice, so the bound returned `limit + 313` — and for an overshoot smaller than
    the notice it grew what it was bounding: measured at the shipped ceiling, 60,001 characters in,
    **60,313 out**, 312 more than the tool returned, with the truncation counter incremented and a
    notice telling the model to narrow its question. A ceiling that is exact is also what lets a
    batch's share of it be exact (`bound_tool_results`).
    """
    for total in (60_001, 61_000, 100_000):
        bounded, removed = bounded_content("A" * total, "read_document", 60_000)

        assert len(bounded) <= 60_000, f"{total} characters came back as {len(bounded)}"
        assert len(bounded) < total, "the bound grew the result it was bounding"
        assert removed > 0


def test_a_cut_is_never_silent_at_the_smallest_configurable_ceiling() -> None:
    """`agent_max_tool_result_chars` is `ge=0`, so a deployment may set 1 — and did lose the notice.

    Three fifths of the limit goes to the head, which rounds to 0 below 2: the head loop then broke
    before its first iteration, `last_head` stayed at -1, no index matched, and the notice was
    dropped. `bounded_content("A" * 1_000, …, 1)` returned a single character with nothing saying
    so — the one contract this module has, that a cut is never silent, broken at the edge of its
    own configuration range.
    """
    bounded, removed = bounded_content("A" * 1_000, "read_document", 1)

    assert removed == 1_000, "every character of the result was dropped"
    # The brief form, because the explanatory sentence is 312 characters and the limit is 1. It
    # keeps the three facts the model cannot act correctly without — that something was removed,
    # how much, and that the system removed it rather than the tool returning nothing — and drops
    # the advice about narrowing the question, which is what there is no room for.
    assert "read_document" in bounded and "by the system" in bounded
    assert "1,000 chars cut" in bounded


def test_a_result_smaller_than_the_notice_is_left_alone() -> None:
    """Below the notice's own length there is nothing to reclaim, so nothing is cut.

    The two rules — a cut is never silent, and a bound never grows what it bounds — collide only
    here, and this is the resolution: a result shorter than the sentence explaining the cut cannot
    be made smaller by cutting it, so it is not cut and no notice is owed.
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
    """The ceiling bounds one *result*; what neither context edit can reclaim is one *batch*.

    **Measured before the fix, on this graph.** `ClearOlderToolResultsEdit` raises `keep` to the
    newest batch's size so the batch survives by construction, and the conversation window clamps
    its cut at the newest group — both correct for evidence the model has not read yet, and exactly
    why a fan-out escapes. Nothing bounded the product of the per-result ceiling and the batch
    width: at the shipped 60,000 characters and a width of 8 the request went out at **164,232**
    estimated tokens against a 100,000 budget, and at 20 at **345,735** — every control doing
    precisely what it documents, `chemclaw_context_compactions_total` at 0 because there was
    nothing older to clear.

    `agent_max_parallel_tool_calls` is not the missing bound: it is LangGraph's `max_concurrency`,
    so 20 calls still yield 20 results, which is why the width is swept past it here.
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


# --- the bound has to survive what runs *outside* it ---------------------------------------------


#: Characters the envelope's two delimiters and the truncation notice may add on top of a call's
#: share of the ceiling. The opening tag carries the nonce and the origin id, the closing tag the
#: nonce again, and the notice is ~313. 800 is roughly double the widest this repository can
#: produce and is not a budget anything is meant to grow into.
_ENVELOPE_SLACK = 800

#: A zero-width space between `</` and the tag word. `framing._INVISIBLE` strips it, `_FORGERY` then
#: matches the stripped copy, and `_defang` treats the whole payload as deliberately obfuscated —
#: the branch that escapes every `<` rather than only the delimiter's.
_DISGUISED_DELIMITER = "</​retrieved-note-x>"


def _forgery_dense_sweep(payload: str) -> Any:
    """A connector tool whose every answer is a payload the defang pass expands."""

    async def sweep(q: str) -> str:
        """Sweep the corpus.

        Args:
            q: the query
        """
        return payload

    tool = StructuredTool.from_function(coroutine=sweep, name="sweep", description="Sweep.")
    tool.metadata = {SERVED_BY: {"connector": "fakeconn", "server": "s"}}
    return tool


def test_defanging_cannot_grow_a_result_back_past_the_ceiling() -> None:
    """A rewrite that runs outside the size control is a rewrite the size control does not bound.

    **Measured before the fix, on the compiled graph, at the shipped fan-out width.**
    `framing._defang`'s second pass replaces **every** `<` with `&lt;` — one character for four —
    as soon as an invisible character reveals a disguised delimiter, and it ran inside
    `frame_connector_results`, which is the **outer** middleware (`tool_call_middleware`). So the
    expansion happened after the cut: each of eight calls was bounded to its 7,500-character share
    and reached the model at **29,096** characters, 3.88x, for a batch of 58,399 estimated tokens —
    and a request of **101,899** against a 100,000 budget once the static prefix is charged. Neither
    context edit can reclaim a byte of it: this is the newest batch.

    Upstream's `FilesystemMiddleware` is not the missing bound either. It evicts a result over
    80,000 characters to `/large_tool_results/`, so it catches a *lone* expanded result and misses
    the band this repository's own 60,000 ceiling exists to cover — which is exactly the 60,000
    against 80,000 gap `agent/tool_result_shape.py` already records for a helper's report.

    **The nesting is not the defect and did not change.** The envelope must stay outside the cut or
    the cut severs its closing delimiter. What moved is the *defang*, into `defang_tool_results`
    below the cut, so the size control measures the characters the model will actually read.
    """
    _SENT.clear()
    _BOUND.clear()
    width = max(settings.agent_max_parallel_tool_calls, 1)
    calls = [{"name": "sweep", "args": {"q": f"q{i}"}, "id": f"c{i}"} for i in range(width)]
    payload = "<" * 200_000 + _DISGUISED_DELIMITER
    model = _FanOutModel(
        messages=iter([AIMessage(content="", tool_calls=calls), AIMessage(content="done")])
    )
    graph = build_langgraph_agent(
        model=model, connectors=[_forgery_dense_sweep(payload)], audit_sink=NullAuditSink()
    )

    asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="go")]}))

    sent = _SENT[1]
    results = [m for m in sent if isinstance(m, ToolMessage)]
    assert len(results) == width, "the fixture did not actually fan out"
    share = settings.agent_max_tool_result_chars // width
    for result in results:
        read = len(str(result.content))
        assert read <= share + _ENVELOPE_SLACK, (
            f"the model was handed {read} characters against a batch share of {share}"
        )
    assert ENVELOPE_TAG in str(results[0].content), "the cut severed the envelope"
    assert "&lt;" in str(results[0].content), "the fixture's forged delimiter was never defanged"
    # The same whole-request assertion the width sweep above makes, because a per-result bound that
    # holds while the request does not is the defect one layer up.
    prefix = count_tokens_approximately([m for m in sent if isinstance(m, SystemMessage)])
    prefix += estimate_tool_schemas(_BOUND)
    thread = count_tokens_approximately([m for m in sent if not isinstance(m, SystemMessage)])
    assert prefix + thread <= settings.agent_context_token_budget, (
        f"a {width}-wide fan-out of forgery-dense results sent {prefix + thread} estimated tokens "
        f"against a budget of {settings.agent_context_token_budget}"
    )


def test_the_unreducible_floor_fits_the_context_budget() -> None:
    """The two ceilings nobody was multiplying, asserted against the budget they have to fit.

    `agent_max_tool_result_chars` and `agent_max_parallel_tool_calls` are declared a hundred lines
    apart in `core/config/agent.py`, and their product is the one thing the context policy is
    designed **not** to reduce: `ClearOlderToolResultsEdit` raises `keep` to the newest batch's size
    so the batch survives structurally, and `KeepLastConversationGroupsEdit` clamps its cut at the
    newest group. So the floor under every request is the static prefix plus a full-width fan-out at
    the ceiling — and nothing computed it from the shipped settings.

    It fits *because* `bound_tool_results` divides the ceiling by the batch's width: the whole batch
    shares one 60,000-character allowance rather than each call getting one. That division is the
    reconciliation, and this is the assertion that keeps it true. Raising the ceiling, widening the
    parallelism, or growing the tool surface past the point where the three no longer fit together
    fails here — in the pull request that does it, rather than in a provider's context-length error
    that names none of them.

    The prefix is `tests/test_context_floor.py`'s ratchet **ceiling** rather than today's
    measurement, deliberately: this assertion is about whether the *configuration* is coherent, and
    pinning it to a number that moves whenever a docstring does would make it a second ratchet
    (`D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit`).
    """
    from tests.test_context_floor import CEILINGS

    prefix = CEILINGS["__default__"]
    width = max(settings.agent_max_parallel_tool_calls, 1)
    share = settings.agent_max_tool_result_chars // width + _ENVELOPE_SLACK
    batch = [ToolMessage(content="x" * share, tool_call_id=f"c{index}") for index in range(width)]
    floor = prefix + int(count_tokens_approximately(batch))

    assert floor <= settings.agent_context_token_budget, (
        f"the unreducible floor is {floor} estimated tokens — a {prefix}-token static prefix "
        f"plus a "
        f"{width}-wide fan-out at {settings.agent_max_tool_result_chars} characters — against a "
        f"budget of {settings.agent_context_token_budget}. Neither context edit can reclaim any of "
        "it: lower agent_max_tool_result_chars, lower agent_max_parallel_tool_calls, raise "
        "agent_context_token_budget, or shrink the tool surface."
    )


# --- and the ceiling has to be the floor under the refusals this system composes itself ----------


def _authorization_denial(_: Any) -> None:
    """Raise the denial `surface_authorization_denials` words for the model."""
    raise AuthorizationError("you are not authorized to call " + "N" * 150_000)


def _domain_error(_: Any) -> None:
    """Raise the fault `surface_domain_errors` words for the model."""
    raise ChemclawError("the arguments were not valid JSON: " + "D" * 200_000)


@pytest.mark.parametrize(
    ("middleware", "raiser"),
    [
        (surface_authorization_denials, _authorization_denial),
        (surface_domain_errors, _domain_error),
    ],
)
def test_a_refusal_this_system_composed_is_bounded_like_any_other_result(
    middleware: Any, raiser: Any
) -> None:
    """The two outer converters *manufacture* a `ToolMessage`, so the cut below them never sees it.

    `bound_tool_results` sits at index 3 of `tool_call_middleware`; `surface_authorization_denials`
    and `surface_domain_errors` are at 0 and 1. A gate below raises, the exception travels up
    *past* the cut, and the converter builds the result the model reads — so this module's own
    claim to be "the floor under all of them, applied at the one place every tool result passes"
    was false for exactly the two messages that interpolate **model-authored** text.

    Measured on the real chain before the fix, against a 60,000-character ceiling: a 200,000-char
    malformed-argument document (`refuse_unparsed_arguments` embeds `defang(str(document))`) came
    back as a **200,254**-character result, and a 150,000-character invented tool name under
    `tool_authz_default="deny"` as a **150,141**-character refusal. Neither is one-shot: the repeat
    guard keys on name plus arguments, so every distinct invented name is a fresh call.

    The fix is `bound_refusal_text`, called by `_refusal_message` — the one function both converters
    compose through — rather than a second cut in each of them.
    """
    request = _Request("find_notes")

    async def handler(inner: Any) -> Any:
        raiser(inner)

    message = asyncio.run(middleware.awrap_tool_call(cast(Any, request), handler))

    assert isinstance(message, ToolMessage)
    assert len(str(message.content)) <= settings.agent_max_tool_result_chars, (
        f"a composed refusal reached the model at {len(str(message.content))} characters against a "
        f"ceiling of {settings.agent_max_tool_result_chars}"
    )
    # Head and tail, so the sentence the refusal opens with still says what happened.
    assert str(message.content).startswith(("Refused:", "Error:"))


@pytest.mark.parametrize("width", [190, 400, 1000])
def test_the_batch_share_bounds_the_batch_at_every_width(width: int) -> None:
    """The share has to bound the *batch*, and past a certain width it stopped doing so.

    `bounded_content` refused to return less than the sentence explaining the cut, so once
    `agent_max_tool_result_chars // width` fell below that sentence's ~312 characters every result
    floored there and the batch total grew linearly with the width instead of being capped.
    Measured before this: **124,800** characters at width 400 against a 60,000 ceiling, and
    **312,000** at width 1000 — the defect the share was introduced to close, one order of
    magnitude up.

    Swept past the crossover deliberately. The first version of this test used widths 8 and 20,
    both comfortably below it, which is why it passed against the floor.
    """
    ceiling = settings.agent_max_tool_result_chars
    share = max(ceiling // width, 1)
    out, _ = bounded_content("x" * 200_000, "sweep", share)
    # Per result, the share or the brief notice, whichever is larger — the notice is never cut,
    # because a bound paid for by saying nothing is not what this module is for.
    assert len(out) <= max(share, 19), f"one result overran its share at width {width}"
    assert len(out) * width <= ceiling, (
        f"the batch totalled {len(out) * width:,} against a {ceiling:,} ceiling at width {width}"
    )


def test_a_cut_is_not_silent_when_the_first_block_carries_no_text() -> None:
    """The notice has to land on a block that survives the rebuild.

    `_kept` placed it at index 0 regardless, and `_rebuilt` drops the text computed for any block
    that is neither a string nor a dict with a `text` key — so an image-first result lost the
    notice entirely: the characters went, the truncation counter moved, and the model was handed
    the image with nothing saying the rest had been removed. That is the silent cut this module
    exists to prevent, one block along from where it was being prevented.
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


def test_an_invented_tool_name_cannot_carry_a_result_past_the_ceiling() -> None:
    """The ceiling has to hold against the *name*, which is model-authored like the payload.

    `bounded_content` interpolates the tool name into both notices, so `widest` grows with the name.
    Past `limit < widest` it takes the brief branch, and the brief form is deliberately never cut —
    which means a long enough name walks the whole result through untouched, with `removed=0`, no
    counter and no log. Measured before the fix, on a compiled `default` graph at the shipped
    60,000-character ceiling: a 70,000-character invented name put **70,051** characters in front of
    the model, and eight distinct such names in one batch made a 464,083-token request, all of it in
    the newest batch neither context edit may reclaim.

    The comment at the call site used to argue the opposite — that a returned result must name the
    call it belongs to, and that the raw name "is bounded here by the result it rides on". It is
    not, and `bound_refusal_text` had already reached the other conclusion for the same input, so
    the two paths disagreed about whether a model-authored name may be trusted with a length.

    Asserted through `bounded_content` directly rather than through the middleware, because the
    defect is in the arithmetic and a middleware test would need a compiled graph to see one number.
    The existing name-safety test uses a 200-character name, which is comfortably inside `widest`
    and therefore cannot reach this branch at all.
    """
    ceiling = settings.agent_max_tool_result_chars
    payload = "x" * (ceiling * 2)

    # The escape has two shapes and the length is what they share: with a payload larger than the
    # notice the whole result is cut and the *notice* is oversized (70,051 here); with a smaller one
    # `total <= len(brief)` returns the content untouched at `removed=0`. Either way the model reads
    # more than the ceiling, so the length is the assertion.
    raw, _ = bounded_content(payload, "N" * 70_000, ceiling)
    assert len(raw) > ceiling, "this is the escape: an unclamped name is interpolated uncut"
    untouched, removed_none = bounded_content("x" * 100_000, "N" * 150_000, ceiling)
    assert len(untouched) == 100_000 and removed_none == 0, (
        "and past the notice's own width the payload is not cut at all, silently"
    )

    clamped, removed = bounded_content(payload, metric_tool_name(None, "N" * 70_000), ceiling)
    assert len(clamped) <= ceiling, f"the clamped name must keep the cut: {len(clamped)}"
    assert removed > 0, "and the cut must still be counted"
