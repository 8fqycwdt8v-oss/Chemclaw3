"""A model call is a span, it carries the token counts, and it carries nothing a chemist said.

The content assertion sweeps every attribute value of every exported span rather than naming keys,
so it answers "can a chemist's text reach the collector" and catches a new content-bearing
attribute an upstream release adds.
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
    """A model call becomes a span between the turn and tool spans, carrying token counts.

    A span without counts would close the trace gap and leave attribution open.
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
    """The suppressed span carries no word the chemist typed, in any exported attribute."""
    spans = _turn(content_allowed=False, monkeypatch=monkeypatch)

    assert _attributes_carrying_content(spans) == set(), (
        "turn content reached a span under the default configuration"
    )


def test_the_flag_is_what_decides_it_and_it_costs_none_of_the_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Content appears only when `otel_include_sensitive_data` allows it, at no cost in counts.

    The counts are identical across both runs, so the privacy-preserving default trades nothing
    away.
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

    A flag read but never acted on looks identical from outside to one that works.
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
    """Every hide flag is set together, from an explicit list.

    A span with the prompt but not the completion still leaks the question. The list is written out
    rather than derived, so a new upstream flag fails here rather than inheriting a permissive
    default.
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
    """Enabling content warns, naming the endpoint it will be exported to.

    A deployment carrying the flag in its values file would otherwise start exporting chemists'
    questions without a decision.
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
    """The inert direction keeps its warning, so a dead switch does not read as a live one.

    Both directions are pinned because the guarded bug is a branch firing on the wrong case.
    """
    monkeypatch.setattr(settings, "otel_include_sensitive_data", True)
    monkeypatch.setattr(settings, "otel_llm_spans", False)

    with caplog.at_level("WARNING"):
        _warn_about_sensitive_data()

    assert "has no effect" in caplog.text
    assert "exported to" not in caplog.text, "the live-case warning fired while the flag was inert"
