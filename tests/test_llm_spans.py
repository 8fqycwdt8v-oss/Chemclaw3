"""A model call is a span, it carries the counts, and it carries nothing a chemist said.

The two gaps this closes were named regressions rather than wishes: `core/tracing.py` opens exactly
two spans — the turn and the tool call — so the model call between them was invisible, and
`chemclaw_*_tokens_total` carries `profile` rather than model since the framework that emitted
`gen_ai.client.token.usage` was removed. Both are in `docs/guides/runbook.md` as things an operator
can no longer ask.

**The content assertion scans every attribute value rather than naming keys**, and that is the
whole design of this file. Naming keys tests the list of keys somebody thought of; a deployment's
question is "can a chemist's question reach the collector", and only sweeping the exported spans
answers it. It is also the assertion that would catch an upstream release adding a new
content-bearing attribute, which naming keys would not.
"""

import asyncio
from typing import Any

import pytest
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.core.config import settings
from chemclaw.core.logging import (
    _instrument_llm_calls,
    _trace_config,
    _warn_about_sensitive_data,
)

# Distinctive enough that a substring sweep cannot match them by accident, which is what lets the
# content assertion be a sweep rather than a list of attribute names.
QUESTION = "WHICHSOLVENTMARKER"
ANSWER = "ANSWERTEXTMARKER"


class _Fake(GenericFakeChatModel):
    """A scripted model that accepts tool binding and reports usage like a real one.

    `usage_metadata` is set because the token counts are half of what is under test here, and a
    fake that omitted them would let an assertion pass on a span that carried nothing.
    """

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Accept the binding; the script does not reason about tools."""
        return self


def _turn(*, content_allowed: bool, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Run one instrumented turn and return the spans it exported."""
    monkeypatch.setattr(settings, "otel_llm_spans", True)
    monkeypatch.setattr(settings, "otel_include_sensitive_data", content_allowed)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    answer = AIMessage(content=ANSWER)
    answer.usage_metadata = {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}
    _instrument_llm_calls(provider)
    try:
        graph = build_langgraph_agent(model=_Fake(messages=iter([answer])))
        asyncio.run(graph.ainvoke({"messages": [HumanMessage(content=QUESTION)]}))
    finally:
        # Uninstrumented in a `finally` because the instrumentor is a process-wide singleton: a test
        # that left it attached would silently instrument every later test in the session, and the
        # failure would surface somewhere else entirely.
        from openinference.instrumentation.langchain import LangChainInstrumentor

        LangChainInstrumentor().uninstrument()
    return list(exporter.get_finished_spans())


def _attributes_carrying_content(spans: list[Any]) -> set[str]:
    """Every span attribute whose value mentions the question or the answer."""
    return {
        key
        for span in spans
        for key, value in (span.attributes or {}).items()
        if QUESTION in str(value) or ANSWER in str(value)
    }


def test_a_model_call_becomes_a_span_carrying_its_token_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gap this closes: there was no span between `chemclaw.turn` and `chemclaw.tool`.

    Asserted on the counts as well as on the span's existence, because a span that named the model
    call and carried nothing would close the *trace* gap and leave the *attribution* one open — and
    those are two separate rows in `docs/planning/BACKLOG.md`.
    """
    spans = _turn(content_allowed=False, monkeypatch=monkeypatch)

    llm = [s for s in spans if (s.attributes or {}).get("openinference.span.kind") == "LLM"]
    assert llm, (
        "no LLM span was exported; kinds were "
        f"{[(s.attributes or {}).get('openinference.span.kind') for s in spans]}"
    )
    counts = {k: v for k, v in (llm[0].attributes or {}).items() if "token_count" in k}
    assert counts.get("llm.token_count.prompt") == 11, counts
    assert counts.get("llm.token_count.completion") == 7, counts


def test_the_suppressed_span_carries_no_word_the_chemist_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default must satisfy `core/tracing.py`'s rule, and satisfy it by measurement.

    "identifiers and counts, never a question, an argument or an answer" is a property of what
    reaches the collector, so it is checked against what reached the exporter — every attribute of
    every span, not the ones this test's author could name.
    """
    spans = _turn(content_allowed=False, monkeypatch=monkeypatch)

    assert _attributes_carrying_content(spans) == set(), (
        "turn content reached a span under the default configuration"
    )


def test_the_flag_is_what_decides_it_and_it_costs_none_of_the_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Content appears only when `otel_include_sensitive_data` says so, and hiding is free.

    Both halves in one test because they are one claim. A suppression that also cost the token
    counts would be a trade-off a deployment might reasonably refuse; measured, it is not one — the
    counts are identical across the two runs, so the privacy-preserving setting can be the default
    without anybody weighing it against what the instrumentation was added for.
    """
    hidden = _turn(content_allowed=False, monkeypatch=monkeypatch)
    shown = _turn(content_allowed=True, monkeypatch=monkeypatch)

    assert _attributes_carrying_content(shown), (
        "content was suppressed even with otel_include_sensitive_data set, so the flag decides "
        "nothing and the knob is dead again"
    )

    def _counts(spans: list[Any]) -> list[Any]:
        return sorted(
            (k, v)
            for s in spans
            for k, v in (s.attributes or {}).items()
            if "token_count" in k or k == "llm.provider"
        )

    assert _counts(hidden) == _counts(shown), (
        f"suppression changed the counts: {_counts(hidden)} vs {_counts(shown)}"
    )


def test_the_instrumentation_is_absent_until_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off by default, and off means the instrumentation is never imported or attached.

    The counter-example every observability switch here needs: a flag that is read but never acted
    on looks identical from outside to one that works, which is the defect
    `agent/compaction.py` exists because of.
    """
    monkeypatch.setattr(settings, "otel_llm_spans", False)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    _instrument_llm_calls(provider)

    graph = build_langgraph_agent(model=_Fake(messages=iter([AIMessage(content=ANSWER)])))
    asyncio.run(graph.ainvoke({"messages": [HumanMessage(content=QUESTION)]}))
    assert exporter.get_finished_spans() == (), "spans were exported with the flag off"


def test_every_hide_flag_is_set_together(monkeypatch: pytest.MonkeyPatch) -> None:
    """Suppression is all-or-nothing, and the list is written out rather than derived.

    Two things are pinned. That no hide flag is left False when content is disallowed — a span
    carrying the prompt but not the completion still carries a chemist's question, so a partial
    answer here is not an answer. And that the list is *explicit*: deriving it from the dataclass's
    fields would silently adopt whatever a future release adds, including a field whose default is
    the permissive one, so a new upstream flag should fail this test rather than inherit a decision.
    """
    from openinference.instrumentation import TraceConfig

    monkeypatch.setattr(settings, "otel_include_sensitive_data", False)
    config = _trace_config(TraceConfig)

    unset = [
        name
        for name in TraceConfig.__dataclass_fields__
        if name.startswith("hide_") and not getattr(config, name)
    ]
    assert unset == [], f"hide flag(s) left unset while content is disallowed: {unset}"

    monkeypatch.setattr(settings, "otel_include_sensitive_data", True)
    permissive = _trace_config(TraceConfig)
    assert not any(
        getattr(permissive, name)
        for name in TraceConfig.__dataclass_fields__
        if name.startswith("hide_")
    ), "content was allowed but hide flags were still set"


def test_enabling_content_says_so_out_loud(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The dangerous direction must warn, because it is the one that used to be silent.

    `otel_include_sensitive_data` governed nothing for a phase, and the process warned about
    exactly that. Giving it a consumer back inverts which case needs saying: a deployment that set
    the flag while it was inert — and `core/config/observability.py` kept it "because a deployment
    may still have it in its values file" — gets `otel_llm_spans` switched on by the shipped chart
    and starts exporting a chemist's question to the collector, without anybody deciding that in
    this release.

    The endpoint is in the assertion because a warning that does not say *where* the content is
    going leaves the operator with the wrong half of the question.
    """
    monkeypatch.setattr(settings, "otel_include_sensitive_data", True)
    monkeypatch.setattr(settings, "otel_llm_spans", True)
    monkeypatch.setattr(settings, "otel_endpoint", "http://collector.observability.svc:4317")

    with caplog.at_level("WARNING"):
        _warn_about_sensitive_data()

    warning = caplog.text
    assert "exported to http://collector.observability.svc:4317" in warning, warning
    assert "has no effect" not in warning, "the inert-case warning fired while the flag was live"


def test_the_inert_case_still_says_it_is_inert(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The inert direction keeps its warning: a dead switch must not read as a live one.

    Both directions are pinned because the bug being guarded against is a *branch* that fires on
    the wrong one — and a test for only the new half would have passed against the code that had
    only the old half.
    """
    monkeypatch.setattr(settings, "otel_include_sensitive_data", True)
    monkeypatch.setattr(settings, "otel_llm_spans", False)

    with caplog.at_level("WARNING"):
        _warn_about_sensitive_data()

    assert "has no effect" in caplog.text
    assert "exported to" not in caplog.text, "the live-case warning fired while the flag was inert"
