"""A document artefact streams as `exhibit_draft` frames while the model writes its call.

Driven through the real compiled graph and `graph_events` with a model streaming a call's
arguments in fragments. Pinned: frames arrive before the call's `tool_call`, `tool_result` and
`exhibit`, each carries the whole text so far, the throttle bounds their number, and nothing is
streamed for a non-document call. Parser edge cases are driven on `DraftStream` directly.
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
from langchain_core.utils.json import parse_partial_json

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

#: How many characters of a call's JSON arguments each streamed fragment carries.
_FRAGMENT_CHARS = 12
_REPORT = "# Plan\n\nStep one: dry the THF.\n\nStep two: add the base slowly.\n"
#: Long enough that the arguments have doubled several times after the text begins, which is what
#: lets a call's first frame go (`exhibit_drafts`' module docstring) — so several frames follow it.
_LONG_REPORT = _REPORT + "".join(
    f"\nStep {n}: stir for {n} min, then sample.\n" for n in range(3, 12)
)


class _FragmentingModel(ScriptedChatModel):
    """A scripted model whose tool call arrives as many argument fragments, like a provider's."""

    def __init__(self, script: list[Any]) -> None:
        """Declared so the pydantic plugin types the constructor as the parent's script form."""
        super().__init__(script)

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
            arguments[i : i + _FRAGMENT_CHARS] for i in range(0, len(arguments), _FRAGMENT_CHARS)
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
    events = _drive([_create(_LONG_REPORT), "done"])
    kinds = [event.type for event in events]
    drafts = [event for event in events if isinstance(event, ExhibitDraftEvent)]
    assert len(drafts) > 3, kinds
    assert kinds[: len(drafts)] == ["exhibit_draft"] * len(drafts)
    assert kinds[len(drafts) :] == ["tool_call", "tool_result", "exhibit", "token"]
    texts = [draft.markdown for draft in drafts]
    assert all(_LONG_REPORT.startswith(text) for text in texts)
    assert all(len(a) < len(b) for a, b in zip(texts, texts[1:], strict=False)), texts
    assert texts[-1] == _LONG_REPORT
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
    # The artefact names the call its drafts named, so a surface settles the draft by it.
    assert exhibit.call_id == first.call_id == "call-1"


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
    monkeypatch.setattr(settings, "exhibit_max_spec_bytes", 200)
    long = json.dumps({"title": "T", "spec": {"kind": "document", "markdown": "x" * 600}})
    frames = _feed(DraftStream(), "create_exhibit", long, size=5)
    assert frames and max(len(frame.markdown.encode()) for frame in frames) <= 200
    assert not any(frame.done for frame in frames)
    monkeypatch.setattr(settings, "exhibit_max_spec_bytes", 200_000)
    unkinded = json.dumps(
        {"title": "T", "spec": {"markdown": "# hi there " * 20, "kind": "document"}}
    )
    # The throttle must not decide which frames exist, so the interval is driven to zero: frames
    # before `kind` is known draft as `""`, and any after it say `document`.
    monkeypatch.setattr(settings, "exhibit_draft_bytes_per_ms", 10**9)
    frames = [f for f in _feed(DraftStream(), "create_exhibit", unkinded, size=5) if not f.done]
    assert frames and frames[0].kind == ""
    assert {frame.kind for frame in frames} <= {"", "document"}
    kinded_from = next((i for i, f in enumerate(frames) if f.kind), len(frames))
    assert all(frame.kind == "document" for frame in frames[kinded_from:])
    table = json.dumps({"title": "T", "spec": {"kind": "table", "markdown": "# not a doc"}})
    assert _feed(DraftStream(), "create_exhibit", table, size=5) == []


def test_the_draft_interval_stretches_with_the_document(monkeypatch: pytest.MonkeyPatch) -> None:
    """After a frame of N bytes the next waits N / `exhibit_draft_bytes_per_ms` ms: linear cost.

    On a clock advancing one millisecond per fragment, a fixed interval would make bytes sent grow
    with the square of the document; stretched, the total stays a small multiple of it.
    """
    now = [0.0]

    def _clock() -> float:
        now[0] += 0.001
        return now[0]

    monkeypatch.setattr("chemclaw.api.exhibit_drafts.time.monotonic", _clock)
    monkeypatch.setattr(settings, "exhibit_draft_min_interval_ms", 0)
    monkeypatch.setattr(settings, "exhibit_draft_bytes_per_ms", 4)
    text = "x" * 4_000
    arguments = json.dumps({"title": "T", "spec": {"kind": "document", "markdown": text}})
    stream = DraftStream()
    stamped: list[tuple[float, int]] = []
    for start in range(0, len(arguments), 8):
        for frame in stream.feed(
            AIMessageChunk(
                content="",
                id="m",
                tool_call_chunks=[
                    {
                        "name": "create_exhibit" if start == 0 else None,
                        "args": arguments[start : start + 8],
                        "id": "c1" if start == 0 else None,
                        "index": 0,
                        "type": "tool_call_chunk",
                    }
                ],
            )
        ):
            stamped.append((now[0], len(frame.markdown.encode())))
    assert len(stamped) > 2
    for (at, size), (next_at, _) in zip(stamped, stamped[1:], strict=False):
        assert (next_at - at) * 1000 >= size / 4 - 1, (at, size, next_at)
    # Unstretched this is ~500 frames and ~1 MB; stretched, a few times the document.
    assert sum(size for _, size in stamped) < 4 * len(text), stamped


def test_a_slow_reader_holds_one_draft_per_call_and_gets_the_newest() -> None:
    """A reader that is not reading is offered fifty drafts of one call; it holds and reads one.

    The other events keep their order around it, and a draft of a second call is its own slot.
    """
    from chemclaw.api.detach import DetachableTurn
    from chemclaw.api.events import TokenEvent, sse_frame

    def _draft(call_id: str, text: str) -> dict[str, str]:
        return sse_frame(ExhibitDraftEvent(call_id=call_id, op="create", markdown=text))

    async def _source() -> Any:
        yield sse_frame(TokenEvent(text="before"))
        for n in range(1, 51):
            yield _draft("c1", "x" * n)
        yield _draft("c2", "other")
        yield sse_frame(TokenEvent(text="after"))
        yield _draft("c1", "x" * 60)

    async def _run() -> tuple[int, list[dict[str, str]]]:
        turn = DetachableTurn(_source(), session_id="s-drafts")
        await asyncio.sleep(0.05)  # the pump finishes before the reader reads anything
        held = turn._sender.queue.qsize()
        return held, [frame async for frame in turn.events()]

    held, frames = asyncio.run(_run())
    assert held == 5, "the buffer holds a draft per call, not one per frame"
    seen = [(frame["event"], json.loads(frame["data"])) for frame in frames]
    assert [(event, data.get("markdown", data.get("text"))) for event, data in seen] == [
        ("token", "before"),
        # The first slot of `c1` was still queued when its later frames came, so it carries the
        # newest text — even the one offered after "after", since every frame is the whole text.
        ("exhibit_draft", "x" * 60),
        ("exhibit_draft", "other"),
        ("token", "after"),
    ]


@pytest.mark.parametrize(
    "spec",
    [
        # A table whose `kind` comes last: never a document, and nothing says so until the end.
        {"columns": [{"key": "v", "label": "V"}], "rows": [{"v": n} for n in range(6_000)]},
        # A spec the model wrote as a JSON string rather than an object.
        json.dumps({"kind": "document", "markdown": "x " * 38_000}),
    ],
    ids=["kind-last", "string-spec"],
)
def test_a_call_that_shows_nothing_is_parsed_a_logarithmic_number_of_times(
    monkeypatch: pytest.MonkeyPatch, spec: Any
) -> None:
    """Before the first frame a call is re-parsed only when its arguments have doubled.

    Parsing on every fragment is quadratic event-loop CPU for no frame; this bounds it to about log2
    of the size, and a string spec stops at the first parse that sees it.
    """
    from chemclaw.api import exhibit_drafts

    parses: list[int] = []
    real = parse_partial_json

    def _counting(text: str) -> Any:
        parses.append(len(text))
        return real(text)

    monkeypatch.setattr(exhibit_drafts, "parse_partial_json", _counting)
    arguments = json.dumps({"title": "T", "spec": spec})
    assert len(arguments) > 60_000
    assert _feed(DraftStream(), "create_exhibit", arguments, size=12) == []
    assert len(parses) <= 20, len(parses)


def test_arguments_longer_than_any_storable_spec_stop_the_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the spec cap plus a title and a note at their caps, nothing more is read."""
    monkeypatch.setattr(settings, "exhibit_max_spec_bytes", 1_000)
    monkeypatch.setattr(settings, "exhibit_max_title_chars", 10)
    monkeypatch.setattr(settings, "exhibit_max_note_chars", 10)
    monkeypatch.setattr(settings, "exhibit_draft_argument_slack_chars", 500)
    from chemclaw.api import exhibit_drafts

    parses: list[int] = []
    real = parse_partial_json

    def _counting(text: str) -> Any:
        parses.append(len(text))
        return real(text)

    monkeypatch.setattr(exhibit_drafts, "parse_partial_json", _counting)
    table = {"columns": [{"key": "v", "label": "V"}], "rows": [{"v": n} for n in range(2_000)]}
    _feed(DraftStream(), "create_exhibit", json.dumps({"title": "T", "spec": table}), size=50)
    assert parses and max(parses) <= 1_000 + 6 * 20 + 500

    # And the bound is the setting's: a stream fed past it stops within one fragment of it.
    stream = DraftStream()
    arguments = json.dumps({"title": "T", "spec": table})
    for start in range(0, len(arguments), 50):
        stream.feed(
            AIMessageChunk(
                content="",
                id="m",
                tool_call_chunks=[
                    {
                        "name": "create_exhibit" if start == 0 else None,
                        "args": arguments[start : start + 50],
                        "id": "c1" if start == 0 else None,
                        "index": 0,
                        "type": "tool_call_chunk",
                    }
                ],
            )
        )
    [call] = stream._calls.values()
    bound = 1_000 + 6 * 20 + 500
    assert call.stopped and bound < len(call.arguments) <= bound + 50


def test_a_refused_call_names_the_draft_it_leaves_unsettled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`tool_failed.call_id` is the drafts' `call_id`, so a surface drops exactly that draft.

    The hardening contract's item 2: before, the failure named only the tool, and a surface holding
    two drafts of `create_exhibit` could not tell which one would never be settled.
    """
    from chemclaw.api.events import ToolFailedEvent

    monkeypatch.setattr(settings, "exhibit_draft_min_interval_ms", 0)
    untitled = {"name": "create_exhibit", "args": {"title": " ", "spec": _create()["args"]["spec"]}}
    events = _drive([untitled, "done"])
    drafts = [e for e in events if isinstance(e, ExhibitDraftEvent)]
    [failed] = [e for e in events if isinstance(e, ToolFailedEvent)]
    assert drafts and {draft.call_id for draft in drafts} == {"call-1"}
    assert failed.call_id == "call-1" and not any(isinstance(e, ExhibitEvent) for e in events)
