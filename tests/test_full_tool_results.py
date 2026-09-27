"""A result the model was shown a cut of keeps its full text — for the chemist, never the model.

`D-2026-09-27-a-cut-result-is-kept-for-the-chemist-not-the-model`. The cut in
`agent/tool_result_size.py` is the one lossy step between a tool and the model, and the removed
middle used to reach no store: the tool-result store is fed from the stream, and the stream only
ever sees the message the model got. So these drive each hop the full text now takes — the cut
handing it to the turn's sink and stamping the ref, the stream naming that ref, a reload naming it
again, the owner-scoped route serving it, and an erasure taking it — and each asserts the other half
of the decision too: **what the model reads is unchanged, still cut.**

The Postgres-backed tests skip cleanly with no database (`tests/pg.py`) and run for real in CI.
"""

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import psycopg
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage, message_to_dict, messages_from_dict

from chemclaw.agent.framing import envelope_delimiters
from chemclaw.agent.leaver import erase_actor
from chemclaw.agent.tool_framing import frame_connector_results
from chemclaw.agent.tool_result_size import (
    FULL_RESULT_REF_KEY,
    bound_tool_results,
    full_result_ref,
    reset_full_result_sink,
    set_full_result_sink,
    was_cut,
)
from chemclaw.api import tool_results
from chemclaw.api.app import _transcript, create_app
from chemclaw.api.auth import Principal, require_principal
from chemclaw.api.graph_stream import _from_update
from chemclaw.api.runner_trace import ToolCallTrace
from chemclaw.api.schemas import message_text
from chemclaw.api.tool_results import content_address, full_result_sink
from chemclaw.core.config import settings
from chemclaw.core.config.memory import MemorySettings
from chemclaw.core.config.observability import ObservabilitySettings
from chemclaw.core.config.sources import SourcesSettings
from tests.pg import migrated_db_or_skip

#: What sits in the middle of the tool's output — the part the cut removes, and so the part that
#: proves which text a reader was handed.
_MIDDLE = "THE-MIDDLE-THE-MODEL-NEVER-SAW"


def _full_output(chars: int = 200_000) -> str:
    """A result far over the model's ceiling, with a marker only its middle carries."""
    half = (chars - len(_MIDDLE)) // 2
    return "h" * half + _MIDDLE + "t" * (chars - half - len(_MIDDLE))


class _Collecting:
    """A `FullResultSink` that keeps what it is handed and answers with the content address."""

    def __init__(self) -> None:
        """Start with nothing kept."""
        self.kept: dict[str, str] = {}
        self.calls = 0

    async def __call__(self, _tool: str, text: str) -> str:
        """Keep `text` under its address, the way the real store names it."""
        self.calls += 1
        ref = content_address(text)
        self.kept[ref] = text
        return ref


@pytest.fixture
def sink() -> Iterator[_Collecting]:
    """This test's turn has a sink installed, exactly as `api/runner._turn_ambient` installs one."""
    collecting = _Collecting()
    token = set_full_result_sink(collecting)
    try:
        yield collecting
    finally:
        reset_full_result_sink(token)


def _request(name: str = "read_document") -> Any:
    """The attributes the two bounding middlewares read off an in-process tool-call request."""
    return cast(
        Any,
        SimpleNamespace(
            tool_call={"name": name, "id": "c1", "args": {}},
            state={"messages": []},
            tool=SimpleNamespace(metadata={}),
        ),
    )


def _returning(content: str, name: str = "read_document") -> Any:
    """A tool handler returning `content` as its result."""

    async def _tool(_request: Any) -> ToolMessage:
        return ToolMessage(content=content, tool_call_id="c1", name=name)

    return _tool


def _bounded(content: str) -> ToolMessage:
    """`content` through the real `bound_tool_results`, as the tool chain runs it."""
    message = asyncio.run(bound_tool_results.awrap_tool_call(_request(), _returning(content)))
    assert isinstance(message, ToolMessage)
    return message


# --- the cut keeps the full text -----------------------------------------------------------------


def test_a_cut_result_keeps_its_full_text_and_the_model_still_reads_the_cut(
    sink: _Collecting,
) -> None:
    """The decision in one test: the sink holds every byte, the model's message is still cut."""
    raw = _full_output()

    message = _bounded(raw)

    model_text = message_text(message)
    assert len(model_text) <= settings.agent_max_tool_result_chars, "the model's cut was undone"
    assert _MIDDLE not in model_text, "the removed middle reached the model"
    assert was_cut(message)
    ref = full_result_ref(message)
    assert sink.kept == {ref: raw}, "the full text the tool returned was not what was kept"
    # A pointer rides on the thread, never the text: the metadata carries 64 characters.
    assert len(str(message.response_metadata)) < 1_000


def test_a_result_under_the_ceiling_keeps_nothing_extra_and_carries_no_stamp(
    sink: _Collecting,
) -> None:
    """Nothing was cut, so there is no second text to keep and nothing to say about one."""
    message = _bounded("a small result")

    assert sink.calls == 0
    assert not was_cut(message)
    assert FULL_RESULT_REF_KEY not in message.response_metadata


def test_with_no_sink_a_cut_is_still_marked_and_names_no_full_text() -> None:
    """The CLI and a template step install no sink: the cut is still a fact, the ref is empty."""
    message = _bounded(_full_output())

    assert was_cut(message), "whether the model saw a cut does not depend on who could store it"
    assert full_result_ref(message) == ""


def test_a_cut_only_the_framing_pass_makes_keeps_the_text_before_escaping(
    monkeypatch: pytest.MonkeyPatch, sink: _Collecting
) -> None:
    """Under the ceiling until escaping pushed it over: the outer pass cuts, and keeps the original.

    A disguised delimiter makes `framing._defang` escape every `<`, a 4x expansion, so a result the
    inner `bound_tool_results` let through whole reaches the model cut by `frame_connector_results`
    instead. Nothing stamped it on the way in, so the outer pass is the one that has to keep it.
    """
    monkeypatch.setattr(settings, "agent_max_tool_result_chars", 5_000)
    opening, _ = envelope_delimiters("probe")
    raw = f"{opening[0]}​{opening[1:]}" + "<" * 3_000
    assert len(raw) < settings.agent_max_tool_result_chars  # the inner pass cannot cut this

    async def _inner(inner_request: Any) -> Any:
        return await bound_tool_results.awrap_tool_call(inner_request, _returning(raw, "read_file"))

    message = asyncio.run(frame_connector_results.awrap_tool_call(_request("read_file"), _inner))

    assert isinstance(message, ToolMessage)
    assert len(message_text(message)) <= settings.agent_max_tool_result_chars
    assert was_cut(message), "the outer pass cut this result and said nothing about it"
    assert sink.kept == {full_result_ref(message): raw}


def test_a_result_both_passes_cut_is_kept_once_from_the_inner_pass(
    monkeypatch: pytest.MonkeyPatch, sink: _Collecting
) -> None:
    """The first pass to cut saw more of the tool's output; its stamp is the one that stands."""
    monkeypatch.setattr(settings, "agent_max_tool_result_chars", 5_000)
    opening, _ = envelope_delimiters("probe")
    raw = f"{opening[0]}​{opening[1:]}" + "<" * 50_000

    async def _inner(inner_request: Any) -> Any:
        return await bound_tool_results.awrap_tool_call(inner_request, _returning(raw, "read_file"))

    message = asyncio.run(frame_connector_results.awrap_tool_call(_request("read_file"), _inner))

    assert isinstance(message, ToolMessage)
    assert sink.calls == 1, "one cut result, kept twice"
    assert sink.kept == {full_result_ref(message): raw}


# --- the stream and the reload name the full text ------------------------------------------------


def _stream(message: ToolMessage, trace: ToolCallTrace) -> Any:
    """The `tool_result` event `graph_stream` raises for `message`, via its own update reader."""
    trace.issued("c1", "read_document", "{}")

    async def _events() -> list[Any]:
        payload = {"tools": {"messages": [message]}}
        return [event async for event in _from_update(payload, "", trace, [])]

    [event] = asyncio.run(_events())
    return event


def test_the_stream_names_the_full_text_and_grounds_on_the_cut(sink: _Collecting) -> None:
    """`result_ref` opens what the tool returned; everything a grounding check reads is the cut."""
    raw = _full_output()
    message = _bounded(raw)
    trace_writes: list[str] = []

    async def _trace_sink(_tool: str, text: str) -> str:
        trace_writes.append(text)
        return content_address(text)

    trace = ToolCallTrace(sink=_trace_sink)
    event = _stream(message, trace)

    assert event.result_cut is True
    assert event.result_ref == content_address(raw), "the ref names the model's cut, not the result"
    assert trace_writes == [], "the full text was already kept; the cut was stored a second time"
    # The grounding corpus is what was in front of the model, and the middle was not.
    assert trace.outputs == [message_text(message)]
    assert _MIDDLE not in event.preview


def test_when_the_full_text_was_not_kept_the_stream_stores_the_cut_as_before() -> None:
    """No sink (or a refusal): the ref falls back to the model's text, which says it is a cut."""
    message = _bounded(_full_output())

    async def _trace_sink(_tool: str, text: str) -> str:
        return content_address(text)

    event = _stream(message, ToolCallTrace(sink=_trace_sink))

    assert event.result_cut is True
    assert event.result_ref == content_address(message_text(message))


def test_an_uncut_result_reports_no_cut() -> None:
    """The flag is false exactly when the model read the whole result."""

    async def _trace_sink(_tool: str, text: str) -> str:
        return content_address(text)

    event = _stream(_bounded("small"), ToolCallTrace(sink=_trace_sink))

    assert event.result_cut is False
    assert event.result_ref == content_address("small")


def test_a_reload_names_the_same_full_text_the_stream_named(sink: _Collecting) -> None:
    """Through the JSON round trip `session_messages` puts a message through, the stamp survives."""
    raw = _full_output()
    message = _bounded(raw)
    call = AIMessage(
        content="",
        tool_calls=[{"name": "read_document", "args": {}, "id": "c1", "type": "tool_call"}],
    )
    stored = list(messages_from_dict([message_to_dict(m) for m in (call, message)]))

    [row] = [m for m in _transcript(stored, fetchable={content_address(raw)}) if m.tool_calls]
    [reloaded] = row.tool_calls

    assert reloaded.result_ref == content_address(raw)
    assert reloaded.result_cut is True
    assert reloaded.result is not None and _MIDDLE not in reloaded.result


def test_a_turn_through_the_front_door_names_the_full_text_it_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """From `run_turn` in: the runner installs the sink, the real chain cuts, the event names it.

    The production entry point rather than the middleware alone, because the one hop no unit test
    above can see is the runner installing the sink for the turn (`_turn_ambient`) — without it
    every cut stamps `""` and the feature is silently off. The store is replaced by a collecting
    sink at the runner's own import, so this needs no database; the rest is the compiled graph.
    """
    from langchain_core.tools import tool as make_tool

    from chemclaw.agent.audit import NullAuditSink
    from chemclaw.agent.langgraph_agent import build_langgraph_agent
    from chemclaw.agent.session import TurnSession
    from chemclaw.api import runner
    from tests.fakes_langgraph import ScriptedChatModel

    raw = _full_output()
    kept = _Collecting()
    installed: list[tuple[str, str]] = []

    def _sink_for(session_id: str, correlation_id: str) -> Any:
        installed.append((session_id, correlation_id))
        return kept

    monkeypatch.setattr(runner, "full_result_sink", _sink_for)

    @make_tool
    def read_long_document(query: str) -> str:
        """Return a document far longer than the model may read in one result."""
        return raw

    history: list[Any] = []

    class _History:
        async def save_messages(self, _session_id: str, messages: Any, **_kw: Any) -> None:
            history.extend(messages)

    def _graph(**build_kwargs: Any) -> Any:
        build_kwargs["connectors"] = [*(build_kwargs.get("connectors") or []), read_long_document]
        build_kwargs["audit_sink"] = NullAuditSink()
        script: list[Any] = [{"name": "read_long_document", "args": {"query": "x"}}, "Done."]
        return build_langgraph_agent(ScriptedChatModel(script), **build_kwargs)

    async def _collect() -> list[Any]:
        session = TurnSession(session_id="s-full-result-turn")
        return [
            event
            async for event in runner.run_turn(
                session, "read it", connectors=[], graph_factory=_graph, history=_History()
            )
        ]

    events = asyncio.run(_collect())

    [result] = [e for e in events if e.type == "tool_result"]
    assert [sid for sid, _ in installed] == ["s-full-result-turn"], "no sink for this turn"
    assert result.result_cut is True
    assert result.result_ref == content_address(raw)
    assert kept.kept == {content_address(raw): raw}
    [stored] = [m for m in history if getattr(m, "tool_call_id", None)]
    assert _MIDDLE not in message_text(stored), "the thread the model reads holds the whole result"
    assert full_result_ref(stored) == content_address(raw)


# --- the size bound ------------------------------------------------------------------------------


def test_the_store_cap_admits_the_largest_first_party_result_it_exists_for() -> None:
    """The cap's default is derived from the per-tool ceilings it has to admit, and pinned to them.

    Four UTF-8 bytes a character over the largest first-party per-tool ceiling. Raising one of
    those ceilings without this would silently stop keeping the results it lets through.
    """
    cap = ObservabilitySettings.model_fields["stream_max_result_bytes"].default
    document = SourcesSettings.model_fields["document_read_max_chars"].default
    calc = (
        MemorySettings.model_fields["calc_find_max_results"].default
        * MemorySettings.model_fields["calc_find_max_result_chars"].default
    )
    assert cap >= 4 * max(document, calc)


def test_a_full_text_over_the_cap_is_refused_whole_not_trimmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refused rather than trimmed: a trimmed "full" result would read as whole, and it is not."""
    written: list[str] = []

    async def _store(**kwargs: Any) -> str:
        written.append(kwargs["text"])
        return content_address(kwargs["text"])

    monkeypatch.setattr(tool_results, "store_tool_result", _store)
    monkeypatch.setattr(settings, "stream_max_result_bytes", 100)
    put = full_result_sink("s-cap", "corr")

    async def _put(text: str) -> str:
        return await put("read_document", text)

    assert asyncio.run(_put("x" * 101)) == ""
    assert asyncio.run(_put("x" * 100)) == content_address("x" * 100)
    assert written == ["x" * 100]


# --- the route and erasure, against a real database ----------------------------------------------

_ALICE = Principal(oid="full-results-alice", upn="alice@corp", roles=frozenset())
_BOB = Principal(oid="full-results-bob", upn="bob@corp", roles=frozenset())


def _keep_through_the_real_store(session_id: str, raw: str) -> ToolMessage:
    """Cut `raw` with this session's real `full_result_sink` installed, as a turn would."""
    token = set_full_result_sink(full_result_sink(session_id, "corr-full"))
    try:
        return _bounded(raw)
    finally:
        reset_full_result_sink(token)


def test_the_owner_opens_the_full_result_and_nobody_else_can() -> None:
    """Owner 200 with every byte; another actor 404; an unknown ref 404 — the session's one gate."""
    asyncio.run(migrated_db_or_skip())
    app = create_app()
    client = TestClient(app)
    app.dependency_overrides[require_principal] = lambda: _ALICE
    session_id = client.post("/sessions").json()["session_id"]
    raw = _full_output()

    message = _keep_through_the_real_store(session_id, raw)
    ref = full_result_ref(message)
    assert ref == content_address(raw)
    assert _MIDDLE not in message_text(message), "the model's message was not cut"

    owner = client.get(f"/sessions/{session_id}/tool-results/{ref}")
    assert owner.status_code == 200
    assert owner.json()["text"] == raw
    assert owner.json()["byte_size"] == len(raw.encode("utf-8"))
    assert client.get(f"/sessions/{session_id}/tool-results/{'0' * 64}").status_code == 404

    app.dependency_overrides[require_principal] = lambda: _BOB
    stranger = client.get(f"/sessions/{session_id}/tool-results/{ref}")
    assert stranger.status_code == 404, "another actor read a chemist's full tool result"


def test_erasing_the_chemist_takes_the_full_result_with_them() -> None:
    """`make user-erase` reaches the full text: it is a stored result like any other.

    Ownership is written the way the durable owner store writes it (`session_owners`), because that
    row is what `erase_actor` reaches a person's sessions through.
    """
    asyncio.run(migrated_db_or_skip())
    actor, session_id = "full-results-leaver", "s-full-results-leaver"

    async def _own() -> None:
        async with await psycopg.AsyncConnection.connect(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO session_owners (session_id, owner) VALUES (%s, %s) "
                    "ON CONFLICT (session_id) DO UPDATE SET owner = EXCLUDED.owner",
                    (session_id, actor),
                )
            await conn.commit()

    asyncio.run(_own())
    raw = _full_output() + session_id  # this test's own bytes, shared with no other session
    ref = full_result_ref(_keep_through_the_real_store(session_id, raw))
    stored = asyncio.run(tool_results.load_tool_result(session_id, ref))
    assert stored is not None and stored.text == raw

    report = asyncio.run(erase_actor(actor, apply=True))

    assert asyncio.run(tool_results.load_tool_result(session_id, ref)) is None, (
        "the erased chemist's full tool result survived the erasure"
    )
    assert report.erased["tool_result_blobs"] >= 1
