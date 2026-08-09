"""What a tool returned, reachable — the store, the producer's refusal, and the two read routes.

Everything chemical a tool computes used to reach the browser as `ToolResultEvent.preview`: 200
characters, cut at whatever byte the budget lands on, explicitly not JSON. So a hazard screen with
severities and citations arrived as prose the model wrote about it, and the frontend could not fix
that because the data never crossed the wire. This covers the three pieces that change it — the
content-addressed store, the producer that names a result on the trace event, and the routes that
read a stored result and a cited note back.

The Postgres-backed tests follow `tests/test_postgres_artifacts.py`'s pattern exactly:
`migrated_db_or_skip()` skips cleanly with no database (this sandbox) and runs for real in CI, each
test is a sync `def` wrapping an inner `async def _run()` driven by `asyncio.run`, and each uses its
own session id so it is independent of anything else sharing the schema.
"""

import asyncio
import logging
from typing import Any

import pytest
from agent_framework import AgentSession
from fastapi.testclient import TestClient

import chemclaw.api.runner_trace as runner_trace
from chemclaw.agent.graph_tools import NoteRef, NoteView
from chemclaw.api.app import create_app
from chemclaw.api.tool_results import (
    StoredToolResult,
    content_address,
    load_tool_result,
    session_sink,
    store_tool_result,
)
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.metrics import METRICS
from tests.fakes import FakeUpdate, fed
from tests.pg import migrated_db_or_skip

_SCREEN = (
    '{"flags": [{"rule_id": "azide", "severity": "high", "explanation": "energetic", '
    '"citation": "Bretherick 7th ed.", "matched": "CCN=[N+]=[N-]"}], "screened": ["CCN=[N+]=[N-]"]}'
)


class _ResultContent:
    """A function-result content: a `call_id` and a `result`, and no `arguments` attribute at all.

    The same shape `tests/test_runner.py` uses, repeated rather than imported because importing a
    private double across test modules couples two files that otherwise share nothing.
    """

    def __init__(self, *, call_id: str, result: str) -> None:
        self.call_id = call_id
        self.result = result


class _CallContent:
    """A function-call content: the opening one carries the name, the fragments carry arguments."""

    def __init__(self, *, call_id: str, name: str = "", arguments: Any = None) -> None:
        self.call_id = call_id
        self.name = name
        self.arguments = arguments


def _issued(trace: runner_trace.ToolCallTrace, call_id: str, tool: str) -> None:
    """Drive `trace` to the point where `call_id` has been announced under `tool`."""
    fed(trace, FakeUpdate(contents=[_CallContent(call_id=call_id, name=tool, arguments={})]))
    fed(trace, FakeUpdate(contents=[_CallContent(call_id=call_id, arguments="{}")]))


# --- the store ---------------------------------------------------------------------------------


def test_a_stored_result_comes_back_byte_for_byte() -> None:
    """The round trip the whole surface rests on: what a tool returned, read back unchanged.

    Not a paraphrase and not the preview — a client that fetches a ref must get the exact text the
    answer verifier scored the turn against, or the two surfaces disagree about what a tool said.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        ref = await store_tool_result(
            session_id="tr-roundtrip", correlation_id="corr-1", tool="screen_hazards", text=_SCREEN
        )
        assert ref == content_address(_SCREEN)

        stored = await load_tool_result("tr-roundtrip", ref)
        assert stored is not None
        assert stored.text == _SCREEN
        assert stored.tool == "screen_hazards"
        assert stored.correlation_id == "corr-1"
        assert stored.byte_size == len(_SCREEN.encode("utf-8"))

    asyncio.run(_run())


def test_an_identical_result_stores_one_blob_and_keeps_one_link() -> None:
    """Content addressing, so a repeated identical call stores nothing (D-011, applied to bytes).

    Asserted on the row counts rather than on the ref alone: two equal refs prove the *address* is
    stable, and only the counts prove the second write did not duplicate the payload.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        from chemclaw.core import db

        first = await store_tool_result(
            session_id="tr-dedup", correlation_id="corr-1", tool="screen_hazards", text=_SCREEN
        )
        second = await store_tool_result(
            session_id="tr-dedup", correlation_id="corr-2", tool="screen_hazards", text=_SCREEN
        )
        assert first == second

        async with db.connection(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT count(*) FROM tool_result_blobs WHERE content_hash = %s", (first,)
                )
                blobs = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*), max(correlation_id) FROM tool_result_links "
                    "WHERE session_id = %s AND content_hash = %s",
                    ("tr-dedup", first),
                )
                links = await cur.fetchone()
        assert blobs is not None and blobs[0] == 1
        assert links is not None and links[0] == 1
        # The link is refreshed rather than left behind: the row names the most recent turn that
        # produced these bytes, which is the correlation id worth having on a fetch.
        assert links[1] == "corr-2"

    asyncio.run(_run())


def test_a_ref_from_another_session_is_a_miss() -> None:
    """The read joins the link, and that join is the second half of the ownership story.

    `resolve_session` proves the caller owns the conversation; this proves the conversation owns
    the bytes. Without it a ref — which is only the SHA-256 of a result, so anyone able to
    reproduce the text can compute it — would read as a bearer token for any session.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        ref = await store_tool_result(
            session_id="tr-mine", correlation_id="c", tool="find_notes", text="mine"
        )
        assert await load_tool_result("tr-mine", ref) is not None
        assert await load_tool_result("tr-theirs", ref) is None

    asyncio.run(_run())


def test_a_write_that_fails_costs_the_turn_nothing(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Storing must never fail a turn: the sink answers `""` and logs, it does not raise.

    Driven against a DSN pointing at nothing, which is the real shape of the failure — a database
    that has gone away mid-turn — rather than a patched exception that would prove only that the
    `except` clause is spelled correctly. Runs everywhere, including with no Postgres: an
    unreachable server is exactly the state under test.

    The counter is asserted alongside the log line because the write is per *tool call*: a run of
    these is a log flood and one aggregate number, and the number is the half an operator alerts
    on.
    """
    monkeypatch.setattr(settings, "postgres_dsn", "postgresql://127.0.0.1:1/nowhere")

    async def _run() -> str:
        return await session_sink("tr-broken", "corr-1")("screen_hazards", _SCREEN)

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(_run()) == ""
    assert "screen_hazards" in caplog.text
    assert 'chemclaw_degraded_total{subsystem="tool_result_store"}' in METRICS.render()


# --- the producer ------------------------------------------------------------------------------


def test_the_trace_names_the_result_it_stored() -> None:
    """A `tool_result` event carries the ref its full text was stored under.

    Against a fake sink rather than a database: what is under test is that the producer reads the
    same `text` for the ref as it does for the preview, ids and numbers — four views of one string.
    """
    stored: list[tuple[str, str]] = []

    async def _sink(tool: str, text: str) -> str:
        stored.append((tool, text))
        return content_address(text)

    trace = runner_trace.ToolCallTrace(sink=_sink)
    _issued(trace, "s1", "screen_hazards")
    events = fed(trace, FakeUpdate(contents=[_ResultContent(call_id="s1", result=_SCREEN)]))

    (event,) = [e for e in events if e.type == "tool_result"]
    assert event.result_ref == content_address(_SCREEN)
    assert stored == [("screen_hazards", _SCREEN)]
    assert event.preview == _SCREEN[: settings.agent_audit_max_arg_chars]


def test_a_trace_with_no_sink_reports_no_ref() -> None:
    """`result_ref` is empty rather than invented when nothing is storing — the honest default.

    This is the shape every existing caller of `ToolCallTrace` has, so the field being additive is
    a property of the code and not of a comment.
    """
    trace = runner_trace.ToolCallTrace()
    _issued(trace, "n1", "find_notes")
    events = fed(trace, FakeUpdate(contents=[_ResultContent(call_id="n1", result="[]")]))

    (event,) = [e for e in events if e.type == "tool_result"]
    assert event.result_ref == ""


def test_an_oversize_result_is_refused_whole_and_says_so(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Over the cap the result is not stored at all — and never trimmed — and the refusal is logged.

    Trimming would be the worse failure: half a `ScreenResult` is still valid JSON and renders as a
    complete hazard screen with flags missing, which is the "silent truncation reads as
    completeness" problem `_capped_numbers` exists to avoid, made worse by the payload looking
    whole.

    The sink must not be called at all — a refusal that still wrote the bytes would keep the cost
    the cap exists to bound.
    """
    monkeypatch.setattr(settings, "stream_max_result_bytes", 64)
    called: list[str] = []

    async def _sink(tool: str, text: str) -> str:
        called.append(tool)
        return content_address(text)

    trace = runner_trace.ToolCallTrace(sink=_sink)
    _issued(trace, "b1", "gather_evidence")
    with caplog.at_level(logging.WARNING, logger=runner_trace.__name__):
        events = fed(trace, FakeUpdate(contents=[_ResultContent(call_id="b1", result=_SCREEN)]))

    (event,) = [e for e in events if e.type == "tool_result"]
    assert event.result_ref == ""
    assert called == []
    assert "gather_evidence" in caplog.text
    assert str(len(_SCREEN.encode("utf-8"))) in caplog.text


def test_the_cap_is_measured_in_bytes_not_characters(monkeypatch: pytest.MonkeyPatch) -> None:
    """A result of multi-byte characters is bounded by what it costs the column, not its length.

    Twenty `字` is twenty characters and sixty bytes; a cap read as characters would admit it into a
    fifty-byte budget it does not fit.
    """
    monkeypatch.setattr(settings, "stream_max_result_bytes", 50)

    async def _sink(_tool: str, text: str) -> str:
        return content_address(text)

    trace = runner_trace.ToolCallTrace(sink=_sink)
    _issued(trace, "u1", "find_notes")
    events = fed(trace, FakeUpdate(contents=[_ResultContent(call_id="u1", result="字" * 20)]))

    (event,) = [e for e in events if e.type == "tool_result"]
    assert event.result_ref == ""


def test_setting_the_cap_to_zero_disables_the_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """One knob: the floor of the cap is the off switch, so there is no second flag to disagree."""
    monkeypatch.setattr(settings, "stream_max_result_bytes", 0)

    async def _sink(_tool: str, _text: str) -> str:  # pragma: no cover - must not be reached
        raise AssertionError("the store is disabled and must not be written to")

    trace = runner_trace.ToolCallTrace(sink=_sink)
    _issued(trace, "z1", "find_notes")
    events = fed(trace, FakeUpdate(contents=[_ResultContent(call_id="z1", result="[]")]))

    (event,) = [e for e in events if e.type == "tool_result"]
    assert event.result_ref == ""


# --- the routes --------------------------------------------------------------------------------


class _SessionOnlyAgent:
    """Enough agent to create a session and no more — these tests never run a turn."""

    def create_session(self, *, session_id: str) -> AgentSession:  # noqa: D102 - see class
        return AgentSession(session_id=session_id)


@pytest.fixture
def client() -> TestClient:
    """The real app, built the way the service builds it, with a session-creating stub agent."""
    return TestClient(create_app(agent_factory=lambda _profile: _SessionOnlyAgent()))


def test_a_stored_result_is_fetchable_from_its_session(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route the whole change exists for: the full typed result, not the 200-char preview."""
    session_id = client.post("/sessions").json()["session_id"]

    async def _load(sid: str, ref: str) -> StoredToolResult | None:
        assert sid == session_id
        return StoredToolResult(
            ref=ref, tool="screen_hazards", correlation_id="corr-1", byte_size=12, text=_SCREEN
        )

    monkeypatch.setattr("chemclaw.api.app.load_tool_result", _load)
    res = client.get(f"/sessions/{session_id}/tool-results/{content_address(_SCREEN)}")

    assert res.status_code == 200
    body = res.json()
    assert body["text"] == _SCREEN
    assert body["tool"] == "screen_hazards"
    # The join a GxP reviewer makes: this fetched result and the audit rows for the turn that
    # produced it carry the same id.
    assert body["correlation_id"] == "corr-1"


def test_an_unknown_ref_is_a_404_and_not_a_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never stored, swept by retention, someone else's: one answer (see `load_tool_result`)."""
    session_id = client.post("/sessions").json()["session_id"]

    async def _load(_sid: str, _ref: str) -> StoredToolResult | None:
        return None

    monkeypatch.setattr("chemclaw.api.app.load_tool_result", _load)
    assert client.get(f"/sessions/{session_id}/tool-results/{'0' * 64}").status_code == 404


def test_a_cited_note_resolves_to_the_view_the_agent_tool_returns(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """US-21: a citation chip can follow its citation, and gets the *same* `NoteView`.

    Including the framing envelope on the body. A route that unwrapped it to look tidier would be
    handing a surface the one representation the injection discipline exists to avoid.
    """
    view = NoteView(
        note=NoteRef(id="compound-4-bromoanisole", type="compound"),
        body="<retrieved-note>mp 12 C</retrieved-note>",
        neighbors=[NoteRef(id="reaction-suzuki-1", type="reaction")],
    )

    async def _expand(note_id: str, hops: int = 1) -> NoteView:
        assert (note_id, hops) == ("compound-4-bromoanisole", 1)
        return view

    monkeypatch.setattr("chemclaw.api.app.expand_note", _expand)
    res = client.get("/notes/compound-4-bromoanisole")

    assert res.status_code == 200
    body = res.json()
    assert body["note"]["id"] == "compound-4-bromoanisole"
    assert body["body"] == "<retrieved-note>mp 12 C</retrieved-note>"
    assert [n["id"] for n in body["neighbors"]] == ["reaction-suzuki-1"]


def test_the_note_route_reaches_the_real_expand_note(client: TestClient) -> None:
    """The same two calls again with nothing patched, because a stub cannot disagree with itself.

    Both tests above replace `expand_note`, which makes exactly one thing unobservable: whether
    `front_door.expand_note` still resolves to a coroutine this route can call with this signature.
    That is the failure a rename or an added required argument would cause, and it is the failure
    the stubs would sail straight through — the lesson `tasks/lessons.md` records from an outage
    two monkeypatches hid. So this drives the shipped knowledge graph: a note that is on disk
    answers 200, and one that is not answers 404.
    """
    note_id = sorted(p.stem for p in (settings.knowledge_path / "compound").glob("*.md"))[0]

    assert client.get(f"/notes/{note_id}").json()["note"]["id"] == note_id
    assert client.get("/notes/compound-no-such-note-exists").status_code == 404


def test_an_unmerged_note_is_a_404_carrying_its_reason(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commonest real miss is a citation to a note still awaiting its PR-gate review (D-018).

    `ChemclawError` is chemclaw's always-safe bad-input contract, so its message is passed through
    rather than replaced by a generic 404 body — a chip that cannot resolve gets told why.
    """

    async def _expand(_note_id: str, _hops: int = 1) -> NoteView:
        raise ChemclawError("unknown note id: note-not-yet-merged")

    monkeypatch.setattr("chemclaw.api.app.expand_note", _expand)
    res = client.get("/notes/note-not-yet-merged")

    assert res.status_code == 404
    assert "note-not-yet-merged" in res.json()["detail"]
