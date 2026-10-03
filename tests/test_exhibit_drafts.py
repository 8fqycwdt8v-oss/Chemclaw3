"""A document artefact streams as `exhibit_draft` frames while the model writes its call.

Driven through the real compiled graph and `graph_events` with a model that streams a call's
arguments in fragments, the way a provider does — because what is pinned is a property of the
stream: the frames arrive before the `tool_call`, `tool_result` and `exhibit` of the call they
preview, each carries the whole text so far, the throttle bounds how many there are, and nothing is
streamed for a call that is not a document. The fragment parser's edge cases (`edits`, a cap, a
`kind` still being written) are driven on `DraftStream` directly.
"""

import asyncio
import json
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk

from chemclaw.agent.audit import NullAuditSink
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.api.events import ExhibitDraftEvent, ExhibitEvent
from chemclaw.api.exhibit_drafts import DraftStream
from chemclaw.api.graph_stream import graph_events
from chemclaw.api.runner_trace import ToolCallTrace
from chemclaw.core.config import settings
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from chemclaw.core.session_context import reset_current_session_id, set_current_session_id
from tests.fakes_langgraph import ScriptedChatModel

_REPORT = "# Plan\n\nStep one: dry the THF.\n\nStep two: add the base slowly.\n"


class _FragmentingModel(ScriptedChatModel):
    """A scripted model whose tool call arrives as many argument fragments, like a provider's."""

    fragment_chars: int = 12

    def _stream(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        """The call's id and name on the first fragment, then its JSON arguments piece by piece."""
        message = next(self.messages)
        assert isinstance(message, AIMessage)
        if not message.tool_calls:
            yield ChatGenerationChunk(message=AIMessageChunk(content=message.content, id="run-a"))
            return
        call = message.tool_calls[0]
        arguments = json.dumps(call["args"])
        pieces = [
            arguments[i : i + self.fragment_chars]
            for i in range(0, len(arguments), self.fragment_chars)
        ]
        for position, piece in enumerate(pieces):
            first = position == 0
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    id="run-call",
                    tool_call_chunks=[
                        {
                            "name": call["name"] if first else None,
                            "args": piece,
                            "id": call["id"] if first else None,
                            "index": 0,
                            "type": "tool_call_chunk",
                        }
                    ],
                )
            )


class _Usage:
    """The token ledger's shape; nothing here reads it."""

    def add(self, usage: Any) -> None:
        """Accept one update's usage."""


def _drive(script: list[Any]) -> list[Any]:
    """One turn of `script` in a fresh session, through the real graph and stream translator."""
    session = uuid4().hex

    async def _run() -> list[Any]:
        token = set_current_session_id(session)
        identity = set_current_identity("oid-ana", frozenset())
        try:
            graph = build_langgraph_agent(_FragmentingModel(script), audit_sink=NullAuditSink())
            return [
                event
                async for event in graph_events(
                    graph,
                    "draft me a plan",
                    config={"configurable": {"thread_id": session}},
                    trace=ToolCallTrace(),
                    on_signal=lambda signal: None,
                    usage=_Usage(),
                )
            ]
        finally:
            reset_current_identity(identity)
            reset_current_session_id(token)

    return asyncio.run(_run())


@pytest.fixture(autouse=True)
def _memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every artefact here lives in the process's in-memory store."""
    monkeypatch.setattr(settings, "session_store", "memory")


def _create(markdown: str = _REPORT) -> dict[str, Any]:
    spec = {"kind": "document", "markdown": markdown}
    return {"name": "create_exhibit", "args": {"title": "Plan", "spec": spec}}


def test_drafts_grow_and_precede_the_call_its_result_and_its_artefact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unthrottled: a frame per growth, each the whole text so far, all before `tool_call`."""
    monkeypatch.setattr(settings, "exhibit_draft_min_interval_ms", 0)
    events = _drive([_create(), "done"])
    kinds = [event.type for event in events]
    drafts = [event for event in events if isinstance(event, ExhibitDraftEvent)]
    assert len(drafts) > 3, kinds
    assert kinds[: len(drafts)] == ["exhibit_draft"] * len(drafts)
    assert kinds[len(drafts) :] == ["tool_call", "tool_result", "exhibit", "token"]
    texts = [draft.markdown for draft in drafts]
    assert all(_REPORT.startswith(text) for text in texts)
    assert all(len(a) < len(b) for a, b in zip(texts, texts[1:], strict=False)), texts
    assert texts[-1] == _REPORT
    first = drafts[0]
    assert (first.call_id, first.op, first.exhibit_id, first.kind, first.title) == (
        "call-1",
        "create",
        "",
        "document",
        "Plan",
    )
    exhibit = events[kinds.index("exhibit")]
    assert isinstance(exhibit, ExhibitEvent) and exhibit.kind == "document"


def test_the_throttle_holds_a_call_to_its_first_frame_and_one_closing_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Within one interval only the first frame goes; the rest of the text is the `done` frame."""
    monkeypatch.setattr(settings, "exhibit_draft_min_interval_ms", 60_000)
    drafts = [e for e in _drive([_create(), "done"]) if isinstance(e, ExhibitDraftEvent)]
    assert [draft.done for draft in drafts] == [False, True]
    assert len(drafts[0].markdown) < len(_REPORT) and drafts[1].markdown == _REPORT


def test_nothing_is_drafted_for_a_table_and_the_turn_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spec that is not a document streams no frame at all."""
    monkeypatch.setattr(settings, "exhibit_draft_min_interval_ms", 0)
    table = {
        "kind": "table",
        "columns": [{"key": "y", "label": "Yield"}],
        "rows": [{"y": 76}, {"y": 71}],
    }
    events = _drive([{"name": "create_exhibit", "args": {"title": "T", "spec": table}}, "done"])
    assert [event.type for event in events] == ["tool_call", "tool_result", "exhibit", "token"]


def _feed(stream: DraftStream, name: str, arguments: str, size: int = 7) -> list[Any]:
    """`arguments` fed as fragments of one call, returning every frame including the close."""
    frames: list[Any] = []
    for start in range(0, len(arguments), size):
        frames += stream.feed(
            AIMessageChunk(
                content="",
                id="m",
                tool_call_chunks=[
                    {
                        "name": name if start == 0 else None,
                        "args": arguments[start : start + size],
                        "id": "c1" if start == 0 else None,
                        "index": 0,
                        "type": "tool_call_chunk",
                    }
                ],
            )
        )
    return frames + stream.close()


def test_a_revision_by_spec_names_its_artefact_and_one_by_edits_streams_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`revise_exhibit` with a document spec drafts under its id; with `edits`, nothing."""
    monkeypatch.setattr(settings, "exhibit_draft_min_interval_ms", 0)
    by_spec = json.dumps(
        {
            "exhibit_id": "xb-00000000000000aa",
            "base_revision": 2,
            "note": "tightened",
            "spec": {"kind": "document", "markdown": _REPORT},
        }
    )
    frames = _feed(DraftStream(), "revise_exhibit", by_spec)
    assert frames and {(f.op, f.exhibit_id, f.title) for f in frames} == {
        ("revise", "xb-00000000000000aa", "")
    }
    by_edits = json.dumps(
        {
            "exhibit_id": "xb-00000000000000aa",
            "base_revision": 2,
            "note": "n",
            "edits": [{"old": "one", "new": "two"}],
            "spec": None,
        }
    )
    assert _feed(DraftStream(), "revise_exhibit", by_edits) == []
    assert _feed(DraftStream(), "read_exhibit", by_spec) == []


def test_a_draft_past_the_spec_cap_stops_and_a_kind_in_progress_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past `exhibit_max_spec_bytes` the call goes quiet; markdown before `kind` drafts as `""`."""
    monkeypatch.setattr(settings, "exhibit_draft_min_interval_ms", 0)
    monkeypatch.setattr(settings, "exhibit_max_spec_bytes", 20)
    long = json.dumps({"title": "T", "spec": {"kind": "document", "markdown": "x" * 60}})
    frames = _feed(DraftStream(), "create_exhibit", long, size=5)
    assert frames and max(len(frame.markdown.encode()) for frame in frames) <= 20
    assert not any(frame.done for frame in frames)
    monkeypatch.setattr(settings, "exhibit_max_spec_bytes", 200_000)
    unkinded = json.dumps({"title": "T", "spec": {"markdown": "# hi there", "kind": "document"}})
    frames = _feed(DraftStream(), "create_exhibit", unkinded, size=5)
    assert frames and {frame.kind for frame in frames} == {""}
    table = json.dumps({"title": "T", "spec": {"kind": "table", "markdown": "# not a doc"}})
    assert _feed(DraftStream(), "create_exhibit", table, size=5) == []
