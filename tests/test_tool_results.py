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
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage, message_to_dict, messages_from_dict

import chemclaw.api.runner_trace as runner_trace
from chemclaw.agent.graph_tools import NoteRef, NoteView
from chemclaw.agent.message_migration import to_langchain
from chemclaw.agent.session import TurnSession
from chemclaw.api.app import _transcript, create_app
from chemclaw.api.schemas import message_text
from chemclaw.api.tool_results import (
    StoredToolResult,
    content_address,
    fetchable_refs,
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
        # And the label the dedup cost is **empty**, not the last writer's. The row is one row for
        # two turns, so no correlation id belongs to it; `SET correlation_id = EXCLUDED
        # .correlation_id` made the fetch route answer with the right bytes under the wrong turn's
        # id, which is the near-miss pairing this whole surface refuses on the read side.
        assert links[1] == ""

    asyncio.run(_run())


def test_a_result_two_calls_produced_names_neither_of_them() -> None:
    """The label is dropped rather than guessed, and the bytes are still exactly right.

    This is not a corner case. `include_detailed_errors` is off (`agent/tool_authz.py` says why),
    so *every* unexpected tool exception in the system returns the same byte string "Error:
    Function failed." — one blob per session covering every failed call it ever makes. A fetch of
    it under one arbitrary tool name and one arbitrary correlation id would be a mispairing served
    with full confidence, and `StoredToolResult.correlation_id` is documented as "the join a
    reviewer asks for".

    Asserted through `load_tool_result` rather than on the row, because what matters is what a
    *reviewer* is handed: unknown where it is unknown, and the result text where it is not.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        failure = "Error: Function failed."

        ref = await store_tool_result(
            session_id="tr-ambiguous", correlation_id="corr-1", tool="predict_pka", text=failure
        )
        await store_tool_result(
            session_id="tr-ambiguous",
            correlation_id="corr-2",
            tool="screen_hazards",
            text=failure,
        )

        stored = await load_tool_result("tr-ambiguous", ref)
        assert stored is not None
        assert (stored.tool, stored.correlation_id) == ("", "")
        assert stored.text == failure

        # A third disagreeing write must not un-collapse it: `''` disagrees with every value, so
        # the column stays empty once it has been emptied.
        await store_tool_result(
            session_id="tr-ambiguous", correlation_id="corr-1", tool="predict_pka", text=failure
        )
        again = await load_tool_result("tr-ambiguous", ref)
        assert again is not None and (again.tool, again.correlation_id) == ("", "")

    asyncio.run(_run())


def test_the_same_call_written_twice_keeps_the_labels_it_agrees_with() -> None:
    """The other half of the rule: only *disagreement* costs the labels.

    A turn that re-runs the same tool with the same arguments — a retry after a transient failure,
    the repeat guard letting one through — writes the same bytes under the same tool and the same
    correlation id. There is nothing ambiguous about that row, and emptying it would throw away a
    join that is correct, which is the opposite error.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        for _ in range(2):
            ref = await store_tool_result(
                session_id="tr-repeat", correlation_id="corr-9", tool="screen_hazards", text=_SCREEN
            )

        stored = await load_tool_result("tr-repeat", ref)
        assert stored is not None
        assert (stored.tool, stored.correlation_id) == ("screen_hazards", "corr-9")

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

    def create_session(self, *, session_id: str) -> TurnSession:
        return TurnSession(session_id=session_id)


@pytest.fixture
def client() -> TestClient:
    """The real app, built the way the service builds it, with a session-creating stub agent."""
    return TestClient(create_app())


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
    # The join a reviewer makes: this fetched result and the audit rows for the turn that
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


# --- the transcript ----------------------------------------------------------------------------


# What a screening tool returns: a model object dumped to a `dict`, never a string. It starts here
# rather than at a string literal because the whole ref identity is about *coercion* — feeding the
# same literal to both sides would prove only that `content_address` is a function.
_SCREEN_RESULT: dict[str, Any] = {
    "flags": [
        {
            "rule_id": "azide",
            "severity": "high",
            "explanation": "energetic",
            "citation": "Bretherick 7th ed.",
            "matched": "CCN=[N+]=[N-]",
        }
    ],
    "screened": ["CCN=[N+]=[N-]"],
}


def _turn(result: object, *, call_id: str = "t1", tool: str = "screen_hazards") -> list[Any]:
    """One tool call and its result, in the messages a turn actually produces.

    A `ToolMessage` is where a non-string return value becomes text, so the coercion is inside the
    constructor rather than in front of it — which is the point: the producer and the transcript
    have to be looking at the same bytes, and a test that stringified first would never find out.
    """
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": tool,
                    "args": {"smiles": ["CCN=[N+]=[N-]"]},
                    "id": call_id,
                    "type": "tool_call",
                }
            ],
        ),
        # `cast` because the annotation says `str | list` while the runtime coerces anything —
        # which is exactly the coercion `test_every_entry_point_coerces_a_result_to_text...`
        # exists to pin, and a test that could not pass a `dict` could not pin it.
        ToolMessage(content=cast(str, result), tool_call_id=call_id),
    ]


def _reloaded(messages: list[Any]) -> list[Any]:
    """`messages` after the round trip a reload puts them through.

    `PostgresHistoryProvider` writes `message_to_dict()` into a JSONB column and rebuilds it with
    `messages_from_dict`, so this is the transformation between "what the turn produced" and "what
    `_transcript` reads". Doing it for real is what makes the identity below a property of the
    serialization rather than of this file.
    """
    return list(messages_from_dict([message_to_dict(message) for message in messages]))


def _stored_turn(result: str, *, call_id: str = "t1", tool: str = "screen_hazards") -> list[Any]:
    """A round-tripped turn whose result is already a string — the ordinary stored shape."""
    return _reloaded(_turn(result, call_id=call_id, tool=tool))


def test_the_transcript_names_a_result_by_the_same_ref_the_stream_named_it_by() -> None:
    """The whole pairing argument, driven through both real paths rather than asserted about them.

    A reload had no way to resolve a past turn's results: `result_ref` reached a surface on the SSE
    stream only, so a chemist coming back to a conversation saw *that* `screen_hazards` ran and
    400 characters of prose about what it found, while the full text sat in `tool_result_blobs`.

    The join is content addressing and nothing else. The producer hashes the result text; the
    transcript hashes the result text it reads out of the stored message; **both reach it through
    `schemas.message_text`**, so they are the same bytes by construction rather than by two
    implementations happening to agree. Nothing pairs on `(session, tool, correlation_id,
    created_at)` — which could not tell two calls of one tool in one turn apart anyway — so there
    is no near-miss pairing available to get wrong.

    Driven from a `dict` the tool never stringified, and through the real producer call
    (`graph_stream` hands `trace.returned` exactly this text), because the one event that could
    break the identity is a change to how a message's content becomes a string.
    """
    stored: dict[str, str] = {}

    async def _sink(_tool: str, text: str) -> str:
        ref = content_address(text)
        stored[ref] = text
        return ref

    turn = _turn(_SCREEN_RESULT)
    trace = runner_trace.ToolCallTrace(sink=_sink)
    trace.issued("t1", "screen_hazards", "{}")
    event = asyncio.run(trace.returned("t1", message_text(turn[1])))

    [message] = [m for m in _transcript(_reloaded(turn), fetchable=stored) if m.tool_calls]
    [call] = message.tool_calls

    assert event.result_ref != ""  # the stream stored it
    assert call.result_ref == event.result_ref  # and the reload names the same bytes
    assert call.tool == "screen_hazards"
    # And the bytes carry the screen itself, not a summary of it: the store holds what a client
    # will render, and a coercion that changed shape would move every ref at once.
    assert call.result is not None and "azide" in call.result


def test_a_result_the_store_cannot_serve_is_advertised_as_unfetchable() -> None:
    """The retention case, and the reason the ref is *checked* rather than merely computed.

    A ref in a transcript outlives the blob it names the moment the TTL sweep runs, and it is also
    computable for results the store never took (off, over the cap, a failed write). Advertising a
    derivable address in either case would hand a client a link that 404s and no way to know in
    advance — so the transcript reports only refs the store can currently serve, and `""` keeps
    exactly the meaning it has on the live stream: there is nothing to fetch.

    What the client still has is the 400-character `result`, which is why this is a degradation of
    the rendering and never a loss of the transcript.
    """
    [message] = [m for m in _transcript(_stored_turn(_SCREEN), fetchable=()) if m.tool_calls]
    [call] = message.tool_calls

    assert call.result_ref == ""
    assert call.result is not None and "azide" in call.result


def test_an_unanswered_call_stays_distinguishable_from_an_unfetchable_one() -> None:
    """Three states, and the pair that must not collapse into each other.

    `result is None` means the call has no result at all — it ran and nobody knows how it ended.
    `result` set with an empty `result_ref` means it returned and only the preview survives. A
    surface that conflated them would tell a chemist a tool produced nothing when it produced
    something the store no longer holds, which is the more reassuring of the two claims and the
    wrong one.
    """
    orphan = _reloaded(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "screen_hazards", "args": {}, "id": "gone", "type": "tool_call"}
                ],
            )
        ]
    )
    [unanswered] = _transcript(orphan)[0].tool_calls
    [unfetchable] = [
        call for m in _transcript(_stored_turn(_SCREEN), fetchable=()) for call in m.tool_calls
    ]

    assert (unanswered.result, unanswered.result_ref) == (None, "")
    assert unfetchable.result is not None and unfetchable.result_ref == ""


def test_the_transcript_route_carries_the_ref_the_store_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end on the route a client actually reloads through.

    `GET /sessions/{id}/messages` is the rehydration path, and it is where the ref had to arrive: a
    projection that can produce one is worth nothing if the route never asks for it. The app is
    built here rather than taken from the `client` fixture because the stored history has to be
    replaced on `app.state.history`, which is the seam the route reads its transcript through.
    """
    app = create_app()
    client = TestClient(app)
    session_id = client.post("/sessions").json()["session_id"]
    ref = content_address(_SCREEN)

    async def _messages(_session_id: str | None, **_kwargs: Any) -> list[Any]:
        return _stored_turn(_SCREEN)

    async def _fetchable(session: str) -> frozenset[str]:
        assert session == session_id
        return frozenset({ref})

    monkeypatch.setattr(app.state.history, "get_messages", _messages)
    monkeypatch.setattr("chemclaw.api.app.fetchable_refs", _fetchable)

    [call] = [
        call
        for message in client.get(f"/sessions/{session_id}/messages").json()
        for call in message["tool_calls"]
    ]
    assert call["result_ref"] == ref


def test_the_refs_a_session_can_fetch_are_its_own() -> None:
    """`fetchable_refs` is scoped by the link row's session, like every other read of this store.

    Otherwise the transcript would advertise a ref that `load_tool_result` then refuses — the same
    ownership boundary applied twice, and it must give the same answer both times or a surface
    renders a link that cannot resolve.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        mine = await store_tool_result(
            session_id="tr-refs-mine", correlation_id="c", tool="screen_hazards", text=_SCREEN
        )
        theirs = await store_tool_result(
            session_id="tr-refs-theirs", correlation_id="c", tool="find_notes", text="[]"
        )
        refs = await fetchable_refs("tr-refs-mine")
        assert mine in refs
        assert theirs not in refs

    asyncio.run(_run())


def test_a_store_that_cannot_be_read_costs_the_transcript_only_its_refs(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading fails the same way writing does: an empty answer, a count, and no raised error.

    A chemist reloading a conversation must still get every message and every tool call when the
    blob store is unreachable; what they lose is the link to a full result, which is a rendering.
    Driven against a DSN pointing at nothing rather than a patched exception, for the reason the
    write-side test states — that is the real shape of the failure.
    """
    monkeypatch.setattr(settings, "postgres_dsn", "postgresql://127.0.0.1:1/nowhere")

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(fetchable_refs("tr-unreadable")) == frozenset()
    assert "tr-unreadable" in caplog.text
    assert 'chemclaw_degraded_total{subsystem="tool_result_store"}' in METRICS.render()


def test_the_off_switch_asks_the_database_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the store disabled there is nothing stored, so there is nothing to look up.

    Asserted by making any connection attempt fail the test: a lookup against a store that is off
    is a round trip per reload buying an answer that is known in advance.
    """
    monkeypatch.setattr(settings, "stream_max_result_bytes", 0)

    def _no_connection(*_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover - must not be called
        raise AssertionError("the store is disabled and must not be queried")

    monkeypatch.setattr("chemclaw.api.tool_results.db.connection", _no_connection)
    assert asyncio.run(fetchable_refs("tr-off")) == frozenset()


def test_every_entry_point_coerces_a_result_to_text_before_it_can_be_addressed() -> None:
    """Why there is no "a stored dict 500s the reload" test here — measured, not assumed.

    There used to be one, and it pinned a real defect: a stored row carrying `"result": {…}`
    reached `content_address`, which calls `.encode`, and raised `AttributeError` — an uncaught
    exception on `GET /sessions/{id}/messages`, which is the route a chemist reloads a *whole*
    conversation through. Losing the conversation because one result card cannot be addressed is
    the wrong trade by a wide margin.

    On this engine that row cannot exist. Measured across all three ways a `ToolMessage` is made —
    the constructor, `messages_from_dict` rebuilding a stored row, and `message_migration
    .to_langchain` converting a row the previous framework wrote — every one coerces a non-string
    content to `str` before anything reads it. So the guard belongs where the coercion is, and a
    test asserting "does not 500" would pass for a reason unrelated to its name.

    What is pinned instead is the coercion itself, at each entry, because *that* is the property
    the ref identity rests on: if one of them stopped coercing, the defect above comes back
    somewhere this file no longer looks.
    """
    payload: dict[str, list[str]] = {"flags": [], "screened": []}
    constructed = ToolMessage(content=cast(str, payload), tool_call_id="t1")
    rebuilt = messages_from_dict(
        [{"type": "tool", "data": {"content": payload, "tool_call_id": "t1", "type": "tool"}}]
    )[0]
    converted = to_langchain(
        {
            "type": "message",
            "role": "tool",
            "contents": [{"type": "function_result", "call_id": "t1", "result": payload}],
        }
    )

    for message in (constructed, rebuilt, converted):
        assert isinstance(message.content, str), f"{type(message).__name__} kept a non-string"
        # And the ref is computable from it, which is the consequence that actually matters.
        assert len(content_address(message_text(message))) == 64
